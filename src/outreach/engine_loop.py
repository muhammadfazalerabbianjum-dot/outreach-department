"""Engine loop (B17): the OS-level runner the Startup .cmd boots.

One pass per wake: wake = ``seconds_to_next_wake`` (0 inside an open
window, seconds to the next one, or the 1-hour fallback when idle),
then ONE scheduler tick through the real sender maps. Exceptions park
the loop with a short retry and land in the digest (ENGINE ERROR) —
it never dies silently and never spins hot. All the send gates
(queue pause, windows, caps, template owner-gate, unwired channels)
live in the tick; this loop adds none.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

_FALLBACK_SLEEP_S = 3600.0  # idle: re-check hourly
_FAILURE_RETRY_S = 300.0  # a crashed cycle retries after five minutes
_ERROR_REPEAT_S = 1800.0  # one ENGINE ERROR digest row per 30 min (B22)
_ENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_REFRESH_BUDGET_S = 600.0  # one refresh pass may not exceed this (R2)
_STAGE_PROMISE_S = 900.0  # mid-cycle promise: the watchdog's own grace (R2)


def load_env_file(path: str | Path) -> set[str]:
    """Load KEY=VALUE secrets from `.env` into os.environ (prod entry only).

    setdefault semantics: a REAL environment variable always wins;
    blank/#/malformed lines are skipped; values are never printed or
    logged. The repo contract is that secrets live ONLY in the
    git-ignored `.env` (AGENTS.md) — without this loader the production
    process never sees them (found 2026-10-27: SMTP/IMAP were
    unreachably blind inside the engine even though the owner had
    pasted the creds). Returns the keys this call actually set.
    """
    file_path = Path(path)
    if not file_path.exists():
        return set()
    loaded: set[str] = set()
    for raw in file_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in os.environ:
            continue  # setdefault: the process env wins
        os.environ[key] = value
        loaded.add(key)
    return loaded


def _wa_reply_probe(db: "Database", now: datetime) -> tuple[int, ...]:
    """B13 leg 2 through the real Outreach profile (ADR-13). Module-level
    so tests patch `outreach.replies.poll_wa_replies` at the seam."""
    from outreach.browser import launch_outreach_profile
    from outreach.replies import poll_wa_replies

    return poll_wa_replies(db, now, opener=launch_outreach_profile)


def _ig_reply_probe(db: "Database", now: datetime) -> tuple[int, ...]:
    """B24 leg 3: IG in-thread probe, one session per account (v1.1 b4).

    Each account's threads are read through that account's own profile
    (login isolation): a thread that belongs to `Outreach-ig2` is never
    opened in the legacy session, and vice versa. Tests patch
    `outreach.replies.poll_ig_replies` at the seam.
    """
    from outreach.accounts import get_account
    from outreach.browser import launch_outreach_profile
    from outreach.fleet import LEGACY_PROFILE, require_profile
    from outreach.replies import poll_ig_replies

    def opener_for(account_id: str) -> Callable[[], Any]:
        account = get_account(db, account_id) if account_id else None
        profile = (
            str(account["profile_dir"]) if account is not None else LEGACY_PROFILE
        )

        def _open() -> Any:
            require_profile(profile)  # never auto-create a logged-out profile
            return launch_outreach_profile(headless=True, profile_dir=profile)

        return _open

    return poll_ig_replies(db, now, opener_for=opener_for)


def _engage_stage(db: "Database", now: datetime) -> Any:
    """O1: the engagement stage, through ONE account-scoped browser session.

    The stage itself decides which account acts (it already knows the caps and
    the 90 s gap); this opener only guarantees the profile it acts in is that
    account's own session, never the legacy one, and that it is never
    auto-created logged-out.
    """
    from outreach.accounts import get_account
    from outreach.browser import launch_outreach_profile
    from outreach.engage_stage import engage_armed, run_engage_stage
    from outreach.fleet import LEGACY_PROFILE, require_profile

    if not engage_armed(db):
        # O8 (2026-11-14): a DISARMED message lane still spends the ambient time
        # the owner approved, and that needs a page. Live 15:15Z: without this the
        # first ambient tick died as "engage stage needs a page or a page_factory"
        # because the disarmed path never opened one. When ambient is also off the
        # stage reports itself with no browser at all.
        from outreach import ambient

        if not ambient.config(db)["enabled"]:
            return run_engage_stage(db, now)

    def _open(account_id: str) -> Any:
        """That account's OWN session: a comment typed by ig2 must never be
        typed inside ig1's profile (ADR-13 login isolation)."""
        account = get_account(db, account_id)
        profile = (
            str(account["profile_dir"]) if account is not None else LEGACY_PROFILE
        )
        require_profile(profile)  # never auto-create a logged-out profile
        return launch_outreach_profile(headless=True, profile_dir=profile)

    return run_engage_stage(db, now, page_factory=_open)


def _notify(
    stage: Callable[..., Any] | None, name: str, seconds: float | None = None
) -> None:
    """Name a stage on the heartbeat (no-op when the loop passed no recorder)."""
    if stage is not None:
        stage(name, seconds)


def _default_cycle(
    db_path: str | Path, *, stage: Callable[..., Any] | None = None
) -> float | None:
    """One real cycle: DISPATCH first, then the scheduled refresh pass.

    Order is the fix (R2, 2026-11-13): dispatch used to wait behind a ~15 min
    `refresh_leads`, which is why the heartbeat stamped past its own promise.
    The refresh is now a separate, off-window, budgeted stage that runs only
    when `intake.refresh_due` says so, so a slow one can neither delay a send
    nor run a second time the same day.
    """
    from outreach.command_center import real_resenders, real_senders
    from outreach.replies import _default_imap_factory
    from outreach.scheduler import (
        open_session_regions,
        run_engine_tick,
        seconds_to_next_wake,
    )
    from outreach.store import Database

    now = datetime.now(timezone.utc)
    with Database(db_path) as db:
        wake = seconds_to_next_wake(db, now)
        # O9 (2026-11-14): name a touch whose window the machine slept through,
        # BEFORE any claim can touch it. Fail-soft: a reporting fault must never
        # stop a cycle.
        try:
            from outreach.replan import replan_stale

            replan_stale(db, now)
        except Exception as exc:  # noqa: BLE001 — reporting is not a send
            from outreach.scheduler import _record_probe_error

            _record_probe_error(db, "replan", exc, now, label="REPLAN ERROR")
        run_engine_tick(
            db,
            now,
            senders=dict(real_senders),
            resenders=dict(real_resenders),
            # ladder + escalation only reach leads inside an OPEN ADR-9
            # window (config rows; B24 — manual runs may pass None)
            regions=open_session_regions(db, now),
            imap_factory=_default_imap_factory,  # reply leg 1 (email)
            wa_reply_probe=_wa_reply_probe,  # reply leg 2 (WA in-thread)
            ig_reply_probe=_ig_reply_probe,  # reply leg 3 (IG in-thread)
            engage_stage=_engage_stage,  # O1: comments / warm-up / warming
            review_path=Path(db_path).parent / "OWNER_REVIEW.md",  # F3
            stage_cb=stage,
        )
        _sweep_alerts(db, now)  # R3: P0/P1 to the owner, P2 to the record
        _refresh_if_due(db, now, stage=stage)  # B20 + R2: off-window only
    return wake


def _sweep_alerts(db: "Database", now: datetime) -> None:
    """R3: raise the P0/P1 conditions this cycle can see.

    Runs AFTER the tick so the reply-probe stamps it reads are this cycle's,
    and never raises: an alerting fault must not park a send cycle. A failure
    is itself surfaced as a digest row, deeply throttled by the same code path
    that writes every other engine error.
    """
    from outreach import alerts

    try:
        alerts.sweep(db, now)
    except Exception as exc:  # noqa: BLE001 — alerting is never a send blocker
        with db.conn:
            db.conn.execute(
                "INSERT INTO digest(body, created_at) VALUES (?, ?)",
                (
                    f"ALERT SWEEP ERROR: {repr(exc)[:300]}",
                    now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )


def _refresh_if_due(
    db: "object", now: datetime, *, stage: Callable[..., Any] | None = None
) -> None:
    """Run the enrichment pass only when the schedule law allows it.

    The day stamp is written only for a pass that finished inside its budget:
    a truncated pass left work behind, so the next off-window cycle resumes it
    instead of waiting a whole day for the remainder of a big run.
    """
    from outreach.intake import mark_refresh_done, refresh_due
    from outreach.store import get_config

    if not refresh_due(db, now):
        return
    if get_config(db, "queue_status") == "PAUSED":
        # The owner's pause is state, not a fault (same law as `_run_watcher`).
        # It has to gate the pass HERE: `intake_run` imports first and raises
        # QueuePaused at qualification, so an ungated attempt would re-run the
        # whole ~15 min import every off-window cycle and never stamp the day.
        return
    _notify(stage, "refresh")
    started = time.monotonic()
    report = _run_watcher(db, now, budget_s=_REFRESH_BUDGET_S)
    _notify(stage, "refresh", time.monotonic() - started)
    if report is not None and not report.truncated:
        mark_refresh_done(db, now)


def _run_watcher(
    db: "object", now: datetime, *, budget_s: float | None = _REFRESH_BUDGET_S
) -> Any:
    """Poll the scraper once; a paused queue is state, not a fault.

    Live evidence 2026-11-07: with the queue PAUSED the old blanket except
    wrote `WATCHER ERROR: QueuePaused(...)` every tick, ~24 rows an hour into
    the owner's digest. The pause is his own switch, so it is silent here; a
    genuine watcher fault is still recorded, throttled to one row per
    `_ERROR_REPEAT_S` like every other repeating engine error.
    """
    from outreach.icp import QueuePaused
    from outreach.intake import watcher_tick

    try:
        return watcher_tick(db, now=now, budget_s=budget_s)
    except QueuePaused:
        return None  # deliberate pause: nothing to do, nothing to report
    except Exception as exc:  # noqa: BLE001 — a watcher fault must not kill the cycle
        _note_watcher_error(db, repr(exc)[:300], now)
        return None


def _note_watcher_error(db: "object", detail: str, now: datetime) -> None:
    """One WATCHER ERROR row per repeat window, stamped on the tick clock."""
    since = (now - timedelta(seconds=_ERROR_REPEAT_S)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    existing = db.conn.execute(  # type: ignore[attr-defined]
        "SELECT 1 FROM digest WHERE substr(body, 1, 13) = 'WATCHER ERROR'"
        " AND substr(created_at, 1, 19) >= ? LIMIT 1",
        (since,),
    ).fetchone()
    if existing:
        return
    with db.conn:  # type: ignore[attr-defined]
        db.conn.execute(
            "INSERT INTO digest(body, created_at) VALUES (?, ?)",
            (
                f"WATCHER ERROR: {detail}",
                now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )


def _log_engine_error(db_path: str | Path, error: str) -> None:
    """Surface a failing cycle in the digest — throttled to one row per
    30 min per reason-prefix law: the failure loop already retries every
    5 min, so an unthrottled row would bury the owner's digest 12×/hour
    (live risk 2026-10-28: persistent profile contention)."""
    from datetime import timedelta

    from outreach.store import Database

    with Database(db_path) as db:
        since = (
            datetime.now(timezone.utc) - timedelta(seconds=_ERROR_REPEAT_S)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        existing = db.conn.execute(
            "SELECT 1 FROM digest WHERE substr(body, 1, 12) = 'ENGINE ERROR'"
            " AND substr(created_at, 1, 19) >= ? LIMIT 1",
            (since,),
        ).fetchone()
        if existing:
            return
        with db.conn:
            db.conn.execute(
                "INSERT INTO digest(body) VALUES (?)",
                (f"ENGINE ERROR: loop cycled through {error}",),
            )


def _drain_owner_toast(db_path: str | Path) -> None:
    """B21/Q11: new digest rows -> local balloon. Module-attribute call so
    tests can patch `outreach.toast`; the drain itself never raises."""
    from outreach import toast

    toast.drain_engine_toasts(db_path)


def run_forever(
    db_path: str | Path,
    *,
    sleep: Callable[[float], None] = time.sleep,
    max_cycles: int | None = None,
    _cycle: Callable[..., float | None] | None = None,
) -> None:
    """Wake → tick → sleep(wake). `max_cycles` exists only for tests; the
    real bootloader calls with none and lets the Shutdown or crash log
    own the exit."""
    from outreach.heartbeat import heartbeat_path, lease_path, write_heartbeat, write_lease
    from outreach.store import Database

    lease_file = lease_path(db_path)
    if lease_file is not None:  # who claims THE engine (watchdog reads it)
        write_lease(lease_file, pid=os.getpid(), started_at=datetime.now(timezone.utc))
    beat_file = heartbeat_path(db_path)
    if db_path is not None:
        # B29 cutover repair (live incident 2026-10-31): leads ROUTED by the
        # old phone-wins law carry a stale route_channel='wa', and dispatch
        # trusts that column — the lane law re-routes (or parks) them here,
        # once per engine process, before the first send can fire.
        from outreach.router import reroute_stale_routes, reroute_stranded

        try:
            with Database(db_path) as db:
                reroute_stale_routes(db)
                # 2026-11-16: UNREACHABLE was a one-way door (867 parked, 312
                # of them reachable again after link repair/import). Same boot
                # repair, same law, no invented channels.
                stranded = reroute_stranded(db)
                if stranded.recovered:
                    with db.conn:
                        db.conn.execute(
                            "INSERT INTO digest(body) VALUES (?)",
                            (
                                f"STRANDED REPAIR: {stranded.recovered} parked lead(s)"
                                " re-routed under the current lane law"
                                f" ({stranded.still_stranded} still have no lawful channel)",
                            ),
                        )
        except Exception as exc:  # noqa: BLE001 — a repair fault must not
            # stop the engine: the tick's own gates still fail closed, and
            # the digest row surfaces the fault to the owner.
            _log_engine_error(db_path, f"reroute: {exc!r}")
    cycle = _cycle if _cycle is not None else _default_cycle
    trace: dict[str, float | None] = {}

    def _stage(name: str, seconds: float | None = None) -> None:
        """Stamp the heartbeat the instant a stage starts or finishes (R2).

        THE one heartbeat writer during a cycle: the promise is the stage
        grace, so a long stage cannot make the watchdog think the engine died
        between stamps, and the trace keeps every stage this cycle named with
        its measured duration.
        """
        trace[name] = None if seconds is None else round(float(seconds), 1)
        if beat_file is None:
            return
        stamped = datetime.now(timezone.utc)
        label = name if seconds is None else f"{name} {float(seconds):.1f}s"
        try:
            write_heartbeat(
                beat_file,
                stamped_at=stamped,
                next_wake_at=stamped + timedelta(seconds=_STAGE_PROMISE_S),
                stage=label,
                stages=[{"stage": k, "seconds": v} for k, v in trace.items()],
            )
        except OSError:
            pass

    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        trace.clear()
        try:
            wake = (
                _cycle(db_path)  # test seam: a fake cycle names no stages
                if _cycle is not None
                else cycle(db_path, stage=_stage)
            )
        except Exception as exc:  # noqa: BLE001 — the loop must survive
            _log_engine_error(db_path, repr(exc)[:300])
            _drain_owner_toast(db_path)
            delay = _FAILURE_RETRY_S
        else:
            _drain_owner_toast(db_path)
            delay = wake if wake is not None else _FALLBACK_SLEEP_S
        if beat_file is not None:
            # The promise the watchdog checks: best-effort liveness, never a
            # reason to crash a cycle that already succeeded.
            stamped = datetime.now(timezone.utc)
            try:
                write_heartbeat(
                    beat_file,
                    stamped_at=stamped,
                    next_wake_at=stamped + timedelta(seconds=delay),
                    stage=f"cycle {cycles + 1} parked {delay:.0f}s",
                    stages=[
                        {"stage": k, "seconds": v} for k, v in trace.items()
                    ],
                )
            except OSError:
                pass
        sleep(delay)
        cycles += 1


def main(argv: list[str] | None = None) -> None:
    """`python -m outreach.engine_loop [max_cycles]` — the machine runs it
    with NO argument (forever). A cycle count = dev smoke: one or more
    cycles, no sleeps, exit after them (nothing ever sends here: every
    gate lives in the tick)."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    repo = Path(__file__).resolve().parents[2]
    load_env_file(repo / ".env")  # secrets in-process before any sender/poll
    # 2026-11-04: the engine died twice and left no trace. This line plus the
    # printed traceback below land in logs/engine.log via the watchdog's
    # redirect, so a death is a boundary in the file, not a mystery.
    print(
        f"[{datetime.now(timezone.utc).isoformat()}] engine_loop boot pid={os.getpid()}",
        flush=True,
    )
    try:
        if args:
            run_forever(
                repo / "outreach.db",
                sleep=lambda _s: None,  # dev mode: no waiting between cycles
                max_cycles=int(args[0]),
            )
            return
        run_forever(repo / "outreach.db")  # prod mode: wake → tick → sleep
    except Exception:  # noqa: BLE001 - re-raised below; the trace must be written
        import traceback

        traceback.print_exc()
        raise


if __name__ == "__main__":  # pragma: no cover — process entry; live-proved
    main()  # via `python -m outreach.engine_loop 1` (evidence in BATCH17)
