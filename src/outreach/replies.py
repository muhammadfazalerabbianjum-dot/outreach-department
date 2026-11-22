"""Reply detection (B13): any reply = hard human handover.

A reply from a lead cancels every pending follow-up EXACTLY once
(gate #4), moves the lead to REPLIED, and drops an owner alert into the
digest table (B16 surfaces it). The system NEVER auto-replies — the
owner takes every conversation from here.

Detectors:
- `match_email_replies(db, messages)` — pure matcher over parsed email
  objects; the IMAP poller feeds real messages from the mailbox in
  `.env` (Hostinger), fail-closed when unreachable.
- WA/IG in-thread probes join once the owner redoes the automation
  profile logins (cannot read a wall behind a login wall).
  **WA leg BUILT 2026-10-27** (`poll_wa_replies`): walks every active
  WA chat once per interval and registers a lead reply found as the
  NEWEST rendered message. Direction comes from the
  `data-pre-plain-text` header — outgoing lines carry the owner's own
  pushname, because this build renders NO `.message-in/.message-out`
  classes (live probe 2026-10-27: 0 of either; history renders
  ~26-42s after the chat opens).
  **IG leg BUILT 2026-10-29** (`poll_ig_replies`, B24): scans every
  thread we have actually messaged on Instagram (day-7 escalation or
  IG-lane send) once per interval. There is no sender header on IG, so
  direction is decided by copy: the newest rendered message that does
  NOT substantially match our frozen copy is a lead reply (the owner
  typing from their phone counts as the owner taking over — which is
  the handover law anyway). Login walls, dead pages and unreadable
  history all fail closed with zero writes.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from outreach.store import Database

_REPLY_ACTOR = "reply-detector"
_SOURCE_STATES = ("SENT", "FOLLOWUP")


def _normalize_email(addr: str | None) -> str:
    return (addr or "").strip().lower()


def _owner_alert(db: "Database", body: str) -> None:
    with db.conn:
        db.conn.execute("INSERT INTO digest(body) VALUES (?)", (body,))


def register_reply(
    db: "Database",
    lead_id: int,
    *,
    channel: str,
    now: datetime | None = None,
    reply_text: str | None = None,
    account_id: str = "",
    sender_handle: str = "",
) -> str:
    """A reply arrived: REPLIED + cancel follow-ups once + owner alert.

    Idempotent by construction: a second call for the same lead returns
    "already" and writes nothing anywhere (gate #4's exact-once law).

    `now` is the detecting tick's clock; the REPLIED history row and the
    owner-inbox row are stamped with it so the reply measurement can compare
    them honestly against `message_log.sent_at`. Omitted (email poller legacy
    path), SQLite stamps the wall clock as before.
    """
    from outreach.inbox import add_reply
    from outreach.machine import InvalidTransition, transition

    row = db.conn.execute(
        "SELECT id, state, name FROM leads WHERE id=?", (lead_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown lead id={lead_id}")

    current = row["state"]
    if current == "REPLIED":
        return "already"
    if current not in _SOURCE_STATES:
        raise InvalidTransition(
            f"reply cannot land on state {current!r}: not a frozen edge"
            f" (lead stays {current!r})"
        )

    transition(db, lead_id, "REPLIED", actor=f"{_REPLY_ACTOR}:{channel}", at=now)
    with db.conn:
        db.conn.execute(
            "UPDATE followups SET status='cancelled', cancelled_by_reply=1"
            " WHERE lead_id=? AND status='pending'",
            (lead_id,),
        )
    # The alert names the lane account and handle, and quotes what the lead
    # actually wrote. Owner order 2026-11-09: a reply notification that does
    # not say WHICH account received it, from WHOM, and WHAT it says is not
    # usable - the supervisor reading this row must be able to act on it
    # without opening a browser.
    quoted = " ".join((reply_text or "").split())[:280]
    lane = f" on {account_id}" if account_id else ""
    handle = f" @{sender_handle.lstrip('@')}" if sender_handle else ""
    alert = (
        f"REPLY DETECTED ({channel}{lane}): lead {lead_id} {row['name']!r}"
        f"{handle} is now REPLIED — hard handover: owner takes this"
        " conversation; all pending follow-ups cancelled."
        + (f" They wrote: \u201c{quoted}\u201d" if quoted else "")
    )
    _owner_alert(db, alert)
    # The durable half of the alert (10.6): a suppressed toast or an unread
    # digest must never be the reason a live conversation went unnoticed.
    add_reply(
        db,
        lead_id=lead_id,
        channel=channel,
        body=alert,
        now=now or datetime.now(timezone.utc),
        reply_text=quoted or None,
        account_id=account_id,
        sender_handle=sender_handle.lstrip("@"),
    )
    return "registered"


def _email_body(msg: Any) -> str:
    """Plain-text body of a parsed mail, bounded; empty when unreadable."""
    try:
        part = msg.get_body(preferencelist=("plain",)) if hasattr(msg, "get_body") else None
        if part is not None:
            return " ".join((part.get_content() or "").split())[:600]
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            return " ".join(payload.decode("utf-8", "replace").split())[:600]
    except Exception:  # noqa: BLE001 - a malformed mail must not stop the pass
        return ""
    return ""


def match_email_replies(
    db: "Database", messages: Iterable[Any], *, now: datetime | None = None
) -> list[int]:
    """Match parsed inbox messages (From + headers) against ACTIVE leads.

    A lead is active for replies while in SENT or FOLLOWUP with a known
    email. Returns the newly REPLIED lead ids (in inbox order); noise
    from strangers/completed leads is ignored.
    """
    active: dict[str, int] = {}
    for row in db.conn.execute(
        "SELECT id, email, state FROM leads WHERE state IN (?, ?)"
        " AND email IS NOT NULL",
        _SOURCE_STATES,
    ):
        addr = _normalize_email(row["email"])
        if addr:
            active[addr] = int(row["id"])

    replied: list[int] = []
    for msg in messages:
        from_addr = _normalize_email(parseaddr(str(msg.get("From", "")))[1])
        lead_id = active.get(from_addr)
        if lead_id is None:
            continue  # stranger or completed lead — no handover
        result = register_reply(
            db,
            lead_id,
            channel="email",
            now=now,
            reply_text=_email_body(msg),
            sender_handle=from_addr,
        )
        if result == "registered":
            replied.append(lead_id)
    return replied


@dataclass(frozen=True)
class ReplyPollOutcome:
    state: str  # "ok" | "unreachable" | "pending_owner"
    replied_lead_ids: tuple[int, ...]
    error: str = ""  # empty on clean runs; carries the failure otherwise


class MailboxPendingOwner(RuntimeError):
    """IMAP creds missing in .env — reply polling stays off (owner task)."""


# Bounded IMAP socket (live incident 2026-10-28: an unbounded TLS read
# froze the engine tick INSIDE imaplib.readline for over an hour, silently
# swallowing the whole ME send window — py-spy stack proof).
_IMAP_TIMEOUT_S = 30.0


def _default_imap_factory():
    import imaplib
    import os

    host = os.environ.get("IMAP_HOST", "")
    port = int(os.environ.get("IMAP_PORT", "993") or "993")
    user = os.environ.get("IMAP_USER", "")
    password = os.environ.get("IMAP_PASSWORD", "")
    missing = [
        name
        for name, val in (
            ("IMAP_HOST", host),
            ("IMAP_USER", user),
            ("IMAP_PASSWORD", password),
        )
        if not val
    ]
    if missing:
        raise MailboxPendingOwner(
            "IMAP creds missing in .env: " + ", ".join(missing)
        )
    connection = imaplib.IMAP4_SSL(host, port, timeout=_IMAP_TIMEOUT_S)
    connection.login(user, password)  # AUTH state: only then select works
    return connection


def poll_email_replies(
    db: "Database", *, imap_factory: Any = None, now: datetime | None = None
) -> ReplyPollOutcome:
    """Poll the mailbox for unseen replies and hand matched leads over.

    Fail-closed by shape: an unreachable mailbox returns "unreachable"
    WITHOUT inventing reply facts; missing creds raise
    MailboxPendingOwner loudly. Unread state is left untouched (the
    matcher is idempotent, re-scans are harmless).
    """
    import email as _email_mod

    factory = imap_factory or _default_imap_factory
    imap = None
    try:
        imap = factory()
    except MailboxPendingOwner:
        raise
    except Exception as exc:  # noqa: BLE001 — connectivity, not data
        return ReplyPollOutcome("unreachable", (), error=repr(exc)[:300])
    try:
        typ, _ = imap.select("INBOX")
        if typ != "OK":
            return ReplyPollOutcome(
                "unreachable", (), error=f"select INBOX -> {typ!r}"
            )
        typ, data = imap.search(None, "UNSEEN")
        if typ != "OK":
            return ReplyPollOutcome(
                "unreachable", (), error=f"search UNSEEN -> {typ!r}"
            )
        raw_bytes: list[bytes] = []
        fetch_failures: list[str] = []
        for num in data[0].split():
            typ, parts = imap.fetch(num, "(RFC822)")
            if typ != "OK" or not parts or parts[0] is None:
                fetch_failures.append(f"fetch {num.decode()} -> {typ!r}")
                continue
            raw = parts[0][1]
            if isinstance(raw, bytes):
                raw_bytes.append(raw)
        messages = [
            _email_mod.message_from_bytes(raw, policy=_email_mod.policy.default)
            for raw in raw_bytes
        ]
        replied = match_email_replies(db, messages, now=now)
        return ReplyPollOutcome(
            "ok", tuple(replied), error=";".join(fetch_failures)
        )
    except Exception as exc:  # noqa: BLE001 — connectivity, not data
        return ReplyPollOutcome("unreachable", (), error=repr(exc)[:300])
    finally:
            if imap is not None:
                try:
                    imap.logout()
                except Exception:  # noqa: BLE001 — goodbye path
                    pass


# --- WA in-thread probe (B13 leg 2) ------------------------------------------------

# Newest rendered message + its direction header (live probe 2026-10-27:
# this build renders NO .message-in/.message-out classes — direction is
# the header's sender segment: outgoing = the owner's own pushname).
_WA_LAST_JS = """() => {
  // wa-last: newest rendered message and its data-pre-plain-text header
  const all = [...document.querySelectorAll('.copyable-text[data-pre-plain-text]')];
  if (!all.length) return null;
  const n = all[all.length - 1];
  return {header: n.getAttribute('data-pre-plain-text') || '',
          text: (n.innerText || '').trim()};
}"""

_HEADER_SENDER_RE = re.compile(r"^\[[^\]]*\]\s*(.*?):\s*$")
_PROBE_INTERVAL_S = 1800.0  # a pass walks every active chat: at most / 30 min
#: Evidence 2026-11-17 02:55Z: 16 threads in 426.5 s, avg 26.7 s, slowest 38.4 s
#: of which 30.0 s was the state wait on a thread that never became decisive.
#: That 30 s is the SEND path's deadline; a read-only probe does not need it.
_IG_PROBE_STATE_DEADLINE_S = 8.0
#: ...and a pass must never overrun the stage's 240 s: between threads it looks
#: at the clock, stops, and leaves the rest to the next pass (the cursor already
#: rotates the slice).
_IG_PROBE_BUDGET_S = 200.0
_PROBE_FRESH_S = _PROBE_INTERVAL_S + 300.0  # guard slack: one throttle
# window + one tick of retry headroom (owner directive 2026-10-30 —
# no ladder rung may fire on a channel whose reply probe is staler).
_PROBE_STAMP_KEYS = {"wa": "wa_reply_probe_at", "ig": "ig_reply_probe_at"}


def probe_fresh(db: "Database", now: datetime, channel: str) -> bool:
    """Has this channel's reply probe confirmed its threads recently?

    Missing stamp = fresh (a fresh DB / test seam must never deadlock the
    ladder — the guard's only job is deferring rungs while an EXISTING
    stamp ages past the throttle window, i.e. a probe that used to work
    has stopped working: live shape 2026-10-30, 39 failed WA passes in
    one network outage while the rungs kept coming due)."""
    from outreach.store import get_config

    key = _PROBE_STAMP_KEYS[channel]
    stamp = get_config(db, key)
    if not stamp:
        return True
    try:
        age = (now - datetime.fromisoformat(str(stamp))).total_seconds()
    except ValueError:
        return True  # corrupted stamp: the probe runs fresh anyway
    return age <= _PROBE_FRESH_S
_HISTORY_DEADLINE_S = 45.0  # live probe 2026-10-27: history at ~26-42s
_HISTORY_POLL_S = 1.0


def _header_sender(header: Any) -> str | None:
    """`[9:11 am, 27/10/2026] Faz: ` -> `Faz` (None when unparseable)."""
    match = _HEADER_SENDER_RE.match(str(header or ""))
    return match.group(1).strip() or None if match else None


def _wait_last_message(page: Any) -> dict[str, Any] | None:
    """Poll until the thread's history renders its newest message (fail-closed
    at the deadline: no reading → this chat is skipped, nothing invented)."""
    deadline = time.monotonic() + _HISTORY_DEADLINE_S
    while True:
        reading = page.evaluate(_WA_LAST_JS)
        if reading:
            return reading
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(_HISTORY_POLL_S, remaining))


def poll_wa_replies(
    db: "Database",
    now: datetime,
    *,
    opener: Any,
    evidence_dir: Any = None,
) -> tuple[int, ...]:
    """Scan every active WA chat once; register lead replies (B13 WA leg).

    Fail-closed by construction: a chat that will not open (login wall,
    dead number, boot timeout) or whose history never renders is SKIPPED
    with zero writes, and an unknown direction (no self-pushname learned
    yet, message not our copy) never invents a reply fact. The self
    pushname is learned from our OWN frozen copy on the first pass that
    sees it, after which direction is a name comparison — the owner
    typing from their own phone carries the same pushname and is never
    counted as a lead reply.

    One pass per `_PROBE_INTERVAL_S` (throttle stamp in config); each
    found reply keeps its ADR-15 screenshot before the register.
    """
    from outreach.browser import EVIDENCE_DIR
    from outreach.store import get_config, set_config
    from outreach.wa_sender import _MIN_MATCH, _norm, _wait_wa_state, deep_link

    stamp = get_config(db, "wa_reply_probe_at")
    if stamp:
        try:
            elapsed = (now - datetime.fromisoformat(stamp)).total_seconds()
        except ValueError:
            elapsed = None  # corrupted stamp: run the pass
        if elapsed is not None and 0 <= elapsed < _PROBE_INTERVAL_S:
            return ()

    rows = db.conn.execute(
        "SELECT id, phone, phone_e164 FROM leads"
        " WHERE state IN ('SENT', 'FOLLOWUP') AND route_channel='wa'"
        " AND (phone_e164 IS NOT NULL OR phone IS NOT NULL) ORDER BY id"
    ).fetchall()
    if not rows:
        return ()  # nothing to scan: the browser never opens

    shot_dir = Path(evidence_dir) if evidence_dir else Path(EVIDENCE_DIR)
    replied: list[int] = []
    with opener() as page:
        for row in rows:
            lead_id = int(row["id"])
            number = row["phone_e164"] or row["phone"]
            page.goto(
                deep_link(number),
                wait_until="domcontentloaded",
                timeout=45000,
            )
            if _wait_wa_state(page) != "ready":
                continue  # login wall / dead / boot: fail-closed, no writes
            reading = _wait_last_message(page)
            if not reading:
                continue  # history never rendered: skip, invent nothing
            sender = _header_sender(reading.get("header"))
            text = _norm(reading.get("text") or "")
            self_name = get_config(db, "wa_self_pushname")
            if not self_name:
                lead_row = db.conn.execute(
                    "SELECT * FROM leads WHERE id=?", (lead_id,)
                ).fetchone()
                ours = _ours_copy(db, "wa", lead_row, now=now)
                if sender and text and len(text) >= _MIN_MATCH and text in ours:
                    set_config(db, "wa_self_pushname", sender)  # bootstrap
                continue  # direction unknowable on this pass either way
            if sender == self_name:
                continue  # our message — or the OWNER typing from their phone
            lead_row = db.conn.execute(
                "SELECT * FROM leads WHERE id=?", (lead_id,)
            ).fetchone()
            ours = _ours_copy(db, "wa", lead_row, now=now)
            if text and len(text) >= _MIN_MATCH and _looks_like_our_copy(text, ours):
                continue  # our own copy (opener or follow-up rung): not a reply
            shot = shot_dir / f"wa_reply_{lead_id}_{now:%Y%m%dT%H%M%S}.png"
            page.screenshot(path=str(shot))  # ADR-15 evidence before the register
            if register_reply(
                db,
                lead_id,
                channel="wa",
                now=now,
                reply_text=reading.get("text") or "",
                sender_handle=sender or "",
            ) == "registered":
                replied.append(lead_id)
    set_config(db, "wa_reply_probe_at", now.isoformat())
    return tuple(replied)


# --- IG in-thread probe (B24 leg 3) ----------------------------------------------

_IG_PROBE_AT_KEY = "ig_reply_probe_at"
#: Threads one probe pass may walk (LIVE 2026-11-09). The probe runs inside the
#: reply-priority stage BEFORE dispatch, so its cost comes out of the send
#: window: a 48-thread walk at ~30-45 s a page meant one tick ran half an hour
#: with nothing sent (last send 11:47Z, probe stamp 13:19Z). Coverage is kept
#: by rotation - each pass starts after the last thread it scanned and wraps.
_IG_PROBE_MAX_THREADS = 16


def _bounded_threads(db: "Database", rows: list[Any]) -> list[Any]:
    """This pass's slice of the thread list, rotating from the last cursor.

    `ig_probe_max_threads` = 0 (or a negative value) restores the old
    unbounded walk for a manual run.
    """
    from outreach.store import get_config, set_config

    try:
        cap = int(get_config(db, "ig_probe_max_threads", _IG_PROBE_MAX_THREADS))
    except (TypeError, ValueError):
        cap = _IG_PROBE_MAX_THREADS
    if cap <= 0 or len(rows) <= cap:
        return rows
    ordered = sorted(rows, key=lambda r: int(r["id"]))
    try:
        cursor = int(get_config(db, "ig_probe_cursor", 0) or 0)
    except (TypeError, ValueError):
        cursor = 0
    start = 0
    for index, row in enumerate(ordered):
        if int(row["id"]) > cursor:
            start = index
            break
    window = (ordered[start:] + ordered[:start])[:cap]
    set_config(db, "ig_probe_cursor", int(window[-1]["id"]))
    return window


def _looks_like_our_copy(reading: str, ours: str) -> bool:
    """Is the newest rendered text (a prefix of) something WE shipped?

    Exact membership is not enough. Measured 2026-11-14: `owner_inbox` row 14
    held OUR OWN DM as `reply_text`, because that send (2026-11-04) logged no
    `body` and `register_reply` truncates what it stores at 280 chars — so our
    copy appears in a thread as a PREFIX of the body, never as an equal.

    A lead reply that genuinely begins with 60 characters of our frozen copy
    is not a realistic shape; a truncation of it is the observed one.
    """
    if not reading:
        return False
    if reading in ours:
        return True
    head = reading[:60]
    for body in ours.split(""):
        body = body.strip()
        if not body:
            continue
        if body.startswith(reading):
            return True
        if len(head) == 60 and body.startswith(head):
            return True
        if _most_words_are_ours(reading, body):
            return True
    return False


def _most_words_are_ours(reading: str, body: str) -> bool:
    """Fallback for a copy we can no longer render (older paste, edited P.S.).

    Four fifths of the candidate's content words appearing in ONE of our known
    copies is our own text with a word changed - a restaurant answering with
    80 % of our marketing sentence is not a realistic shape.
    """
    words = {w for w in re.split(r"\W+", reading.lower()) if len(w) > 2}
    if len(words) < 6:
        return False
    mine = {w for w in re.split(r"\W+", body.lower()) if len(w) > 2}
    return bool(mine) and len(words & mine) / len(words) >= 0.8


#: Instagram/business canned acknowledgements. A bot answering is not a human
#: reading the message, so it must not advance the lead or cancel the ladder.
#: A business auto-responder is not a human, and this department's leads answer
#: in their own language. Live 2026-11-15 (lead 1250, Le Palais Libanais): a
#: French canned reply ("Merci de nous avoir envoyé un message… consultez notre
#: menu… nos heures d'ouverture") was read as a human, so the lead went REPLIED
#: and its ladder was cancelled. Markers are matched on ACCENT-FOLDED text, so
#: one entry covers "envoyé"/"envoye" and "horaires" alike.
_AUTO_REPLY_MARKERS = (
    # French
    "merci de nous avoir envoye",
    "merci pour votre message",
    "nous avons bien recu votre message",
    "consultez notre menu",
    "consultez notre site",
    "visitez notre site",
    "notre site web",
    "nos heures d'ouverture",
    "toutes les reponses",
    "reponse automatique",
    "message automatique",
    # Spanish
    "gracias por contactarnos",
    "gracias por escribirnos",
    "gracias por su mensaje",
    "hemos recibido su mensaje",
    "visita nuestro sitio",
    "nuestro sitio web",
    "nuestros horarios",
    "respuesta automatica",
    # German
    "danke fur ihre nachricht",
    "vielen dank fur ihre nachricht",
    "wir haben ihre nachricht erhalten",
    "besuchen sie unsere website",
    "automatische antwort",
    # Italian / Portuguese
    "grazie per averci contattato",
    "abbiamo ricevuto il tuo messaggio",
    "visita il nostro sito",
    "obrigado por entrar em contato",
    "visite nosso site",
    "resposta automatica",
    # English
    "thanks for contacting us",
    "thank you for contacting us",
    "thanks for reaching out",
    "thank you for reaching out",
    "we've received your message",
    "we have received your message",
    "we received your message",
    "we appreciate you reaching out",
    "this is an automated",
    "automated message",
    "auto-reply",
    "autoreply",
    "our team will get back to you",
    "we'll get back to you shortly",
    "we will respond during business hours",
    "respond during business hours",
)

_JUDGEABLE_CHARS = 20  # below this a bubble is too short to call a direction


def _fold(text: str) -> str:
    """Lower-case, strip diacritics and collapse whitespace.

    "Merci de nous avoir envoyé" and "merci de nous avoir envoye" must be the
    same sentence to this gate; a lead's language is not a reason to miss it.
    """
    import unicodedata

    lowered = " ".join(str(text or "").casefold().split())
    decomposed = unicodedata.normalize("NFKD", lowered)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _looks_like_auto_reply(reading: str) -> bool:
    text = _fold(reading)
    if not text:
        return False
    return any(marker in text for marker in _AUTO_REPLY_MARKERS)


def classify_newest(
    reading: str, ours: str, *, min_len: int = _JUDGEABLE_CHARS
) -> str | None:
    """What is the newest bubble in a thread: `ours`, `auto`, `reply`, or None?

    One seam, so the direction law is testable without a browser. `None` means
    the bubble is too short to judge — the probe then invents nothing.

    Order matters: OUR OWN copy is checked first. A canned acknowledgement that
    happens to repeat our words is still ours, and calling it `auto` would at
    least be harmless — calling it `reply` would not.
    """
    text = " ".join(str(reading or "").split())
    if len(text) < min_len:
        return None
    if ours and _looks_like_our_copy(text, ours):
        return "ours"
    if _looks_like_auto_reply(text):
        return "auto"
    return "reply"


def _ours_copy(
    db: "Database", channel: str, lead_row: Any, *, now: Any = None
) -> str:
    """Everything WE ship on this lead's channel: frozen opener + the
    owner's temporary follow-up line (directive 2026-10-30), normalized
    and joined with a byte that never appears in copy.

    The ladder rungs now type the follow-up line — without it in here the
    IG probe would read OUR rung as a lead reply (false REPLIED + ladder
    cancel + owner handover) and the WA probe could do the same under a
    pushname mismatch. An unrenderable opener (MessageHold) contributes
    nothing instead of killing the whole probe pass (live failure shape:
    one hold lead aborted every WA reply scan before this helper)."""
    from outreach.templates import MAX_LADDER_TOUCH, TemplateError, render_followup
    from outreach.variants import render_lane_opener
    from outreach.wa_sender import _norm

    # BOTH copy families: the lane that is on TODAY and the frozen v1.0 file
    # family (`templates/*.txt`). A thread sent while `variant_engine` was off
    # holds frozen copy that the variant lane can never re-render (live
    # 2026-11-15, leads 86/109: our own DM read as a restaurant reply).
    frozen_parts: list[str] = []
    try:
        from outreach.templates import frozen_family

        frozen_parts = [
            _norm(text) for text in frozen_family(channel, lead_row) if text
        ]
    except Exception:  # noqa: BLE001 - a family that refuses contributes none
        frozen_parts = []
    opener = render_lane_opener(db, channel, lead_row, now=now)
    # callers compare a _norm-ed reading against this string, so every
    # part must be _norm-ed: the rungs always were, but the opener only
    # rendered raw (LIVE_LESSONS 2026-11-02: an unnormalized opener made
    # our own frozen copy read foreign and flip a false reply)
    opener = _norm(opener) if opener else ""
    # GROUND TRUTH: everything this lead was actually sent on this channel.
    # LIVE 2026-11-09: `variant_engine=1` picks a variant per render, so the
    # opener rendered here is usually NOT the one in the thread (lead 346's
    # log held one copy while the DM held another). Comparing only against
    # the fresh render made our own delivered message read foreign - which
    # registers a false REPLIED, cancels the ladder and hands the thread to
    # the owner as if the restaurant answered. The log is what we sent.
    sent: list[str] = []
    try:
        for row in db.conn.execute(
            "SELECT body FROM message_log WHERE lead_id=? AND channel=?",
            (int(lead_row["id"]), channel),
        ):
            body = _norm(row["body"])
            if body:
                sent.append(body)
    except (KeyError, IndexError, TypeError):
        sent = []  # a caller without message_log in scope still gets the copy
    rungs: list[str] = []
    # the ladder's own cap decides how many rungs exist (Ruling B: two).
    # Asking the ladder matters: a hardcoded 1..4 list made the guard call
    # render_followup(3), which the cap now refuses - catching TemplateError
    # keeps a copy refusal from ever killing the read-only probe.
    for touch in range(1, MAX_LADDER_TOUCH + 1):
        try:
            rungs.append(_norm(render_followup(lead_row, touch).text))
        except TemplateError:
            continue  # a fill stage the lead can't render contributes nothing
    # THE WHOLE ROTATING FAMILY (live 2026-11-15): the log holds the copy that
    # was committed, but a thread can hold a DIFFERENT variant (lead 86's thread
    # carried the review-quote opener while its log held the dish opener), and
    # an unmatched own copy reads as a restaurant reply. Rendering every
    # eligible opener is deterministic and model-free.
    family: list[str] = []
    try:
        from outreach.variants import all_possible_openers

        family = [
            _norm(copy)
            for copy in all_possible_openers(db, lead_row, now=now)
            if copy
        ]
    except Exception:  # noqa: BLE001 - a guard never blocks a probe pass
        family = []
    return "\u0001".join([opener, *frozen_parts, *rungs, *sent, *family])


def poll_ig_replies(
    db: "Database",
    now: datetime,
    *,
    opener: Any = None,
    opener_for: Any = None,
    evidence_dir: Any = None,
    budget_s: float = _IG_PROBE_BUDGET_S,
) -> tuple[int, ...]:
    """Scan every thread we messaged on Instagram once (B24 escalation).

    Covers both IG-lane leads and the day-7 escalation threads (matched
    via a `message_log` row with `channel='ig'`). Fail-closed by
    construction: a login wall / dead page / unreadable history SKIPS
    with zero writes, a message too short to judge is never invented into
    a reply, and a copy we cannot render (MessageHold) is skipped rather
    than guessed. One pass per `_PROBE_INTERVAL_S` (config throttle
    stamp); each found reply keeps its ADR-15 screenshot before register.

    Direction without a sender header: the newest rendered message is a
    lead reply unless it substantially matches OUR frozen copy for that
    lead (the owner typing in the thread counts as the owner taking the
    conversation — the handover law either way).

    v1.1 batch 4 (login isolation): threads are grouped by the account that
    sent them and each group is scanned through THAT account's own profile
    via `opener_for(account_id)`. A single `opener` keeps the pre-fleet
    behaviour for manual runs (one session, every thread).
    """
    from outreach.browser import EVIDENCE_DIR
    from outreach.dispatcher import IgTransportError
    from outreach.ig_sender import (
        _observe_delivery,
        _open_dm_thread,
        _thread_texts,
        _wait_ig_state,
    )
    from outreach.store import get_config, set_config
    from outreach.variants import render_lane_opener
    from outreach.wa_sender import _MIN_MATCH, _norm

    stamp = get_config(db, _IG_PROBE_AT_KEY)
    if stamp:
        try:
            elapsed = (now - datetime.fromisoformat(stamp)).total_seconds()
        except ValueError:
            elapsed = None  # corrupted stamp: run the pass
        if elapsed is not None and 0 <= elapsed < _PROBE_INTERVAL_S:
            return ()

    rows = db.conn.execute(
        "SELECT DISTINCT l.id, l.instagram, m.account_id AS account_id"
        " FROM leads l"
        " JOIN message_log m ON m.lead_id = l.id AND m.channel = 'ig'"
        " WHERE l.state IN ('SENT', 'FOLLOWUP')"
        " AND l.instagram IS NOT NULL AND TRIM(l.instagram) != ''"
        " ORDER BY l.id"
    ).fetchall()
    if not rows:
        return ()  # nothing to scan: the browser never opens

    rows = _bounded_threads(db, rows)

    groups: dict[str, list[Any]] = {}
    for row in rows:
        groups.setdefault(str(row["account_id"] or ""), []).append(row)

    shot_dir = Path(evidence_dir) if evidence_dir else Path(EVIDENCE_DIR)
    replied: list[int] = []
    #: Instrumentation (2026-11-17): the pass overran its 240 s budget and the
    #: log could not say WHERE the time went - nav, state wait or reads. One
    #: summary row per pass answers it with numbers instead of theories.
    timings: list[dict[str, float | int | str]] = []
    surfaces: dict[str, int] = {}
    deadline = time.monotonic() + budget_s
    stopped_early = False
    left_for_next = 0
    for account_id, group in groups.items():
        factory = opener if opener_for is None else opener_for(account_id)
        if factory is None:
            continue  # no session for this account: skip, invent nothing
        with factory() as page:

            def _scan(row: Any, page: Any = page, account_id: str = account_id) -> None:
                """One thread, fully timed.

                Everything inside runs under a `finally`, so a thread that ends
                on a skip (no texts, our own copy, a canned auto-reply) still
                reports its cost - otherwise the summary would describe only the
                threads that happened to reach a verdict.
                """
                lead_id = int(row["id"])
                started = time.monotonic()
                nav_s = state_s = 0.0
                state = "unknown"
                try:
                    # Same two-surface walk the send path uses (live 2026-11-16:
                    # `ig.me` times out from this network on :80 and :443 while
                    # `instagram.com/<handle>/` answers 302 in 0.4 s). A raw goto
                    # to the deep link raised out of the WHOLE pass on the first
                    # blocked handle, so no thread after it was ever read.
                    try:
                        surface = _open_dm_thread(page, row["instagram"])
                    except IgTransportError:
                        surfaces["unreachable"] = surfaces.get("unreachable", 0) + 1
                        return  # both surfaces dead for this thread: no writes
                    surfaces[surface] = surfaces.get(surface, 0) + 1
                    nav_s = time.monotonic() - started
                    state_started = time.monotonic()
                    state = _wait_ig_state(
                        page, deadline_s=_IG_PROBE_STATE_DEADLINE_S
                    )
                    state_s = time.monotonic() - state_started
                    if state != "ready":
                        return  # login wall / dead / halt: fail-closed, no writes
                    # the gate opens on evidence, not on a schedule
                    _observe_delivery(db, page, now)
                    texts = _thread_texts(page)
                    if not texts:
                        return  # nothing rendered: skip, invent nothing
                    last = _norm(texts[-1])
                    if len(last) < _MIN_MATCH:
                        return  # too short to judge direction: never invent
                    lead_row = db.conn.execute(
                        "SELECT * FROM leads WHERE id=?", (lead_id,)
                    ).fetchone()
                    ours = _ours_copy(db, "ig", lead_row, now=now)
                    # ONE direction law: ours / auto / reply, judged on the whole
                    # copy family (2026-11-15: an unlogged variant and the no-dish
                    # frozen copy both used to read as restaurant replies).
                    verdict = classify_newest(last, ours, min_len=_MIN_MATCH)
                    if verdict is None:
                        return  # too short to judge: never invent a direction
                    if verdict == "ours":
                        return  # our own copy is the newest bubble
                    if verdict == "auto":
                        # a canned acknowledgement is not a human reading it: the
                        # ladder keeps running and the lead does not advance.
                        with db.conn:
                            db.conn.execute(
                                "INSERT INTO digest(body) VALUES (?)",
                                (f"AUTO REPLY [lead {lead_id}]: {texts[-1][:120]}"
                                 " (canned acknowledgement - no human yet,"
                                 " ladder continues)",),
                            )
                        return
                    shot = shot_dir / f"ig_reply_{lead_id}_{now:%Y%m%dT%H%M%S}.png"
                    page.screenshot(path=str(shot))  # ADR-15 evidence before register
                    if register_reply(
                        db,
                        lead_id,
                        channel="ig",
                        now=now,
                        reply_text=texts[-1],
                        account_id=str(account_id or ""),
                        sender_handle=str(row["instagram"] or ""),
                    ) == "registered":
                        replied.append(lead_id)
                finally:
                    total_s = time.monotonic() - started
                    timings.append({
                        "id": lead_id, "total": total_s, "nav": nav_s,
                        "state": state_s,
                        "read": max(0.0, total_s - nav_s - state_s),
                        "state_name": state,
                    })

            for row in group:
                if time.monotonic() >= deadline:
                    stopped_early = True
                    left_for_next += 1
                    continue
                _scan(row)
    if timings or stopped_early:
        total_s = sum(float(t["total"]) for t in timings)
        summary = f"REPLIES PASS: {len(timings)} thread(s) in {total_s:.1f}s"
        if timings:
            slowest = max(timings, key=lambda t: float(t["total"]))
            summary += (
                f" (avg {total_s / len(timings):.1f}s)"
                f" | slowest lead {slowest['id']}: {float(slowest['total']):.1f}s"
                f" (nav {float(slowest['nav']):.1f} / state {float(slowest['state']):.1f}"
                f" / read {float(slowest['read']):.1f}, {slowest['state_name']})"
            )
        if stopped_early:
            summary += (
                f" | stopped early at the {budget_s:.0f}s budget,"
                f" {left_for_next} thread(s) left for the next pass"
            )
        summary += f" | surfaces {surfaces or '{}'}"
        with db.conn:
            db.conn.execute("INSERT INTO digest(body) VALUES (?)", (summary,))
    set_config(db, _IG_PROBE_AT_KEY, now.isoformat())
    return tuple(replied)
