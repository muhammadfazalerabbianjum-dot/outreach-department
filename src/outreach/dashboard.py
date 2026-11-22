"""v1.1 batch 11: the `fleet` view of the command center.

Blueprint: "command_center gains `fleet` view: per-account status/ramp/today's
sends/engagement counts/alerts + daily-target progress bar + the morning supply
alarm."

Two rules shape it:

  * **Read-only.** Opening the dashboard must never send, alert or write a
    config row. The supply line is computed from the same read the intake
    alarm uses (`dispatch_ready_by_region`) instead of calling
    `scan_low_leads`, which would emit a digest as a side effect;
  * **Never hide a switch.** The owner's global switches (queue, WA pause,
    email arming, IG lane halt) appear on every render next to the numbers,
    so a paused lane can never look like a healthy quiet one.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from outreach.caps import PKT
from outreach.health import fleet_health, lane_switches
from outreach.store import get_config
from outreach.targets import targets

if TYPE_CHECKING:
    from outreach.store import Database

BAR_WIDTH = 10


def _bar(done: int, total: int, width: int = BAR_WIDTH) -> str:
    if total <= 0:
        return "-" * width
    filled = min(width, max(0, round(width * done / total)))
    return "#" * filled + "-" * (width - filled)


def _switch_line(db: "Database") -> str:
    switches = lane_switches(db)
    parts = ["queue PAUSED" if switches["queue_paused"] else "queue LIVE"]
    parts.append("WA paused" if switches["wa_paused"] else "WA live")
    parts.append("email armed" if switches["email_enabled"] else "email paused")
    parts.append("IG lane HALTED" if switches["ig_halted"] else "IG lane live")
    return " | ".join(parts)


def _supply_line(db: "Database") -> str:
    from outreach.intake import dispatch_ready_by_region

    threshold = int(get_config(db, "low_lead_threshold", 10))
    ready = dispatch_ready_by_region(db)
    regions = [
        str(r[0])
        for r in db.conn.execute(
            "SELECT DISTINCT region FROM leads WHERE region IS NOT NULL"
        )
    ]
    starving = [r for r in regions if ready.get(r, 0) < threshold]
    if not starving:
        counts = ", ".join(f"{r}={ready.get(r, 0)}" for r in sorted(regions))
        return f"supply ok: {counts or 'no leads'} (threshold {threshold})"
    counts = ", ".join(f"{r}={ready.get(r, 0)}" for r in sorted(starving))
    return f"LOW LEADS: {counts} (threshold {threshold}) - scraper leg needed"


def _last_signals(db: "Database") -> dict[str, str]:
    rows = db.conn.execute(
        "SELECT account_id, signal FROM account_health a"
        " WHERE id = (SELECT MAX(id) FROM account_health b"
        "             WHERE b.account_id = a.account_id)"
    ).fetchall()
    return {str(r["account_id"]): str(r["signal"]) for r in rows}


def build_fleet(
    db: "Database", now: datetime, *, root: str | Path | None = None
) -> list[str]:
    """The whole fleet on one screen: accounts, targets, switches, supply."""
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    pkt = moment.astimezone(PKT)
    lines = [
        f"FLEET  {pkt.date().isoformat()} {pkt.hour:02d}:{pkt.minute:02d} PKT",
    ]
    accounts = fleet_health(db, now, root=root)
    signals = _last_signals(db)
    for channel, title in (("ig", "IG"), ("wa", "WA")):
        rows = [row for row in accounts if row["channel"] == channel]
        lines.append(title)
        if not rows:
            lines.append("  (no accounts)")
            continue
        for row in rows:
            signal = f"   last signal: {signals[row['id']]}" if row["id"] in signals else ""
            health = {
                "signed_in": "signed in",
                "no_session": "no IG session",
                "missing": "no profile",
                "unknown": "session unknown",
            }.get(str(row.get("health")), "n/a")
            lines.append(
                f"  {row['id']:<6} {row['status']:<13}"
                f" {row['sends_today']}/{row['cap']} sends"
                f"  {row['acts_today']} act(s)"
                f"  profile {row['profile_dir']} ({health}){signal}"
            )
    lines.append("TARGETS")
    goals = targets(db)
    for channel in ("ig", "wa", "email"):
        goal = goals[channel]
        if goal <= 0:
            lines.append(f"  {channel:<5} no target set")
            continue
        # `daily_sends` is the same ledger the cap and the pace alarm read, so
        # the bar can never disagree with either of them.
        from outreach.targets import sent_today

        sent = sent_today(db, channel, now)
        lines.append(
            f"  {channel:<5} [{_bar(sent, goal)}] {sent}/{goal}"
            f"  ({min(100, round(100 * sent / goal))}%)"
        )
    lines.append("SWITCHES")
    lines.append(f"  {_switch_line(db)}")
    lines.append("REPLIES")
    lines.append(f"  UNREAD REPLIES: {_unread_replies(db)}")
    lines.append("SUPPLY")
    lines.append(f"  {_supply_line(db)}")
    return lines


def _unread_replies(db: "Database") -> int:
    """Replies the owner has not acknowledged (10.6: the reachable surface)."""
    from outreach.inbox import unread

    return len(unread(db))
