"""R3: a P0/P1 reaches the owner without him reading the digest by hand.

`raise_alert` (R3's `alerts.raise`; `raise` is a keyword) is the ONE seam:

1. it **always** writes a digest row — the record is the truth, whatever the
   severity and whether or not a popup happened;
2. it pops a local balloon for **P0/P1 only**, and
3. it throttles that popup **per code per 30 minutes**, so a condition that
   repeats every cycle cannot repaint the desktop for an hour.

A throttled alert still writes its row. A *failed* toast does not burn the
throttle window (no success, no stamp) — a broken shell retries next cycle.

The popup is the existing `outreach.toast` seam (ADR-12: local powershell
NotifyIcon, nothing leaves the machine). Because the generic drain offers every
new digest row to the same balloon, `raise_alert` advances the drain's cursor
past its own row: an alert is toasted once, here, under its own throttle.

`sweep` is the engine seam and carries the four conditions R3 names:
a heartbeat that missed its own promise inside an open window (P0), no reply
probe for two open-window hours (P1), a `TRANSPORT_HOLD` in the day (P1), and a
fleet that spent its cap before the window's midpoint (P2 — information).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable

from outreach.caps import PKT

if TYPE_CHECKING:
    from outreach.store import Database

P0 = "P0"
P1 = "P1"
P2 = "P2"

TOAST_SEVERITIES = frozenset({P0, P1})
THROTTLE_S = 1800  # per code: one popup per half hour
THROTTLE_KEY = "alert_toasts"  # config row: {code: ISO instant of last popup}
PROBE_STALE_S = 7200  # two open-window hours without a successful reply probe

_SEVERITIES = frozenset({P0, P1, P2})
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def raise_alert(
    db: "Database",
    severity: str,
    code: str,
    detail: str,
    *,
    now: datetime | None = None,
    sender: Callable[[str], bool] | None = None,
) -> int:
    """Record an alert (always) and pop a balloon for P0/P1 (throttled).

    Returns the digest row id, so a caller can point at the record.
    """
    if severity not in _SEVERITIES:
        raise ValueError(f"unknown severity {severity!r} (P0, P1, P2)")
    moment = now or datetime.now(timezone.utc)
    body = f"ALERT {severity} [{code}]: {detail}"
    with db.conn:
        cursor = db.conn.execute(
            "INSERT INTO digest(body, created_at) VALUES (?, ?)",
            (body, moment.strftime(_STAMP_FORMAT)),
        )
    row_id = int(cursor.lastrowid or 0)

    from outreach.store import set_config
    from outreach.toast import STAMP_AT, STAMP_ID

    # The alert owns its popup: keep the generic drain's cursor ahead of this
    # row so the same line is not offered a second time as ordinary noise.
    set_config(db, STAMP_ID, row_id)
    if severity not in TOAST_SEVERITIES:
        return row_id
    if _throttled(db, code, moment):
        return row_id

    from outreach.toast import sendast

    send = sendast if sender is None else sender
    try:
        popped = bool(send(body))
    except Exception:  # noqa: BLE001 - a popup fault must never lose the row
        popped = False
    if popped:
        _stamp(db, code, moment)
        set_config(db, STAMP_AT, moment.isoformat())
    return row_id


def _toasts(db: "Database") -> dict[str, str]:
    from outreach.store import get_config

    stamps = get_config(db, THROTTLE_KEY)
    return dict(stamps) if isinstance(stamps, dict) else {}


def _throttled(db: "Database", code: str, now: datetime) -> bool:
    last = _toasts(db).get(code)
    if not last:
        return False
    try:
        previous = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return False  # a corrupted stamp must not silence a real alarm
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=timezone.utc)
    return (now - previous).total_seconds() < THROTTLE_S


def _stamp(db: "Database", code: str, now: datetime) -> None:
    from outreach.store import set_config

    stamps = _toasts(db)
    stamps[code] = now.isoformat()
    set_config(db, THROTTLE_KEY, stamps)


# --------------------------------------------------------------------------
# sweep: the four conditions, measured from state the engine already keeps


def sweep(
    db: "Database",
    now: datetime,
    *,
    sender: Callable[[str], bool] | None = None,
) -> list[str]:
    """Raise whatever is true right now. Never raises; returns the codes."""
    raised: list[str] = []
    try:
        open_regions = _open_regions(db, now)
    except Exception:  # noqa: BLE001 - an unreadable window is not an alarm
        return raised

    if open_regions:
        late = _heartbeat_late(db, now)
        if late:
            raise_alert(db, P0, "HEARTBEAT_STALE", late, now=now, sender=sender)
            raised.append("HEARTBEAT_STALE")
        stale = _probe_stale(db, now)
        if stale:
            raise_alert(db, P1, "PROBE_STALE", stale, now=now, sender=sender)
            raised.append("PROBE_STALE")

    holds = _transport_holds(db, now)
    if holds:
        raise_alert(db, P1, "TRANSPORT_HOLD", holds, now=now, sender=sender)
        raised.append("TRANSPORT_HOLD")

    if open_regions:
        spent = _cap_early(db, now, open_regions)
        if spent:
            raise_alert(db, P2, "CAP_EARLY", spent, now=now, sender=sender)
            raised.append("CAP_EARLY")
    return raised


def _open_regions(db: "Database", now: datetime) -> tuple[str, ...]:
    from outreach.scheduler import open_session_regions

    return tuple(open_session_regions(db, now))


def _as_utc(moment: datetime) -> datetime:
    """Naive instants are UTC (the repo-wide convention, see `in_window`)."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _parse(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _heartbeat_late(db: "Database", now: datetime) -> str | None:
    """A heartbeat that blew its own promise by a whole promise again.

    The engine stamps `next_wake_at` before every sleep, so lateness is measured
    against the engine's own claim rather than a constant nobody maintains. A
    file with no promise gets a minute of rope (first boot, older format).
    """
    from outreach.heartbeat import heartbeat_path, read_heartbeat

    path = heartbeat_path(getattr(db, "path", None))
    if path is None:
        return None
    beat = read_heartbeat(path)
    if not beat:
        return None
    stamped = _parse(beat.get("stamped_at"))
    if stamped is None:
        return None
    promised = _parse(beat.get("next_wake_at"))
    promise_s = (
        max((promised - stamped).total_seconds(), 60.0)
        if promised is not None
        else 60.0
    )
    if now <= stamped + timedelta(seconds=2 * promise_s):
        return None
    overdue_min = int((now - (promised or stamped)).total_seconds() // 60)
    return (
        f"no heartbeat for {int((now - stamped).total_seconds() // 60)} min"
        f" (promised wake was {overdue_min} min ago)"
    )


def _probe_stale(db: "Database", now: datetime) -> str | None:
    """No successful reply probe on a channel for two open-window hours."""
    from outreach.store import get_config

    keys = {"ig": "ig_reply_probe_at", "wa": "wa_reply_probe_at"}
    stale: list[str] = []
    for channel, key in sorted(keys.items()):
        if not _channel_live(db, channel):
            continue  # a paused lane's probe is supposed to be silent
        stamp = _parse(get_config(db, key))
        if stamp is None:
            continue  # never probed: nothing to be stale against
        age = (now - stamp).total_seconds()
        if age > PROBE_STALE_S:
            stale.append(f"[{channel}] last probe {int(age // 3600)}h"
                         f" {int(age % 3600 // 60)}m ago")
    return "; ".join(stale) or None


def _channel_live(db: "Database", channel: str) -> bool:
    """Is this lane sending at all? A lane the owner switched OFF has a stale
    probe BY DESIGN: live 2026-11-15, the first real sweep alarmed on a WhatsApp
    probe 305 h old because WA has been paused since 2026-11-02."""
    from outreach.store import get_config

    if channel == "wa":
        return not bool(get_config(db, "wa_paused"))
    return True


def _transport_holds(db: "Database", now: datetime) -> str | None:
    """A lead parked in TRANSPORT_HOLD today: a DM surface would not load."""
    day = _as_utc(now).astimezone(PKT).strftime("%Y-%m-%d")
    rows = db.conn.execute(
        "SELECT reason FROM state_history WHERE to_state = 'TRANSPORT_HOLD'"
        " AND substr(created_at, 1, 10) = ?",
        (day,),
    ).fetchall()
    if not rows:
        return None
    reason = str(rows[0]["reason"] or "")[:120]
    return f"{len(rows)} lead(s) in TRANSPORT_HOLD today" + (
        f" ({reason})" if reason else ""
    )


def _cap_early(
    db: "Database", now: datetime, open_regions: tuple[str, ...]
) -> str | None:
    """Whole fleet spent before the open band's midpoint: demand > capacity."""
    from outreach.accounts import cap_for, get_account, sends_today

    lanes = db.conn.execute(
        "SELECT id FROM accounts WHERE channel = 'ig' ORDER BY id"
    ).fetchall()
    capacity = 0
    spent = 0
    for row in lanes:
        account = get_account(db, str(row["id"]))
        cap = cap_for(db, account, now)
        if cap <= 0:
            continue  # a warming lane is not capacity yet
        capacity += cap
        spent += min(sends_today(db, str(row["id"]), "ig", now), cap)
    if capacity == 0 or spent < capacity:
        return None
    midpoint = _earliest_midpoint(db, now, open_regions)
    if midpoint is None or now >= midpoint:
        return None
    pkt = midpoint.astimezone(PKT)
    return (
        f"every lane at cap ({spent}/{capacity}) before"
        f" {pkt.strftime('%H:%M')} PKT"
    )


def _earliest_midpoint(
    db: "Database", now: datetime, open_regions: tuple[str, ...]
) -> datetime | None:
    """Midpoint of the earliest-ending open band, in real time."""
    from outreach.store import get_config

    windows = get_config(db, "session_windows") or {}
    pkt_now = _as_utc(now).astimezone(PKT)
    best: datetime | None = None
    for region in open_regions:
        rule = windows.get(region)
        if not rule:
            continue
        try:
            start_h, start_m = (int(p) for p in str(rule["start"]).split(":"))
            end_h, end_m = (int(p) for p in str(rule["end"]).split(":"))
        except (KeyError, ValueError):
            continue
        start = pkt_now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        end = pkt_now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        if end <= start:
            continue
        midpoint = start + (end - start) / 2
        candidate = midpoint.astimezone(timezone.utc)
        if best is None or candidate < best:
            best = candidate
    return best
