"""Instagram sender (B11): real Instagram web via the shared Outreach
profile (ADR-2/13) — no headless messaging libs, no APIs.

Fail-closed per dispatch, plus the ADR-8 channel-halt law: a ban /
login-challenge signal from Instagram HALTS THE IG LANE immediately
(config `ig_halted` + per-lead CHANNEL_BANNED→ROUTED/UNREACHABLE drain)
so no further IG usage happens while the queue drains to email.
Never auto-retries logins (ADR-8); login walls abort untouched.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from outreach.browser import (  # noqa: F401 (shared)
    EVIDENCE_DIR,
    launch_outreach_profile,
)
from outreach.caps import ensure_ig_warmup_start, send_allowed
from outreach import dispatcher as _dispatch
from outreach import fleet
from outreach.dispatcher import (
    IgTransportError,
    _assert_queue_live,
    commit_send,
)
from outreach.machine import transition
from outreach.router import route_lead
from outreach.store import get_config, set_config
from outreach.templates import assert_sendable_template, render_for_lead
from outreach.wa_sender import (  # shared double-send guard vocabulary
    SendOutcome,
    _MIN_MATCH,
    _norm,
    _take_screenshot,
)

if TYPE_CHECKING:
    from outreach.store import Database

_IG_COMPOSER = 'div[contenteditable="true"]'
_PRESENCE_DEADLINE_S = 20.0  # IG renders the sent bubble fast; fail-closed at deadline
_PRESENCE_POLL_S = 0.5
_IG_STATE_DEADLINE_S = 30.0  # live probe 2026-10-27: composer paints ~5.1s after goto
_IG_STATE_POLL_S = 1.0
# Terminal states; anything else (unknown, ready_home) keeps polling — the
# SPA can flash its shell/feed frame before the thread composer exists.
#: Owner-copy signatures: a bubble carrying one of these was written by this
#: department (the opener and the P.S. product line are byte-pinned copy).
_OWN_COPY_MARKERS = (
    "salam",
    "p.s. full transparency, i build",
    "i build restaurant systems",
    "i build ordering apps for restaurants",
)

_IG_DECISIVE_STATES = (
    "ready",
    "undeliverable",
    "ig_dead",
    "login_wall",
    "ban_signal",
)

# Reads the thread's rendered message texts (the `ig-thread` marker names
# this script in the test double; live IG shows one `[dir="auto"]` bubble per
# message carrying the full copy with its line breaks — owner-verified
# 2026-10-27 by their own manual send).
_IG_THREAD_JS = """() => {
  // ig-thread: last-rendered message texts of the open DM
  const nodes = document.querySelectorAll('div[dir="auto"]');
  return [...nodes].map(n => (n.innerText || '').trim()).filter(Boolean);
}"""


#: The one milestone signal on IG: Instagram's own "Delivered" label under a
#: bubble WE sent. Owner ruling 2026-11-11: ig1 stays follower-gated (no cold
#: first touches) until this is seen once, and the first sighting is reported.
_IG_DELIVERED_JS = """() => {
  const nodes = [...document.querySelectorAll('span, div')];
  return nodes.some(n => /^Delivered$/i.test((n.innerText || '').trim()));
}"""


def _observe_delivery(db: "Database", page: Any, now: Any) -> bool:
    """Record the first delivered bubble ever; report it once.

    Read-only on failure: if the page cannot be read, nothing is claimed and
    the gate stays shut. The milestone row is written in the same transaction
    as the config stamp, so a crash cannot announce it twice.
    """
    if get_config(db, "ig_delivery_confirmed"):
        return True
    try:
        seen = bool(page.evaluate(_IG_DELIVERED_JS))
    except Exception:  # noqa: BLE001 - unreadable page proves nothing
        return False
    if not seen:
        return False
    stamp = str(now or "")
    with db.conn:
        db.conn.execute(
            "INSERT INTO digest(body) VALUES (?)",
            (
                "DELIVERY CONFIRMED (milestone): Instagram rendered a"
                " Delivered bubble under our own message - the follower gate"
                f" can open. Observed {stamp}.",
            ),
        )
    set_config(
        db,
        "ig_delivery_confirmed",
        {"at": stamp, "source": "thread probe"},
    )
    set_config(db, "ig_cold_dms_blocked", False)  # the gate opens for good
    return True


def _cold_thread(db: "Database", lead_id: int) -> bool:
    """No message_log row on this lead = a cold first touch."""
    row = db.conn.execute(
        "SELECT COUNT(*) FROM message_log WHERE lead_id=?", (lead_id,)
    ).fetchone()
    return int(row[0] or 0) == 0


def cold_dms_blocked(db: "Database") -> bool:
    """Is the follower gate shut? Armed per lane; opens on first delivery.

    Owner ruling 2026-11-11: ig1 is follower-gated. The flag is armed on the
    live lane, and a single observed Delivered bubble opens it for good.
    """
    return bool(get_config(db, "ig_cold_dms_blocked")) and not bool(
        get_config(db, "ig_delivery_confirmed")
    )


def _thread_texts(page: Any) -> list[str]:
    raw = page.evaluate(_IG_THREAD_JS)
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item) for item in raw if str(item).strip()]


def _already_delivered(db: "Database", lead_id: int, page: Any, copy: str) -> bool:
    """Recovery guard: the copy is already in this thread and was NEVER
    committed (no message_log row) — a previous attempt crashed between the
    DOM write and bookkeeping. Recover instead of typing it a second time.

    A committed send makes this False, so the day3/day7 ladder resend of the
    same frozen copy is never blocked (gate: message_log first, DOM second).
    """
    committed = db.conn.execute(
        "SELECT COUNT(*) FROM message_log WHERE lead_id=?", (lead_id,)
    ).fetchone()[0]
    if committed:
        return False
    texts = _thread_texts(page)
    if not texts:
        return False
    last = _norm(texts[-1])  # IG keeps ONE bubble per message
    if len(last) >= _MIN_MATCH and last in _norm(copy):
        return True
    # LIVE 2026-11-09: the crash left a typable copy in the thread while the
    # render is not stable across attempts (the variant engine picks per
    # render), so an exact match could miss our own words and type a SECOND
    # first touch into the same DM. The owner's copy has a signature clause:
    # a thread whose last bubble carries it is ours, whatever variant sent it.
    lowered = last.casefold()
    return any(marker in lowered for marker in _OWN_COPY_MARKERS)


def _copy_landed(copy: str, texts: list[str]) -> bool:
    """Sent-presence check: our copy is rendered in the thread — either as
    the whole bubble (live IG) or via a substantial fragment of ours
    (verification reads whatever the thread currently holds)."""
    needle = _norm(copy)
    for text in texts:
        seen = _norm(text)
        if not seen:
            continue
        if needle in seen:
            return True  # full copy inside one rendered bubble
        if len(seen) >= _MIN_MATCH and seen in needle:
            return True  # a substantial line of our own copy
    return False


def _wait_presence(page: Any, copy: str) -> bool:
    """Poll until the copy is VERIFIED in the thread (fail-closed at deadline:
    no presence → no commit, no count — ADR-15 + owner policy)."""
    deadline = time.monotonic() + _PRESENCE_DEADLINE_S
    while True:
        if _copy_landed(copy, _thread_texts(page)):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_PRESENCE_POLL_S, remaining))


def _type_copy(composer: Any, text: str) -> None:
    """One bubble, line breaks preserved: EVERY newline is a deliberate
    Shift+Enter and the only plain Enter is the final send key.

    Never a newline inside type() — that mid-typing Enter is the proven
    2026-10-27 double-send root cause (HANDOFF §6).
    """
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.strip():
            composer.type(line, delay=30)
        if index < len(lines) - 1:
            composer.press("Shift+Enter")
    composer.press("Enter")


def ig_handle(raw: str | None) -> str:
    """Harvest the bare handle from a URL, @handle or plain handle."""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("instagram handle required")
    if "://" in text:
        path = re.sub(r"^[a-z]+://[^/]+/?", "", text, flags=re.IGNORECASE)
        text = path.strip("/")
    text = text.lstrip("@").split("?")[0].split("#")[0].strip("/")
    # `instagram.com/m/<handle>` is Instagram's mobile route: never build
    # `ig.me/m/m/<handle>` out of it (live link shape, 2026-11-11).
    parts = [part for part in text.split("/") if part]
    while len(parts) > 1 and parts[0].casefold() in ("m", "instagram.com", "www.instagram.com"):
        parts.pop(0)
    text = parts[0] if parts else ""
    if not text or text.casefold() == "m":
        raise ValueError(
            "instagram handle required (a bare `m` path segment is a route,"
            " not a profile)"
        )
    return text


_IG_NAV_TIMEOUT_MS = 20_000
#: The `ig.me` shortcut is dead from some networks (measured 2026-11-16: TCP
#: :80 and :443 to 157.240.229.174 time out; instagram.com answers 302 in
#: 0.4 s). Waiting the full navigation timeout on it cost every thread of every
#: replies pass - the stage overran its 240 s budget by 54 s. A blocked host is
#: discovered quickly; the profile fallback keeps the long timeout.
_DEEP_LINK_TIMEOUT_MS = 6_000
_IG_TRANSPORT_HINTS = (
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_TIMED_OUT",
    "TIMEOUT",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_REFUSED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_NETWORK_CHANGED",
    "ERR_ABORTED",
)
_IG_MESSAGE_SELECTORS = (
    'a[href*="/direct/t/"]',
    'div[role="button"]:has-text("Message")',
    'button:has-text("Message")',
)


def ig_profile_url(raw: str | None) -> str:
    """The profile URL: the surface that does NOT time out (live 2026-11-13)."""
    return f"https://www.instagram.com/{ig_handle(raw)}/"


def _is_transport_failure(exc: Exception) -> bool:
    text = f"{exc!r}".upper()
    # Chromium names every network failure `net::ERR_...`. The hint list below
    # was written from the shapes seen by 2026-11-13 and missed others: live
    # 2026-11-17 an `ERR_HTTP_RESPONSE_CODE_FAILURE at ig.me/...` escaped it and
    # aborted a whole replies pass. Any `net::ERR_` is a navigation failure.
    return any(hint in text for hint in _IG_TRANSPORT_HINTS) or "NET::ERR_" in text


def _goto(page: Any, url: str, *, timeout_ms: int = _IG_NAV_TIMEOUT_MS) -> None:
    """Navigate with an explicit timeout; tolerate minimal test doubles."""
    try:
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
    except TypeError:  # a fake page with goto(url) only
        page.goto(url)


def _open_dm_thread(page: Any, raw: str | None) -> str:
    """Open the DM thread: `ig.me` first, profile page as the fallback.

    Live 2026-11-13 (audit F3): 24 of 24 IG navigation failures in seven days
    were `net::ERR_CONNECTION_TIMED_OUT at https://ig.me/m/<handle>`. The
    deep-link shortcut is the fragile surface; `instagram.com/<handle>/` is
    not. So: one deep-link attempt, then the profile, then click Message.
    If both surfaces fail to LOAD, raise IgTransportError - the dispatcher
    parks the lead in TRANSPORT_HOLD instead of re-claiming the dead URL on
    the very next tick.

    Returns the surface actually used: `"ig.me"`, `"profile"` (the Message
    affordance was found and clicked), or `"profile_no_message"` (the profile
    loaded but nothing matched — R9, 2026-11-13: the silent third case used to
    return `"profile"` and leave the caller with a nameless frame).
    """
    try:
        _goto(page, ig_dm_link(raw), timeout_ms=_DEEP_LINK_TIMEOUT_MS)
        return "ig.me"
    except Exception as exc:
        if not _is_transport_failure(exc):
            raise
    try:
        _goto(page, ig_profile_url(raw))
    except Exception as exc:
        if not _is_transport_failure(exc):
            raise
        raise IgTransportError(
            f"ig.me and profile both failed to load for {ig_handle(raw)}"
        ) from exc
    for selector in _IG_MESSAGE_SELECTORS:
        try:
            target = page.locator(selector).first
            if target.count() and target.is_visible():
                target.click(timeout=5_000)
                return "profile"
        except Exception:  # a selector that does not exist is not a failure
            continue
    return "profile_no_message"


def _resolve_send_surface(page: Any, raw: str | None) -> tuple[str, str]:
    """Open the DM thread and return `(surface, state)` with no nameless state.

    R9 (2026-11-13): after the fallback lands on the profile, the send must
    either reach the composer or fail with a state a human can act on. The
    three IG outcomes are `ready`, `profile_no_message` (no Message
    affordance on the profile — the selector set needs a look) and
    `thread_slow` (the thread was asked for and never painted inside the
    deadline). `unknown` is a classification, never a send verdict.
    """
    surface = _open_dm_thread(page, raw)
    if surface == "profile_no_message":
        return surface, "profile_no_message"
    state = _wait_ig_state(page)
    if state == "unknown":
        # The classifier could not name the frame at deadline. The send path
        # still can: the thread we asked for is the thing that is missing.
        state = "thread_slow"
    return surface, state


def ig_dm_link(raw: str | None) -> str:
    """ig.me deep link opens the DM thread for a handle (login required)."""
    return f"https://ig.me/m/{ig_handle(raw)}"


def _probe_state(page: Any) -> dict[str, bool]:
    """Read Instagram's structural DOM signals (one round-trip)."""
    return page.evaluate(
        "() => ({"
        "login: !!document.querySelector('input[type=password]') ||"
        "/^log in/i.test(document.body.innerText),"
        "ban: /try again later|action blocked|your account has been locked|"
        "account.*restricted|temporarily blocked/i"
        ".test(document.body.innerText),"
        "dead: /sorry, this page isn.t available|page not found/i"
        ".test(document.body.innerText),"
        # LIVE 2026-11-09: this thread renders a composer (so `ready` fired)
        # while Instagram refuses delivery - 'This account can't receive your
        # message because they don't allow new message requests from
        # everyone.' The DM is typed into a dead thread and every cap slot,
        # SENT state and ladder rung was spent on a message nobody got.
        "undeliverable: /can.t receive your message|don.t allow new message"
        " requests|message requests from everyone/i"
        ".test(document.body.innerText),"
        "composer: !!document.querySelector('" + _IG_COMPOSER + "'),"
        "home: !!document.querySelector('main article')"
        "})"
    )


def classify_ig_state(page: Any) -> str:
    """Map live DOM signals to a fail-closed state tag (ban wins first).

    `ready` = DM composer in an open thread (sendable now); `ready_home` =
    the logged-in feed WITHOUT a thread open (session proof only — the
    sender still needs its deep-link thread, so this blocks sends and
    fails closed).
    """
    probe = _probe_state(page)
    if probe.get("ban"):
        return "ban_signal"
    if probe.get("dead"):
        return "ig_dead"
    # Before `composer`: a blocked thread still paints the composer.
    if probe.get("undeliverable"):
        return "undeliverable"
    if probe.get("composer"):
        return "ready"
    if probe.get("home"):
        return "ready_home"
    if probe.get("login"):
        return "login_wall"
    return "unknown"


def _wait_ig_state(page: Any, *, deadline_s: float = _IG_STATE_DEADLINE_S) -> str:
    """Poll classify until the SPA paints a decisive state (fail-closed at
    deadline).

    Live probe 2026-10-27 16:20Z: goto returns at +7.1s with EVERY signal
    false ("unknown"); the DM composer only exists at +12.2s. A single
    frame-1 read fail-closed every dispatch — zero IG DMs ever left the
    lane. Mirrors WA's `_wait_wa_state`.
    """
    deadline = time.monotonic() + deadline_s
    state = classify_ig_state(page)
    while state not in _IG_DECISIVE_STATES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_IG_STATE_POLL_S, remaining))
        state = classify_ig_state(page)
    return state


def _halt_ig_channel(db: "Database", reason: str) -> int:
    """ADR-8 bank-shot: halt the IG lane and drain every IG-routed lead.

    Returns the number of drained leads. Idempotent at the config level
    (the halt marker short-circuits the claim loop afterwards).
    """
    set_config(
        db, "ig_halted", {"reason": reason, "drained": True}
    )
    rows = db.conn.execute(
        "SELECT id, name, phone, instagram, email, region, data_json FROM leads "
        "WHERE state='ROUTED' AND route_channel='ig' ORDER BY id"
    ).fetchall()
    drained = 0
    for row in rows:
        lead_id = int(row["id"])
        transition(
            db,
            lead_id,
            "CHANNEL_BANNED",
            reason=f"ADR-8 halt: {reason}",
            actor="ig_sender",
        )
        transition(db, lead_id, "ROUTED", reason="ig lane halted; re-route",
                   actor="ig_sender")
        try:
            payload = json.loads(row["data_json"] or "{}") or {}
        except (TypeError, ValueError):
            payload = {}
        decision = route_lead(
            phone=row["phone"],
            instagram=row["instagram"],
            email=row["email"],
            ig_inactive=1,  # the lane is halted — treat as inactive
            order_line=bool(payload.get("order_line")) if isinstance(payload, dict) else False,
            region=row["region"],
            # contamination hold 2026-11-03: the drain obeys the email
            # quarantine like every other routing site (own-domain evidence)
            website=(payload or {}).get("website")
            if isinstance(payload, dict) else None,
            email_verified=(payload or {}).get("email_verified")
            if isinstance(payload, dict) else None,
        )
        with db.conn:
            db.conn.execute(
                "UPDATE leads SET route_channel=? WHERE id=?",
                (decision.channel or "", lead_id),
            )
        if decision.channel is None:
            transition(
                db,
                lead_id,
                "UNREACHABLE",
                reason="no remaining channel after IG halt",
                actor="ig_sender",
                park_kind="verdict",
            )
        drained += 1
    return drained


def _ig_row(db: "Database", lead_id: int) -> dict:
    from outreach.variants import render_select

    row = db.conn.execute(render_select(), (lead_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown lead id={lead_id}")
    return row


def _ig_profile_for(db: "Database", lead_id: int) -> str:
    """The profile that owns THIS lead's Instagram session (v1.1 batch 4).

    L1 assignment decides: an IG-assigned lead keeps its account's profile
    (`Outreach-ig2`, ...), so its thread and its login are the same session
    every time. A lead with no IG account (legacy rows, or a WA lead's day-7
    IG escalation sent by the shared login) stays on the ADR-13 profile.
    """
    from outreach.accounts import assigned_account, get_account

    account_id = assigned_account(db, lead_id)
    account = get_account(db, account_id) if account_id else None
    if account is not None and account["channel"] == "ig":
        return str(account["profile_dir"])
    return fleet.LEGACY_PROFILE


def _dismiss_overlay(page: Any) -> None:
    """Dismiss Instagram's blocking "Turn on Notifications" modal if it is up.

    Live incident 2026-10-29 09:48-15:13Z: the modal covers the whole Direct
    UI — the composer still classifies `ready` (it IS in the DOM) but the
    modal backdrop intercepts every click, so each dispatch died in a 30s
    TimeoutError and the engine cycle never reached any other lead.
    Evidence: `checkpoints/evidence/ig_probe_20261029T1445Z.png` (modal up,
    composer NOT the hit target) and `ig_probe_dismissed_20261029T1449Z.png`
    (after clicking "Not Now": composer clicked + focused, zero modals on
    revisit). The dismissal is best-effort by design: if the button vanishes
    while we aim, the goal is already reached; whatever remains is decided by
    the composer click that follows (fail-closed as always)."""
    dismiss = page.locator('button:has-text("Not Now")')
    if not dismiss.count():
        return  # no overlay — zero interaction with the thread UI
    try:
        dismiss.first.click(timeout=5000)
    except Exception:  # noqa: BLE001 — modal gone mid-click = dismissed
        return


def send_ig_message(
    db: "Database",
    lead_id: int,
    now: Any,
    *,
    page: Any,
    screenshot_dir: str | Path,
) -> SendOutcome:
    """Execute one Instagram dispatch end-to-end (guard → send → commit)."""
    _entry = _dispatch._session_clock()  # true elapsed -> the commit instant
    _assert_queue_live(db)
    assert_sendable_template(db)
    row = _ig_row(db, lead_id)
    if row["route_channel"] != "ig":
        raise ValueError(
            f"send_ig_message only handles channel 'ig', lead is "
            f"{row['route_channel']!r}"
        )
    # the ramp begins with real usage of the lane (ADR-8; first attempt stamps)
    ensure_ig_warmup_start(db, now)
    if not send_allowed(db, "ig", now):
        return SendOutcome("capped", None)  # gate #3: browser never opens

    _surface, state = _resolve_send_surface(page, row["instagram"])
    if state == "ban_signal":
        _halt_ig_channel(db, "detector: ADR-8 ban/login-challenge signal on "
                             "Instagram")
        return SendOutcome("ban_signal", None)
    if state == "ig_dead":
        transition(
            db,
            lead_id,
            "IG_DEAD",
            reason="detector: profile page unavailable on Instagram",
            actor="ig_sender",
        )
        return SendOutcome("ig_dead", None)
    if state == "undeliverable":
        # The thread exists but Instagram refuses the DM. Nothing is typed,
        # nothing is counted, no screenshot pretends it shipped; the lead
        # leaves the queue so no rung ever spends a slot on it again.
        transition(
            db,
            lead_id,
            "UNREACHABLE",
            reason=(
                "detector: Instagram refuses the message request"
                " (recipient only accepts messages from people they follow)"
            ),
            actor="ig_sender",
            park_kind="verdict",
        )
        return SendOutcome("undeliverable", None)
    if state != "ready":
        # login_wall → owner session needed (NEVER auto-login, ADR-8);
        # profile_no_message / thread_slow → NAMED (R9), fail-closed: no
        # interaction, nothing recorded, the lead stays ROUTED.
        return SendOutcome(state, None)

    if cold_dms_blocked(db) and _cold_thread(db, lead_id):
        # Owner ruling 2026-11-11: ig1 is follower-gated. While no delivered
        # bubble has ever been observed, no COLD first touch is typed - the
        # cap/slot is not spent and the lead stays ROUTED for later.
        #
        # R1 (owner ruling 2026-11-14: "probe"): three dish-bearing cold first
        # touches a day ARE the measurement that ends the deadlock - a Delivered
        # bubble can only come from a send. Without a slot the gate refuses as
        # before; with one, the send below is watched by _observe_delivery.
        from outreach.cold_probe import take_slot

        if not take_slot(db, lead_id, row, now):
            return SendOutcome("follower_gated", None)

    message = render_for_lead("ig", row)
    time.sleep(0)  # runner loop owns pacing gaps; tests pass instantly
    if _already_delivered(db, lead_id, page, message.text):
        # recovery: the copy is already delivered but uncommitted — land the
        # bookkeeping + ADR-15 proof WITHOUT typing it a second time
        shot = _take_screenshot(page, screenshot_dir, lead_id, "ig")
        commit_send(db, lead_id, _dispatch.commit_instant(now, _entry),
                    screenshot=str(shot), message=message)
        return SendOutcome("sent", shot)
    composer = page.locator(_IG_COMPOSER).first
    _dismiss_overlay(page)  # live 2026-10-29: modal backdrop intercepts clicks
    composer.click()
    _type_copy(composer, message.text)
    if not _wait_presence(page, message.text):
        return SendOutcome("unverified", None)  # no commit, no count, no shot

    _observe_delivery(db, page, now)  # the milestone, if IG says Delivered
    shot = _take_screenshot(page, screenshot_dir, lead_id, "ig")
    commit_send(db, lead_id, _dispatch.commit_instant(now, _entry),
                screenshot=str(shot), message=message)
    return SendOutcome("sent", shot)


def resend_ig_message(
    db: "Database",
    lead_id: int,
    *,
    now: Any,
    message: Any,
    page: Any,
    screenshot_dir: str | Path,
) -> SendOutcome:
    """Transport-only resend for the follow-up ladder (mirrors resend_email).

    Transports the ALREADY-RENDERED frozen copy byte-exact and performs NO
    commit, cap counting, warmup stamping or message_log work — the ladder
    owns that bookkeeping (it already asserted queue live, template and
    cap before calling; the warmup stamp happened on the first fresh send).
    Two safety writes are the documented exceptions to transport-only, both
    mandated by ADR-8/ADR-10: a ban or login-challenge signal HALTS the IG
    lane immediately, and an unavailable profile walks the lead to IG_DEAD
    so the ladder stops retrying it. Every other state fails closed.
    """
    row = _ig_row(db, lead_id)
    if row["route_channel"] not in ("ig", "wa"):
        raise ValueError(
            f"resend_ig_message only handles the IG lane (route 'ig') or the"
            f" day-7 WA escalation, lead is {row['route_channel']!r}"
        )
    if not (row["instagram"] or "").strip():
        raise ValueError(
            "resend_ig_message needs a usable instagram handle to DM"
        )
    if row["ig_class"] == "DEAD":
        raise ValueError(
            "resend_ig_message refuses a DEAD instagram link (no lane)"
        )
    _surface, state = _resolve_send_surface(page, row["instagram"])
    if state == "ban_signal":
        _halt_ig_channel(db, "detector: ADR-8 ban/login-challenge signal on "
                             "Instagram")
        return SendOutcome("ban_signal", None)
    if state == "ig_dead":
        transition(
            db,
            lead_id,
            "IG_DEAD",
            reason="detector: profile page unavailable on Instagram",
            actor="ig_sender",
        )
        return SendOutcome("ig_dead", None)
    if state == "undeliverable":
        # The thread exists but Instagram refuses the DM. Nothing is typed,
        # nothing is counted, no screenshot pretends it shipped; the lead
        # leaves the queue so no rung ever spends a slot on it again.
        transition(
            db,
            lead_id,
            "UNREACHABLE",
            reason=(
                "detector: Instagram refuses the message request"
                " (recipient only accepts messages from people they follow)"
            ),
            actor="ig_sender",
            park_kind="verdict",
        )
        return SendOutcome("undeliverable", None)
    if state != "ready":
        # login_wall → owner session needed (NEVER auto-login, ADR-8);
        # profile_no_message / thread_slow → named, fail-closed: nothing
        # typed, the ladder keeps its row pending for the next window.
        return SendOutcome(state, None)

    if _already_delivered(db, lead_id, page, message.text):
        # recovery WITHOUT typing (transport-only law untouched: no commit)
        return SendOutcome(
            "sent", _take_screenshot(page, screenshot_dir, lead_id, "ig")
        )
    composer = page.locator(_IG_COMPOSER).first
    _dismiss_overlay(page)  # live 2026-10-29: modal backdrop intercepts clicks
    composer.click()
    _type_copy(composer, message.text)
    if not _wait_presence(page, message.text):
        return SendOutcome("unverified", None)  # pending row stays for a retry
    return SendOutcome(
        "sent", _take_screenshot(page, screenshot_dir, lead_id, "ig")
    )


def send_ig_via_profile(
    db: "Database",
    lead_id: int,
    now: Any,
    *,
    screenshot_dir: str | Path | None = None,
) -> SendOutcome:
    """Engine adapter for `real_senders["ig"]`: owns the profile lifecycle.

    Matches the dispatch contract `senders[channel](db, lead_id, now)` —
    no page parameter — by opening the shared Outreach profile (ADR-13)
    for exactly this dispatch. The template gate is asserted BEFORE any
    browser boot so a parked queue never launches Chrome; the core
    re-asserts it after boot (the same defense-in-depth the claim loop
    applies for queue-live, caps and the ADR-8 warmup ramp).
    """
    assert_sendable_template(db)
    screenshot_dir = screenshot_dir or EVIDENCE_DIR
    profile = _ig_profile_for(db, lead_id)
    fleet.require_profile(profile)  # fail closed BEFORE a browser can boot
    with launch_outreach_profile(headless=True, profile_dir=profile) as page:
        return send_ig_message(
            db, lead_id, now, page=page, screenshot_dir=screenshot_dir
        )


def resend_ig_via_profile(
    db: "Database",
    lead_id: int,
    *,
    now: Any,
    message: Any,
    screenshot_dir: str | Path | None = None,
) -> SendOutcome:
    """Ladder adapter for `real_resenders["ig"]`: same profile lifecycle.

    `process_due_followups` calls `senders[channel](db, lead_id, now=now,
    message=message)` — this matches that shape exactly and delegates to
    the transport-only core (the ladder keeps all bookkeeping). Template
    gate asserted before any browser boot.
    """
    assert_sendable_template(db)
    screenshot_dir = screenshot_dir or EVIDENCE_DIR
    profile = _ig_profile_for(db, lead_id)
    fleet.require_profile(profile)  # fail closed BEFORE a browser can boot
    with launch_outreach_profile(headless=True, profile_dir=profile) as page:
        return resend_ig_message(
            db,
            lead_id,
            now=now,
            message=message,
            page=page,
            screenshot_dir=screenshot_dir,
        )
