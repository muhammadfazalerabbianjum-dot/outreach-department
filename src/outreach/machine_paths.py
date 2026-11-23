"""Resolve the five machine-local roots without publishing a disk layout.

``OUTREACH_SCRAPER_ROOT``, ``OUTREACH_LEG_PYTHON``, ``OUTREACH_LEG_LOG_DIR``,
``OUTREACH_RETIREMENT_STATE`` and ``OUTREACH_LINKEDIN_MOUNT`` point outside this
repository (OPERATIONS.md §12). Those values belong to the machine, not to the
source, so this tree carries no drive letter, no user directory and no absolute
default. Resolution order, first hit wins:

1. the environment variable itself — how a fork, a service, or a one-off shell run
   configures the engine;
2. ``machine_paths_local.py`` beside this module — **git-ignored**, holds THIS
   machine's values under the same ``OUTREACH_*`` names, and is never committed
   or published;
3. ``KeyError`` naming what to set. A missing root is a configuration error;
   guessing one would silently read somebody else's disk.

Nothing is cached and nothing is imported as logic: the local file is only read
for its ``OUTREACH_*`` attributes, so an environment override always wins.
"""
from __future__ import annotations

import importlib
import os
from types import ModuleType
from typing import Final

LOCAL_MODULE: Final = "machine_paths_local"
"""File name, in this package, that a machine may fill in; see OPERATIONS.md §12."""


def _local() -> ModuleType | None:
    """The git-ignored per-machine module, or None when the machine has none."""
    dotted = f"{__package__}.{LOCAL_MODULE}"
    try:
        return importlib.import_module(dotted)
    except ModuleNotFoundError as exc:
        if exc.name in (dotted, LOCAL_MODULE):
            return None
        raise  # the local file exists but imports something broken: not a "missing"


def get(name: str, default: str | None = None) -> str:
    """Resolve one setting: environment, then the local file, then ``default``.

    Raises ``KeyError`` (rather than returning a guess) when nothing supplies a
    value and no default was given.
    """
    value = os.environ.get(name)
    if value:
        return value
    local = _local()
    if local is not None:
        local_value = getattr(local, name, None)
        if local_value:
            return str(local_value)
    if default is not None:
        return default
    raise KeyError(
        f"{name} is not set. Export {name}, or put {name} = \"<path>\" in "
        f"src/outreach/{LOCAL_MODULE}.py (git-ignored, this machine only — "
        "see OPERATIONS.md §12)."
    )
