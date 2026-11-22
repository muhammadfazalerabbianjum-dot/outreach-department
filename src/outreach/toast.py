"""Owner toast (B21, Q11 opt-in): local Windows balloon on new digest rows.

Every digest writer (REPLIED handover, watcher alerts, engine/watcher
errors, retirement import) lands in the `digest` table, so the drain reads
THAT table - one seam catches them all. Config-flagged via `owner_toast`
(default ON: the owner opted in on 2026-10-26; flip the row to 0 to
disable), throttled to one popup per 600s, and the stamp always advances
(the digest row is the durable record; the toast is best-effort sugar).
ADR-12 compliant: a local powershell NotifyIcon balloon - no accounts, no
cloud, no listeners, nothing leaves the machine.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from outreach.store import Database

TOAST_FLAG = "owner_toast"     # 0 = silent; absent = ON (owner opted in)
STAMP_ID = "last_toasted_id"   # digest ids <= this were already offered
STAMP_AT = "toast_last_at"     # UTC ISO of the last real send attempt
_COOLDOWN_S = 600              # max one popup per 10 minutes (error loops)
_MAX_BODY = 200                # balloon text limit is ~255 chars

# NotifyIcon balloon: dependency-free (Windows built-in assemblies), no
# AUMID registration, no window flash (CREATE_NO_WINDOW). The icon lives
# 9s so the balloon is not cancelled on dispose.
_PS_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "Add-Type -AssemblyName System.Drawing;"
    "$n = New-Object System.Windows.Forms.NotifyIcon;"
    "$n.Icon = [System.Drawing.SystemIcons]::Information;"
    "$n.Visible = $true;"
    "$n.ShowBalloonTip(8000, 'Outreach department', $env:OUTREACH_TOAST_MSG,"
    " [System.Windows.Forms.ToolTipIcon]::Info);"
    "Start-Sleep -Seconds 9;$n.Dispose()"
)


def sendast(message: str) -> bool:
    """Pop one local balloon. Best-effort: returns False on ANY failure
    (shell missing, timeout, odd session) - never raises."""
    text = message[:_MAX_BODY] or "digest alert"
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             _PS_SCRIPT],
            capture_output=True,
            timeout=20,
            env={**os.environ, "OUTREACH_TOAST_MSG": text},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001 - toast faults never propagate
        return False


def _cooldown_passed(db: "Database", now: datetime) -> bool:
    from outreach.store import get_config

    last_at = get_config(db, STAMP_AT)
    if not last_at:
        return True
    try:
        prev = datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=timezone.utc)
    return (now - prev).total_seconds() >= _COOLDOWN_S


def drain_new_digest_toasts(
    db: "Database",
    sender: Callable[[str], bool] | None = None,
) -> int:
    """Offer every digest row newer than the stamp to the toast sender.

    Returns the number of popups actually SENT. The stamp advances for
    every offered row in all cases (disabled, cooldown, failure): the
    digest row remains the durable record and re-popping old rows would
    only add noise. Cooldown is stamped per drain-attempt so a broken
    shell retries at most once per window, not once per cycle.
    """
    from outreach.store import get_config, set_config

    sender = sendast if sender is None else sender
    last = int(get_config(db, STAMP_ID, 0) or 0)
    rows = db.conn.execute(
        "SELECT id, body FROM digest WHERE id > ? ORDER BY id", (last,)
    ).fetchall()
    if not rows:
        return 0

    enabled = bool(int(get_config(db, TOAST_FLAG, 1) or 0))
    now = datetime.now(timezone.utc)
    if not enabled or not _cooldown_passed(db, now):
        set_config(db, STAMP_ID, int(rows[-1]["id"]))
        return 0

    # ONE balloon per pass (LIVE LESSONS 2026-11-02): offering every
    # unread row stacked popups back to back and an error storm flooded
    # the owner's desktop. The oldest unread row goes now; the stamp
    # stops there so later passes (behind the cooldown) walk the backlog
    # one at a time — nothing is dropped, nothing stacks.
    offered = rows[0]
    sent = 0
    try:
        if sender(offered["body"]):
            sent = 1
    except Exception:  # noqa: BLE001 - a row must not sink the drain
        sent = 0
    set_config(db, STAMP_AT, now.isoformat())
    set_config(db, STAMP_ID, int(offered["id"]))
    return sent


def drain_engine_toasts(
    db_path: str | Path,
    sender: Callable[[str], bool] | None = None,
) -> int:
    """Engine-loop entry: open the DB and drain. NEVER raises - a toast
    fault must not park the loop (the digest row is the record)."""
    from outreach.store import Database

    try:
        with Database(db_path) as db:
            return drain_new_digest_toasts(db, sender=sender)
    except Exception:  # noqa: BLE001 - even an unopenable DB is swallowed
        return 0
