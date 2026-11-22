"""Engine heartbeat + run lease (built 2026-10-27 overnight).

Motivation: the engine died SILENTLY between 14:06–15:47 UTC — no ENGINE
ERROR, no Windows crash event — and nothing noticed for two hours. The
engine now stamps `.heartbeat.json` after every cycle carrying the instant
it promised to run again (`next_wake_at`), and `outreach.watchdog` (a
Task Scheduler check) restarts a GONE engine whose promise has expired.

Law: atomic writes (temp + rename) everywhere; a heartbeat is never a
reply/send fact, only a liveness fact.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

HEARTBEAT_NAME = ".heartbeat.json"
LEASE_NAME = ".engine_lease.json"


def heartbeat_path(db_path: str | Path | None) -> Path | None:
    """The heartbeat lives NEXT TO the database (repo root in production,
    the tmp dir in tests — no test can ever touch the real file)."""
    if db_path is None:
        return None
    return Path(db_path).resolve().parent / HEARTBEAT_NAME


def write_heartbeat(
    path: str | Path,
    *,
    stamped_at: datetime,
    next_wake_at: datetime | None,
    stage: str | None = None,
    stages: Sequence[dict[str, Any]] | None = None,
) -> None:
    """Atomically record the liveness promise (temp + rename).

    `stages` is the current cycle's stage trace (`[{"stage", "seconds"}]`),
    written by the ONE heartbeat writer in `engine_loop`. 2026-11-13 (R2):
    a single `stage` string could only name the last thing that ran, so a
    slow refresh and a slow dispatch looked identical from the file.
    """
    file_path = Path(path)
    payload: dict[str, Any] = {
        "stamped_at": stamped_at.astimezone(timezone.utc).isoformat(),
        "next_wake_at": (
            next_wake_at.astimezone(timezone.utc).isoformat()
            if next_wake_at is not None
            else None
        ),
    }
    if stage is not None:
        # 2026-11-13 (audit F4): a slow tick and a stalled tick looked the same.
        payload["stage"] = stage
    if stages is not None:
        payload["stages"] = [dict(entry) for entry in stages]
    tmp = file_path.with_name(file_path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, file_path)


def lease_path(db_path: str | Path | None) -> Path | None:
    """The lease lives NEXT TO the database (same rule as the heartbeat)."""
    if db_path is None:
        return None
    return Path(db_path).resolve().parent / LEASE_NAME


def read_heartbeat(path: str | Path) -> dict[str, Any] | None:
    """Parsed heartbeat, or None when missing/corrupt (never raises)."""
    file_path = Path(path)
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def heartbeat_stale(
    beat: dict[str, Any] | None, now: datetime, grace_s: float = 900.0
) -> bool:
    """True when the engine's promise expired more than `grace_s` ago.

    Missing/corrupt heartbeat counts as stale (a dead-or-never-started
    engine must be restartable), and `next_wake_at` wins over the stamp:
    during a long closed-window sleep the stamp is naturally old while
    the promise is still in the future.
    """
    if not beat:
        return True
    target = _parse(beat.get("next_wake_at")) or _parse(beat.get("stamped_at"))
    if target is None:
        return True
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return (now - target).total_seconds() > grace_s


def write_lease(path: str | Path, *, pid: int, started_at: datetime) -> None:
    """Atomically record which process claims to be THE engine."""
    file_path = Path(path)
    payload = {
        "pid": int(pid),
        "started_at": started_at.astimezone(timezone.utc).isoformat(),
    }
    tmp = file_path.with_name(file_path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, file_path)


def read_lease(path: str | Path) -> dict[str, Any] | None:
    """Parsed lease, or None when missing/corrupt (never raises)."""
    file_path = Path(path)
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
        return None
    return data
