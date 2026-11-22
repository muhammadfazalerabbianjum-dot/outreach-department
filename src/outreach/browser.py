"""Shared real-browser ownership (B10/11): playwright chromium under the
dedicated Outreach profile (ADR-13). One profile, both senders, strict
teardown — sync playwright is started on enter and stopped on exit even
when the body raises.
"""
from __future__ import annotations

import json
import os
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

PROFILE_DIRECTORY = "Outreach"
_CHROME_ARGS = ["--profile-directory=" + PROFILE_DIRECTORY]
# WhatsApp/Instagram reject "HeadlessChrome" UAs with browser-update walls;
# a plain current-Chrome UA (plus the profile args) gets the real app shell.
_NORMAL_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_PROFILE_ROOT = os.path.join(_REPO_ROOT, ".chrome")
# ADR-15 evidence home: every sender screenshot lands here by default.
EVIDENCE_DIR = os.path.join(_REPO_ROOT, "checkpoints", "evidence")

# ROOT CAUSE NOTE (found 2026-10-25, proven by cookie-jar comparison):
# Playwright's default headless binary is `chrome-headless-shell`, which
# silently IGNORES --profile-directory and falls back to the Default
# sub-profile. Headed launches (full Chromium) honor it — so warm runs
# used the owner's real logins in .chrome/Outreach while every cold
# engine run read the anonymous .chrome/Default jar and saw a login
# wall. Fix: pin channel="chromium" (full binary, new headless) for
# every launch so warm and cold select the SAME physical profile.


def chrome_launch_options(
    profile_root: str, profile_dir: str = PROFILE_DIRECTORY
) -> dict[str, Any]:
    """Persistent-context options pinning ONE account's profile (ADR-13).

    `profile_root` is the user data dir; the profile directory arg
    selects an `Outreach` lane profile inside it (v1.1 batch 4: one
    profile per account, so logins never leak between accounts), and
    `channel="chromium"` forces the full Chromium binary (see ROOT
    CAUSE NOTE) so headless launches honor that profile selection
    exactly like headed ones.
    """
    from outreach.fleet import validate_profile_name

    return {
        "user_data_dir": profile_root,
        "args": ["--profile-directory=" + validate_profile_name(profile_dir)],
        "channel": "chromium",
        "user_agent": _NORMAL_CHROME_UA,
        "viewport": {"width": 1280, "height": 800},
    }


class _LivePage(AbstractContextManager):
    """One automation page from one account profile (ownership ends on exit)."""

    def __init__(
        self,
        headless: bool,
        profile_dir: str = PROFILE_DIRECTORY,
        user_data_root: str | Path | None = None,
    ) -> None:
        self._headless = headless
        self._profile_dir = profile_dir
        self._user_data_root = user_data_root
        self._pw: Any = None
        self._context: Any = None

    def __enter__(self) -> Any:
        from playwright.sync_api import sync_playwright

        from outreach.fleet import profile_root

        self._pw = sync_playwright().start()
        options = chrome_launch_options(
            str(profile_root(self._user_data_root)), self._profile_dir
        )
        try:
            self._context = self._pw.chromium.launch_persistent_context(
                options["user_data_dir"],
                headless=self._headless,
                args=options["args"],
                channel=options["channel"],
                user_agent=options["user_agent"],
                viewport=options["viewport"],
            )
        except BaseException:
            # When __enter__ raises, the `with` statement never calls
            # __exit__ — so stop the session we already started ourselves.
            # A leaked sync-Playwright session keeps its running-loop flag
            # set process-wide: every later sync_playwright().start() then
            # dies with "Sync API inside the asyncio loop" and the engine
            # bricks forever (live: digests 25-30, 2026-10-27 17:53 PKT).
            leaked, self._pw = self._pw, None
            try:
                leaked.stop()
            except Exception:  # noqa: BLE001 — the launch failure is the error
                pass
            raise
        page = self._context.pages[0] if self._context.pages else (
            self._context.new_page()
        )
        return page

    def __exit__(self, *exc_info: object) -> None:
        try:
            if self._context is not None:
                self._context.close()
        finally:
            if self._pw is not None:
                self._pw.stop()


# --- one profile, one Chromium (2026-11-15) -------------------------------
#
# Live 22:05 PKT: the engagement stage died with
# `TargetClosedError('BrowserType.launch_persistent_context: ... browser has been
# closed')` because ig1's dispatch session already held `.chrome`. Chromium
# refuses a second persistent context on a user-data dir that is in use, so this
# is ownership, not retry: the live session is kept per (user data dir, profile)
# and BORROWED. The second caller gets the same page; the context closes exactly
# once, when the last borrower leaves (or `close_all_sessions` tears down a
# leaked one). Login isolation is untouched: different profiles never share.
_SESSIONS: dict[tuple[str, str], dict[str, Any]] = {}
_SESSIONS_LOCK = __import__("threading").RLock()


class _Pooled(AbstractContextManager):
    """Lazy entry: the browser opens (or is borrowed) when the page is asked for.

    `launch_outreach_profile()` keeps its old shape — building one launches
    nothing, so a caller that only holds the handle (a test, a lock check) never
    boots Chrome behind the owner's back.
    """

    def __init__(
        self, *, headless: bool, profile_dir: str, user_data_root: Any = None
    ) -> None:
        self._headless = headless
        self._profile_dir = profile_dir
        self._user_data_root = user_data_root
        self._inner: AbstractContextManager | None = None

    def __enter__(self) -> Any:
        self._inner = _acquire_session(
            headless=self._headless,
            profile_dir=self._profile_dir,
            user_data_root=self._user_data_root,
        )
        return self._inner.__enter__()

    def __exit__(self, *exc_info: object) -> None:
        inner, self._inner = self._inner, None
        if inner is not None:
            inner.__exit__(*exc_info)


class _Borrowed(AbstractContextManager):
    """A borrow of an already-open session; never closes what it did not open."""

    def __init__(self, entry: dict[str, Any]) -> None:
        self._entry = entry
        self._returned = False

    def __enter__(self) -> Any:
        return self._entry["page"]

    def __exit__(self, *exc_info: object) -> None:
        if self._returned:
            return
        self._returned = True
        _release_session(self._entry)


def _release_session(entry: dict[str, Any]) -> None:
    with _SESSIONS_LOCK:
        entry["refs"] = int(entry.get("refs", 0)) - 1
        if entry["refs"] > 0:
            return
        _SESSIONS.pop(entry.get("key"), None)
        holder = entry.get("holder")
        entry["page"] = None
    if holder is not None:
        holder.__exit__(None, None, None)


def close_all_sessions() -> None:
    """Tear down every live session (a crashed stage can leak one)."""
    with _SESSIONS_LOCK:
        entries = list(_SESSIONS.values())
        _SESSIONS.clear()
    for entry in entries:
        holder = entry.get("holder")
        entry["page"] = None
        if holder is None:
            continue
        try:
            holder.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 — teardown never raises out of a repair
            pass


def _acquire_session(
    *,
    headless: bool,
    profile_dir: str,
    user_data_root: str | Path | None,
) -> AbstractContextManager:
    """Borrow the live session for this profile, or open the first one."""
    from outreach.fleet import profile_root

    key = (str(profile_root(user_data_root)), profile_dir)
    with _SESSIONS_LOCK:
        entry = _SESSIONS.get(key)
        if entry is not None and entry.get("page") is not None:
            entry["refs"] = int(entry.get("refs", 0)) + 1
            return _Borrowed(entry)
    holder = _LivePage(
        headless=headless,
        profile_dir=profile_dir,
        user_data_root=user_data_root,
    )
    page = holder.__enter__()  # cleans up its own playwright session on failure
    entry = {"key": key, "holder": holder, "page": page, "refs": 1}
    with _SESSIONS_LOCK:
        existing = _SESSIONS.get(key)
        if existing is not None and existing.get("page") is not None:
            # Another thread won the race: hand the newcomer the live session
            # and close the one we just opened, so a dir is never double-held.
            existing["refs"] = int(existing.get("refs", 0)) + 1
            keeper = existing
        else:
            _SESSIONS[key] = entry
            keeper = entry
    if keeper is not entry:
        holder.__exit__(None, None, None)
    return _Borrowed(keeper)


class ProfileBusy(RuntimeError):
    """The Outreach profile is held by someone else right now (B25).

    Fail-closed by law: never a second Chrome on one profile (ADR-13) —
    the caller decides what a busy profile means for ITS context (the
    engine parks the tick; a probe just doesn't probe).
    """


_LOCK_FILENAME = ".browser_probe.lock"
_PROBE_TTL_S = 300.0


def probe_lock_path(root: str | Path | None = None) -> Path:
    """The contention-lock file, next to the heartbeat/lease files."""
    base = Path(root) if root is not None else Path(_REPO_ROOT)
    return base / _LOCK_FILENAME


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _lock_pid(lock: dict) -> int:
    try:
        return int(lock.get("pid", -1))
    except (TypeError, ValueError):
        return -1


def _lock_live(lock: dict, now: datetime) -> bool:
    try:
        expires = datetime.fromisoformat(str(lock.get("expires_at")))
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > now


def _engine_state(root: Path, now: datetime) -> str:
    """'sleeping:<runway_s>' | 'running' | 'gone' — the same two signals the
    watchdog reads: the heartbeat PROMISE (when the engine wakes next) and
    the leased pid (is that promise being kept right now)."""
    beat = _read_json(root / ".heartbeat.json")
    if not beat:
        return "gone"  # no promise on record
    # 2026-11-13 (R11 / IL-10): a stamp written INSIDE a stage carries a long
    # promise on purpose (R2 writes 900 s so the watchdog tolerates a slow
    # stage). Reading that promise alone called a busy engine "asleep" and let
    # a probe take the live profile out from under a running cycle — two live
    # browser tests then died in launch_persistent_context at 15:25Z. The final
    # stamp of every cycle is the parked shape ("cycle N parked Ns"); any other
    # stage name means a cycle is running right now.
    stage = beat.get("stage")
    if isinstance(stage, str) and stage.strip() and not stage.startswith("cycle "):
        return "running"
    try:
        wake = datetime.fromisoformat(str(beat.get("next_wake_at")))
    except ValueError:
        return "gone"
    if wake.tzinfo is None:
        wake = wake.replace(tzinfo=timezone.utc)
    if now < wake:
        return f"sleeping:{(wake - now).total_seconds():.0f}"
    from outreach.watchdog import _pid_alive

    lease = _read_json(root / ".engine_lease.json")
    return "running" if _pid_alive(_lock_pid(lease)) else "gone"


def acquire_probe_lock(
    holder: str,
    *,
    ttl_s: float = _PROBE_TTL_S,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> None:
    """Take the profile for a manual probe — ONLY while the engine sleeps.

    Law (overnight directive, phase-1 item 5): probes run while the engine
    sleeps, never against a live cycle. Refuses (ProfileBusy, never a race)
    when the engine wakes inside `ttl_s`, when it is mid-cycle right now, or
    when another probe holds a live lock. Raises on refusal; returns None
    when the lock is taken.
    """
    base = Path(root) if root is not None else Path(_REPO_ROOT)
    now = now or datetime.now(timezone.utc)
    state = _engine_state(base, now)
    if state.startswith("sleeping:"):
        runway = float(state.split(":", 1)[1])
        if runway < ttl_s:
            raise ProfileBusy(
                f"engine wakes in {runway:.0f}s (< ttl {ttl_s:.0f}s) — "
                f"probe '{holder}' refused"
            )
    elif state == "running":
        raise ProfileBusy(
            f"engine is running a cycle — probe '{holder}' refused until it sleeps"
        )

    path = probe_lock_path(base)
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "holder": str(holder),
            "acquired_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl_s)).isoformat(),
        }
    )
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        existing = _read_json(path)
        if _lock_live(existing, now):
            raise ProfileBusy(
                f"profile held by probe '{existing.get('holder')}' until "
                f"{existing.get('expires_at')}"
            ) from None
        # stale or our own: steal atomically (temp + rename)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
        return
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)


def release_probe_lock(*, root: str | Path | None = None) -> None:
    """Free the probe lock (idempotent; never frees a LIVE foreign lock)."""
    path = probe_lock_path(root)
    lock = _read_json(path)
    if lock and _lock_pid(lock) != os.getpid() and _lock_live(
        lock, datetime.now(timezone.utc)
    ):
        return  # not ours to free
    path.unlink(missing_ok=True)


def launch_outreach_profile(
    headless: bool = True,
    *,
    root: str | Path | None = None,
    profile_dir: str = PROFILE_DIRECTORY,
) -> AbstractContextManager:
    """Open (or borrow) one automation-capable page under ONE account's profile.

    `profile_dir` is the Chrome profile directory name (v1.1 batch 4: `ig1`
    keeps the legacy `Outreach` login, every new account gets its own
    `Outreach-igN`); the name is validated against the ADR-13 lane before
    any browser work, so an owner profile can never be opened here.

    B25 contention lock: refuses to boot Chrome while a FOREIGN live probe
    lock exists — an engine waking mid-probe fail-closes (ProfileBusy,
    contained by the tick) instead of stomping the probe's browser. The
    engine's own process (pid match) is never blocked by its own probes.
    """
    from outreach.fleet import validate_profile_name

    profile_dir = validate_profile_name(profile_dir)
    base = Path(root) if root is not None else Path(_REPO_ROOT)
    lock = _read_json(probe_lock_path(base))
    if (
        lock
        and _lock_live(lock, datetime.now(timezone.utc))
        and _lock_pid(lock) != os.getpid()
    ):
        raise ProfileBusy(
            f"profile held by probe '{lock.get('holder')}' until "
            f"{lock.get('expires_at')}"
        )
    return _Pooled(
        headless=headless,
        profile_dir=profile_dir,
        user_data_root=root,
    )
