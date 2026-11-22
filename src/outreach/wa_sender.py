"""WhatsApp Web sender (B10): real WhatsApp Web via Playwright, ADR-2/13.

Dedicated automation profile: a repo-local Chromium user-data dir with
`--profile-directory=Outreach` (ADR-13 — the owner's personal profile is
NEVER opened). Send path per dispatch, fail-closed at every step:

1. guard trio BEFORE any browser I/O: queue live, template owner-confirmed,
   daily cap under limit (gate #3) — refusals never launch the browser.
2. navigate straight into WhatsApp Web's own send URL (DCL, not `load` —
   `load` lands at ~27.7s here and blew the 30s default in 6/6 engine
   cycles; wa.me's interstitial never auto-forwards, 2026-10-27 evidence)
   and poll classify through the ~54s app boot: login wall (QR = owner
   session required), dead number (WA_DEAD exit), ready (composer
   visible), deadline → unknown (fail-closed: no interaction, nothing
   written).
3. ready → humanized typing into the composer + Enter + screenshot evidence
   (ADR-15) → atomic `commit_send` (caps + message_log incl. screenshot
   path + ROUTED→SENT).

The WA composer selector is confirmed against the live DOM at the owner's
login session; the classifier keys on structural signals, not selectors.
"""
from __future__ import annotations

import json
import os
import re
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from outreach.browser import (  # re-exports for the existing B10 imports
    EVIDENCE_DIR,
    PROFILE_DIRECTORY,
    chrome_launch_options,
    launch_outreach_profile,
)
from outreach.caps import send_allowed
from outreach import dispatcher as _dispatch
from outreach.dispatcher import _assert_queue_live, commit_send
from outreach.machine import transition
from outreach.templates import (
    assert_sendable_template,
    render_for_lead,
)

if TYPE_CHECKING:
    from outreach.store import Database

_COMPOSER = 'div[contenteditable="true"]'
_STATE_DEADLINE_S = 90.0  # live 2026-10-27: ~42s to shell, ~54s to decision
_STATE_POLL_S = 1.0
_TICK_DEADLINE_S = 20.0  # sent-✓ wait; fail-closed → "unverified", nothing counted
_TICK_POLL_S = 0.5
_MIN_MATCH = 40  # guard only trusts a substantial run of OUR copy
_DECISIVE_STATES = ("ready", "wa_dead", "login_wall")  # anything else keeps polling

# Reads the LAST rendered message's text. Live-probed 2026-10-27 against the
# owner's session: messages are `.copyable-text` nodes carrying
# `data-pre-plain-text` (the attribute header is NOT part of innerText), and
# this build renders NO `.message-out` class — so direction is proven by the
# text itself (a foreign reply is never a substring of our frozen copy).
# Returns `{"text": ...}` or null — the recovery guard keys on this.
_OUTGOING_JS = """() => {
  // wa-outgoing: text of the newest rendered message (data-pre-plain-text)
  const all = [...document.querySelectorAll('.copyable-text[data-pre-plain-text]')];
  if (!all.length) return null;
  const text = (all[all.length - 1].innerText || '').trim();
  return text ? {text} : null;
}"""

# Delivery proof template (`__COPY__` is json.dumps of the frozen copy).
# The classic msg-check/msg-check-double ticks are NOT rendered by this
# WhatsApp Web build (live probe 2026-10-27: zero `[data-icon]` ticks in the
# chat), so the proof is stronger and build-independent: EVERY substantial
# paragraph of OUR copy must be present as a rendered message. A partial
# send (paragraph missing) therefore fails closed.
_TICK_JS = """() => {
  // wa-tick: our copy must be present in the rendered messages
  const copy = __COPY__;
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const texts = [...document.querySelectorAll('.copyable-text')]
    .map(n => norm(n.innerText)).filter(Boolean);
  const paras = copy.split('\\n').map(norm).filter(p => p.length >= 40);
  if (paras.length) {
    return paras.every(p => texts.some(t => t.includes(p)
      || (t.length >= 40 && p.includes(t))));
  }
  const needle = norm(copy);
  return texts.some(t => t === needle || (t.length >= 40 && needle.includes(t)));
}"""


def _delivery_script(copy: str) -> str:
    return _TICK_JS.replace("__COPY__", json.dumps(copy))


@dataclass(frozen=True)
class SendOutcome:
    state: str  # "sent" | "wa_dead" | "login_wall" | "unknown" | "capped"
    screenshot: Path | None


def deep_link(phone: str | None) -> str:
    """Click-to-chat link that lands directly IN WhatsApp Web.

    Digits only (full intl number). Deliberately NOT `wa.me`: that
    interstitial (api.whatsapp.com) never auto-forwards — classify stays
    `unknown` forever (live-proven 2026-10-27) — so the send path uses
    the web app's own send URL, where the live session opens the chat.
    """
    digits = re.sub(r"\D", "", str(phone or ""))
    if not digits:
        raise ValueError("phone number required for a WhatsApp deep link")
    return (
        "https://web.whatsapp.com/send/"
        f"?phone={digits}&type=phone_number&app_absent=0"
    )


def _probe_state(page: Any) -> dict[str, bool]:
    """Read WhatsApp Web's structural DOM signals (one round-trip)."""
    return page.evaluate(
        "() => ({"
        "qr: !!document.querySelector('canvas') ||"
        "/scan to log in|link a device/i.test(document.body.innerText),"
        "dead: /not on WhatsApp|isn['\u2019]t on WhatsApp|out of WhatsApp/i"
        ".test(document.body.innerText),"
        "composer: !!document.querySelector('" + _COMPOSER + "'),"
        "shell: !!document.querySelector('#side')"
        " || !!document.querySelector('div[data-testid=\"chat-list\"]')"
        "})"
    )


def classify_wa_state(page: Any) -> str:
    """Map live DOM signals to a fail-closed state tag.

    `ready` = composer in an open chat (sendable now); `ready_home` = the
    chat-list shell of a live session WITHOUT a chat open (session proof
    only — the sender still needs its deep-link chat, so this blocks sends
    and fails closed).
    """
    probe = _probe_state(page)
    if probe.get("dead"):
        return "wa_dead"
    if probe.get("composer"):
        return "ready"
    if probe.get("shell"):
        return "ready_home"
    if probe.get("qr"):
        return "login_wall"
    return "unknown"


def _wait_wa_state(page: Any) -> str:
    """Poll classify through the web app's boot (fail-closed at deadline).

    The app renders `unknown` → shell (`ready_home`) → chat over ~54s on
    this box before any decisive state appears, so a single frame-1 read
    would fail closed every dispatch (live evidence 2026-10-27). At
    deadline the LAST non-decisive state is returned, so callers still
    fail closed with nothing typed.
    """
    deadline = time.monotonic() + _STATE_DEADLINE_S
    state = classify_wa_state(page)
    while state not in _DECISIVE_STATES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_STATE_POLL_S, remaining))
        state = classify_wa_state(page)
    return state


def _norm(text: Any) -> str:
    """Whitespace-insensitive form — DOM text and typed copy must compare equal."""
    return " ".join(str(text or "").split())


def _last_outgoing(page: Any) -> str | None:
    payload = page.evaluate(_OUTGOING_JS)
    if isinstance(payload, dict):
        text = payload.get("text")
    else:
        text = payload
    text = _norm(text)
    return text or None


def _already_delivered(db: "Database", lead_id: int, page: Any, copy: str) -> bool:
    """Recovery guard (HANDOFF §6, layer 2): the copy is already in this chat
    and was NEVER committed (no message_log row) — a previous attempt sent it
    and died before bookkeeping (the 2026-10-27 incident shape). Recover
    instead of typing it a second time.

    A committed send makes this False, so the day3/day7 ladder resend of the
    same frozen copy is never blocked (message_log gate first, DOM second).
    """
    committed = db.conn.execute(
        "SELECT COUNT(*) FROM message_log WHERE lead_id=?", (lead_id,)
    ).fetchone()[0]
    if committed:
        return False
    last = _last_outgoing(page)
    if last is None:
        return False
    return len(last) >= _MIN_MATCH and last in _norm(copy)


def _wait_sent_tick(page: Any, copy: str) -> bool:
    """Poll for delivery proof before screenshot + commit (ADR-15 layer 3):
    no proof → caller fails closed with `unverified`, nothing is counted, and
    the recovery guard lands it on the next claim."""
    script = _delivery_script(copy)
    deadline = time.monotonic() + _TICK_DEADLINE_S
    while True:
        if bool(page.evaluate(script)):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_TICK_POLL_S, remaining))


def _type_copy(composer: Any, text: str) -> None:
    """Owner-kept multi-bubble style: one deliberate type+Enter per paragraph.

    NEVER a newline inside type() — WhatsApp's composer treats it as an Enter
    keypress, which fired sends mid-typing and caused the 2026-10-27
    double-send (HANDOFF §6).
    """
    for line in text.split("\n"):
        if line.strip():
            composer.type(line, delay=30)
            composer.press("Enter")


def _take_screenshot(page: Any, screenshot_dir: str | Path, lead_id: int,
                     kind: str) -> Path:
    """ADR-15 visible proof: one screenshot of the chat per attempt."""
    directory = Path(screenshot_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    shot = directory / f"{kind}_{lead_id}_{stamp}.png"
    page.screenshot(path=str(shot))
    return shot


def _wa_row(db: "Database", lead_id: int) -> dict:
    from outreach.variants import render_select

    row = db.conn.execute(render_select(), (lead_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown lead id={lead_id}")
    return row


def send_wa_message(
    db: "Database",
    lead_id: int,
    now: datetime,
    *,
    page: Any,
    screenshot_dir: str | Path,
) -> SendOutcome:
    """Execute one WhatsApp dispatch end-to-end (guard → send → commit)."""
    _entry = _dispatch._session_clock()  # true elapsed -> the commit instant
    _assert_queue_live(db)
    assert_sendable_template(db)
    row = _wa_row(db, lead_id)
    if row["route_channel"] != "wa":
        raise ValueError(
            f"send_wa_message only handles channel 'wa', lead is "
            f"{row['route_channel']!r}"
        )
    if not send_allowed(db, "wa", now):
        return SendOutcome("capped", None)  # gate #3: browser never opens

    page.goto(
        deep_link(row["phone"]), wait_until="domcontentloaded", timeout=45000
    )
    state = _wait_wa_state(page)
    if state == "wa_dead":
        transition(
            db,
            lead_id,
            "WA_DEAD",
            reason="detector: number is not on WhatsApp (WA deep link)",
            actor="wa_sender",
        )
        return SendOutcome("wa_dead", None)
    if state != "ready":
        # login_wall → owner QR session needed (never auto-login, ADR-8
        # spirit); unknown → fail-closed, nothing typed, nothing recorded.
        return SendOutcome(state, None)

    message = render_for_lead("wa", row)
    time.sleep(0)  # runner loop owns pacing gaps; tests pass instantly
    if _already_delivered(db, lead_id, page, message.text):
        # recovery: copy already delivered but uncommitted — land the
        # bookkeeping + ADR-15 proof WITHOUT typing it a second time
        shot = _take_screenshot(page, screenshot_dir, lead_id, "wa")
        commit_send(db, lead_id, _dispatch.commit_instant(now, _entry),
                    screenshot=str(shot), message=message)
        return SendOutcome("sent", shot)
    composer = page.locator(_COMPOSER).first
    composer.click()
    _type_copy(composer, message.text)
    if not _wait_sent_tick(page, message.text):
        return SendOutcome("unverified", None)  # no commit, no count, no shot

    shot = _take_screenshot(page, screenshot_dir, lead_id, "wa")
    commit_send(db, lead_id, _dispatch.commit_instant(now, _entry),
                screenshot=str(shot), message=message)
    return SendOutcome("sent", shot)


def resend_wa_message(
    db: "Database",
    lead_id: int,
    *,
    now: datetime,
    message: Any,
    page: Any,
    screenshot_dir: str | Path,
) -> SendOutcome:
    """Transport-only resend for the follow-up ladder (mirrors resend_email).

    Transports the ALREADY-RENDERED frozen copy byte-exact and performs NO
    commit, cap counting or message_log work — the ladder owns that
    bookkeeping (it already asserted queue live, template and cap before
    calling). One safety write is the documented exception: a number that
    left WhatsApp mid-window still walks the lead to WA_DEAD so the ladder
    stops retrying it. Every other state fails closed with nothing typed.
    """
    row = _wa_row(db, lead_id)
    if row["route_channel"] != "wa":
        raise ValueError(
            f"resend_wa_message only handles channel 'wa', lead is "
            f"{row['route_channel']!r}"
        )
    page.goto(
        deep_link(row["phone"]), wait_until="domcontentloaded", timeout=45000
    )
    state = _wait_wa_state(page)
    if state == "wa_dead":
        transition(
            db,
            lead_id,
            "WA_DEAD",
            reason="detector: number is not on WhatsApp (WA deep link)",
            actor="wa_sender",
        )
        return SendOutcome("wa_dead", None)
    if state != "ready":
        # login_wall → owner QR session needed (never auto-login, ADR-8);
        # unknown → fail-closed: nothing typed, the ladder keeps its row
        # pending for the next window.
        return SendOutcome(state, None)

    if _already_delivered(db, lead_id, page, message.text):
        # recovery WITHOUT typing (transport-only law untouched: no commit)
        return SendOutcome(
            "sent", _take_screenshot(page, screenshot_dir, lead_id, "wa")
        )
    composer = page.locator(_COMPOSER).first
    composer.click()
    _type_copy(composer, message.text)
    if not _wait_sent_tick(page, message.text):
        return SendOutcome("unverified", None)  # pending row stays for a retry
    return SendOutcome(
        "sent", _take_screenshot(page, screenshot_dir, lead_id, "wa")
    )


def send_wa_via_profile(
    db: "Database",
    lead_id: int,
    now: datetime,
    *,
    screenshot_dir: str | Path | None = None,
) -> SendOutcome:
    """Engine adapter for `real_senders["wa"]`: owns the profile lifecycle.

    Matches the dispatch contract `senders[channel](db, lead_id, now)` —
    no page parameter — by opening the dedicated Outreach profile (ADR-13)
    for exactly this dispatch. The template gate is asserted BEFORE any
    browser boot so a parked queue never launches Chrome; the core
    re-asserts it after boot (the same defense-in-depth the claim loop
    applies for queue-live and caps).
    """
    assert_sendable_template(db)
    screenshot_dir = screenshot_dir or EVIDENCE_DIR
    with launch_outreach_profile(headless=True) as page:
        return send_wa_message(
            db, lead_id, now, page=page, screenshot_dir=screenshot_dir
        )


def resend_wa_via_profile(
    db: "Database",
    lead_id: int,
    *,
    now: datetime,
    message: Any,
    screenshot_dir: str | Path | None = None,
) -> SendOutcome:
    """Ladder adapter for `real_resenders["wa"]`: same profile lifecycle.

    `process_due_followups` calls `senders[channel](db, lead_id, now=now,
    message=message)` — this matches that shape exactly and delegates to
    the transport-only core (the ladder keeps all bookkeeping). Template
    gate asserted before any browser boot.
    """
    assert_sendable_template(db)
    screenshot_dir = screenshot_dir or EVIDENCE_DIR
    with launch_outreach_profile(headless=True) as page:
        return resend_wa_message(
            db,
            lead_id,
            now=now,
            message=message,
            page=page,
            screenshot_dir=screenshot_dir,
        )
