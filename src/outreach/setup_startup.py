"""Install, verify or remove this lane's boot launcher (R4).

The definition of the launcher lives in `outreach.retirement` (one source of
truth, in version control); this module is the operator surface for it:

    python -m outreach.setup_startup            # install (idempotent)
    python -m outreach.setup_startup --verify    # installed AND current?
    python -m outreach.setup_startup --remove    # stop booting at login

Exit codes are the contract: 0 = the requested state holds, 1 = it does not
(a missing launcher after a reboot is exactly the failure this lane must not
discover silently). `--verify` never writes; `--install` re-renders an outdated
launcher, which is how an old template gets replaced.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from outreach.retirement import (
    register_startup,
    startup_status,
    unregister_startup,
)
from outreach.store import Database

_DB_PATH = Path(__file__).resolve().parents[2] / "outreach.db"  # same as command_center


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m outreach.setup_startup")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--verify",
        action="store_true",
        help="report whether the launcher is installed and current (writes nothing)",
    )
    mode.add_argument(
        "--remove",
        action="store_true",
        help="delete the launcher so a login no longer boots this lane",
    )
    args = parser.parse_args(argv)

    with Database(_DB_PATH) as db:
        if args.remove:
            removed = unregister_startup(db)
            print(f"startup launcher removed: {removed}")
            return 0
        if args.verify:
            status = startup_status(db)
            print(f"startup launcher: {status['path']}")
            print(f"  installed: {status['installed']}")
            print(f"  current:   {status['matched']}")
            print(f"  detail:    {status['detail']}")
            return 0 if status["matched"] else 1
        path = register_startup(db)
        status = startup_status(db)
        print(f"startup launcher written: {path}")
        print(f"  verified: {status['matched']}")
        return 0 if status["matched"] else 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main(sys.argv[1:]))
