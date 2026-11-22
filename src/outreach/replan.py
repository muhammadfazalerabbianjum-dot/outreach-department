"""O9 (2026-11-14): what happens to a touch whose window the machine slept through.

Three layers answer "the PC was off":

1. **Restart** — the watchdog task plus the boot launcher (R4) bring the lane back.
2. **Missed window** — `dispatcher.reconcile_window_extensions` grants one ≤90 min
   recovery band (B27), already pinned.
3. **Missed TOUCH** — the real gap. A comment made at 15:00 whose 12–48 h window
   falls while the machine is off must not fire as a stale DM at 09:00 the next
   day. `warmup.comment_state` already refuses that claim (O3); this module makes
   the situation VISIBLE: on boot, before any claim, every lead whose comment
   expired gets one `REPLAN` row explaining that it will be re-commented rather
   than messaged cold.

Nothing here changes a lead's state: the stage owns re-commenting, the claim
owns refusing. This is the record that lets the owner see a 14-hour outage in the
digest as N replanned leads instead of silence.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from outreach.store import get_config, set_config

if TYPE_CHECKING:
    from outreach.store import Database

REPLAN_KEY = "replan_last_pass"
_REPEAT_S = 6 * 3600.0  # a lead is named at most every 6 h


def stale_leads(db: "Database", now: datetime) -> list[int]:
    """ROUTED leads whose comment expired while nobody was looking."""
    from outreach.warmup import comment_state

    ids = [
        int(row[0])
        for row in db.conn.execute(
            "SELECT id FROM leads WHERE state='ROUTED' ORDER BY id"
        )
    ]
    return [lead_id for lead_id in ids if comment_state(db, lead_id, now) == "stale"]


def replan_stale(db: "Database", now: datetime) -> list[int]:
    """Name every stale lead once, and say what will happen to it.

    Idempotent and throttled: the per-lead stamp lives in config, so a restart
    loop cannot bury the digest. Returns the leads named in THIS pass.
    """
    stale = stale_leads(db, now)
    named: list[int] = []
    for lead_id in stale:
        key = f"replan_at:{lead_id}"
        stored = get_config(db, key)
        if stored:
            try:
                last = datetime.fromisoformat(str(stored))
            except ValueError:
                last = None
            if last is not None and (now - last).total_seconds() < _REPEAT_S:
                continue
        with db.conn:
            db.conn.execute(
                "INSERT INTO digest(body) VALUES (?)",
                (
                    f"REPLAN lead {lead_id}: its comment expired past the 12-48 h"
                    " window — the engagement stage re-comments before any DM"
                    " (no stale touch will be sent)",
                ),
            )
        set_config(db, key, now.isoformat())
        named.append(lead_id)
    set_config(db, REPLAN_KEY, now.isoformat())
    return named
