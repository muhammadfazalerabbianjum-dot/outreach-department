"""Retirement glue (B17): the old reminder's history, the LinkedIn mount.

- ADR-11: the retired reminder app (``OUTREACH_RETIREMENT_STATE``) is
  retired-IN — its fired slot history is imported into THIS engine's
  digest log exactly
  once per file fingerprint, and its login auto-start is switched off
  (`retire_reminder_app` renames the Startup shortcut); the retiree is
  NEVER written to or deleted.
- ADR-14: the LinkedIn poster is a mounted sibling
  (``OUTREACH_LINKEDIN_MOUNT``). This module records the mount point
  and verifies presence — it NEVER imports, configures, or generates
  LinkedIn content. Any new LinkedIn code here = red batch.
- Startup registration: an idempotent .cmd in the user's Startup folder
  boots ``outreach.startup_readiness`` (B26: proves Ollama + engine are
  up before handing off). Tests inject a fake Startup dir; nothing here
  touches the real folder unless the function runs for real.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from outreach.store import get_config, set_config

if TYPE_CHECKING:
    from outreach.store import Database

# Defaults = this machine's layout. Both are overridable so a fork can point
# the retirement glue at its own archive and mount without an edit.
RETIREMENT_STATE = Path(
    os.environ.get("OUTREACH_RETIREMENT_STATE", r"A:\outreach\state.json")
)
LINKEDIN_MOUNT = os.environ.get(
    "OUTREACH_LINKEDIN_MOUNT", r"A:\AI Stuff\automation"
)
_STARTUP_NAME = "outreach_engine_startup.cmd"
# R4 (2026-11-13): the launcher is DEFINED HERE, in source control, and the
# Startup-folder file is a rendered copy of it. Two changes from the B17 form:
#   * an ABSOLUTE interpreter path — a login shell's PATH is not the PATH this
#     lane was tuned against, and a silent `pythonw is not recognized` at 02:00
#     is exactly the "PC rebooted and nothing turned on" failure;
#   * a LOG REDIRECT through `cmd /c` — pythonw discards stderr, so the two
#     blind deaths of 2026-11-04 left no traceback anywhere.
# The window stays minimized (`start /min`) so a 90 s readiness wait never
# blocks the login desktop.
_STARTUP_TMPL = (
    "@echo off\r\n"
    "rem Outreach boot (B17 start, B26 readiness gate): proves Ollama\r\n"
    "rem answers and an engine exists, then logs one STARTUP READINESS\r\n"
    "rem digest row. GENERATED from outreach/retirement.py — this file is not\r\n"
    "rem the source of truth. Remove it with: python -m outreach.setup_startup --remove\r\n"
    "cd /d {repo}\r\n"
    'set "PYTHONPATH={src}"\r\n'
    'start "Outreach readiness" /min cmd /c ""{python}" -m outreach.startup_readiness'
    ' >> "{log}" 2>&1"\r\n'
)

def _python_exe() -> Path:
    """The console interpreter (not pythonw): a traceback must land in the log."""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    return console if console.exists() else exe


def _render_startup(repo: Path | None = None) -> str:
    repo = repo if repo is not None else Path(__file__).resolve().parents[2]
    return _STARTUP_TMPL.format(
        repo=repo,
        src=repo / "src",
        python=_python_exe(),
        log=repo / "logs" / "startup.log",
    )


def import_reminder_history(db: "Database", path: Path | None = None) -> str:
    """Import the retired reminder's fired slots (digest summary row).

    Idempotent by content fingerprint stored in config: the same file
    never logs twice; a changed file logs its (changed) history anew.
    """
    state_path = Path(path) if path is not None else RETIREMENT_STATE
    fired = json.loads(state_path.read_text(encoding="utf-8")).get("fired", [])
    if not fired:
        return "nothing"
    fingerprint = hashlib.sha256(
        json.dumps(fired, sort_keys=True).encode()
    ).hexdigest()
    if get_config(db, "reminder_import_fingerprint") == fingerprint:
        return "already"
    body = (
        f"RETIRING reminder app: imported {len(fired)} fired slots from"
        f" {state_path} (read-only, never deleted):\n" + "\n".join(fired)
    )
    with db.conn:
        db.conn.execute("INSERT INTO digest(body) VALUES (?)", (body,))
    set_config(db, "reminder_import_fingerprint", fingerprint)
    return "imported"


def link_mount_linkedin(db: "Database") -> str:
    """Record + verify the mounted poster sibling (never any LinkedIn code)."""
    if not Path(LINKEDIN_MOUNT).exists():
        raise FileNotFoundError(
            f"LinkedIn sibling missing at {LINKEDIN_MOUNT}: it must be"
            " MOUNTED as-is (ADR-14), never rebuilt"
        )
    set_config(db, "linkedin_mount", LINKEDIN_MOUNT)
    return LINKEDIN_MOUNT


def _default_startup_dir() -> Path:
    return (
        Path(os.environ["APPDATA"])
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )


_LEGACY_STARTUP_PATTERN = "Outreach Reminder*.lnk"


def retire_reminder_app(
    db: "Database | None" = None,
    *,
    startup_dir: Path | None = None,
) -> list[Path]:
    """Switch the retired reminder OFF: disable auto-start, never delete.

    ADR-11 endgame: the reminder's history is imported by
    `import_reminder_history` and its folder stays on disk forever as a
    read-only archive. The one thing retirement changes
    is its login auto-start — the Startup shortcut is renamed to
    ``*.retired`` (Windows ignores the suffix), which is the whole switch
    and is reversible by renaming it back. Idempotent: an already-retired
    or absent shortcut is a clean noop; the paths renamed this run are
    recorded in config as evidence.
    """
    target_dir = (
        startup_dir if startup_dir is not None else _default_startup_dir()
    )
    renamed: list[Path] = []
    if target_dir.is_dir():
        for legacy in sorted(target_dir.glob(_LEGACY_STARTUP_PATTERN)):
            retired = legacy.with_name(legacy.name + ".retired")
            legacy.rename(retired)
            renamed.append(retired)
    if renamed and db is not None:
        set_config(db, "reminder_app_retired", [str(p) for p in renamed])
    return renamed


def startup_status(
    db: "Database | None" = None,
    *,
    startup_dir: Path | None = None,
) -> dict[str, object]:
    """Is the boot launcher installed, and is it the CURRENT definition?

    Verification is content, not existence: a file left over from an older
    template boots an old entry point and reads as "installed" to any check
    that only looks for the filename. `matched` compares the bytes.
    """
    repo = Path(__file__).resolve().parents[2]
    target_dir = startup_dir if startup_dir is not None else _default_startup_dir()
    target = target_dir / _STARTUP_NAME
    expected = _render_startup(repo)
    installed = target.is_file()
    # Compare BYTES: a text read applies universal-newline translation, so a
    # CRLF launcher and its own definition would never match.
    actual = target.read_bytes() if installed else b""
    matched = installed and actual == expected.encode("utf-8")
    if matched:
        detail = "installed and matches source"
    elif installed:
        detail = "installed but DIFFERS from the definition in retirement.py"
    else:
        detail = "not installed (nothing boots this lane after a reboot)"
    status: dict[str, object] = {
        "path": str(target),
        "installed": installed,
        "matched": matched,
        "detail": detail,
    }
    if db is not None:
        set_config(db, "startup_registered", str(target) if matched else None)
    return status


def register_startup(
    db: "Database | None" = None,
    *,
    startup_dir: Path | None = None,
) -> Path:
    """Write (or re-render) the boot launcher, then VERIFY what landed.

    Idempotent: the same definition renders byte-identical output, so a second
    run is a no-op on disk. The write is followed by a content check — a
    silently truncated file (full disk, AV lock) must fail here, not at 02:00.
    """
    repo = Path(__file__).resolve().parents[2]
    target_dir = startup_dir if startup_dir is not None else _default_startup_dir()
    target = target_dir / _STARTUP_NAME
    target_dir.mkdir(parents=True, exist_ok=True)
    (repo / "logs").mkdir(parents=True, exist_ok=True)
    target.write_text(_render_startup(repo), encoding="utf-8", newline="")
    status = startup_status(db, startup_dir=target_dir)
    if not status["matched"]:
        raise RuntimeError(
            f"startup launcher did not land as written at {target} "
            f"({status['detail']})"
        )
    return target


def unregister_startup(
    db: "Database | None" = None,
    *,
    startup_dir: Path | None = None,
) -> bool:
    """Remove the boot launcher (idempotent). True when a file was removed.

    Removing it does NOT stop a running engine; it only stops the next login
    from starting one. Nothing else in the Startup folder is touched.
    """
    target_dir = startup_dir if startup_dir is not None else _default_startup_dir()
    target = target_dir / _STARTUP_NAME
    removed = False
    if target.is_file():
        target.unlink()
        removed = True
    if db is not None:
        set_config(db, "startup_registered", None)
    return removed
