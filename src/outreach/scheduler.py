"""Session scheduler (B15): ADR-9 windows as config rows, PKT, reboot-safe.

Nothing here lives in memory between ticks: the wake time derives
entirely from the DB (queued routes, armed/expired follow-ups) and the
`session_windows` config row. A restarted process turns the same crank.

`run_engine_tick` is the run loop's one body:
  1. polls the mailbox for replies (reply-priority: a reply registers
     before the ladder or dispatch can touch the lead),
  2. runs the follow-up ladder through transport-only *resenders*,
  3. dispatches fresh sends through end-to-end *senders* until the
     window/caps/templates/the queue stop it — report, never crash.

(B24 / ADR-10 amendment: the NO_REPLY expiry pass is gone — the ladder
runs forever; the tick also writes the owner final-review file when
asked and runs the IG in-thread reply probe alongside the WA one.)
"""
from __future__ import annotations

import time as _time
from datetime import datetime, time, timedelta, timezone
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from outreach.caps import PKT
from outreach.dispatcher import IgTransportError, tick_anchor
from outreach.followups import email_rung_allowed, process_due_followups
from outreach.replies import (
    MailboxPendingOwner,
    ReplyPollOutcome,
    poll_email_replies,
)
from outreach.store import get_config, set_config
from outreach.templates import EmailPendingOwner, TemplateUnconfirmed

if TYPE_CHECKING:
    from outreach.store import Database

_MAX_DISPATCH_TICK = 500  # defensive bound; caps terminate well before
_MAX_PAUSE_S = 300.0  # pacing park is clamped: poll replies/ladder every ≤5 min
_STOP_REPEAT_S = 1800.0  # one SEND BLOCKED row per reason per 30 min
_BACKOFF_S = 300.0  # bounded stop: re-check at most every 5 min (B23)
_BACKOFF_KEY = "dispatch_backoff_until"


def _backoff_remaining(db: "Database", now: datetime) -> float:
    """Seconds left on the bounded-stop park, 0 when none/expired/stale.

    Set by `run_engine_tick` whenever dispatch stops for a reason that
    cannot change by itself (login_wall, caps, owner gates): without it
    the engine re-cycles every ~20 s booting a full browser per attempt
    (live 2026-10-28: heartbeat stamps 20 s apart behind a throttled row).
    """
    raw = get_config(db, _BACKOFF_KEY) or ""
    if not raw:
        return 0.0
    try:
        until = datetime.fromisoformat(raw)
    except ValueError:
        return 0.0
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    remaining = (until - now).total_seconds()
    if remaining <= 0:
        return 0.0
    return min(remaining, _BACKOFF_S)


def _record_send_stop(db: "Database", reason: str, now: datetime) -> None:
    """Persist WHY dispatch stopped before typing (live incident
    2026-10-28: WhatsApp's login_wall fail-closed correctly but wrote
    NOTHING — the engine ran blind through an entire send window and the
    owner saw zero sends and zero errors). Throttled per reason so a
    persistent wall surfaces once per 30 min instead of burying the
    digest row every tick."""
    prefix = f"SEND BLOCKED [{reason}]"
    since = (now - timedelta(seconds=_STOP_REPEAT_S)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    existing = db.conn.execute(
        "SELECT 1 FROM digest WHERE substr(body, 1, ?) = ?"
        " AND substr(created_at, 1, 19) >= ? LIMIT 1",
        (len(prefix), prefix, since),
    ).fetchone()
    if existing:
        return
    with db.conn:
        db.conn.execute(
            "INSERT INTO digest(body) VALUES (?)",
            (
                f"{prefix}: dispatch stopped before typing; fail-closed, "
                "nothing sent, lead stays ROUTED (retries next tick)",
            ),
        )


def _record_sender_error(
    db: "Database", channel: str, lead_id: int, exc: Exception, now: datetime
) -> None:
    """Durable, throttled record of a sender CRASH (a raise out of the send
    path — unlike the clean fail-closed stops `_record_send_stop` covers).

    Live incident 2026-10-29: a composer click TimeoutError surfaced only in
    a 30-min-throttled `ENGINE ERROR` row with NO channel/lead context, which
    cost hours of diagnosis. One row per lane per 30 min carries the lane,
    the lead id and the repr."""
    prefix = f"SENDER ERROR [{channel}]"
    since = (now - timedelta(seconds=_STOP_REPEAT_S)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    existing = db.conn.execute(
        "SELECT 1 FROM digest WHERE substr(body, 1, ?) = ?"
        " AND substr(created_at, 1, 19) >= ? LIMIT 1",
        (len(prefix), prefix, since),
    ).fetchone()
    if existing:
        return
    where = _innermost_frame(exc)
    with db.conn:
        db.conn.execute(
            "INSERT INTO digest(body) VALUES (?)",
            (f"{prefix} lead {lead_id}: {repr(exc)[:240]}"
             + (f" at {where}" if where else ""),),
        )


def _record_send_skip(
    db: "Database", reason: str, lead_id: int, now: datetime
) -> None:
    """One row per lead per 30 min when a send is refused for a PER-LEAD fact.

    2026-11-13 (R9): `profile_no_message` is a fact about one profile (no
    Message affordance we can name), not about the lane. Recorded under its
    own prefix so the owner can count it, throttled per LEAD so a queue full
    of them cannot bury the digest — and the dispatch loop keeps claiming.
    """
    prefix = f"SEND SKIPPED [{reason}] lead {lead_id}"
    since = (now - timedelta(seconds=_STOP_REPEAT_S)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    existing = db.conn.execute(
        "SELECT 1 FROM digest WHERE substr(body, 1, ?) = ?"
        " AND substr(created_at, 1, 19) >= ? LIMIT 1",
        (len(prefix), prefix, since),
    ).fetchone()
    if existing:
        return
    with db.conn:
        db.conn.execute(
            "INSERT INTO digest(body) VALUES (?)",
            (
                f"{prefix}: no Message affordance on the profile — nothing"
                " typed, lead stays ROUTED; the next lead was claimed",
            ),
        )


def _innermost_frame(exc: Exception) -> str:
    """`file.py:123 in function` of the frame that actually raised.

    LIVE 2026-11-09: a bare `IndexError('No item with that key')` names neither
    the module nor the line, so finding the sender crash meant grepping every
    row access. The digest now carries the throw site.
    """
    tb = exc.__traceback__
    last = None
    while tb is not None:
        last = tb
        tb = tb.tb_next
    if last is None:
        return ""
    frame = last.tb_frame
    code = frame.f_code
    name = code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
    return f"{name}:{frame.f_lineno} in {code.co_name}"


_PROBE_ERROR_REPEAT_S = 1800.0  # one probe-error row per lane per 30 min

# --- stage clocks (R2, 2026-11-13) -------------------------------------------
#
# Every stage of the tick names itself as it starts and again with its measured
# duration when it ends, so the heartbeat can say WHERE a cycle is instead of
# only that one happened (the 2026-11-13 late-heartbeat review). The budgets
# below are the alarm bounds: a stage may legitimately take this long, and an
# overrun is recorded once per stage per 30 min. The refresh pass — the one that
# actually took ~15 min — is HARD-stopped by its own budget in `intake`.
_STAGE_BUDGET_S: dict[str, float] = {
    "boot": 120.0,
    "replies": 240.0,
    "ladder": 300.0,
    "engage": 300.0,  # O1: one act per account, one browser, hard cap
    "dispatch": 600.0,
    "review": 60.0,
}
_STAGE_OVERRUN_REPEAT_S = 1800.0


def _record_stage_overrun(
    db: "Database", stage: str, seconds: float, budget: float, now: datetime
) -> None:
    """One `STAGE OVERRUN` row per stage per 30 min.

    A budget with no record is a wish: this is the row that turns "the cycle
    felt slow" into a named stage and a number the owner can act on.
    """
    prefix = f"STAGE OVERRUN [{stage}]"
    since = (now - timedelta(seconds=_STAGE_OVERRUN_REPEAT_S)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    existing = db.conn.execute(
        "SELECT 1 FROM digest WHERE substr(body, 1, ?) = ?"
        " AND substr(created_at, 1, 19) >= ? LIMIT 1",
        (len(prefix), prefix, since),
    ).fetchone()
    if existing:
        return
    with db.conn:
        db.conn.execute(
            "INSERT INTO digest(body) VALUES (?)",
            (f"{prefix} {seconds:.1f}s > {budget:.1f}s budget",),
        )


def _record_probe_error(
    db: "Database",
    channel: str,
    exc: Exception,
    now: datetime,
    *,
    label: str | None = None,
) -> None:
    """Durable, throttled record of a reply-probe fault.

    LIVE LESSONS 2026-11-02: a dead DNS made every tick's probe fail and
    each failure wrote an unthrottled digest row — the toast drain then
    popped one balloon per row and flooded the owner's desktop. Same
    throttle law as SEND BLOCKED / SENDER ERROR: one row per lane per
    30 min, the digest row stays the durable record."""
    prefix = label or f"{channel.upper()} REPLY PROBE ERROR"
    since = (now - timedelta(seconds=_PROBE_ERROR_REPEAT_S)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    existing = db.conn.execute(
        "SELECT 1 FROM digest WHERE substr(body, 1, ?) = ?"
        " AND substr(created_at, 1, 19) >= ? LIMIT 1",
        (len(prefix), prefix, since),
    ).fetchone()
    if existing:
        return
    with db.conn:
        db.conn.execute(
            "INSERT INTO digest(body) VALUES (?)",
            (f"{prefix}: {repr(exc)[:300]}",),
        )


@dataclass(frozen=True)
class TickOutcome:
    replies: ReplyPollOutcome | None
    followups_processed: tuple[int, ...]
    dispatched: tuple[tuple[int, str], ...]
    stopped_by: str
    wa_replied: tuple[int, ...] = ()  # B13 leg 2: WA in-thread registrations
    ig_replied: tuple[int, ...] = ()  # B24: IG in-thread registrations


# --- wake math ---------------------------------------------------------------

def next_window_start(
    db: "Database", region: str | None, now: datetime
) -> datetime | None:
    """Next PKT instant this region's ADR-9 window OPENS (None: no rule)."""
    windows = get_config(db, "session_windows") or {}
    rule = (windows or {}).get(region or "")
    if not rule:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)  # in_window's convention
    pkt = now.astimezone(PKT)
    for offset in range(8):  # a week of days; rules repeat weekly
        day = pkt.date() + timedelta(days=offset)
        if day.weekday() not in rule["days"]:
            continue
        start_h, start_m = (int(p) for p in rule["start"].split(":"))
        candidate = datetime.combine(
            day, time(hour=start_h, minute=start_m), tzinfo=PKT
        )
        if candidate > pkt:
            return candidate
    return None


def _slot_hold(db: "Database", now: datetime) -> float:
    """Owner law (c): seconds until a queued platform lane's rolling 4/hour
    slot admits another send (0.0 when no slot-paced lane is queued)."""
    from outreach.dispatcher import slot_free_in

    rows = db.conn.execute(
        "SELECT DISTINCT route_channel FROM leads WHERE state='ROUTED'"
        " AND route_channel IN ('wa', 'ig')"
    ).fetchall()
    return max((slot_free_in(db, row[0], now) for row in rows), default=0.0)


def _fleet_cap_left(db: "Database", channels: set[str], now: datetime) -> bool:
    """Does any account of these lanes still have DM cap to give today?

    R11 (2026-11-13): the wake math has to tell three different "no claim"
    states apart — an empty queue (sleep), a fleet that is SPENT for the day
    (keep the probe cadence alive), and a lane that can never act today
    (warming-only or halted accounts: nothing to wait for).
    """
    from outreach.accounts import cap_for, eligible_accounts

    for channel in sorted(channels):
        for account in eligible_accounts(db, channel):
            if cap_for(db, account, now) > 0:
                return True
    return False


def seconds_to_next_wake(db: "Database", now: datetime) -> float | None:
    """Seconds until the engine has *work* a window can act on.    None = nothing queued at all. 0.0 = wake immediately (open window).

    A window that is OPEN over queued work whose fleet is spent (every account
    at its daily cap) parks at the probe cadence, not at None: the reply probes
    run inside the cycle, so the one-hour idle sleep used to blind reply
    detection for the rest of a capped day (live 2026-11-13, R11). A CLOSED
    window still sleeps to its own open, so a capped night costs no probes.
    When the send lane is PACED (180–300 s gap after
    the last commit, owner law (f)) or its rolling 4/hour slot is full (owner
    law (c)), the wake is the gap/slot expiry clamped to 300 s so reply
    polling and the ladder still run at least every five minutes — never a
    hot spin on a claim that cannot succeed.

    Reboot-safe: pure DB + config math; windows config is (re-)seeded
    idempotently so a cold process wakes correctly.
    """
    from outreach.dispatcher import (
        ensure_dispatcher_config,
        in_window,
        reconcile_window_extensions,
        send_gap_remaining,
    )

    ensure_dispatcher_config(db)
    reconcile_window_extensions(db, now)  # B27: recovery zone counts as open

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)  # in_window's convention

    wa_paused = bool(get_config(db, "wa_paused"))
    ig_halted = bool(get_config(db, "ig_halted"))

    def _lane_asleep(channel: object) -> bool:
        """Live 2026-11-04: the wake math ignored the pause gates, so 31 due
        WA rungs (wa_paused) counted as actionable and pinned the engine to
        wake=0 for ten hours — a reply-probe storm every 12 s and a burned
        core. A lane the dispatch/ladder gates refuse is not work."""
        return (channel == "wa" and wa_paused) or (
            channel == "ig" and ig_halted
        )

    due_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    work: set[str] = set()
    actionable: set[str] = set()  # regions with work a gate would ACTUALLY fire
    lane_work: dict[str, set[str]] = {}  # region -> lanes holding that work
    for row in db.conn.execute(
        "SELECT DISTINCT region, route_channel FROM leads WHERE state='ROUTED'"
        " AND region IS NOT NULL"
    ):
        if _lane_asleep(row["route_channel"]):
            continue
        region = str(row["region"])
        work.add(region)
        actionable.add(region)
        lane_work.setdefault(region, set()).add(str(row["route_channel"] or ""))
    for row in db.conn.execute(
        "SELECT f.due_at, f.channel AS fchannel, l.region, l.route_channel,"
        " l.email, l.data_json"
        " FROM followups f JOIN leads l ON l.id = f.lead_id"
        " WHERE f.status='pending' AND l.region IS NOT NULL"
    ):
        if _lane_asleep(row["fchannel"] or row["route_channel"]):
            continue
        region = str(row["region"])
        work.add(region)
        lane_work.setdefault(region, set()).add(
            str(row["fchannel"] or row["route_channel"] or "")
        )
        if (row["fchannel"] or row["route_channel"]) == "email" and not (
            email_rung_allowed(db, row)
        ):
            # the ladder will refuse it (quarantined address / disarmed lane),
            # so it is NOT work a wake may be pinned to (2026-11-04 spin class)
            continue
        if str(row["due_at"]) <= due_iso:
            actionable.add(region)
    if not work:
        return None
    gap = send_gap_remaining(db, now)
    from outreach.accounts import seconds_until_any_account_open

    open_regions: set[str] = set()
    candidates: list[float] = []
    capped: list[float] = []  # R11: open window, queued work, fleet spent
    for region in work:
        if in_window(db, region, now):
            lanes = lane_work.get(region, set())
            fleet_lanes = {lane for lane in lanes if lane in ("ig", "wa")}
            if not fleet_lanes:
                # email (or an unstamped legacy row) has no fleet and no
                # stagger slice: the region's window alone governs it
                open_regions.add(region)
                continue
            # L3 stagger: a region window is only WORK when some account is
            # inside its own slice; otherwise the claim loop refuses every
            # row and wake=0 would hot-spin the engine (the 2026-11-04 bug
            # class), so wait for the earliest account stagger instead.
            wait = seconds_until_any_account_open(db, region, now, fleet_lanes)
            if wait is None:
                # No account may act in this open window (R11). Spent cap is
                # queued work the gates refuse, so the probes keep their
                # cadence; a fleet with no cap to give today is not work.
                if _fleet_cap_left(db, fleet_lanes, now):
                    capped.append(_MAX_PAUSE_S)
                continue
            if wait > 0:
                candidates.append(wait)
                continue
            open_regions.add(region)
        else:
            start = next_window_start(db, region, now)
            if start is not None:
                candidates.append((start - now).total_seconds())
    if open_regions:
        if actionable & open_regions:
            hold = max(gap, _slot_hold(db, now))
            if hold > 0:
                return min(hold, _MAX_PAUSE_S)  # paced: wake at gap's end
            backoff = _backoff_remaining(db, now)
            if backoff > 0:
                return backoff  # B23: bounded stop parks, never spins
            return 0.0
        # window open, but nothing a gate would act on yet (future rung, or
        # a lane asleep): park at the probe cadence, never a hot spin.
        return _MAX_PAUSE_S
    if candidates or capped:
        return min([*candidates, *capped])
    return None


# --- the tick ----------------------------------------------------------------

def open_session_regions(db: "Database", now: datetime) -> tuple[str, ...]:
    """Regions whose ADR-9 window is open right now (config rows only).

    The engine passes this as `regions` so the ladder — including the
    day-7 IG escalation — only reaches leads inside their session window
    (ADR-9 law; manual/command_center runs may still pass None = unfiltered).
    """
    from outreach.dispatcher import in_window

    windows = get_config(db, "session_windows") or {}
    return tuple(sorted(r for r in windows if in_window(db, r, now)))


@tick_anchor
def run_engine_tick(
    db: "Database",
    now: datetime,
    *,
    senders: dict[str, Callable[..., Any]],
    resenders: dict[str, Callable[..., Any]] | None = None,
    regions: tuple[str, ...] | None = None,
    imap_factory: Any = None,
    wa_reply_probe: Callable[..., Any] | None = None,
    ig_reply_probe: Callable[..., Any] | None = None,
    engage_stage: Callable[..., Any] | None = None,
    review_path: Any = None,
    stage_cb: Callable[..., Any] | None = None,
    stage_budget_s: dict[str, float] | None = None,
) -> TickOutcome:
    """One engine pass: replies → ladder → dispatch.

    Reply-priority: the mailbox is polled FIRST, then the WA in-thread
    probe (`wa_reply_probe`, B13 leg 2) and the IG in-thread probe
    (`ig_reply_probe`, B24) run — so a lead that has answered is handed
    over before any send path can touch it. All legs fail softly:
    missing IMAP creds (MailboxPendingOwner) park ONLY the email leg,
    and a probe fault lands in the digest — sends are never held hostage
    by reply detection.
    When `review_path` is set, the owner final-review file (both channels
    dead) is regenerated atomically at the end of the pass.
    Dispatch walks `claim_next` (which owns windows, caps, templates,
    and the ADR-5/8 gates per channel) through the END-TO-END senders;
    a paused queue OR an unconfirmed template parks the whole tick
    gracefully (stop reason in `stopped_by`). Every failure shape is
    recorded in the TickOutcome — the loop never crashes.

    Stages (`stage_cb`, R2): boot → replies → ladder → engage → dispatch → review. The
    callback is called as `stage_cb(name)` on entry and `stage_cb(name,
    seconds)` on exit, which is what lets the heartbeat name a stage WHILE it
    runs, not only after the whole cycle. A stage past its budget (see
    `_STAGE_BUDGET_S`, override with `stage_budget_s`) lands one STAGE OVERRUN
    row; budgets are alarms, the refresh pass's own budget is the hard stop.
    """
    budgets = dict(_STAGE_BUDGET_S)
    if stage_budget_s:
        budgets.update(stage_budget_s)

    def _mark(name: str) -> float:
        if stage_cb is not None:
            stage_cb(name)
        return _time.monotonic()

    def _close(name: str, started: float) -> None:
        seconds = _time.monotonic() - started
        if stage_cb is not None:
            stage_cb(name, seconds)
        budget = budgets.get(name)
        if budget is not None and seconds > budget:
            _record_stage_overrun(db, name, seconds, budget, now)

    from outreach.icp import QueuePaused
    from outreach.dispatcher import claim_next, ensure_dispatcher_config

    t_boot = _mark("boot")
    ensure_dispatcher_config(db)  # cold-process boot: windows/pacing/caps
    from outreach.countdown import write_from_tick

    # Supply countdown (owner order 2026-11-09): first tick after 00:00 PKT
    # writes SUPPLY_COUNTDOWN.{md,json} beside outreach.db, 23:00 PKT
    # refreshes it. Fail-soft: a file/DB fault must never park the tick.
    write_from_tick(db, now)
    from outreach.dispatcher import reconcile_window_extensions

    reconcile_window_extensions(db, now)  # B27: grant/clear before dispatch
    from outreach.targets import check_targets

    # v1.1 batch 10: one below-pace alert per channel per day at most, and a
    # no-op while `daily_targets` is 0 (the seeded value).
    check_targets(db, now)
    _close("boot", t_boot)
    resenders = resenders or {}
    t_replies = _mark("replies")
    replies: ReplyPollOutcome | None = None
    if imap_factory is not None:
        try:
            replies = poll_email_replies(db, imap_factory=imap_factory)
        except MailboxPendingOwner:
            replies = None  # owner task: creds not pasted — park, never crash

    wa_replied: tuple[int, ...] = ()
    # owner directive 2026-11-02: NO WhatsApp usage at all — the in-thread
    # probe opens WhatsApp Web, so it is paused with the lane (skipped here,
    # never attempted)
    if wa_reply_probe is not None and not get_config(db, "wa_paused"):
        try:
            wa_replied = tuple(wa_reply_probe(db, now) or ())
        except Exception as exc:  # noqa: BLE001 — a probe fault never stops sends
            _record_probe_error(db, "wa", exc, now)

    ig_replied: tuple[int, ...] = ()
    if ig_reply_probe is not None:
        try:
            ig_replied = tuple(ig_reply_probe(db, now) or ())
        except Exception as exc:  # noqa: BLE001 — a probe fault never stops sends
            _record_probe_error(db, "ig", exc, now)
    _close("replies", t_replies)

    # Reply-guard (owner directive 2026-10-30): a rung may only fire on a
    # channel whose reply probe confirmed its threads within the throttle
    # window + slack. Computed AFTER the probe legs above ran — a probe
    # that just succeeded stamps itself fresh; a probe that failed (or is
    # throttled behind an old stamp) blocks its channel's rungs until it
    # succeeds again. Missing stamp = fresh (never deadlocks a fresh DB).
    from outreach.replies import probe_fresh

    t_ladder = _mark("ladder")
    probe_blocked: list[str] = []
    if wa_reply_probe is not None and not probe_fresh(db, now, "wa"):
        probe_blocked.append("wa")
    if ig_reply_probe is not None and not probe_fresh(db, now, "ig"):
        probe_blocked.append("ig")
    followups_processed = process_due_followups(
        db, now, senders=resenders, regions=regions,
        probe_blocked=tuple(probe_blocked),
    )
    _close("ladder", t_ladder)

    # O1 (2026-11-13): engagement runs AFTER the ladder, so a lead that just
    # replied is already out of the ROUTED set this stage reads. It is optional
    # and it fails SOFT: warm-up/comment machinery must never hold a send
    # hostage, so a fault lands one throttled row and the cycle moves on.
    t_engage = _mark("engage")
    if engage_stage is not None:
        try:
            engage_stage(db, now)
        except Exception as exc:  # noqa: BLE001 — engagement never blocks sends
            _record_probe_error(db, "engage", exc, now, label="ENGAGE ERROR")
    _close("engage", t_engage)

    t_dispatch = _mark("dispatch")

    dispatched: list[tuple[int, str]] = []
    stopped_by = "drained"
    skip: set[int] = set()  # leads whose sender crashed this pass
    try:
        while len(dispatched) < _MAX_DISPATCH_TICK:
            prepared = claim_next(db, now, skip=skip)
            if prepared is None:
                break
            if prepared.channel not in senders:
                stopped_by = f"unwired:{prepared.channel}"
                break
            sender = senders[prepared.channel]
            try:
                outcome = sender(db, prepared.lead_id, now)
            except (QueuePaused, TemplateUnconfirmed, EmailPendingOwner):
                raise  # handled below/above: lane parks, not crash containment
            except IgTransportError as exc:
                # 2026-11-13 (audit F3): both DM surfaces refused to load.
                # Park the lead (never IG_DEAD - the account is fine), tell the
                # digest once, and do not re-claim it this pass.
                from outreach.machine import transition

                transition(
                    db,
                    prepared.lead_id,
                    "TRANSPORT_HOLD",
                    reason=str(exc)[:200],
                    actor="dispatcher",
                )
                _record_send_stop(db, "transport_hold", now)
                skip.add(prepared.lead_id)
                stopped_by = "transport_hold"
                continue
            except Exception as exc:  # noqa: BLE001 — live 2026-10-29: an IG
                # composer TimeoutError killed the WHOLE cycle every wake and
                # starved the WA lane for five hours (digest #64-72). One
                # lane's crash is recorded + parked + skipped for this pass;
                # the tick keeps serving every other lane.
                _record_sender_error(db, prepared.channel, prepared.lead_id, exc, now)
                skip.add(prepared.lead_id)
                stopped_by = f"sender_error:{prepared.channel}"
                continue
            state = getattr(outcome, "state", "")
            if state == "sent":
                dispatched.append((prepared.lead_id, prepared.channel))
                continue  # next lead, same window
            if state == "profile_no_message":
                # 2026-11-13 (R9): a PER-LEAD fact, not a lane fact. Breaking
                # here (the pre-R9 behaviour for any non-`sent` state) put the
                # same lead at the head of a stable claim order on every tick
                # and starved every send behind it — the IL-2 shape. Skip it
                # for this pass instead; the lead stays ROUTED because a stale
                # selector is not proof the account cannot receive a DM.
                _record_send_skip(db, state, prepared.lead_id, now)
                skip.add(prepared.lead_id)
                continue
            stopped_by = state or "unknown"
            break
    except QueuePaused:
        stopped_by = "paused"
    except TemplateUnconfirmed:
        stopped_by = "template_gate"  # owner unverifies — park, don't crash
    except EmailPendingOwner:
        # SMTP creds absent from the process env (the `.env` loader only
        # runs in the production entry): park the email LANE, never
        # crash-loop the engine every five minutes.
        stopped_by = "email_pending_owner"
    if stopped_by != "drained":
        # visibility law: every non-healthy stop reason is observable
        # ("drained" = normal end of work, never a row)
        _record_send_stop(db, stopped_by, now)
        # backoff law (B23): park the wake for the same bounded 5 min so
        # a persistent wall cannot hot-spin the loop behind the row's
        # 30-min throttle.
        set_config(
            db,
            _BACKOFF_KEY,
            (now + timedelta(seconds=_BACKOFF_S)).isoformat(),
        )
    else:
        set_config(db, _BACKOFF_KEY, "")  # healthy end: release any park
    _close("dispatch", t_dispatch)
    if review_path is not None:
        t_review = _mark("review")
        from outreach.owner_review import write_owner_review

        try:
            write_owner_review(db, review_path, now)
        except OSError as exc:  # a disk hiccup never kills the tick
            with db.conn:
                db.conn.execute(
                    "INSERT INTO digest(body) VALUES (?)",
                    (f"OWNER REVIEW WRITE ERROR: {repr(exc)[:300]}",),
                )
        _close("review", t_review)
    return TickOutcome(
        replies=replies,
        followups_processed=tuple(followups_processed),
        dispatched=tuple(dispatched),
        stopped_by=stopped_by,
        wa_replied=wa_replied,
        ig_replied=ig_replied,
    )
