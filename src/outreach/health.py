"""v1.1 batch 9: account health, lane isolation and redistribution.

Blueprint batch 9 (gate 14): "account halted mid-window -> its leads
redistributed, other accounts unaffected, zero double-touch (L1 holds through
redistribution)". L8: a ban/challenge/login-wall signal halts THAT account only
and the fleet survives it.

The one non-obvious rule lives in `redistribute`. L1 says one lead, one
account, EVER, so a halted account's leads do not all move:

  * a lead it already TOUCHED (a DM in `message_log`, an engagement act, a
    warm-up) stays assigned to it and parks. Handing it to a healthy account
    would be a second account contacting the same restaurant, which is exactly
    what L1 forbids;
  * a lead it never touched (still ROUTED, no message, no act, no warm-up) is
    released - the assignment row is deleted - so the surviving accounts can
    claim it normally.

A halt is idempotent (one `account_health` row, one owner alert), and a soft
signal (timeout, unreadable page) is recorded without halting anything: a
single slow page is not a ban.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from outreach.accounts import HALTED, cap_for, get_account, sends_today
from outreach.engagement import acts_today
from outreach.store import get_config

if TYPE_CHECKING:
    from outreach.store import Database

#: Signals that mean this account cannot act until a human looks at it.
BLOCKING_SIGNALS = frozenset({"login_wall", "challenge", "checkpoint", "banned"})
#: Signals worth recording that must NOT halt a working account.
SOFT_SIGNALS = frozenset({"timeout", "navigation_error", "unknown"})

#: A lead counts as touched when ANY of these has a row for it.
_TOUCH_TABLES = ("message_log", "engagement_log", "warmups")


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _record(
    db: "Database",
    account_id: str,
    signal: str,
    *,
    detail: str,
    action: str,
    now: datetime | None = None,
) -> None:
    with db.conn:
        db.conn.execute(
            "INSERT INTO account_health(account_id, signal, detail, action,"
            " created_at) VALUES (?, ?, ?, ?, ?)",
            (account_id, signal, detail, action, _now_iso(now)),
        )


def _alert(db: "Database", body: str) -> None:
    with db.conn:
        db.conn.execute("INSERT INTO digest(body) VALUES (?)", (body,))


def untouched_leads(db: "Database", account_id: str) -> list[int]:
    """ROUTED leads this account still owns and has never touched."""
    clauses = " AND ".join(
        f"NOT EXISTS (SELECT 1 FROM {table} t WHERE t.lead_id=l.id)"
        for table in _TOUCH_TABLES
    )
    rows = db.conn.execute(
        "SELECT l.id FROM leads l JOIN lead_accounts la ON la.lead_id=l.id"
        f" WHERE la.account_id=? AND l.state='ROUTED' AND {clauses} ORDER BY l.id",
        (account_id,),
    ).fetchall()
    return [int(r["id"]) for r in rows]


def redistribute(db: "Database", account_id: str) -> int:
    """Release the leads this account never touched; return how many moved."""
    leads = untouched_leads(db, account_id)
    if not leads:
        return 0
    marks = ",".join("?" for _ in leads)
    with db.conn:  # the ledger row and its readable mirror move together
        db.conn.execute(
            f"DELETE FROM lead_accounts WHERE lead_id IN ({marks})", leads
        )
        # leads.account_id is NOT NULL with '' meaning unassigned (the same
        # convention seed_fleet and the senders read).
        db.conn.execute(
            f"UPDATE leads SET account_id='' WHERE id IN ({marks})", leads
        )
    return len(leads)


def halt_account(
    db: "Database",
    account_id: str,
    *,
    reason: str,
    now: datetime | None = None,
    detail: str = "",
) -> int:
    """Halt one account, keep its touched leads, release the rest.

    Returns the number of leads returned to the queue. A second halt of the
    same account is a no-op: no duplicate health row, no second alert, and no
    further redistribution (the first one already happened).
    """
    account = get_account(db, account_id)
    if account is None:
        raise ValueError(f"unknown account {account_id!r}")
    if account["status"] == HALTED:
        return 0
    with db.conn:
        db.conn.execute(
            "UPDATE accounts SET status=? WHERE id=?", (HALTED, account_id)
        )
    _record(db, account_id, reason, detail=detail, action="halted", now=now)
    moved = redistribute(db, account_id)
    _alert(
        db,
        f"ACCOUNT HALTED {account_id} ({reason}): {moved} untouched lead(s)"
        " returned to the queue; the rest of the fleet keeps running.",
    )
    return moved


def report_signal(
    db: "Database",
    account_id: str,
    signal: str,
    *,
    detail: str = "",
    now: datetime | None = None,
) -> str:
    """Feed one health signal in; returns 'halted' or 'recorded'.

    Blocking signals go through `halt_account`, so there is exactly one halt
    path. Soft signals (and an unrecognised signal) are recorded and change
    nothing about the account's status — fail open on the account, because
    parking a working sender on one slow page costs real sends.
    """
    if signal in BLOCKING_SIGNALS:
        halt_account(db, account_id, reason=signal, now=now, detail=detail)
        return "halted"
    if signal in SOFT_SIGNALS:
        _record(db, account_id, signal, detail=detail, action="recorded", now=now)
        return "recorded"
    _record(db, account_id, signal, detail=detail, action="recorded", now=now)
    _alert(db, f"ACCOUNT SIGNAL {account_id}: unknown signal {signal!r} ({detail})")
    return "recorded"


def fleet_health(
    db: "Database", now: datetime, *, root: str | Path | None = None
) -> list[dict[str, Any]]:
    """Per-account status + today's activity, for the dashboard (batch 11).

    `health` is what the DISK says about the account's own browser profile
    (live 2026-11-15: four rows, one working login). It is read, never guessed.
    """
    from outreach.accounts import ACTIVE_STATUSES, profile_health

    rows = db.conn.execute("SELECT * FROM accounts ORDER BY channel, id").fetchall()
    out: list[dict[str, Any]] = []
    for account in rows:
        account_id = str(account["id"])
        out.append(
            {
                "id": account_id,
                "health": profile_health(db, account_id, root=root),
                "channel": account["channel"],
                "profile_dir": account["profile_dir"],
                "status": account["status"],
                "active": account["status"] in ACTIVE_STATUSES,
                "cap": cap_for(db, account, now),
                "sends_today": sends_today(db, account_id, account["channel"], now),
                "acts_today": acts_today(db, account_id, now),
                "last_ok_at": account["last_ok_at"],
            }
        )
    return out


def lane_switches(db: "Database") -> dict[str, bool]:
    """The owner's global switches, so the dashboard never hides them."""
    return {
        "wa_paused": bool(get_config(db, "wa_paused")),
        "ig_halted": bool(get_config(db, "ig_halted")),
        "email_enabled": bool(get_config(db, "email_enabled")),
        "queue_paused": get_config(db, "queue_status") == "PAUSED",
    }
