"""Email sender (B12): Hostinger SMTP direct (ADR-5), smtplib, ≤20/day.

Footer-free by construction — the body IS the frozen copy file. The real
transport reads credentials ONLY from `.env` (D11: secrets never in code,
logs, or the DB). Tests inject a fake transport through the same narrow
context-manager interface; nothing is sent until `email_enabled` is armed
(config) AND `.env` carries the SMTP creds (the factory fails loudly
otherwise) — fail-closed at every layer.
"""
from __future__ import annotations

import contextlib
import os
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any

from outreach.caps import send_allowed
from outreach import dispatcher as _dispatch
from outreach.dispatcher import _assert_queue_live, commit_send, email_enabled
from outreach.store import get_config
from outreach.templates import (
    EmailPendingOwner,
    MessageHold,
    TemplateError,
    assert_sendable_template,
    render_for_lead,
)

if TYPE_CHECKING:
    from outreach.store import Database


@dataclass(frozen=True)
class EmailOutcome:
    state: str  # "sent" | "disabled" | "capped" | "nodish"
    receipt: str | None


def email_subject(db: "Database") -> str:
    """Pinned subject row (owner-delegated choice, editable in config).

    May carry a {{restaurant}} fill — personalized subjects are the
    proven lift in cold-email opens; the fill uses ONLY the lead's name.
    """
    return str(get_config(db, "email_subject") or "")


def _fill_subject(subject: str, lead_name: str | None) -> str:
    """Apply the subject's {{restaurant}} fill; never leave a raw marker."""
    name = (lead_name or "").strip()
    if "{{restaurant}}" in subject and not name:
        raise TemplateError(
            "email subject {{restaurant}} fill has no lead name"
        )
    out = subject.replace("{{restaurant}}", name)
    if "{{" in out:
        raise TemplateError(
            f"email subject placeholder drift: unresolved '{{' remains"
        )
    return out


class _RealSmtp:
    """smtplib transport over .env creds (Hostinger, ADR-5).

    The raw SMTP connection comes from `_smtp_factory` (real default: a
    live connection); every mechanic — creds validation, EHLO, TLS,
    login, sendmail, quit — runs the same code in tests and production.
    """

    def __init__(self, smtp_factory: Any | None = None) -> None:
        self._factory = smtp_factory or _default_smtp_factory
        self.host = ""
        self.port = 587
        self.user = ""
        self.password = ""
        self.from_addr = ""
        self._connection: Any = None

    def __enter__(self) -> "_RealSmtp":
        self.host = os.environ.get("SMTP_HOST", "")
        try:
            self.port = int(os.environ.get("SMTP_PORT", "587") or "587")
        except ValueError:
            raise EmailPendingOwner(
                f"SMTP_PORT in .env is not a number: "
                f"{os.environ.get('SMTP_PORT')!r}"
            )
        self.user = os.environ.get("SMTP_USER", "")
        self.password = os.environ.get("SMTP_PASSWORD", "")
        self.from_addr = os.environ.get("SMTP_FROM", "")
        missing = [
            k
            for k, v in (
                ("SMTP_HOST", self.host),
                ("SMTP_USER", self.user),
                ("SMTP_PASSWORD", self.password),
                ("SMTP_FROM", self.from_addr),
            )
            if not v
        ]
        if missing:
            raise EmailPendingOwner(
                "SMTP creds missing in .env: " + ", ".join(missing)
            )
        self._connection = self._factory(self.host, self.port, timeout=30)
        self._connection.ehlo()
        if self.port != 465:
            self._connection.starttls()
            self._connection.ehlo()
        self._connection.login(self.user, self.password)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._connection is not None:
            with contextlib.suppress(smtplib.SMTPException):
                self._connection.quit()

    def send_email(self, from_addr: str, to_addr: str, subject: str,
                   body: str) -> str:
        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.set_content(body)
        refused = self._connection.sendmail(from_addr, [to_addr], msg.as_string())
        return "refused:" + ",".join(refused) if refused else "250"


def _default_smtp_factory(host: str, port: int, *, timeout: int) -> Any:
    """Port 465 = implicit TLS (SMTP_SSL); everything else = plaintext + STARTTLS."""
    if port == 465:
        return smtplib.SMTP_SSL(host, port, timeout=timeout)
    return smtplib.SMTP(host, port, timeout=timeout)


def _email_row(db: "Database", lead_id: int) -> dict:
    from outreach.variants import render_select

    row = db.conn.execute(render_select(), (lead_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown lead id={lead_id}")
    return row


def send_email_message(
    db: "Database",
    lead_id: int,
    now: datetime,
    *,
    transport_factory: Any = None,
) -> EmailOutcome:
    """Execute one email dispatch end-to-end (guard → send → commit)."""
    _entry = _dispatch._session_clock()  # true elapsed -> the commit instant
    _assert_queue_live(db)
    assert_sendable_template(db)
    row = _email_row(db, lead_id)
    if row["route_channel"] != "email":
        raise ValueError(
            f"send_email_message only handles channel 'email', lead is "
            f"{row['route_channel']!r}"
        )
    if not email_enabled(db):
        return EmailOutcome("disabled", None)  # ADR-5 gate: nothing sent
    if not send_allowed(db, "email", now):
        return EmailOutcome("capped", None)  # gate #3: no transport opens
    try:
        message = render_for_lead("email", row)
    except MessageHold:
        return EmailOutcome("nodish", None)  # Q7 hold — nothing invented
    subject = _fill_subject(email_subject(db), row["name"])

    from_addr = os.environ.get("SMTP_FROM", "")
    if transport_factory is None:
        if not from_addr:
            raise EmailPendingOwner("SMTP_FROM missing in .env")
        transport = _RealSmtp()
    else:
        transport = transport_factory()

    with transport as mbox:
        receipt = mbox.send_email(from_addr, row["email"], subject, message.text)
    commit_send(db, lead_id, _dispatch.commit_instant(now, _entry),
                message=message)
    return EmailOutcome("sent", receipt)


def resend_email(
    db: "Database",
    lead_id: int,
    *,
    now: datetime,
    message: Any,
    transport_factory: Any = None,
) -> EmailOutcome:
    """Transport-only resend for the B14/B15 follow-up ladder.

    Matches the ladder adapter contract exactly: transports the
    ALREADY-RENDERED frozen copy, returns an outcome, performs NO
    commit, cap counting, or state machine work — the ladder owns the
    bookkeeping (its loop asserts queue/template before calling).
    """
    row = _email_row(db, lead_id)
    if not email_enabled(db):
        return EmailOutcome("disabled", None)  # ADR-5 gate: nothing sent
    subject = _fill_subject(email_subject(db), row["name"])
    from_addr = os.environ.get("SMTP_FROM", "")
    if transport_factory is None:
        if not from_addr:
            raise EmailPendingOwner(
                "SMTP_FROM missing in .env — the owner must arm the "
                "channel before follow-up resends can transport"
            )
        transport = _RealSmtp()
    else:
        transport = transport_factory()
    with transport as mbox:
        receipt = mbox.send_email(from_addr, row["email"], subject, message.text)
    return EmailOutcome("sent", receipt)
