"""O6 (2026-11-14): targeting policy — fewer sends, better hit rate.

The owner's brief: stop spending the cap on delivery-only kitchens, chains and
brand accounts; go dine-in first. Every rule is config-driven and the whole
policy ships **off** (`targeting_policy.enabled` false), because parking a lead
changes who receives a message.

A parked lead moves to `DEPRIORITIZED`: not terminal, no dispatch path reads it,
and one repair edge returns it to `ROUTED`. The refusal is always visible
(`PARKED [targeting:<rule>] lead N`, throttled per lead) and counted, so "fewer
sends" reads as a decision rather than as a supply shortage.

`is_on_delivery_platform` alone is NOT delivery-only: many dine-in restaurants
are also on delivery apps, so that rule additionally requires an explicit
`has_dine_in == False` and is off by default.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from outreach.store import get_config, set_config

if TYPE_CHECKING:
    from outreach.store import Database

TARGETING_KEY = "targeting_policy"
DEFAULT_MAX_FOLLOWERS = 5000
_REPEAT_S = 1800.0

DEFAULTS: dict[str, Any] = {
    "enabled": False,  # owner arms it: this decides who is messaged
    "max_followers": DEFAULT_MAX_FOLLOWERS,
    "dine_in_first_categories": [],  # owner's list; empty = rule inert
    "park_chain": True,
    "park_ghost_kitchen": True,
    "park_delivery_only": False,  # needs has_dine_in == False, see module docstring
}


def ensure_targeting_config(db: "Database") -> None:
    if get_config(db, TARGETING_KEY) is None:
        set_config(db, TARGETING_KEY, dict(DEFAULTS))


def policy(db: "Database") -> dict[str, Any]:
    raw = get_config(db, TARGETING_KEY) or {}
    merged = dict(DEFAULTS)
    merged.update({k: raw[k] for k in DEFAULTS if k in raw})
    return merged


def targeting_armed(db: "Database") -> bool:
    return bool(policy(db)["enabled"])


def _payload(row: Any) -> dict[str, Any]:
    try:
        payload = json.loads(row["data_json"] or "{}") or {}
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def disqualify_reason(db: "Database", row: Any) -> str | None:
    """The rule that parks this lead, or None when it may be worked.

    Pure: reads config + the lead's own scraper payload. No writes, no clock.
    """
    cfg = policy(db)
    if not cfg["enabled"]:
        return None
    payload = _payload(row)

    category = str(payload.get("category") or "").strip().lower()
    categories = {
        str(c).strip().lower()
        for c in (cfg.get("dine_in_first_categories") or [])
        if str(c).strip()
    }
    if category and category in categories:
        return f"dine_in_first_category:{category}"

    followers = payload.get("ig_followers")
    try:
        count = int(followers) if followers is not None else None
    except (TypeError, ValueError):
        count = None
    if count is not None and count > int(cfg["max_followers"]):
        return f"brand_account:{count}"

    if cfg["park_chain"] and payload.get("is_chain"):
        return "chain"
    if cfg["park_ghost_kitchen"] and payload.get("is_ghost_kitchen"):
        return "ghost_kitchen"
    if cfg["park_delivery_only"] and _delivery_only(payload):
        return "delivery_only"
    return None


def _delivery_only(payload: dict[str, Any]) -> bool:
    """Delivery with no dine-in, from whichever flag the scraper actually filled.

    Measured 2026-11-14 against the scraper cache: `service_options` is the
    field Google fills (`dine_in` / `delivery`, often null), and an explicit
    `has_dine_in` is not present. A null is UNKNOWN, never "no dine-in" —
    treating it as false would park every restaurant whose flags were not
    scraped that run.
    """
    options = payload.get("service_options")
    if isinstance(options, dict):
        dine_in = options.get("dine_in")
        delivery = options.get("delivery")
        if dine_in is False and (delivery is True or payload.get("is_on_delivery_platform")):
            return True
        if dine_in is True:
            return False
    return bool(
        payload.get("is_on_delivery_platform") and payload.get("has_dine_in") is False
    )


def park(db: "Database", lead_id: int, reason: str, now: datetime) -> bool:
    """Move the lead to DEPRIORITIZED and record why (throttled per lead)."""
    from outreach.machine import transition

    transition(
        db, lead_id, "DEPRIORITIZED", reason=reason, actor="targeting", at=now
    )
    prefix = f"PARKED [targeting:{reason}] lead {lead_id}"
    since = (now - timedelta(seconds=_REPEAT_S)).strftime("%Y-%m-%dT%H:%M:%S")
    existing = db.conn.execute(
        "SELECT 1 FROM digest WHERE substr(body, 1, ?) = ?"
        " AND substr(created_at, 1, 19) >= ? LIMIT 1",
        (len(prefix), prefix, since),
    ).fetchone()
    if not existing:
        with db.conn:
            db.conn.execute(
                "INSERT INTO digest(body) VALUES (?)",
                (f"{prefix}: targeting policy moved it out of the send queue",),
            )
    return True


def repair(db: "Database", lead_id: int, at: datetime | None = None) -> str:
    """Return a parked lead to ROUTED (the one repair edge back)."""
    from outreach.machine import transition

    return transition(
        db, lead_id, "ROUTED", reason="targeting repair", actor="targeting", at=at
    )
