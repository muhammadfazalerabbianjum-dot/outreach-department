"""Startup readiness (B26): the boot gate that proves the stack is up.

Motivation — live incident 2026-10-31 04:55 PKT: the PC booted, both
Startup entries existed (``outreach_engine_startup.cmd`` +
``Ollama.lnk``), yet Ollama was dead hours later and nothing noticed —
the owner's send windows passed while the apps simply "didn't turn on".
The watchdog (B22) only restarts a GONE engine every five minutes;
nothing ever proved AT BOOT that:

  * Ollama answers on :11434 (the AI legs park silently without it), and
  * an engine process exists at all.

Law (B26):
  1. probe first — Ollama is spawned only when it is really down (after
     a grace period so the existing ``Ollama.lnk`` instance may finish
     starting), then re-probed until it answers. Bounded wait; never
     assumed, never double-launched while it is healthy.
  2. a leased pid that is ALIVE is never touched (ADR-13: two engines =
     Outreach profile contention). A DEAD or MISSING lease always
     respawns — even when the old heartbeat promise still looks fresh,
     because a dead process cannot keep a promise. This is the
     boot/resume hole the watchdog's staleness check cannot see.
  3. exactly ONE digest row per boot (``STARTUP READINESS: ...``), so
     the verdict is visible to the owner instead of silent.
  4. a readiness failure reports (digest row + non-zero exit) and never
     crashes the boot.

The Startup ``.cmd`` (owned by ``outreach.retirement``) boots THIS
module instead of ``outreach.engine_loop`` directly: one entry point,
one verdict.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable

from outreach.heartbeat import lease_path, read_lease
from outreach.watchdog import _pid_alive, spawn_engine

OLLAMA_URL = "http://127.0.0.1:11434/api/tags"
_OLLAMA_GRACE_S = 5.0  # let the Startup Ollama.lnk instance finish first
_OLLAMA_WAIT_S = 90.0  # bounded re-probe window after our own spawn
_POLL_S = 2.0


def _probe_ollama(timeout: float = 2.0) -> bool:
    """True only when Ollama's API actually answers. Any transport failure
    (refused/timeout/5xx) means down — never raises."""
    try:
        with urllib.request.urlopen(OLLAMA_URL, timeout=timeout) as response:
            return int(getattr(response, "status", 200)) < 500
    except Exception:  # noqa: BLE001 — any failure here simply means "down"
        return False


def _ollama_command() -> list[str]:
    """Locate the Ollama executable: PATH first, then the standard
    per-user install. Raises FileNotFoundError when neither exists."""
    found = shutil.which("ollama")
    if found:
        return [found, "serve"]
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        exe = Path(local) / "Programs" / "Ollama" / "ollama.exe"
        if exe.exists():
            return [str(exe), "serve"]
    raise FileNotFoundError("ollama executable not found (PATH/LOCALAPPDATA)")


def _spawn_ollama() -> None:
    """Detach a ``ollama serve`` process, never tied to this console."""
    kwargs: dict = {"cwd": str(Path.home())}
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(_ollama_command(), **kwargs)


def ensure_ollama(
    *,
    probe: Callable[[], bool] | None = None,
    spawn: Callable[[], None] | None = None,
    wait_s: float = _OLLAMA_WAIT_S,
    grace_s: float = _OLLAMA_GRACE_S,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> str:
    """Prove Ollama is answering. Returns ``up`` (already), ``restarted``
    (we spawned it and the probe confirmed it) or ``down`` (bounded wait
    exhausted / no executable). Never raises."""
    probe = probe if probe is not None else _probe_ollama
    spawn = spawn if spawn is not None else _spawn_ollama
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic
    if probe():
        return "up"
    # Grace: the Startup Ollama.lnk instance may still be starting.
    sleep(grace_s)
    if probe():
        return "up"
    try:
        spawn()
    except OSError:
        return "down"
    deadline = monotonic() + wait_s
    while monotonic() < deadline:
        sleep(_POLL_S)
        if probe():
            return "restarted"
    return "down"


def ensure_engine(
    db_path: str | Path,
    *,
    spawn: Callable[[], None] | None = None,
) -> str:
    """Prove an engine process exists. Returns ``alive`` (live leased pid,
    untouched: ADR-13), ``restarted`` (spawned) or
    ``spawn_failed(...)``. A dead or missing lease ALWAYS respawns, even
    when the heartbeat promise looks fresh."""
    lease = read_lease(lease_path(db_path))
    if lease and _pid_alive(lease.get("pid", -1)):
        return "alive"
    do_spawn = spawn if spawn is not None else (lambda: spawn_engine(db_path))
    try:
        do_spawn()
    except Exception as exc:  # noqa: BLE001 — surface detail, never mask it
        return f"spawn_failed({repr(exc)[:120]})"
    return "restarted"


def run_readiness(
    db_path: str | Path,
    *,
    ollama: Callable[[], str] | None = None,
    engine: Callable[[], str] | None = None,
) -> str:
    """Run both legs, write ONE digest row, return the verdict string.

    The digest write is best-effort: a boot report must never crash the
    boot (the verdict is still printed by ``main``)."""
    ollama_verdict = (ollama or ensure_ollama)()
    engine_verdict = (engine or (lambda: ensure_engine(db_path)))()
    verdict = f"ollama={ollama_verdict} engine={engine_verdict}"
    try:
        from outreach.store import Database

        with Database(db_path) as db:
            with db.conn:
                db.conn.execute(
                    "INSERT INTO digest(body) VALUES (?)",
                    (f"STARTUP READINESS: {verdict}",),
                )
    except Exception as exc:  # noqa: BLE001 — report on stdout instead
        print(f"startup readiness: digest unwritable ({exc!r})", file=sys.stderr)
    return verdict


def main(argv: list[str] | None = None, *, run: Callable[[Path], str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    repo = Path(__file__).resolve().parents[2]
    db_path = repo / "outreach.db"
    if args[:1] == ["--db"] and len(args) > 1:
        db_path = Path(args[1])
    verdict = (run or run_readiness)(db_path)
    print(f"startup readiness: {verdict}")
    healthy = "down" not in verdict and "spawn_failed" not in verdict
    return 0 if healthy else 1


if __name__ == "__main__":  # pragma: no cover — process entry; live-registered
    raise SystemExit(main())
