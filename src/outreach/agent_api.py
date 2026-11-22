"""Read-only JSON surface for a supervising agent (the Chief of Staff).

The supervisor needs to answer, without opening a browser or guessing from a
digest line: is the queue live, what went out, WHO replied on WHICH account and
WHAT they said, what is held, which rungs are pending or expired, how many
leads are dispatch-ready, and whether the scraper leg is keeping up.

Every command here is READ-ONLY (SQLite opened `mode=ro`, no config writes, no
sends, no state transitions) so a supervisor can call it as often as it likes.
Nothing in this module may import the dispatcher, the senders or the engine.

    python -m outreach.agent_api all      # one snapshot, everything below
    python -m outreach.agent_api status
    python -m outreach.agent_api inbox
    python -m outreach.agent_api sends --limit 25
    python -m outreach.agent_api leads --state ROUTED --limit 25
    python -m outreach.agent_api ladder
    python -m outreach.agent_api holds
    python -m outreach.agent_api digest --limit 25
    python -m outreach.agent_api accounts
    python -m outreach.agent_api scraper

Exit codes: 0 ok, 2 usage/unknown command, 1 the DB could not be read.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(__file__).resolve().parents[2] / "outreach.db"


def _connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"outreach db not found: {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params)]


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def _config(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    raw = _scalar(conn, "SELECT value FROM config WHERE key=?", (key,))
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_hours(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return round((now - moment).total_seconds() / 3600.0, 1)


def _last_halt(conn: sqlite3.Connection) -> dict | None:
    halts = _config(conn, "halts", []) or []
    if not isinstance(halts, list) or not halts:
        return None
    return halts[-1] if isinstance(halts[-1], dict) else None


def _engine(now: datetime) -> dict:
    """Engine liveness from the watchdog's own files (never a guess)."""
    try:
        from outreach.heartbeat import heartbeat_stale, read_heartbeat, read_lease
    except Exception:  # noqa: BLE001 - heartbeat is optional for a snapshot
        return {"state": "unknown"}
    try:
        beat = read_heartbeat()
        lease = read_lease()
        return {
            "state": "stale" if heartbeat_stale(now) else "alive",
            "heartbeat": beat,
            "pid": (lease or {}).get("pid") if isinstance(lease, dict) else None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"state": "unknown", "error": repr(exc)[:160]}


# --- snapshots ---------------------------------------------------------------


def status(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    now = now or _now()
    today = now.date().isoformat()
    caps = _config(conn, "daily_caps", {}) or {}
    sent_today = {
        str(r["channel"]): int(r["n"])
        for r in _rows(
            conn,
            "SELECT channel, COUNT(*) n FROM message_log"
            " WHERE substr(sent_at,1,10)=? GROUP BY channel",
            (today,),
        )
    }
    ready = {
        str(r["region"]): int(r["n"])
        for r in _rows(
            conn,
            "SELECT region, COUNT(*) n FROM leads WHERE state IN"
            " ('QUALIFIED','ROUTED') AND previously_contacted=0"
            " AND region IS NOT NULL GROUP BY region",
        )
    }
    unread = _scalar(
        conn, "SELECT COUNT(*) FROM owner_inbox WHERE acknowledged_at IS NULL"
    )
    oldest_unread_h = _age_hours(
        _scalar(
            conn,
            "SELECT MIN(created_at) FROM owner_inbox"
            " WHERE acknowledged_at IS NULL",
        ),
        now,
    )
    by_state = {
        str(r["state"]): int(r["n"])
        for r in _rows(conn, "SELECT state, COUNT(*) n FROM leads GROUP BY state")
    }
    return {
        "generated_at": now.isoformat(),
        "queue_status": _config(conn, "queue_status"),
        "template_status": _config(conn, "template_status"),
        "variant_engine": _config(conn, "variant_engine"),
        "wa_paused": bool(_config(conn, "wa_paused")),
        "email_enabled": bool(_config(conn, "email_enabled")),
        "last_halt": _last_halt(conn),
        "next_send_after": _config(conn, "next_send_after"),
        "dispatch_backoff_until": _config(conn, "dispatch_backoff_until"),
        "caps": caps,
        "sent_today": sent_today,
        "remaining_today": {
            str(ch): max(0, int(cap) - int(sent_today.get(str(ch), 0)))
            for ch, cap in caps.items()
        },
        "dispatch_ready_by_region": ready,
        "dispatch_ready_total": sum(ready.values()),
        "low_lead_threshold": int(_config(conn, "low_lead_threshold", 10)),
        "leads_by_state": by_state,
        "dish_leads_ready": int(
            _scalar(
                conn,
                "SELECT COUNT(*) FROM leads WHERE state='ROUTED'"
                " AND coalesce(extracted_dish,'')!=''",
            )
            or 0
        ),
        "open_holds": int(
            _scalar(
                conn,
                "SELECT COUNT(*) FROM proofread_log WHERE vetoed=1"
                " AND cleared_at IS NULL",
            )
            or 0
        ),
        "unread_replies": int(unread or 0),
        "oldest_unread_hours": oldest_unread_h,
        "pending_rungs": int(
            _scalar(conn, "SELECT COUNT(*) FROM followups WHERE status='pending'") or 0
        ),
        "expired_rungs": int(
            _scalar(conn, "SELECT COUNT(*) FROM followups WHERE status='expired'") or 0
        ),
        "last_send": _rows(
            conn,
            "SELECT lead_id, channel, account_id, dish, sent_at, body"
            " FROM message_log ORDER BY id DESC LIMIT 1",
        ),
        "engine": _engine(now),
    }


def inbox(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Every unacknowledged reply: which account, which handle, what was said."""
    now = now or _now()
    rows = _rows(
        conn,
        "SELECT i.id, i.lead_id, l.name AS lead_name, i.channel,"
        " i.account_id, i.sender_handle, i.reply_text, i.body, i.created_at,"
        " i.acknowledged_at, l.instagram, l.phone_e164, l.email"
        " FROM owner_inbox i LEFT JOIN leads l ON l.id = i.lead_id"
        " ORDER BY i.created_at DESC, i.id DESC",
    )
    for row in rows:
        row["age_hours"] = _age_hours(row["created_at"], now)
    unread = [r for r in rows if not r["acknowledged_at"]]
    return {
        "generated_at": now.isoformat(),
        "unread_count": len(unread),
        "oldest_unread_hours": max(
            (r["age_hours"] or 0.0 for r in unread), default=None
        ),
        "replies": rows,
    }


def sends(conn: sqlite3.Connection, limit: int = 25) -> dict:
    return {
        "sends": _rows(
            conn,
            "SELECT id, lead_id, channel, account_id, dish, sent_at, body"
            " FROM message_log ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )
    }


def leads(conn: sqlite3.Connection, state: str | None = None, limit: int = 25) -> dict:
    sql = (
        "SELECT id, name, city, region, state, route_channel, account_id,"
        " extracted_dish, previously_contacted, instagram, phone_e164, email,"
        " icp_score, reject_reason, updated_at FROM leads"
    )
    params: tuple = ()
    if state:
        sql += " WHERE state=?"
        params = (state,)
    sql += " ORDER BY id DESC LIMIT ?"
    return {"leads": _rows(conn, sql, params + (int(limit),))}


def ladder(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    now = now or _now()
    rows = _rows(
        conn,
        "SELECT f.id, f.lead_id, l.name AS lead_name, f.touch, f.channel,"
        " f.due_at, f.status, f.expired_reason"
        " FROM followups f LEFT JOIN leads l ON l.id=f.lead_id"
        " ORDER BY f.due_at",
    )
    for row in rows:
        row["overdue_hours"] = (
            -_age_hours(row["due_at"], now) if row["status"] == "pending" else None
        )
    return {"followups": rows}


def holds(conn: sqlite3.Connection) -> dict:
    return {
        "holds": _rows(
            conn,
            "SELECT id, lead_id, variant, dish, layers, reason, body, created_at"
            " FROM proofread_log WHERE vetoed=1 AND cleared_at IS NULL"
            " ORDER BY id DESC",
        )
    }


def digest(conn: sqlite3.Connection, limit: int = 25) -> dict:
    return {
        "digest": _rows(
            conn,
            "SELECT id, created_at, body FROM digest ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )
    }


def accounts(conn: sqlite3.Connection) -> dict:
    return {
        "accounts": _rows(
            conn,
            "SELECT id, channel, status, ramp_week, warmup_start, last_ok_at"
            " FROM accounts ORDER BY id",
        ),
        "recent_health": _rows(
            conn,
            "SELECT account_id, signal, detail, action, created_at"
            " FROM account_health ORDER BY id DESC LIMIT 20",
        ),
    }


def scraper(conn: sqlite3.Connection) -> dict:
    """Scraper stock as the pipeline sees it, plus ingest bookkeeping."""
    from outreach.intake import list_scraper_runs, pending_runs
    from outreach.scraper_adapter import SCRAPER_DB

    runs: dict[str, int] = {}
    pending: dict[str, int] = {}
    error = ""
    try:
        runs = list_scraper_runs()
    except Exception as exc:  # noqa: BLE001 - a snapshot must not crash
        error = repr(exc)[:200]
    try:
        pending = pending_runs(_DatabaseShim(conn))
    except Exception as exc:  # noqa: BLE001
        error = error or repr(exc)[:200]
    return {
        "scraper_db": str(SCRAPER_DB),
        "scraper_db_mtime": (
            datetime.fromtimestamp(SCRAPER_DB.stat().st_mtime, timezone.utc).isoformat()
            if SCRAPER_DB.exists()
            else None
        ),
        "runs": runs,
        "pending_runs": pending,
        "ingested_runs": _config(conn, "ingested_runs", {}),
        "leg_starts": _config(conn, "leg_starts", {}),
        "auto_restart_leg": bool(_config(conn, "auto_restart_leg", False)),
        "error": error or None,
    }


class _DatabaseShim:
    """`pending_runs` only reads `config`; this keeps the API read-only."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> "_DatabaseShim":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def all_snapshot(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    now = now or _now()
    return {
        "status": status(conn, now),
        "inbox": inbox(conn, now),
        "holds": holds(conn),
        "ladder": ladder(conn, now),
        "accounts": accounts(conn),
        "scraper": scraper(conn),
    }


COMMANDS = {
    "status": lambda c: status(c),
    "inbox": lambda c: inbox(c),
    "sends": lambda c: sends(c),
    "leads": lambda c: leads(c),
    "ladder": lambda c: ladder(c),
    "holds": lambda c: holds(c),
    "digest": lambda c: digest(c),
    "accounts": lambda c: accounts(c),
    "scraper": lambda c: scraper(c),
    "all": lambda c: all_snapshot(c),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m outreach.agent_api")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--state", default=None)
    args = parser.parse_args(argv)
    try:
        conn = _connect(Path(args.db))
    except (OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    try:
        command = args.command
        if command == "sends":
            payload = sends(conn, args.limit)
        elif command == "leads":
            payload = leads(conn, args.state, args.limit)
        elif command == "digest":
            payload = digest(conn, args.limit)
        else:
            payload = COMMANDS[command](conn)
    finally:
        conn.close()
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
