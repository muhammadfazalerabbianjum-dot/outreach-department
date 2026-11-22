"""What earned a reply (v1.1 batch 10.5): 72h measurement, per variant.

The fleet can send 20 a day, so "is this working?" has to stop being a feeling.
Two facts we already store answer it exactly: `message_log` says what shipped,
when, and under which frozen variant hash; `state_history` says when a lead went
REPLIED. Nothing here estimates, extrapolates or invents a benchmark.

The laws that keep the number honest:

  * a FIRST TOUCH is the earliest message_log row per lead — the ladder's later
    rungs are not independent experiments (L1: one first touch per lead);
  * a send is DECIDED only once it is `window_hours` old or the lead already
    replied. Counting a three-hour-old send as a failure would make every
    morning look worse than the day is; counting only old sends would hide the
    replies we already hold;
  * a reply after the window is reported as LATE, never folded into the rate;
  * attribution is the hash that actually shipped, reversed through every
    body a send can carry (`PINNED_SHA256` plus the variant engine's own
    bodies, including the menu-sourced V4/V7); a hash nobody claims stays
    `unknown:<prefix>`, because a drifting template is exactly what this report
    must expose.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Iterable

from outreach.templates import PINNED_SHA256
from outreach.variants import MENU_DISH_BODIES, VARIANT_BODIES

if TYPE_CHECKING:
    from outreach.store import Database

#: Every body a send can actually carry, mapped back to its label: the pinned
#: file templates AND the variant engine's bodies (owner order 2026-11-12 - a
#: menu-sourced V4/V7 ships a different body, and it must not report as
#: `unknown:` just because its bytes are new).
_NAME_BY_HASH = {digest: name for name, digest in PINNED_SHA256.items()}
for _bodies in (VARIANT_BODIES, MENU_DISH_BODIES):
    for _variant_id, _body in _bodies.items():
        _NAME_BY_HASH.setdefault(
            hashlib.sha256(_body.encode()).hexdigest(), _variant_id
        )


@dataclass(frozen=True)
class VariantOutcome:
    """One variant on one channel, as measured over the window."""

    variant: str
    channel: str
    decided: int  # mature sends + any that already replied
    replies: int  # replied inside the window
    late: int  # replied, but after it
    pending: int  # too young to judge, no reply yet
    median_hours: float | None

    @property
    def rate(self) -> float | None:
        """Replies per decided send; None when nothing is decided yet."""
        if self.decided == 0:
            return None
        return self.replies / self.decided


def _parse(stamp: object) -> datetime | None:
    if not stamp:
        return None
    text = str(stamp).replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _label(template_hash: str) -> str:
    return _NAME_BY_HASH.get(template_hash) or f"unknown:{template_hash[:8]}"


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 2)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 2)


def _first_touches(db: "Database", channel: str | None) -> list[dict]:
    """The earliest message_log row per lead, optionally restricted to a channel."""
    where = "" if channel is None else " WHERE channel=?"
    params: tuple = () if channel is None else (channel,)
    sql = (
        "SELECT m.lead_id, m.channel, m.template_hash, m.sent_at FROM message_log m"
        " JOIN (SELECT lead_id, MIN(sent_at) AS first_at FROM message_log"
        f"{where} GROUP BY lead_id) first"
        " ON first.lead_id = m.lead_id AND first.first_at = m.sent_at"
    )
    seen: set[int] = set()
    rows: list[dict] = []
    for row in db.conn.execute(sql, params):
        lead_id = int(row["lead_id"])
        if lead_id in seen:  # two rows in the same instant: count the lead once
            continue
        seen.add(lead_id)
        rows.append(dict(row))
    return rows


def _reply_times(db: "Database") -> dict[int, datetime]:
    """First REPLIED moment per lead (state_history is append-only)."""
    times: dict[int, datetime] = {}
    for row in db.conn.execute(
        "SELECT lead_id, MIN(created_at) AS at FROM state_history"
        " WHERE to_state='REPLIED' GROUP BY lead_id"
    ):
        moment = _parse(row["at"])
        if moment is not None:
            times[int(row["lead_id"])] = moment
    return times


def reply_measurement(
    db: "Database",
    now: datetime,
    *,
    window_hours: int = 72,
    channel: str | None = None,
) -> list[VariantOutcome]:
    """Per (variant, channel) reply outcomes for first touches inside `now`."""
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    window = timedelta(hours=window_hours)
    replies_at = _reply_times(db)

    buckets: dict[tuple[str, str], dict[str, object]] = {}
    for send in _first_touches(db, channel):
        sent_at = _parse(send["sent_at"])
        if sent_at is None:
            continue
        key = (_label(str(send["template_hash"])), str(send["channel"]))
        bucket = buckets.setdefault(
            key, {"decided": 0, "replies": 0, "late": 0, "pending": 0, "hours": []}
        )
        replied_at = replies_at.get(int(send["lead_id"]))
        if replied_at is None:
            if moment - sent_at >= window:
                bucket["decided"] = int(bucket["decided"]) + 1
            else:
                bucket["pending"] = int(bucket["pending"]) + 1
            continue
        elapsed = (replied_at - sent_at).total_seconds() / 3600
        bucket["decided"] = int(bucket["decided"]) + 1
        if elapsed <= window_hours:
            bucket["replies"] = int(bucket["replies"]) + 1
            hours = bucket["hours"]
            assert isinstance(hours, list)
            hours.append(elapsed)
        else:
            bucket["late"] = int(bucket["late"]) + 1

    outcomes = [
        VariantOutcome(
            variant=variant,
            channel=chan,
            decided=int(data["decided"]),
            replies=int(data["replies"]),
            late=int(data["late"]),
            pending=int(data["pending"]),
            median_hours=_median([float(h) for h in data["hours"]]),
        )
        for (variant, chan), data in buckets.items()
    ]
    outcomes.sort(key=lambda row: (row.channel, -row.decided, row.variant))
    return outcomes


def format_measurement(
    rows: Iterable[VariantOutcome], *, window_hours: int = 72
) -> list[str]:
    """Human lines: rate out of decided, median hours, late replies, pending."""
    rows = list(rows)
    if not rows:
        return [f"REPLY MEASUREMENT ({window_hours}h): no first touches yet"]
    lines = [f"REPLY MEASUREMENT ({window_hours}h, first touch only)"]
    for row in rows:
        rate = "n/a" if row.rate is None else f"{round(100 * row.rate)}%"
        median = (
            f"  median {row.median_hours:g}h" if row.median_hours is not None else ""
        )
        late = f"  late {row.late}" if row.late else ""
        pending = f"  {row.pending} pending" if row.pending else ""
        lines.append(
            f"  {row.channel:<5} {row.variant:<16} {rate:>4}"
            f"  ({row.replies}/{row.decided}){median}{late}{pending}"
        )
    return lines
