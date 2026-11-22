"""O5: send in THEIR clock, not ours.

The law: a message to a kitchen should arrive while someone is in that kitchen.
Until now every window in this codebase was a region window on our own clock, so
`me` sends could land at a Dubai 3 a.m. This module is the missing half — the
venue's own open hours and busy curve, read from the fields the scraper is
filling (`hours`, `popular_times`, `timezone`, contract in
`SCRAPER_PROMPT_2026-11-13.md`).

Two rules it never bends:

* **Unknown is not closed.** A lead with no `hours` for today sends exactly as it
  did before this module existed. Guessing "probably closed" would silently
  starve the queue, and guessing "probably open" sends to a closed kitchen;
  only a READ hour map is allowed to answer either way.
* **No smoothing.** The busy curve is used as given. A flat or missing curve
  means no timing opinion, not a fabricated quiet hour.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from outreach.store import get_config, set_config

CLOCK_KEY = "send_clock"
DEFAULTS: dict[str, Any] = {
    "enabled": True,  # law, not an experiment: their clock, not ours
    "quiet_busy_percent": 40,  # below this the window is "quiet" in the curve
    "park_hours": 6,  # one visible CLOCK row per lead per 6 h, not one per tick
}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class VenueClock:
    """What the lead's own data says about *their* day, right now."""

    state: str  # "open" | "closed" | "unknown"
    local: datetime | None = None
    timezone_name: str = ""
    opens_at: str = ""  # "HH:MM" local, when closed and a later range exists today
    busy: int | None = None  # the curve value for the current local hour


def ensure_config(db: "Database") -> None:
    if get_config(db, CLOCK_KEY) is None:
        set_config(db, CLOCK_KEY, dict(DEFAULTS))


def clock_armed(db: "Database") -> bool:
    raw = get_config(db, CLOCK_KEY) or {}
    enabled = raw.get("enabled", DEFAULTS["enabled"]) if isinstance(raw, dict) else False
    return bool(enabled)


def _payload(row: Any) -> dict[str, Any]:
    raw = row["data_json"] if not isinstance(row, dict) else row.get("data_json")
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _minutes(hhmm: Any) -> int | None:
    try:
        hour, minute = str(hhmm).strip().split(":")[:2]
        value = int(hour) * 60 + int(minute)
    except (ValueError, AttributeError):
        return None
    return value if 0 <= value < 24 * 60 else None


def venue_local(now: datetime, name: Any) -> datetime | None:
    """The venue's local time, or None when the zone is unknown or absurd."""
    if not isinstance(name, str) or "/" not in name:
        return None
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(name)
    except Exception:  # noqa: BLE001 — an unknown zone is "unknown", not UTC
        return None
    stamp = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return stamp.astimezone(zone)


def _ranges(hours: dict[str, Any], day: str) -> list[tuple[int, int]]:
    raw = hours.get(day)
    if not isinstance(raw, list):
        return []
    out: list[tuple[int, int]] = []
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        start, end = _minutes(entry[0]), _minutes(entry[1])
        if start is None or end is None or start == end:
            continue
        out.append((start, end))
    return out


def read_clock(row: Any, now: datetime) -> VenueClock:
    """State from the lead's own hour map; `unknown` whenever it cannot answer."""
    data = _payload(row)
    local = venue_local(now, data.get("timezone"))
    if local is None:
        return VenueClock("unknown")
    hours = data.get("hours")
    if not isinstance(hours, dict):
        return VenueClock("unknown", local=local, timezone_name=str(data.get("timezone")))
    day = DAYS[local.weekday()]
    ranges = _ranges(hours, day)
    if not ranges:
        # an explicit `[]` is data: nothing is known for this day, so no opinion
        return VenueClock("unknown", local=local, timezone_name=str(data.get("timezone")))
    minute = local.hour * 60 + local.minute
    busy: int | None = None
    curve = data.get("popular_times")
    if isinstance(curve, dict):
        today = curve.get(day)
        if isinstance(today, list) and len(today) == 24 and local.hour < len(today):
            value = today[local.hour]
            if isinstance(value, (int, float)):
                busy = int(value)
    for start, end in ranges:
        # a range that crosses midnight ("16:00"–"02:00") is stored split by the
        # scraper; a single range is treated as within the day
        if start < end and start <= minute < end:
            return VenueClock("open", local=local, timezone_name=str(data.get("timezone")), busy=busy)
        if start > end and (minute >= start or minute < end):
            return VenueClock("open", local=local, timezone_name=str(data.get("timezone")), busy=busy)
    later = sorted(start for start, _end in ranges if start > minute)
    opens = f"{later[0] // 60:02d}:{later[0] % 60:02d}" if later else ""
    return VenueClock(
        "closed", local=local, timezone_name=str(data.get("timezone")), opens_at=opens
    )


def clock_reason(row: Any, now: datetime) -> str | None:
    """A park reason when the venue is provably shut, else None (send as before)."""
    state = read_clock(row, now)
    if state.state != "closed":
        return None
    return f"venue closed (opens {state.opens_at} local)" if state.opens_at else "venue closed for today"


def record_clock_park(db: "Database", lead_id: int, reason: str, now: datetime) -> None:
    """One visible CLOCK row per lead per `park_hours`.

    A shut venue must read as a decision in the digest, not as a silent gap in
    the queue; per-lead throttling keeps a closed city from burying the log
    (the R9 lesson).
    """
    stamp = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    since = (
        stamp.astimezone(timezone.utc) - timedelta(hours=float(DEFAULTS["park_hours"]))
    ).strftime("%Y-%m-%dT%H:%M")
    seen = db.conn.execute(
        "SELECT 1 FROM digest WHERE body LIKE ? AND created_at >= ? LIMIT 1",
        (f"CLOCK PARK lead {lead_id} %", since),
    ).fetchone()
    if seen is not None:
        return
    with db.conn:
        db.conn.execute(
            "INSERT INTO digest(body) VALUES (?)",
            (f"CLOCK PARK lead {lead_id}: {reason}",),
        )


def status_line(db: "Database", now: datetime) -> str:
    """How much of the queue is timed on real hours — the O5 coverage answer."""
    rows = db.conn.execute(
        "SELECT id, data_json FROM leads WHERE state='ROUTED'"
    ).fetchall()
    open_n = closed_n = unknown_n = 0
    for row in rows:
        state = read_clock(row, now).state
        if state == "open":
            open_n += 1
        elif state == "closed":
            closed_n += 1
        else:
            unknown_n += 1
    total = len(rows)
    known = open_n + closed_n
    share = f"{known * 100 // total}%" if total else "n/a"
    return (
        f"SEND CLOCK: {open_n} open / {closed_n} closed / {unknown_n} unknown"
        f" of {total} routed (hours known for {share})"
    )
