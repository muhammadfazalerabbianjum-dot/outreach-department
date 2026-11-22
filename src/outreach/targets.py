"""v1.1 batch 10: daily-target assurance (L7) and its owner alarm.

The owner sets a goal per channel; the fleet's caps still decide what is
physically possible (L2: a target never raises a cap). At the halfway point of
the channel's working day, a channel that has sent less than half its goal
raises ONE alert for that PKT day and hands stalled accounts' untouched leads
back to the queue.

"Working day" is deliberately the union of the region windows that still hold
pending work for the channel, because a channel spans regions: with the owner
bands that is 13:00-23:00 PKT when only NA work remains, and it never opens
outside a real window.

The supply alarm is not rebuilt here: `intake.scan_low_leads` already alerts on
queue starvation with its own per-episode dedupe, and batch 10's gate pins that
it still behaves.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from outreach.caps import PKT, pkt_date
from outreach.store import get_config, set_config

if TYPE_CHECKING:
    from outreach.store import Database

TARGETS_KEY = "daily_targets"
ALERTED_KEY = "target_alerted"
CHECKPOINT = 0.5
CHANNELS = ("ig", "wa", "email")


@dataclass(frozen=True)
class PaceReport:
    """One channel's progress through its working day."""

    channel: str
    target: int
    sent: int
    elapsed: float
    below_pace: bool
    hour_start: str
    hour_end: str


def ensure_target_config(db: "Database") -> None:
    """Seed the goals at zero: an unset target alarms nobody."""
    if get_config(db, TARGETS_KEY) is None:
        set_config(db, TARGETS_KEY, {channel: 0 for channel in CHANNELS})


def targets(db: "Database") -> dict[str, int]:
    raw = get_config(db, TARGETS_KEY) or {}
    return {
        channel: max(0, int(raw.get(channel, 0) or 0)) for channel in CHANNELS
    }


def _minutes(hhmm: object) -> int:
    hours, minutes = str(hhmm).split(":")
    return int(hours) * 60 + int(minutes)


def _rule(db: "Database", region: str) -> dict[str, Any] | None:
    windows = get_config(db, "session_windows") or {}
    rule = (windows or {}).get(region)
    return rule if isinstance(rule, dict) else None


def working_day(db: "Database", channel: str, now: datetime) -> tuple[int, int] | None:
    """(start-minute, end-minute) in PKT for the channel's live working day.

    None when the channel has no pending lead in any region with a window, or
    when `now` is outside every one of those windows (nothing can send, so
    there is nothing to be behind on).
    """
    rows = db.conn.execute(
        "SELECT DISTINCT region FROM leads WHERE route_channel=? AND state='ROUTED'",
        (channel,),
    ).fetchall()
    if not rows:
        return None
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    pkt = moment.astimezone(PKT)
    now_min = pkt.hour * 60 + pkt.minute
    starts: list[int] = []
    ends: list[int] = []
    inside = False
    for row in rows:
        region = row["region"]
        if not region:
            continue
        rule = _rule(db, str(region))
        if rule is None:
            continue
        start, end = _minutes(rule["start"]), _minutes(rule["end"])
        if start <= now_min < end:
            inside = True
            starts.append(start)
            ends.append(end)
    if not inside:
        return None
    return min(starts), max(ends)


def sent_today(db: "Database", channel: str, now: datetime) -> int:
    row = db.conn.execute(
        "SELECT count FROM daily_sends WHERE channel=? AND date=?",
        (channel, pkt_date(now)),
    ).fetchone()
    return int(row["count"]) if row else 0


def pace(db: "Database", channel: str, now: datetime) -> PaceReport | None:
    """This channel's target progress, or None when it has no working day."""
    day = working_day(db, channel, now)
    if day is None:
        return None
    start, end = day
    pkt = (now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)).astimezone(PKT)
    now_min = pkt.hour * 60 + pkt.minute
    span = max(1, end - start)
    elapsed = min(1.0, max(0.0, (now_min - start) / span))
    target = targets(db)[channel]
    sent = sent_today(db, channel, now)
    below = target > 0 and elapsed >= CHECKPOINT and sent < CHECKPOINT * target
    return PaceReport(
        channel=channel,
        target=target,
        sent=sent,
        elapsed=elapsed,
        below_pace=below,
        hour_start=f"{start // 60:02d}:{start % 60:02d}",
        hour_end=f"{end // 60:02d}:{end % 60:02d}",
    )


def _alerted(db: "Database") -> dict[str, str]:
    raw = get_config(db, ALERTED_KEY) or {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def _requeue_stalled(db: "Database") -> int:
    """Hand back untouched leads held by HALTED/HEALTH_ALARMED accounts."""
    from outreach.health import redistribute

    rows = db.conn.execute(
        "SELECT id FROM accounts WHERE channel='ig'"
        " AND status IN ('HALTED', 'HEALTH_ALARMED')"
    ).fetchall()
    return sum(redistribute(db, str(row["id"])) for row in rows)


def check_targets(db: "Database", now: datetime) -> list[str]:
    """Raise at most one below-pace alert per channel per PKT day.

    Returns the channels that fired on this call. A channel with no target, no
    window or no pending work fires nothing.
    """
    ensure_target_config(db)
    day = pkt_date(now)
    alerted = _alerted(db)
    fired: list[str] = []
    for channel in CHANNELS:
        report = pace(db, channel, now)
        if report is None or not report.below_pace:
            continue
        if alerted.get(channel) == day:
            continue
        moved = _requeue_stalled(db) if channel == "ig" else 0
        body = (
            f"BELOW PACE {channel}: {report.sent}/{report.target} sent at"
            f" {report.elapsed:.0%} of the {report.hour_start}-{report.hour_end}"
            f" PKT window"
            + (f"; {moved} untouched lead(s) re-queued from stalled accounts." if moved else ".")
        )
        with db.conn:
            db.conn.execute("INSERT INTO digest(body) VALUES (?)", (body,))
        alerted[channel] = day
        set_config(db, ALERTED_KEY, alerted)
        fired.append(channel)
    return fired
