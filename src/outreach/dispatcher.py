"""Dispatcher (B9): window/cap-aware claim loop + atomic send commit.

Split of responsibilities:
- `claim_next` PREPARES a send (queue live + template confirmed + window
  open + under cap + renderable). It writes NOTHING — the caller owns the
  browser mechanics (B10/11/12) and then confirms outcomes.
- `commit_send` lands the outcome atomically: daily_sends increment +
  message_log row + ROUTED→SENT transition in ONE transaction. Any refusal
  (paused queue, provisional template, wrong state, cap full, email gate)
  leaves the ledger and the state machine byte-identical — gate #3's law.

Pacing (ADR-7, owner supersession 2026-10-27 — HANDOFF §7) is a hard gate,
advisory no more:
- every commit arms a randomized 180–300 s `next_send_after` gap INSIDE its
  own transaction (owner law (f): a 3–5 minute break between ANY two sends),
- `claim_next` refuses ALL claims while that gap is live,
- a rolling 4-per-hour slot cap per platform (wa, ig) is enforced over
  `message_log.sent_at` (owner law (c): "we send 4 per hour").
`next_delay` still returns the randomized gap the gates are built from.
"""
from __future__ import annotations
from pathlib import Path

from contextvars import ContextVar
from functools import wraps

import json
import random
import time as _time
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import TYPE_CHECKING

from outreach.caps import (
    PKT,
    current_cap,
    ensure_ig_warmup_start,
    pkt_date,
    send_allowed,
)
from outreach.cleaning import junk_identity_reason
from outreach.followups import schedule_ladder_on_sent
from outreach.icp import QueuePaused
from outreach.machine import transition
from outreach.store import get_config, set_config
from outreach.variants import LintBlocked
from outreach.templates import (
    EmailPendingOwner,
    MessageHold,
    RenderedMessage,
    assert_sendable_template,
    render_for_lead,
)
from outreach.variants import render_lane, render_lane_opener

if TYPE_CHECKING:
    from outreach.store import Database

DEFAULT_PACING = {"wa": [180, 300], "ig": [180, 300], "email": [180, 300]}
SEND_GAP_KEY = "next_send_after"  # config row: ISO instant before the next claim
_SLOT_WINDOW_S = 3600  # rolling hour behind the per-slot figure
_SLOT_MAX_PER_HOUR = 4  # owner law (c): 4 sends per hour-slot per platform
_SLOT_CHANNELS = ("wa", "ig")  # email is priority-3 fallback, not slot-paced
# Owner-confirmed bands (2026-11-04, ruling Q-2026-11-05a): both regions
# are open EVERY day. The ADR-9 band this used to seed (Mon-Thu
# 03:30-09:30 / Mon-Fri 14:00-20:00) is rejected — a fresh install must
# boot on current owner law, never on a superseded schedule.
_DEFAULT_WINDOWS = {
    "na": {"days": [0, 1, 2, 3, 4, 5, 6], "start": "13:00", "end": "23:00"},
    "me": {"days": [0, 1, 2, 3, 4, 5, 6], "start": "14:00", "end": "20:00"},
}
_DEFAULT_EMAIL_SUBJECT = "I mocked up {{restaurant}}'s own ordering app"
_EXTENSION_KEY = "window_extension"  # config row: {region: until-ISO}
_EXTEND_MAX_MIN = 90  # owner decision 2026-10-31: recover an eaten session


#: Variant-engine ids whose body is assembled at send time (candidate for the
#: model's second opinion). The frozen file templates are not in here.
_GENERATED_VARIANTS = frozenset({"V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8", "V9"})


def email_enabled(db: "Database") -> bool:
    """ADR-5 hard gate: the email channel sends only when owner-armed."""
    return bool(get_config(db, "email_enabled"))


class IgTransportError(RuntimeError):
    """Instagram would not load either DM surface.

    2026-11-13 audit F3: a navigation timeout is a network fact, never an
    account verdict - so it must not become `IG_DEAD`. The dispatcher parks
    the lead in TRANSPORT_HOLD instead of re-claiming the same dead URL on
    the very next tick.
    """


@dataclass(frozen=True)
class PreparedSend:
    lead_id: int
    channel: str
    message: RenderedMessage


def ensure_dispatcher_config(db: "Database") -> None:
    """Seed caps, pacing bounds and ADR-9 session windows (idempotent).

    Seeded-once by design: the live `pacing` row is edited in place when the
    owner supersedes bounds (HANDOFF §7), so this never overwrites it.
    """
    from outreach.caps import ensure_caps

    ensure_caps(db)
    if get_config(db, "pacing") is None:
        set_config(db, "pacing", DEFAULT_PACING)
    if get_config(db, "session_windows") is None:
        set_config(db, "session_windows", _DEFAULT_WINDOWS)
    if get_config(db, "email_enabled") is None:
        set_config(db, "email_enabled", 0)  # ADR-5: owner arms the channel
    if get_config(db, "email_subject") is None:
        set_config(db, "email_subject", _DEFAULT_EMAIL_SUBJECT)
    if get_config(db, "owner_fill_enabled") is None:
        # Contamination hold 2026-11-03: fabricated owner names -> fills OFF
        # until the scraper ships evidence-gated owners (ADR-6: no guessing).
        set_config(db, "owner_fill_enabled", 0)
    from outreach.accounts import seed_fleet

    seed_fleet(db)  # v1.1 batch 0: the live profile becomes ig1/wa1 once


def _assert_queue_live(db: "Database") -> None:
    if get_config(db, "queue_status") == "PAUSED":
        raise QueuePaused("queue is PAUSED (fail-closed): no dispatch")


def _parse_until(raw: object) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _window_bounds(
    db: "Database", region: str | None, now: datetime
) -> tuple[datetime, datetime] | None:
    """(start, end) instants of this region's most recent CLOSED window,
    or None when no rule / none closed yet. Weekly rules repeat."""
    windows = get_config(db, "session_windows") or {}
    rule = (windows or {}).get(region or "")
    if not rule:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    pkt = now.astimezone(PKT)
    for offset in range(8):  # a week back; rules repeat weekly
        day = pkt.date() - timedelta(days=offset)
        if day.weekday() not in rule["days"]:
            continue
        sh, sm = (int(p) for p in rule["start"].split(":"))
        eh, em = (int(p) for p in rule["end"].split(":"))
        start = datetime.combine(day, time(sh, sm), tzinfo=PKT)
        end = datetime.combine(day, time(eh, em), tzinfo=PKT)
        if end <= pkt:
            return start, end  # most recent closed occurrence
    return None


def _sends_since(db: "Database", region: str | None, since: datetime) -> bool:
    row = db.conn.execute(
        "SELECT 1 FROM message_log ml JOIN leads l ON l.id = ml.lead_id"
        " WHERE l.region=? AND substr(ml.sent_at, 1, 19) >= ? LIMIT 1",
        (region, since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")),
    ).fetchone()
    return row is not None


def _queued_work(db: "Database", region: str | None) -> bool:
    row = db.conn.execute(
        "SELECT 1 FROM leads WHERE state='ROUTED' AND region=? LIMIT 1",
        (region,),
    ).fetchone()
    if row:
        return True
    row = db.conn.execute(
        "SELECT 1 FROM followups f JOIN leads l ON l.id = f.lead_id"
        " WHERE f.status='pending' AND l.region=? LIMIT 1",
        (region,),
    ).fetchone()
    return row is not None


def grant_window_extension(
    db: "Database", region: str | None, now: datetime
) -> datetime | None:
    """B27: recover a session an outage ate. Grants ONLY when the most
    recent window closed <=90 min ago, produced ZERO sends, and queued
    work is still waiting. Returns the recovery horizon, or None."""
    bounds = _window_bounds(db, region, now)
    if bounds is None:
        return None
    start, end = bounds
    now_utc = _as_utc(now)
    end_utc = _as_utc(end)
    past = (now_utc - end_utc).total_seconds()
    if past < 0 or past > _EXTEND_MAX_MIN * 60:
        return None  # still open, or past the hard ceiling
    if _sends_since(db, region, _as_utc(start)):
        return None  # not eaten: a send committed inside that window
    if not _queued_work(db, region):
        return None  # nothing to recover (never daily digest spam)
    until = end_utc + timedelta(minutes=_EXTEND_MAX_MIN)
    ext = get_config(db, _EXTENSION_KEY) or {}
    if until.isoformat() <= str(ext.get(region, "")):
        return None  # already granted at a later-or-equal horizon
    set_config(db, _EXTENSION_KEY, {**ext, region: until.isoformat()})
    with db.conn:
        db.conn.execute(
            "INSERT INTO digest(body) VALUES (?)",
            (
                f"WINDOW EXTENDED [{region}]: session eaten (no sends since"
                f" {start.strftime('%H:%M')} PKT, queued work present);"
                f" recovery open until"
                f" {until.astimezone(PKT).strftime('%H:%M')} PKT"
                f" (+{_EXTEND_MAX_MIN} min cap)",
            ),
        )
    return until


def reconcile_window_extensions(db: "Database", now: datetime) -> tuple[str, ...]:
    """Clear expired grants, then grant for any region whose most recent
    window was eaten and still sits inside the +90 min recovery zone.
    Idempotent: returns the regions newly granted by THIS pass."""
    ext = get_config(db, _EXTENSION_KEY) or {}
    now_utc = _as_utc(now)
    if ext:
        live = {
            region: raw
            for region, raw in ext.items()
            if (_until := _parse_until(raw)) is not None and _until >= now_utc
        }
        if live != ext:
            ext = live
            set_config(db, _EXTENSION_KEY, ext)
    granted: list[str] = []
    windows = get_config(db, "session_windows") or {}
    for region in sorted(windows):
        if region in ext or in_window(db, region, now):
            continue
        if grant_window_extension(db, region, now) is not None:
            granted.append(region)
    return tuple(granted)


def in_window(db: "Database", region: str | None, now: datetime) -> bool:
    """ADR-9 + B27: inside a session window (PKT), or inside its <=90 min
    recovery extension after an outage ate the session.

    A one-time band may carry `until` (ISO instant): past that instant the
    band is CLOSED no matter what its hours say (LIVE LESSON 2026-11-03 —
    an external one-shot teardown vanished and a one-day catch-up band
    leaked for four days; expiry must be enforced here, fail-closed).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    windows = get_config(db, "session_windows") or {}
    rule = (windows or {}).get(region or "")
    if not rule:
        return False
    until = _parse_until(rule.get("until"))
    if until is not None and _as_utc(now) > until:
        return False  # one-time band expired: closed by construction
    pkt = now.astimezone(PKT)
    if pkt.weekday() in rule["days"]:
        _h, _m = (int(part) for part in rule["start"].split(":"))
        eh, em = (int(part) for part in rule["end"].split(":"))
        minutes = pkt.hour * 60 + pkt.minute
        if _h * 60 + _m <= minutes < eh * 60 + em:
            return True
    ext = get_config(db, _EXTENSION_KEY) or {}
    until = _parse_until((ext or {}).get(region or ""))
    return until is not None and now <= until


def next_delay(db: "Database", channel: str, rng: random.Random) -> float:
    """Randomized human gap in seconds, within the channel's pacing bounds."""
    pacing = get_config(db, "pacing")
    if pacing is None:
        raise RuntimeError("pacing not seeded (run ensure_dispatcher_config)")
    if channel not in pacing:
        raise ValueError(f"no pacing bounds for channel {channel!r}")
    low, high = (float(b) for b in pacing[channel])
    return rng.uniform(low, high)


def _as_utc(now: datetime) -> datetime:
    """Naive instants are UTC (the repo-wide convention, see in_window)."""
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def arm_send_gap(
    db: "Database",
    channel: str,
    now: datetime,
    *,
    rng: random.Random | None = None,
    atomic: bool = True,
) -> float:
    """Arm `next_send_after = now + uniform(pacing bounds)` and return it.

    `atomic=False` writes inside the CALLER's open transaction (commit_send's
    atomic block) — an inner `with db.conn` would land the ledger early.
    """
    gap = next_delay(db, channel, rng or random.Random())
    when = (_as_utc(now) + timedelta(seconds=gap)).isoformat()
    set_config(db, SEND_GAP_KEY, when, atomic=atomic)
    return gap


_session_clock = _time.monotonic  # module seam: tests patch it

# The tick's own monotonic start, set by `tick_anchor` for the length of one
# engine pass (None outside a tick, e.g. a sender called directly by a test).
_tick_anchor: ContextVar[float | None] = ContextVar(
    "outreach_tick_anchor", default=None
)


def tick_anchor(fn):
    """Anchor `commit_instant` at the tick's own start for one pass.

    LIVE LESSON 2026-11-04: the 11-02 and 11-03 fixes anchored the break at
    the SESSION entry, but the base they added the elapsed to was still the
    tick's `now`. Every second the tick spends in the reply probes before the
    session was therefore dropped from the break; two IG rows landed 175.1 s
    apart live (11:25:47.645 -> 11:28:42.708) under the 180 s floor. The tick
    now stamps its own monotonic start, so base + elapsed is the true commit
    instant no matter how long the probes ran.
    """

    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _tick_anchor.set(_session_clock())
        try:
            return fn(*args, **kwargs)
        finally:
            _tick_anchor.reset(token)

    return wrapper


def commit_instant(now: datetime, started: float | None = None) -> datetime:
    """The instant the send gap arms from (owner law (f)).

    LIVE LESSON 2026-11-02: commit sites used to pass the tick's captured
    `now`, stale by the whole send session (~1-2 min live: browser boot,
    typing, delivery proof), so the 180-300 s break counted from BEFORE the
    send and two emails landed 113 s apart (< the 180 s floor). Senders stamp
    `started = _session_clock()` at session entry and hand the stamp in: the
    arming instant = the caller's now + the real elapsed since the base.

    The base is the TICK's start when the caller runs inside `tick_anchor`
    (the live engine), so the probe time before the session counts too; the
    explicit `started` stamp is the fallback outside a tick. In tests the
    elapsed is ~0 ms, so the caller's now survives as-is.
    """
    base = _tick_anchor.get()
    if base is None:
        base = started
    if base is None:
        return _as_utc(now)
    return _as_utc(now) + timedelta(seconds=max(0.0, _session_clock() - base))


def send_gap_remaining(db: "Database", now: datetime) -> float:
    """Seconds left of the global 180–300 s send gap (0.0 = free to claim)."""
    raw = get_config(db, SEND_GAP_KEY)
    if not raw:
        return 0.0
    when = datetime.fromisoformat(str(raw))
    return max(0.0, (_as_utc(when) - _as_utc(now)).total_seconds())


def sends_in_last_hour(db: "Database", channel: str, now: datetime) -> int:
    """Rolling slot figure: how many sends this channel logged in the last
    60 minutes (owner law (c)). `sent_at` is a fixed UTC ISO stamp, so the
    19-char prefix compares lexicographically."""
    since = (_as_utc(now) - timedelta(seconds=_SLOT_WINDOW_S)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    row = db.conn.execute(
        "SELECT COUNT(*) FROM message_log WHERE channel=?"
        " AND substr(sent_at, 1, 19) >= ?",
        (channel, since),
    ).fetchone()
    return int(row[0])


def slot_free_in(db: "Database", channel: str, now: datetime) -> float:
    """Seconds until this channel's rolling 4/hour slot admits another send
    (0.0 = free). The count drops below 4 when the (n-3)-th oldest stamp
    leaves the window."""
    since = (_as_utc(now) - timedelta(seconds=_SLOT_WINDOW_S)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    stamps = [
        row[0]
        for row in db.conn.execute(
            "SELECT substr(sent_at, 1, 19) FROM message_log"
            " WHERE channel=? AND substr(sent_at, 1, 19) >= ? ORDER BY sent_at",
            (channel, since),
        ).fetchall()
    ]
    if len(stamps) < _SLOT_MAX_PER_HOUR:
        return 0.0
    index = max(0, len(stamps) - _SLOT_MAX_PER_HOUR)
    pivot = datetime.strptime(stamps[index], "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    free_at = pivot + timedelta(seconds=_SLOT_WINDOW_S)
    return max(0.0, (free_at - _as_utc(now)).total_seconds())


def _routed_row(db: "Database", lead_id: int) -> dict:
    """The commit row: ROUTED assert AND every column the render reads.

    LIVE 2026-11-09: this selected a hand-written six-column list, but
    `commit_send` re-renders the lane (`render_lane`) to freeze the sent copy -
    and the renderer reads `city`. A `sqlite3.Row` raises
    `IndexError: No item with that key`, so the FIRST TOUCH typed the message
    into the thread and then crashed before committing: no message_log row, no
    cap, no screenshot, and the lead stayed ROUTED to be typed again.
    """
    from outreach.variants import render_select

    row = db.conn.execute(render_select(), (lead_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown lead id={lead_id}")
    if row["state"] != "ROUTED":
        raise ValueError(
            f"lead id={lead_id} must be in state ROUTED, is {row['state']!r}"
        )
    return row


def stamp_account_health(db: "Database", lead_id: int, now: datetime) -> str:
    """Record the account that just sent, on the caller's transaction.

    Both send paths land a `message_log` row - the dispatch claim and the
    follow-up ladder - and both must move the account's health clock. The
    ladder used to write its row directly (2026-11-13: two live rungs sent and
    `accounts.last_ok_at` stayed NULL, which is how this gap was found).

    Returns the account id ("" when the lead has no assignment). Never opens
    its own transaction: a health stamp that commits separately from the send
    it describes would be a second source of truth.
    """
    from outreach.accounts import assigned_account

    account_id = assigned_account(db, lead_id) or ""
    if account_id:
        db.conn.execute(
            "UPDATE accounts SET last_ok_at=? WHERE id=?",
            (utc_stamp(now), account_id),
        )
    return account_id


def commit_send(
    db: "Database",
    lead_id: int,
    now: datetime,
    screenshot: str | None = None,
    message: "RenderedMessage | None" = None,
) -> PreparedSend:
    """Atomically land a completed send (caps + log + SENT, or nothing).

    `screenshot` is the ADR-15 evidence path from the sender (B10/11/12).

    `message` is the copy the TRANSPORT actually typed. It must be passed by
    every sender: LIVE 2026-11-09 the commit re-rendered the lane with a fresh
    variant pick, so `message_log.body` (and every ladder rung that resends the
    frozen copy) held a DIFFERENT message than the one in the thread. The log
    is the record of what a restaurant received - it can never be a second
    render. Re-rendering stays only as the fallback for callers that commit
    without a transport (tests, recovery).
    """
    _assert_queue_live(db)
    assert_sendable_template(db)
    row = _routed_row(db, lead_id)
    channel = row["route_channel"]
    if not channel:
        raise ValueError(f"lead id={lead_id} has no route_channel mapped")
    if channel == "email":
        if not email_enabled(db):
            raise EmailPendingOwner(
                "email sender is disabled until the owner arms it "
                "(ADR-5, .env creds + EMAIL_ENABLED); lead stays ROUTED to "
                "drain later"
            )
    day = pkt_date(now)
    cap = current_cap(db, channel, now)
    cap_row = db.conn.execute(
        "SELECT count FROM daily_sends WHERE channel=? AND date=?", (channel, day)
    ).fetchone()
    used = cap_row[0] if cap_row else 0
    if used >= cap:
        from outreach.caps import CapReached

        raise CapReached(f"{channel} daily cap {cap} reached for PKT {day}")

    if message is None:
        message = render_lane(db, channel, row, now=now)
    from outreach.accounts import assigned_account

    account_id = assigned_account(db, lead_id) or ""
    with db.conn:  # ATOMIC: ledger + log + state machine + send gap land together
        db.conn.execute(
            "INSERT INTO daily_sends(channel, date, count) VALUES (?, ?, 1) "
            "ON CONFLICT(channel, date) DO UPDATE SET count=count+1",
            (channel, day),
        )
        db.conn.execute(
            "INSERT INTO message_log(lead_id, channel, template_hash, dish,"
            " screenshot, account_id, body, sent_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (lead_id, channel, message.template_hash, row["extracted_dish"],
             screenshot, account_id, message.text, utc_stamp(now)),
        )
        arm_send_gap(db, channel, now, atomic=False)  # owner law (f)
        transition(
            db,
            lead_id,
            "SENT",
            reason=f"{channel} send committed (daily {used + 1}/{cap} on {day})",
            actor="dispatcher",
            commit=False,
        )
        # 2026-11-13 (audit F4): the per-account health clock was never
        # written, so every account looked eternally unproven. One write,
        # inside the same transaction: a send either happened with its account
        # stamp or did not happen at all.
        stamp_account_health(db, lead_id, now)
    schedule_ladder_on_sent(db, lead_id, now)  # B14: arm day 3, once
    return PreparedSend(lead_id=lead_id, channel=channel, message=message)


def utc_stamp(moment: datetime) -> str:
    """The one send timestamp format, written from the caller's clock.

    SQLite's `strftime('now')` default would stamp the wall clock instead of
    the tick's `now`, and every timestamped law (rolling 4/hour slot, PKT day
    cap, send gap) reads `sent_at`. One clock, one format.
    """
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _weak_lead(data_json: "str | None") -> bool:
    """B29 deprioritisation predicate (owner research 2026-10-31):
    owner_name unknown AND weak rating evidence (rating < 4.3 OR reviews
    < 80). Missing figures count as weak — no evidence is weak evidence.
    Deprioritised means claimed later, NEVER excluded."""
    try:
        payload = json.loads(data_json or "{}") or {}
    except (ValueError, TypeError):  # corrupt bytes/shape: treat as empty
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if str(payload.get("owner_name") or "").strip():
        return False  # owner known: priority regardless of the figures
    try:
        rating = float(payload["rating"]) if payload.get("rating") is not None else None
    except (TypeError, ValueError):
        rating = None
    try:
        reviews = int(payload["review_count"]) if payload.get("review_count") is not None else None
    except (TypeError, ValueError):
        reviews = None
    return rating is None or rating < 4.3 or reviews is None or reviews < 80


def claim_priority(row: Any) -> tuple[int, int]:
    """THE claim order: dish-first (owner 2026-11-07), then B29 strong
    evidence, then lead id.

    Shared with `outreach.forecast` so the forward contract cannot drift from
    what this loop actually claims. Deprioritised means claimed later, never
    excluded.
    """
    dish = str(row["extracted_dish"] or "").strip()
    return (0 if dish else 1, 1 if _weak_lead(row["data_json"]) else 0)


def claim_next(
    db: "Database",
    now: datetime,
    *,
    skip: frozenset[int] | set[int] = frozenset(),
    root: str | Path | None = None,
) -> PreparedSend | None:
    """Prepare the next dispatchable ROUTED lead, or None when nothing fits.

    Prepare-only: the lead stays ROUTED until `commit_send` lands outcome.
    Email-routed leads are never claimed while the ADR-5 gate is on.
    """
    _assert_queue_live(db)
    assert_sendable_template(db)
    if send_gap_remaining(db, now) > 0:
        return None  # owner law (f): the 180–300 s break is still running
    rows = db.conn.execute(
        "SELECT id, name, region, route_channel, email, data_json,"
        " extracted_dish FROM leads WHERE state='ROUTED' ORDER BY id"
    ).fetchall()
    # B29: weak-evidence/unknown-owner leads claim after the strong ones.
    # Stable sort — id order is preserved inside each group, nothing drops.
    # Owner order 2026-11-07: dish-first. A lead whose dish we mined from real
    # review/IG evidence claims before the no-dish pool, because the dish is
    # the only personalized thing in the copy; weak-evidence ordering still
    # applies INSIDE each group (stable sort, nothing drops).
    rows = sorted(rows, key=claim_priority)
    for row in rows:
        lead_id = int(row["id"])
        if lead_id in skip:
            continue
        from outreach.targeting import disqualify_reason, park, targeting_armed

        if targeting_armed(db):
            # O6 (2026-11-14): fewer, better sends. A parked lead leaves the
            # claim set (DEPRIORITIZED is not ROUTED) with one visible row, so
            # the loss of volume reads as a decision, not a shortage.
            rule = disqualify_reason(db, row)
            if rule is not None:
                park(db, lead_id, rule, now)
                continue

        from outreach.clock import clock_armed, clock_reason, record_clock_park

        if clock_armed(db):
            # O5 (2026-11-14): send in THEIR clock. A closed venue is parked with
            # one visible row instead of being messaged into a dark kitchen; an
            # unknown hour map changes nothing, so the lane keeps today's pace.
            reason = clock_reason(row, now)
            if reason is not None:
                record_clock_park(db, lead_id, reason, now)
                continue

        if junk_identity_reason(row["name"]) is not None:
            # 2026-11-04: fail closed at the send boundary too. Intake and the
            # judge already refuse these; a row that predates both never sends.
            continue
        channel = row["route_channel"]
        if channel == "email":
            if not email_enabled(db):
                continue  # ADR-5: email drains only after the owner arms it
            from outreach.router import email_lane_ok

            try:
                _payload = json.loads(row["data_json"] or "{}") or {}
            except (ValueError, TypeError):
                _payload = {}
            if not email_lane_ok(
                row["email"],
                (_payload or {}).get("website"),
                (_payload or {}).get("email_verified"),
            ):
                # D4 + contamination hold 2026-11-03: platform inboxes, free
                # mailboxes and unrelated domains are never claimed (the
                # engine-boot reroute re-lanes/parks stale rows the same way)
                continue
        if channel == "wa" and get_config(db, "wa_paused"):
            continue  # owner directive 2026-11-02: NO WhatsApp usage until
            # the owner re-arms it (config switch, never unconditional —
            # an absent/unset key keeps the lane live)
        if channel == "ig" and get_config(db, "ig_halted"):
            continue  # ADR-8: the IG lane is halted — drain owns these leads
        if channel == "ig":
            # imported lazily: warmup -> engagement -> ig_sender imports this
            # module, so a top-level import would be a cycle.
            from outreach.warmup import (
                comment_state,
                record_comment_park,
                warmup_enabled,
                warmup_gate_enabled,
                warmup_pending,
            )

            if warmup_gate_enabled(db):
                # O2 + O3 (2026-11-13/14), the owner's hard gate: NO COMMENT,
                # NO SEND in a 12-48 h window. `comment_state` is the single
                # truth: none/waiting/stale all park THIS lead, ready claims.
                # The law arms with the comment lane (both use
                # `engage_enabled`), so an armed lane can always satisfy it.
                state = comment_state(db, lead_id, now)
                if state != "ready":
                    record_comment_park(db, lead_id, state, now)
                    continue

            if warmup_enabled(db) and warmup_pending(db, lead_id, now):
                # v1.1 batch 7: a warmed lead's first DM waits out the 30 min
                # between the warm-up and the touch. With the flag off (the
                # seeded default) this branch never runs and the claim is
                # byte-identical to the pre-batch-7 path.
                continue
        if not in_window(db, row["region"], now):
            continue
        if channel not in ("wa", "ig", "email"):
            continue
        if channel in _SLOT_CHANNELS and (
            sends_in_last_hour(db, channel, now) >= _SLOT_MAX_PER_HOUR
        ):
            continue  # owner law (c): 4 per hour-slot per platform (rolling)
        if channel == "ig":
            # ADR-8 bootstrap: the ramp cap is 0 until the start is stamped,
            # and stamping ONLY inside send_ig_message would deadlock — cap 0
            # blocks this claim, and a claim is the only path to that stamp.
            # Reaching here means a real, in-window IG dispatch attempt, so
            # the ramp legitimately begins now (idempotent: once per lane).
            ensure_ig_warmup_start(db, now)
        if not send_allowed(db, channel, now):
            continue
        if channel in ("wa", "ig"):
            # v1.1 batches 2/3: phase 1 of the two-phase claim — which account
            # may send to this lead now (L1 assignment + L2 per-account ramp).
            # Non-browser channels (email) have no fleet.
            from outreach.accounts import NoAccountAvailable, pick_account

            try:
                pick_account(
                    db, channel, lead_id, now, region=row["region"], root=root
                )
            except NoAccountAvailable:
                continue  # every account of this channel is full or halted
        lead = db.conn.execute(
            "SELECT id, name, city, region, route_channel, extracted_dish,"
            " data_json FROM leads WHERE id=?",
            (lead_id,),
        ).fetchone()
        from outreach.proofread import has_open_hold

        if has_open_hold(db, lead_id):
            continue  # vetoed earlier and not yet cleared by the owner
        # Forward-contract check (owner order 2026-11-11): the tier at claim
        # time must be at least the tier the forecast promised for today. A
        # lower one means the forecast counted evidence the lead does not have,
        # and it lands in the digest as "forecast lied" (counting-bug class).
        from outreach.forecast import lead_tier_of, record_divergence

        record_divergence(db, lead_id, lead_tier_of(lead), now)
        try:
            message = render_lane(db, channel, lead, now=now)
        except MessageHold:
            continue  # no owner-blessed copy for this lead's state (Q7 hold)
        except LintBlocked as exc:
            # 2026-11-13 (audit F2): a single poisoned render aborted the WHOLE
            # engine cycle 17 times in two days, because claim order is stable
            # and the same lead came back every tick. A lint refusal is a
            # fact about ONE lead: park it in COPY_HOLD, say so once, keep
            # serving the queue.
            from outreach.machine import transition

            transition(
                db,
                lead_id,
                "COPY_HOLD",
                reason=f"copy lint: {exc}",
                actor="dispatcher",
            )
            continue
        # THE WALL (owner order 2026-11-07): the last thing that sees a render
        # before a browser does. It vetoes or passes; it can never edit, and a
        # veto leaves the lead exactly where it is for a retry after review.
        from outreach.proofread import default_judge, screen

        verdict = screen(
            db,
            message.text,
            lead_id=lead_id,
            variant=getattr(message, "variant", None) or channel,
            dish=(lead["extracted_dish"] or "").strip() or None,
            now=now,
            # Layer 2 only for GENERATED copy (variant engine): pinned owner
            # templates are judged by layer 1 alone, so model taste can never
            # retire a sentence the owner wrote. Fail-open when Ollama is down.
            judge=(
                default_judge
                if getattr(message, "variant", "") in _GENERATED_VARIANTS
                else None
            ),
        )
        if not verdict.ok:
            continue  # held: reason in proofread_log + digest, lead untouched
        return PreparedSend(lead_id=lead_id, channel=channel, message=message)
    return None
