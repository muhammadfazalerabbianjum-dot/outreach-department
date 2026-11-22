"""Daily caps ledger (gate #3): DB-level enforcement, ADR-7/8 semantics.

PKT = fixed UTC+05:00 offset (Pakistan has no DST; avoids a tzdata
dependency while staying deterministic). Caps live in `config.daily_caps`;
the IG cap is additionally throttled by the warmup ramp (ADR-8):
week1 = 5/day, week2 = 10/day, after = the configured cap (15) — starting
only when the engine stamps `ig_warmup_start` (never-started IG = cap 0,
so the ramp begins with real usage, not with a guess).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from outreach.store import get_config, set_config

if TYPE_CHECKING:
    from outreach.store import Database

PKT = timezone(timedelta(hours=5), name="PKT")

DEFAULT_CAPS = {"wa": 15, "ig": 15, "email": 20}
_WARMUP_WEEK1 = 5
_WARMUP_WEEK2 = 10


class CapReached(RuntimeError):
    """Raised when registering one more send would exceed the daily cap."""


def pkt_date(now: datetime) -> str:
    """PKT calendar date (ISO) for an aware-UTC instant; naive => UTC."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(PKT).date().isoformat()


def ensure_caps(db: "Database") -> None:
    """Seed `daily_caps` once (idempotent)."""
    if get_config(db, "daily_caps") is None:
        set_config(db, "daily_caps", DEFAULT_CAPS)


def current_cap(db: "Database", channel: str, now: datetime) -> int:
    """Effective cap for a channel on the given PKT date.

    L2 with a fleet: when `fleet_caps_enabled` is armed, a channel's ceiling is
    the SUM of its active accounts' caps, so the fleet total can emerge from
    per-account ramps. Off by default - arming it changes the live cap of the
    one account sending today, which is the owner's call.
    """
    caps = get_config(db, "daily_caps")
    if caps is None:
        raise RuntimeError("caps not seeded (run ensure_caps/ensure_dispatcher_config)")
    configured = int(caps[channel])
    if channel in ("ig", "wa") and get_config(db, "fleet_caps_enabled"):
        from outreach.accounts import channel_cap

        total = channel_cap(db, channel, now)
        if total is not None:
            return total
    if channel == "ig":
        start = get_config(db, "ig_warmup_start")
        if start is None:
            return 0  # ramp begins only when the engine stamps real usage
        start_d = date.fromisoformat(str(start))
        today = date.fromisoformat(pkt_date(now))
        days = (today - start_d).days
        if days < 0:
            return 0
        warmup = _WARMUP_WEEK1 if days <= 6 else (
            _WARMUP_WEEK2 if days <= 13 else configured
        )
        return min(warmup, configured)
    return configured


def ensure_ig_warmup_start(db: "Database", now: datetime) -> None:
    """Stamp the ramp start with the current PKT date, once (ADR-8)."""
    if get_config(db, "ig_warmup_start") is None:
        set_config(db, "ig_warmup_start", pkt_date(now))


def send_allowed(db: "Database", channel: str, now: datetime) -> bool:
    """Read-only cap pre-check (no mutation) for the claim loop."""
    day = pkt_date(now)
    row = db.conn.execute(
        "SELECT count FROM daily_sends WHERE channel=? AND date=?", (channel, day)
    ).fetchone()
    return (row[0] if row else 0) < current_cap(db, channel, now)


def register_send(db: "Database", channel: str, now: datetime) -> None:
    """Count one send against the channel's daily cap; refuse at the cap."""
    day = pkt_date(now)
    cap = current_cap(db, channel, now)
    row = db.conn.execute(
        "SELECT count FROM daily_sends WHERE channel=? AND date=?", (channel, day)
    ).fetchone()
    count = row[0] if row else 0
    if count >= cap:
        raise CapReached(f"{channel} daily cap {cap} reached for PKT {day}")
    with db.conn:
        db.conn.execute(
            "INSERT INTO daily_sends(channel, date, count) VALUES (?, ?, 1) "
            "ON CONFLICT(channel, date) DO UPDATE SET count=count+1",
            (channel, day),
        )


MAX_CAP = 100


def set_cap(db: "Database", channel: str, value: int) -> int:
    """Validate and persist one channel's daily cap.

    2026-11-13 (audit F9): caps were raw JSON edited with no checking, so a
    typo could either stop a channel silently (0) or blow the day's risk
    budget (999). Every external edit goes through here.
    """
    if channel not in DEFAULT_CAPS:
        raise ValueError(f"unknown channel {channel!r}")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"cap for {channel} must be an int, got {value!r}")
    if value < 0 or value > MAX_CAP:
        raise ValueError(f"cap for {channel} must be 0..{MAX_CAP}, got {value}")
    caps = dict(get_config(db, "daily_caps") or DEFAULT_CAPS)
    caps[channel] = int(value)
    set_config(db, "daily_caps", caps)
    return int(value)
