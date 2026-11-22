"""R1 (owner ruling 2026-11-14: "probe"): the measured exit from the gate.

The cold-DM gate is a deadlock: a `Delivered` bubble can only come from a send,
and the gate blocks the only sends that produce one. The owner chose the measured
probe over accepting the stall:

  * **3 cold first touches per day**, dish-bearing leads only;
  * each one is a real send on the live lane, watched by the existing
    `ig_sender._observe_delivery` observer;
  * the first sighting fires `DELIVERY CONFIRMED (milestone)` and opens the gate
    permanently — the probe then stops existing, because the gate is open.

What the probe can never do: spend more than its three slots, message a lead it
already probed today, or touch a lead with no dish. A refused slot is not a
message and is never counted as one.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from outreach.caps import pkt_date
from outreach.store import get_config, set_config

if TYPE_CHECKING:
    from outreach.store import Database

CONFIG_KEY = "cold_probe_config"
TODAY_KEY = "cold_probe_today"

DEFAULTS: dict[str, Any] = {
    "enabled": True,  # ruling received 2026-11-14: the probe is the chosen exit
    "per_day": 3,
    "dish_only": True,
}


def config(db: "Database") -> dict[str, Any]:
    raw = get_config(db, CONFIG_KEY) or {}
    merged = dict(DEFAULTS)
    if isinstance(raw, dict):
        merged.update({k: raw[k] for k in DEFAULTS if k in raw})
    return merged


def ensure_config(db: "Database") -> None:
    if get_config(db, CONFIG_KEY) is None:
        set_config(db, CONFIG_KEY, dict(DEFAULTS))


def _today(db: "Database", now: datetime) -> dict[str, Any]:
    day = pkt_date(now if now.tzinfo else now.replace(tzinfo=timezone.utc))
    raw = get_config(db, TODAY_KEY)
    if not isinstance(raw, dict) or raw.get("day") != day:
        return {"day": day, "slots": []}
    slots = raw.get("slots")
    return {"day": day, "slots": list(slots) if isinstance(slots, list) else []}


def slots_used(db: "Database", now: datetime) -> int:
    return len(_today(db, now)["slots"])


def take_slot(db: "Database", lead_id: int, row: Any, now: datetime) -> bool:
    """Reserve one of today's probe sends for this lead, or refuse.

    Called only where the gate would otherwise refuse a cold first touch: a
    `True` here means this send IS the measurement, so it is announced in the
    digest before the browser opens.
    """
    cfg = config(db)
    if not cfg["enabled"]:
        return False
    dish = str(row["extracted_dish"] or "").strip()
    if cfg["dish_only"] and not dish:
        return False
    today = _today(db, now)
    tried = {str(slot[0]) for slot in today["slots"] if slot}
    if str(lead_id) in tried:
        return False  # one measurement per lead per day, never a re-ask
    if len(today["slots"]) >= int(cfg["per_day"]):
        return False
    today["slots"].append([str(lead_id), now.astimezone(timezone.utc).isoformat()])
    set_config(db, TODAY_KEY, today)
    with db.conn:
        db.conn.execute(
            "INSERT INTO digest(body) VALUES (?)",
            (
                f"COLD PROBE {len(today['slots'])}/{cfg['per_day']} lead {lead_id}"
                f" (dish: {dish}): a measured cold first touch, watched for"
                " Delivered - the gate opens on the first sighting",
            ),
        )
    return True


def status_line(db: "Database", now: datetime) -> str:
    """One human line for the owner: how much of today's probe is spent."""
    cfg = config(db)
    used = slots_used(db, now)
    confirmed = bool(get_config(db, "ig_delivery_confirmed"))
    if confirmed:
        return "COLD PROBE: gate already open (a Delivered bubble was seen)"
    if not cfg["enabled"]:
        return "COLD PROBE: off - the gate stays shut and throughput stays at the ladder rate"
    return (
        f"COLD PROBE: {used}/{cfg['per_day']} today, dish-bearing leads only,"
        " watching for the first Delivered"
    )
