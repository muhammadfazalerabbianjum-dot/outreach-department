"""Fleet profile ownership (v1.1 batch 4): one Chrome profile per account.

**ADR-13 boundary.** Only `Outreach` and its numbered lane siblings
(`Outreach-ig2`, `Outreach-ig3`, ...) may ever be opened by automation. The
owner's own profiles are refused BY NAME, so a bad registry row can never
touch his personal session.

**Login-state isolation.** Each account IS a Chrome profile directory inside
the one automation user-data dir (`.chrome`). Chrome keeps one cookie jar per
profile directory, so account A's Instagram session can never answer for
account B, and a login wall on one account leaves every other account signed
in. `instagram_session` reads only the presence of an `instagram.com` host key
in that profile's cookie DB: it never decrypts a cookie and never guesses
("unknown" when the DB is absent or unreadable).

**Fail closed.** A profile that is not on disk raises `ProfileMissing` BEFORE
a browser boots. Chrome would otherwise silently CREATE a fresh profile
directory, and the dispatch would meet a login wall while every counter said
healthy.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

LEGACY_PROFILE = "Outreach"  # ADR-13: the owner's one automation login
USER_DATA_DIR = ".chrome"
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The only names automation may open: the legacy profile and its lane
#: siblings. `ig1` keeps the legacy profile, so `Outreach-ig1` is not a name.
PROFILE_NAME_RE = re.compile(r"^Outreach(?:-ig[0-9]+)?$")
#: Owner profile identities seen on this machine (never touched, ADR-13).
REFUSED_NAMES = frozenset({"barrud bhai", "barud bhai", "default", "your chrome"})
_INSTAGRAM_HOST_LIKE = "%instagram.com"
#: The cookie that only exists once someone is actually logged in.
_SESSION_COOKIE = "sessionid"


class ProfileRefused(PermissionError):
    """This profile name is outside the automation lane (ADR-13)."""


class ProfileMissing(FileNotFoundError):
    """The account's profile is not on disk: fail closed, never create it."""


def validate_profile_name(name: object) -> str:
    """Return the profile directory name, or refuse it (ADR-13).

    Exact match only: no trimming, no case folding, so `Outreach-ig2 ` and
    `outreach-ig2` are refused rather than silently redirected to a profile
    the owner never created.
    """
    text = name if isinstance(name, str) else ""
    if text.strip().lower() in REFUSED_NAMES:
        raise ProfileRefused(
            f"profile {text!r} is an owner profile; automation never opens it (ADR-13)"
        )
    if not PROFILE_NAME_RE.match(text):
        raise ProfileRefused(
            f"profile {text!r} is outside the Outreach lane (ADR-13)"
        )
    return text


def profile_root(root: str | Path | None = None) -> Path:
    """The Chrome user-data dir holding every automation profile."""
    return Path(root) if root is not None else _REPO_ROOT / USER_DATA_DIR


def profile_path(profile_dir: str, root: str | Path | None = None) -> Path:
    """Absolute path of one account's profile directory (validated first)."""
    return profile_root(root) / validate_profile_name(profile_dir)


def require_profile(profile_dir: str, root: str | Path | None = None) -> Path:
    """The profile directory, or `ProfileMissing` when it is not on disk.

    A Chrome profile without its `Preferences` file is not a usable profile
    (a bare directory is a half-created one): both must exist.
    """
    path = profile_path(profile_dir, root)
    if not path.is_dir() or not (path / "Preferences").is_file():
        raise ProfileMissing(
            f"profile {profile_dir!r} is not on disk at {path} - create and sign "
            "it in first, never auto-create (ADR-8/13)"
        )
    return path


def _cookie_db(profile: Path) -> Path | None:
    for candidate in (profile / "Network" / "Cookies", profile / "Cookies"):
        if candidate.is_file():
            return candidate
    return None


def instagram_session(
    profile_dir: str, root: str | Path | None = None
) -> str:
    """`signed_in` / `no_session` / `unknown` for this profile's IG session.

    Evidence only: Instagram's own auth cookie (`sessionid`) must be present
    in the profile's cookie DB. A bare `instagram.com` host key is NOT a
    session - a logged-out browser still carries `csrftoken`, `mid` and
    `ig_did` (live ig2/ig3 shape, 2026-11-07), and calling that signed_in
    would send a lane straight into a login wall. Cookie values stay
    encrypted and are never read; an unreadable DB is `unknown`, never a
    guess in either direction.
    """
    profile = require_profile(profile_dir, root)
    cookie_db = _cookie_db(profile)
    if cookie_db is None:
        return "unknown"
    try:
        conn = sqlite3.connect(f"file:{cookie_db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT 1 FROM cookies WHERE host_key LIKE ? AND name=? LIMIT 1",
                (_INSTAGRAM_HOST_LIKE, _SESSION_COOKIE),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return "unknown"
    return "signed_in" if row else "no_session"
