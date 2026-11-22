"""O10 (2026-11-14): attribution — prove the overhaul, or disprove it.

`measure.py` answers *which variant* earned a reply. It cannot answer the
questions the overhaul actually turns on, because they are not template
questions:

  * did the DM follow a comment (`commented_before_dm`) — the O2/O3 law;
  * at what hour did we send (`send_hour`, PKT) — the O5 lever;
  * what did we aim at (`target_class`) — the O6 lever;
  * did the lead arrive from a story (`touch_kind`) — the O4 lever.

So this module groups the SAME decided sends `measure` already counts, by one
attribution dimension at a time, and never invents a row: every bucket is
computed from `message_log`, `state_history` and `engagement_log`.

Laws inherited and kept, so a number here can be compared to `replies`:

  * a FIRST TOUCH is the earliest `message_log` row per lead;
  * a send is DECIDED once 72 h old or the lead already replied;
  * a reply after the window is LATE, never folded into the rate;
  * an empty dimension reports no buckets rather than a zero rate, because
    "0 %" out of nothing is the most common lie in a dashboard.

`send_hour` is the hour the message LEFT, in the timezone it was aimed at
(PKT, UTC+5), bucketed in three-hour blocks so a bucket is worth reading.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from outreach.measure import _first_touches, _parse, _reply_times

if TYPE_CHECKING:
    from outreach.store import Database

#: PKT has no DST, so a fixed offset is exact (not an approximation).
PKT = timezone(timedelta(hours=5))
HOUR_BLOCK_H = 3

DIMENSIONS = ("commented_before_dm", "send_hour", "target_class", "touch_kind")


@dataclass(frozen=True)
class BucketOutcome:
    """One bucket of one dimension, measured exactly like a variant row."""

    dimension: str
    bucket: str
    decided: int
    replies: int
    late: int
    pending: int

    @property
    def rate(self) -> float | None:
        if self.decided == 0:
            return None
        return self.replies / self.decided

    def as_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "decided": self.decided,
            "replies": self.replies,
            "late": self.late,
            "pending": self.pending,
            "rate": None if self.rate is None else round(self.rate, 4),
        }


def _first_time(db: "Database", act_sql: str, params: tuple = ()) -> dict[int, datetime]:
    """Earliest matching engagement act per lead (comment, or story view)."""
    times: dict[int, datetime] = {}
    for row in db.conn.execute(
        "SELECT lead_id, MIN(acted_at) AS at FROM engagement_log"
        f" WHERE {act_sql} GROUP BY lead_id",
        params,
    ):
        moment = _parse(row["at"])
        if moment is not None:
            times[int(row["lead_id"])] = moment
    return times


def _comment_times(db: "Database") -> dict[int, datetime]:
    """The first warm touch: a comment or (O4, 2026-11-14) a story reply."""
    return _first_time(db, "act IN ('comment', 'story_reply')")


def _story_times(db: "Database") -> dict[int, datetime]:
    return _first_time(db, "target IN ('story','reel')")


def _payload(data_json: Any) -> dict[str, Any]:
    try:
        payload = json.loads(data_json or "{}") or {}
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def target_class_of(data_json: Any) -> str:
    """What we aimed at, from the lead's own scraped facts (O6's classes)."""
    payload = _payload(data_json)
    if payload.get("is_chain"):
        return "chain"
    if payload.get("is_ghost_kitchen"):
        return "ghost_kitchen"
    if payload.get("is_on_delivery_platform") and payload.get("has_dine_in") is False:
        return "delivery_only"
    return "independent"


def send_hour_of(sent_at: datetime) -> str:
    hour = sent_at.astimezone(PKT).hour // HOUR_BLOCK_H * HOUR_BLOCK_H
    return f"{hour:02d}-{hour + HOUR_BLOCK_H:02d}"


def attribution(
    db: "Database",
    now: datetime,
    *,
    dimension: str,
    window_hours: int = 72,
    channel: str | None = None,
) -> list[BucketOutcome]:
    """Decided first touches grouped by one dimension, largest bucket first."""
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown dimension {dimension!r}; use one of {DIMENSIONS}")
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    window = timedelta(hours=window_hours)
    replies_at = _reply_times(db)
    comments_at = _comment_times(db)
    stories_at = _story_times(db)
    classes = {
        int(row["id"]): target_class_of(row["data_json"])
        for row in db.conn.execute("SELECT id, data_json FROM leads")
    }

    counts: dict[str, dict[str, int]] = {}
    for send in _first_touches(db, channel):
        lead_id = int(send["lead_id"])
        sent_at = _parse(send["sent_at"])
        if sent_at is None:
            continue
        if dimension == "commented_before_dm":
            comment_at = comments_at.get(lead_id)
            bucket = "commented" if comment_at is not None and comment_at <= sent_at else "cold"
        elif dimension == "send_hour":
            bucket = send_hour_of(sent_at)
        elif dimension == "target_class":
            bucket = classes.get(lead_id, "independent")
        else:  # touch_kind
            story_at = stories_at.get(lead_id)
            bucket = "story-first" if story_at is not None and story_at <= sent_at else "dm-only"

        entry = counts.setdefault(
            bucket, {"decided": 0, "replies": 0, "late": 0, "pending": 0}
        )
        replied_at = replies_at.get(lead_id)
        if replied_at is None:
            if moment - sent_at >= window:
                entry["decided"] += 1
            else:
                entry["pending"] += 1
            continue
        elapsed = (replied_at - sent_at).total_seconds() / 3600
        entry["decided"] += 1
        if elapsed <= window_hours:
            entry["replies"] += 1
        else:
            entry["late"] += 1

    rows = [
        BucketOutcome(
            dimension=dimension,
            bucket=bucket,
            decided=data["decided"],
            replies=data["replies"],
            late=data["late"],
            pending=data["pending"],
        )
        for bucket, data in counts.items()
    ]
    rows.sort(key=lambda row: (-row.decided, row.bucket))
    return rows


def contract(
    db: "Database",
    now: datetime,
    *,
    window_hours: int = 72,
    channel: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Every dimension, keyed by name — the shape that goes in the JSON."""
    return {
        dimension: [
            row.as_dict()
            for row in attribution(
                db, now, dimension=dimension, window_hours=window_hours, channel=channel
            )
        ]
        for dimension in DIMENSIONS
    }


def format_attribution(
    rows: list[BucketOutcome],
    *,
    window_hours: int = 72,
    dimensions: tuple[str, ...] | None = None,
) -> list[str]:
    """One table per dimension; an empty dimension says so instead of 0 %.

    `dimensions` narrows the report — `command_center replies --by X` asks for
    one table and must not print "no decided first touches yet" for the three
    dimensions it did not compute.
    """
    lines: list[str] = []
    for dimension in dimensions or DIMENSIONS:
        group = [row for row in rows if row.dimension == dimension]
        lines.append(f"REPLY RATE BY {dimension.upper()} ({window_hours}h, first touch)")
        if not group:
            lines.append("  (no decided first touches yet)")
            continue
        for row in group:
            rate = "n/a" if row.rate is None else f"{round(100 * row.rate)}%"
            extras = f"  ({row.replies}/{row.decided})"
            if row.late:
                extras += f"  late {row.late}"
            if row.pending:
                extras += f"  {row.pending} pending"
            lines.append(f"  {row.bucket:<16} {rate:>4}{extras}")
    return lines


def all_rows(
    db: "Database", now: datetime, *, window_hours: int = 72, channel: str | None = None
) -> list[BucketOutcome]:
    """Every dimension's rows in one list (for `command_center replies --by`)."""
    rows: list[BucketOutcome] = []
    for dimension in DIMENSIONS:
        rows.extend(
            attribution(
                db, now, dimension=dimension, window_hours=window_hours, channel=channel
            )
        )
    return rows
