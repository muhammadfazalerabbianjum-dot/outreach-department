"""Watchdog (B22): the Task Scheduler check that restarts a GONE engine.

Motivation — live incident 2026-10-28: the engine died mid-day with NO
ENGINE ERROR row, and nothing noticed for hours; entire send windows
passed in silence until a human looked. The engine now stamps
`.heartbeat.json` after every cycle (the instant it promises to run again)
and `.engine_lease.json` (its pid). This check runs every five minutes
under Task Scheduler and re-spawns the loop ONLY when the promise expired
AND the leased pid is gone:

  * alive promise        -> do nothing (the common case, silent);
  * promise gone + pid alive -> a HUNG engine — never double-spawn (two
    engines = Outreach profile contention, ADR-13); the digest already
    carries its ENGINE ERRORs;
  * promise gone + pid dead (or no lease) -> GONE: spawn a detached
    engine and write a WATCHDOG digest row (visible), throttled so a
    task re-run never stacks spawns.

No listener, no secrets, no network — one local process check + one
detached spawn. `--install` / `--uninstall` register the Task Scheduler
entry (`schtasks`), mirroring the B17 Startup `.cmd` registration.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from outreach.heartbeat import (
    heartbeat_path,
    heartbeat_stale,
    lease_path,
    read_heartbeat,
    read_lease,
)

TASK_NAME = "OutreachEngineWatchdog"
ENGINE_LOG_NAME = "engine.log"
ENGINE_LOG_MAX_BYTES = 5 * 1024 * 1024


def engine_log_path(db_path: str | Path) -> Path:
    return Path(db_path).resolve().parent / "logs" / ENGINE_LOG_NAME


def open_engine_log(db_path: str | Path):
    """Open the engine's append-only trace, rotating it first when oversized.

    LIVE 2026-11-04: the engine died twice (leased pids 1856 and 3416) and left
    nothing behind, because a detached pythonw process inherits no console and
    both streams went nowhere. Every spawn now writes both streams here, so
    the next death has a traceback instead of a mystery.
    """
    path = engine_log_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > ENGINE_LOG_MAX_BYTES:
        os.replace(path, path.with_suffix(path.suffix + ".1"))
    return path.open("a", encoding="utf-8", errors="replace")
_RESTART_THROTTLE_S = 900.0  # one spawn at most per 15 min
_PROCESS_QUERY_LIMITED = 0x1000  # Windows OpenProcess right (no kill rights)


_STILL_ACTIVE = 259  # GetExitCodeProcess sentinel
# Boot-to-lease slack: the lease is written moments after the process starts,
# and a long-lived process is still the same process. 15 min is orders of
# magnitude tighter than pid reuse (Windows cycles thousands of pids first).
_CREATE_TIME_TOLERANCE_S = 900.0


def _process_started_at(handle: int) -> float | None:
    """Process creation time as a POSIX timestamp, or None when unreadable."""
    import ctypes
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD)]

    kernel32 = ctypes.windll.kernel32
    created, _exited, _k, _u = (_FILETIME() for _ in range(4))
    ok = kernel32.GetProcessTimes(
        handle,
        ctypes.byref(created), ctypes.byref(_exited),
        ctypes.byref(_k), ctypes.byref(_u),
    )
    if not ok:
        return None
    ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
    return ticks / 10_000_000 - 11644473600  # FILETIME epoch -> POSIX


def _pid_alive(pid: int, *, started_at: str | datetime | None = None) -> bool:
    """True when `pid` names the SAME process the lease was written for.

    LIVE LESSON 2026-11-03 (kill-test): a bare OpenProcess probe called a
    dead process alive twice — (a) when a parent still held the exited
    process's handle (the kernel object outlives the exit), and (b) when a
    stale lease's pid had been REUSED by an unrelated process. Both make
    the watchdog believe a HUNG engine is holding the lane, so a GONE
    engine is never respawned: silent death, the exact brick this module
    exists to prevent. The probe now reads the exit code and, when the
    lease carries `started_at`, matches the process creation instant.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            if code.value != _STILL_ACTIVE:
                return False  # exited; the handle merely outlived it
            when = _start_instant(started_at)
            if when is not None:
                created = _process_started_at(handle)
                if created is not None:
                    return abs(created - when) <= _CREATE_TIME_TOLERANCE_S
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _start_instant(value: str | datetime | None) -> float | None:
    """Lease `started_at` (ISO string or datetime) as a POSIX timestamp."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def spawn_engine(db_path: str | Path) -> bool:
    """Detach a fresh engine loop, never tied to this process's console.

    Refuses while the leased pid is still alive: live 2026-11-04 a manual
    spawn raced the watchdog's own restart and two loops ran at once. Two
    engines double-send, so the guard lives here, at the one place that
    starts one (ADR-13). The watchdog's own call path is already guarded by
    its lease read; this closes the manual/raced path too.
    """
    lease = read_lease(lease_path(db_path))
    if lease and _pid_alive(
        lease.get("pid", -1), started_at=lease.get("started_at")
    ):
        return False
    repo = Path(db_path).resolve().parent
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    target = pythonw if pythonw.exists() else exe
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(repo / "src"))
    kwargs: dict = {"cwd": str(repo), "env": env}
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: survives this check
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    log = open_engine_log(db_path)
    try:
        # stderr merged into the same trace: an uncaught exception is the whole
        # point of this file (two blind deaths on 2026-11-04).
        subprocess.Popen(
            [str(target), "-m", "outreach.engine_loop"],
            stdout=log,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
    finally:
        log.close()  # the child owns its inherited copy
    return True


def _recent_watchdog_row(db, now: datetime) -> bool:
    since = (now - timedelta(seconds=_RESTART_THROTTLE_S)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    row = db.conn.execute(
        "SELECT 1 FROM digest WHERE substr(body, 1, 8) = 'WATCHDOG'"
        " AND substr(created_at, 1, 19) >= ? LIMIT 1",
        (since,),
    ).fetchone()
    return row is not None


def check(
    db_path: str | Path,
    *,
    now: datetime | None = None,
    spawn: Callable[[], None] | None = None,
    grace_s: float = 900.0,
) -> str:
    """One watchdog pass. Returns the verdict for the task log:

    ``alive`` | ``pid_alive`` | ``throttled`` | ``restarted`` | ``spawn_failed``
    """
    from outreach.store import Database

    now = now or datetime.now(timezone.utc)
    beat = read_heartbeat(heartbeat_path(db_path))
    if not heartbeat_stale(beat, now, grace_s=grace_s):
        return "alive"  # the engine promised to be back before grace ended

    lease = read_lease(lease_path(db_path))
    if lease and _pid_alive(
        lease.get("pid", -1), started_at=lease.get("started_at")
    ):
        return "pid_alive"  # hung, not gone — no second engine (ADR-13)

    with Database(db_path) as db:
        if _recent_watchdog_row(db, now):
            return "throttled"
        try:
            (spawn or (lambda: spawn_engine(db_path)))()
        except Exception as exc:  # noqa: BLE001 — surface, never mask, a bad spawn
            with db.conn:
                db.conn.execute(
                    "INSERT INTO digest(body) VALUES (?)",
                    (f"WATCHDOG ERROR: spawn failed {repr(exc)[:200]}",),
                )
            return "spawn_failed"
        with db.conn:
            db.conn.execute(
                "INSERT INTO digest(body) VALUES (?)",
                (
                    "WATCHDOG: engine GONE (heartbeat promise expired, leased pid"
                    f" {lease['pid'] if lease else 'unknown'} dead) — restarted",
                ),
            )
    return "restarted"


def _task_command() -> str:
    """The schtasks /TR body: repo cwd + PYTHONPATH, then the check."""
    repo = Path(__file__).resolve().parents[2]
    return (
        f'cmd /c "cd /d {repo} && set PYTHONPATH={repo}\\src'
        f' && pythonw -m outreach.watchdog"'
    )


def _run_schtasks(argv: list[str]) -> int:
    return subprocess.run(argv, capture_output=True, text=True).returncode


def install(*, runner: Callable[[list[str]], int] | None = None) -> int:
    """Register the five-minute Task Scheduler check (idempotent, /F)."""
    run = runner or _run_schtasks
    return run(
        [
            "schtasks",
            "/Create",
            "/F",
            "/TN",
            TASK_NAME,
            "/SC",
            "MINUTE",
            "/MO",
            "5",
            "/TR",
            _task_command(),
        ]
    )


def uninstall(*, runner: Callable[[list[str]], int] | None = None) -> int:
    run = runner or _run_schtasks
    return run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    repo = Path(__file__).resolve().parents[2]
    if args == ["--install"]:
        return install()
    if args == ["--uninstall"]:
        return uninstall()
    verdict = check(repo / "outreach.db")
    print(f"watchdog: {verdict}")
    return 0 if verdict in ("alive", "pid_alive", "restarted") else 1


if __name__ == "__main__":  # pragma: no cover — process entry; live-registered
    raise SystemExit(main())
