"""Intake pipeline driver (B20): scraper runs -> dispatch-ready leads.

`intake_run(run_id)` is the conveyor belt the four intake stages were
missing: import (read-only, ADR-3) -> region detection (addendum 3) ->
cleaning (E.164 phones + IG link repair, spec 2) -> dedupe (inside the
importer, ADR-3 order) -> ICP qualification (multi-location brain first,
LLM second) -> dish extraction (confidence-gated, addendum 5) -> routing
(WA -> IG -> email, ADR-5/6). Every stage is idempotent, so a partially
processed run resumes exactly where it stopped (state machine scoped).
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from outreach.cleaning import repair_instagram, resolve_regions, to_e164
from outreach.dishes import extract_dishes
from outreach.icp import qualify_new_leads
from outreach.importer import refresh_leads
from outreach.router import apply_routes
from outreach.store import get_config, set_config

if TYPE_CHECKING:
    from outreach.store import Database


@dataclass(frozen=True)
class IntakeReport:
    run_id: str | None
    created: int
    refreshed: int
    dups: int
    skipped_contacted: int
    region_resolved: int
    region_held: int
    qualified: int
    rejected: int
    dishes_with: int
    dishes_no: int
    routed: int
    unreachable: int
    complete: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def clean_run(db: "Database", *, run_id: str | None = None) -> int:
    """E.164 phones + IG link repair/classification for a run's leads (spec 2).

    Deterministic and idempotent: the original values stay in `data_json`
    (the scraper row); repaired values land on the lead's columns.
    Returns the number of leads processed.
    """
    scope = " WHERE run_id=?" if run_id is not None else ""
    rows = db.conn.execute(
        "SELECT id, phone, region, instagram FROM leads" + scope,
        (run_id,) if run_id is not None else (),
    ).fetchall()
    for row in rows:
        e164 = to_e164(row["phone"], row["region"])
        value, klass = repair_instagram(row["instagram"])
        with db.conn:
            db.conn.execute(
                "UPDATE leads SET phone_e164=?, instagram=?, ig_class=?"
                " WHERE id=?",
                (e164 or None, value, klass, int(row["id"])),
            )
    return len(rows)


def intake_run(
    db: "Database",
    run_id: str,
    *,
    transport: Any = None,
    sleep_s: float = 0.5,
    limit: int | None = None,
    db_path: str | Any = None,
) -> IntakeReport:
    """Run the full intake pipeline for ONE scraper run (spec 1).

    `limit` bounds the LLM stages (qualification + dishes) per call; the
    import/region/clean/route stages are cheap and always run to the end.
    `complete` reports whether no never-contacted lead of this run is left
    NEW or QUALIFIED — the watcher uses it to know when a run is caught up.
    """
    from outreach.scraper_adapter import SCRAPER_DB

    source = db_path if db_path is not None else SCRAPER_DB
    imp = refresh_leads(db, source, run_id=run_id)
    region = resolve_regions(db, run_id=run_id)
    clean_run(db, run_id=run_id)
    qual = qualify_new_leads(
        db, transport=transport, sleep_s=sleep_s, limit=limit, run_id=run_id
    )
    dishes = extract_dishes(
        db, transport=transport, sleep_s=sleep_s, limit=limit, run_id=run_id
    )
    routing = apply_routes(db, run_id=run_id)
    remaining = int(
        db.conn.execute(
            "SELECT COUNT(*) FROM leads WHERE run_id=?"
            " AND state IN ('NEW', 'QUALIFIED') AND previously_contacted=0",
            (run_id,),
        ).fetchone()[0]
    )
    complete = remaining == 0
    if complete:
        # caught up: record it so the watcher (and the next CLI run) leaves
        # this run alone until the scraper's row count grows
        tracked: dict[str, dict] = dict(get_config(db, "ingested_runs", {}) or {})
        tracked[run_id] = {
            "rows": imp.total_rows,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        set_config(db, "ingested_runs", tracked)
    return IntakeReport(
        run_id=run_id,
        created=imp.created,
        refreshed=imp.refreshed,
        dups=imp.dups,
        skipped_contacted=imp.skipped_contacted,
        region_resolved=region.resolved,
        region_held=region.held,
        qualified=qual.qualified,
        rejected=qual.rejected,
        dishes_with=dishes.with_dish,
        dishes_no=dishes.no_dish,
        routed=routing.routed,
        unreachable=routing.unreachable,
        complete=complete,
    )


# --- run watcher (addendum 1) + low-lead alert (addendum 2) -------------------

# D10 invocation, region-matched; only ever spawned when the owner flips
# `auto_restart_leg` (default FALSE -> alert-only). cwd = the scraper repo.
_LEG_CWD = "A:/scraper"
_LEG_PYTHON = "A:/scraper/venv/Scripts/python.exe"
LEG_COMMANDS: dict[str, tuple[str, ...]] = {
    "na": (_LEG_PYTHON, "-m", "halal_scraper", "--config", "config.yaml", "--auto"),
    "me": (
        _LEG_PYTHON, "-m", "halal_scraper",
        "--config", "config_legs/leg_salalah.yaml", "--auto",
    ),
}
_LEG_LOG_DIR = "A:/scraper/logs"


def _spawn_leg(command: list[str], *, cwd: str | None = None) -> str:
    """Start a scraper leg WITHOUT holding the engine (2026-11-16).

    A leg is a long job (hours). The first wiring used `subprocess.run`, which
    blocks until the scrape finishes, so flipping `auto_restart_leg` on would
    park a whole engine cycle on the start and trip STAGE OVERRUN or the
    watchdog. Detached process, own group, output to a log the digest names.
    """
    logs = Path(_LEG_LOG_DIR)
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"leg_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.log"
    flags = 0
    if os.name == "nt":  # Windows: no console, no Ctrl-C inheritance
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    with open(log, "ab") as handle:
        subprocess.Popen(  # noqa: S603 — fixed argv, no shell, owner-armed
            command, cwd=cwd or _LEG_CWD, stdin=subprocess.DEVNULL,
            stdout=handle, stderr=subprocess.STDOUT, close_fds=True,
            creationflags=flags,
        )
    return str(log)


def list_scraper_runs(db_path: Any = None) -> dict[str, int]:
    """run_id -> live (non-archived) row count, READ-ONLY (ADR-3, gate 6)."""
    from outreach.scraper_adapter import SCRAPER_DB, open_scraper

    con = open_scraper(db_path if db_path is not None else SCRAPER_DB)
    try:
        rows = con.execute(
            "SELECT run_id, COUNT(*) FROM scraped_places"
            " WHERE COALESCE(is_archived, 0) = 0 AND run_id IS NOT NULL"
            " GROUP BY run_id"
        ).fetchall()
    finally:
        con.close()
    return {str(r[0]): int(r[1]) for r in rows}


def pending_runs(db: "Database", db_path: Any = None) -> dict[str, int]:
    """Runs never marked complete, or whose scraper rows grew since (addendum 1).

    The ingested set lives in OUR config (the scraper DB is never written):
    run_id -> {"rows": n, "completed_at": iso}. A partially processed run is
    not tracked, so it stays pending until `intake_run` reports complete.
    """
    tracked: dict[str, dict] = get_config(db, "ingested_runs", {}) or {}
    counts = list_scraper_runs(db_path)
    return {
        run_id: n
        for run_id, n in sorted(counts.items())
        if run_id not in tracked or n > int(tracked[run_id].get("rows", 0))
    }


def dispatch_ready_by_region(db: "Database") -> dict[str, int]:
    """QUALIFIED + ROUTED leads not yet sent, per region (addendum 2)."""
    return {
        str(region): int(n)
        for region, n in db.conn.execute(
            "SELECT region, COUNT(*) FROM leads"
            " WHERE state IN ('QUALIFIED', 'ROUTED') AND previously_contacted=0"
            " AND region IS NOT NULL GROUP BY region"
        )
    }


@dataclass(frozen=True)
class LowLeadReport:
    ready_by_region: dict
    starving: tuple
    alert: str | None


def scan_low_leads(db: "Database", *, now: datetime | None = None) -> LowLeadReport:
    """Dispatch-ready stock per region vs `low_lead_threshold` (default 10).

    Silent on an empty DB (pre-intake is not starvation). One digest alert
    per starvation episode: the episode signature (starving regions) is kept
    in config and cleared on recovery, so a draining queue cannot spam.
    """
    total = int(db.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0])
    if total == 0:
        return LowLeadReport({}, (), None)
    threshold = int(get_config(db, "low_lead_threshold", 10))
    ready = dispatch_ready_by_region(db)
    regions = [
        str(r[0])
        for r in db.conn.execute(
            "SELECT DISTINCT region FROM leads WHERE region IS NOT NULL"
        )
    ]
    starving = tuple(sorted(r for r in regions if ready.get(r, 0) < threshold))
    if not starving:
        if get_config(db, "low_lead_alerted", ""):
            set_config(db, "low_lead_alerted", "")  # episode over
        return LowLeadReport(ready, (), None)
    signature = "|".join(starving)
    if get_config(db, "low_lead_alerted", "") == signature:
        return LowLeadReport(ready, starving, None)  # already alerted this episode
    counts = ", ".join(f"{r}={ready.get(r, 0)}" for r in starving)
    body = (
        f"LOW LEADS: {counts} dispatch-ready (threshold {threshold})"
        " — scraper leg needed"
    )
    with db.conn:
        db.conn.execute("INSERT INTO digest(body) VALUES (?)", (body,))
        set_config(db, "low_lead_alerted", signature)
    return LowLeadReport(ready, starving, body)


def maybe_restart_leg(
    db: "Database",
    region: str,
    *,
    now: datetime | None = None,
    runner: Any = None,
) -> str | None:
    """Start the region's scraper leg via the D10 invocation (addendum 2).

    Gate: config `auto_restart_leg` (default FALSE -> alert-only). Cooldown
    `auto_restart_cooldown_h` (default 6) per region so one starving pass
    cannot respawn the leg in a loop. Returns the digest body when started.
    """
    if not bool(get_config(db, "auto_restart_leg", False)):
        return None
    command = LEG_COMMANDS.get(region)
    if command is None:
        return None
    moment = now or datetime.now(timezone.utc)
    cooldown_h = float(get_config(db, "auto_restart_cooldown_h", 6))
    starts: dict[str, str] = dict(get_config(db, "leg_starts", {}) or {})
    last = starts.get(region)
    if last is not None:
        elapsed = (moment - datetime.fromisoformat(last)).total_seconds()
        if elapsed < cooldown_h * 3600:
            return None
    spawn = runner if runner is not None else _spawn_leg
    try:
        log = spawn(list(command), cwd=_LEG_CWD)
    except Exception as exc:  # noqa: BLE001 — a failed start must not kill the cycle
        # No cooldown stamp: the next pass may retry, but the digest says it
        # once a day so a broken venv cannot spam the owner every 90 seconds.
        from outreach.caps import PKT, pkt_date

        day_key = f"leg_fail_day:{region}"
        today = str(pkt_date(moment))
        if get_config(db, day_key, "") != today:
            body = f"SCRAPER LEG FAILED ({region}): {type(exc).__name__}: {exc}"[:300]
            with db.conn:
                db.conn.execute("INSERT INTO digest(body) VALUES (?)", (body,))
                set_config(db, day_key, today)
        return None
    starts[region] = moment.isoformat()
    set_config(db, "leg_starts", starts)
    body = f"SCRAPER LEG STARTED ({region}): {' '.join(command)}"
    if isinstance(log, str) and log:
        body += f" -> {log}"
    with db.conn:
        db.conn.execute("INSERT INTO digest(body) VALUES (?)", (body,))
    return body


@dataclass(frozen=True)
class WatchReport:
    ingested: tuple
    completed: tuple
    low_lead_alert: str | None
    leg_starts: tuple
    truncated: bool = False  # ran out of its wall budget mid-run (R2)


# --- refresh schedule (R2, 2026-11-13) ---------------------------------------
#
# WHY: `refresh_leads` walks ~2,500 rows (~15 min) and used to run inside the
# same cycle as dispatch, which is why the heartbeat stamped 64 s past its own
# promise and why a refresh could sit exactly on the watchdog's 900 s grace.
# The pass is now a SCHEDULED stage: off-window (never competing with a send
# window), once per PKT day, at/after its hour, and bounded by a wall budget.
# Consequence, stated so it is not a surprise: a machine that is off through
# the off-window hours does not refresh until the next off-window night.
REFRESH_DAY_KEY = "enrichment_day"        # PKT date whose refresh already ran
REFRESH_HOUR_KEY = "enrichment_hour_pkt"  # earliest PKT hour (default 04:00)
DEFAULT_REFRESH_HOUR = 4


def refresh_due(db: "Database", now: datetime | None = None) -> bool:
    """May the off-window enrichment pass run now? (schedule law, R2)

    Three conditions, all measured from config rows: no ADR-9 session window
    is open, the PKT clock is at/after `enrichment_hour_pkt`, and today's PKT
    date is not already stamped in `enrichment_day`.
    """
    from outreach.caps import PKT, pkt_date
    from outreach.dispatcher import ensure_dispatcher_config, in_window

    ensure_dispatcher_config(db)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    windows = get_config(db, "session_windows") or {}
    if any(in_window(db, region, moment) for region in windows):
        return False
    hour = int(get_config(db, REFRESH_HOUR_KEY, DEFAULT_REFRESH_HOUR))
    if moment.astimezone(PKT).hour < hour:
        return False
    return get_config(db, REFRESH_DAY_KEY, "") != pkt_date(moment)


def mark_refresh_done(db: "Database", now: datetime | None = None) -> None:
    """Stamp the PKT day whose refresh pass completed (one pass per day)."""
    from outreach.caps import pkt_date

    set_config(db, REFRESH_DAY_KEY, pkt_date(now or datetime.now(timezone.utc)))


def _record_refresh_budget(
    db: "Database",
    now: datetime,
    done: int,
    elapsed: float,
    budget: float,
) -> None:
    """A truncated pass is a fact the owner can read, never a silent stop."""
    with db.conn:
        db.conn.execute(
            "INSERT INTO digest(body) VALUES (?)",
            (
                f"REFRESH BUDGET: stopped after {done} run(s) at {elapsed:.0f}s"
                f" (budget {budget:.0f}s); next pass resumes",
            ),
        )


def watcher_tick(
    db: "Database",
    *,
    db_path: Any = None,
    transport: Any = None,
    sleep_s: float = 0.5,
    limit: int | None = None,
    now: datetime | None = None,
    budget_s: float | None = None,
) -> WatchReport:
    """One engine-cycle intake pass (addendum 1 + 2).

    Polls the read-only scraper DB for new/grown runs and intakes them with
    a bounded LLM budget (`intake_per_cycle`, default 25 per cycle) — the
    state machine makes every pass resumable, so a big run catches up over
    cycles instead of stalling the loop. Then scans low-lead stock and
    (only if the owner enabled it) restarts a starving region's leg.

    `budget_s` is the pass's wall budget (R2): the check sits BETWEEN runs, so
    one run always lands (a pass always makes progress) and the remainder is
    picked up by the next pass with a digest row naming the truncation.
    """
    moment = now or datetime.now(timezone.utc)
    if limit is None:
        limit = int(get_config(db, "intake_per_cycle", 25))
    started = time.monotonic()
    truncated = False
    ingested: list[str] = []
    completed: list[str] = []
    for run_id in pending_runs(db, db_path):
        if ingested and budget_s is not None and time.monotonic() - started >= budget_s:
            truncated = True
            _record_refresh_budget(
                db, moment, len(ingested), time.monotonic() - started, budget_s
            )
            break
        report = intake_run(
            db, run_id, transport=transport, sleep_s=sleep_s,
            limit=limit, db_path=db_path,
        )
        ingested.append(run_id)
        if report.complete:
            # intake_run itself records rows/completed_at in ingested_runs
            completed.append(run_id)
    low = scan_low_leads(db, now=moment)
    legs = tuple(
        region
        for region in low.starving
        if maybe_restart_leg(db, region, now=moment) is not None
    )
    return WatchReport(
        ingested=tuple(ingested),
        completed=tuple(completed),
        low_lead_alert=low.alert,
        leg_starts=legs,
        truncated=truncated,
    )
