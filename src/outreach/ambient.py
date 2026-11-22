"""O8 (owner ruling 2026-11-14): ambient activity, 60-120 minutes a day.

The owner's answer was "80 min/day scroll, yes — sometimes 80, sometimes 60,
sometimes 120, get me?" So the budget is a **range with a daily number**, not a
fixed constant: `ambient_minutes_today` sets today's minutes, clamped into
`[min_minutes, max_minutes]` (60..120), defaulting to 80. Nothing here invents a
budget the owner did not set, and a day that is spent is spent.

One session is `session_minutes` (5, sized to fit the engage stage's 300 s
budget) of reels scrolling plus up to five
follows **for the whole day**, never per session — the four accounts share one
act budget, and a follow is the one ambient act the world can see.

The driver is deliberately small and fail-soft: it navigates, presses Down on a
slow step, follows only through `engagement.perform_act` (the one function that
knows the real controls), and records time only for a session that actually ran.
"""
from __future__ import annotations

import hashlib
import random
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Sequence

from outreach.caps import pkt_date
from outreach.store import get_config, set_config

if TYPE_CHECKING:
    from outreach.store import Database

CONFIG_KEY = "ambient_config"
TODAY_KEY = "ambient_today"
BUDGET_KEY = "ambient_minutes_today"
REELS_URL = "https://www.instagram.com/reels/"

DEFAULTS: dict[str, Any] = {
    "enabled": True,  # ruling received 2026-11-14: the owner said yes
    "min_minutes": 60,
    "max_minutes": 120,
    "default_minutes": 80,
    # a session must fit inside the engage stage budget (300 s), so the day's
    # minutes accrue over several sessions instead of one 20-minute dwell
    "session_minutes": 5,
    "max_follows": 5,
}

#: One reel is watched ~this long before the next Down; injected sleeps make the
#: driver testable without a browser or a wall clock.
STEP_S = 8.0
#: The shortest scroll worth opening a browser for. 3, not 5 (O8 live fix
#: 2026-11-14): the engage stage gives ambient one 4-minute slice per cycle, and
#: a 5-minute floor silently rounded that slice up to 304 s and overran the
#: stage budget it was sized against.
MIN_USEFUL_SESSION_MIN = 3

#: O8b (owner request 2026-11-15): the day's minutes are SPLIT across the signed-in
#: lanes, unevenly, and the split changes day to day. One lane taking all 80 every
#: day is a pattern; a per-day plan with different weights is not.
PLAN_KEY = "ambient_split"
MIN_LANE_MINUTES = 6  # nobody sits at zero while another lane scrolls for an hour
WEIGHT_RANGE = (0.55, 2.0)  # per-lane weight draw: a 3.6x spread between luckiest and unluckiest


def _rng(day: str, lanes: Sequence[str]) -> random.Random:
    """Stable within a day, different across days (a hash seed, not `hash()`,
    which Python randomises per process)."""
    seed = hashlib.sha256(f"{day}:{','.join(sorted(lanes))}".encode()).hexdigest()
    return random.Random(int(seed[:16], 16))


def daily_split(
    db: "Database", now: datetime, lanes: Sequence[str]
) -> dict[str, int]:
    """Today's per-lane minutes: they sum to the owner's number, never equal.

    Cached for the day (`ambient_split`), so the plan the owner was shown is the
    plan that runs, and written to the digest once — an unequal split nobody can
    see is indistinguishable from a bug.
    """
    day = _day(now)
    lanes = tuple(sorted({str(lane) for lane in lanes if lane}))
    if not lanes:
        return {}
    # Plan what is LEFT of the day, not the whole day: a plan written at 10:00 after
    # 48 minutes were already spent must not promise 80 again (live 2026-11-15: it
    # did, and read as 27/22/31 against a 32-minute pool).
    budget = max(0, minutes_today(db, now) - int(_today(db, now)["spent_minutes"]))
    cached = get_config(db, PLAN_KEY)
    if (
        isinstance(cached, dict)
        and cached.get("day") == day
        and tuple(cached.get("lanes") or ()) == lanes
        and isinstance(cached.get("plan"), dict)
    ):
        return {str(k): int(v) for k, v in cached["plan"].items()}

    rng = _rng(day, lanes)
    weights = {lane: rng.uniform(*WEIGHT_RANGE) for lane in lanes}
    total = sum(weights.values())
    shares = {lane: int(weights[lane] / total * budget) for lane in lanes}
    # hand out the rounding remainder, largest weight first
    for lane in sorted(lanes, key=lambda l: -weights[l])[: budget - sum(shares.values())]:
        shares[lane] += 1
    # nobody at zero: take the floor from the lane that can spare it
    floor = min(MIN_LANE_MINUTES, budget // len(lanes))
    for lane in lanes:
        if shares[lane] >= floor:
            continue
        need = floor - shares[lane]
        donor = max(lanes, key=lambda l: shares[l])
        take = min(need, shares[donor] - floor)
        shares[lane] += take
        shares[donor] -= take

    set_config(db, PLAN_KEY, {"day": day, "lanes": list(lanes), "plan": shares})
    line = f"AMBIENT PLAN {day}: " + " / ".join(
        f"{lane} {shares[lane]}" for lane in lanes
    )
    with db.conn:
        db.conn.execute("INSERT INTO digest(body) VALUES (?)", (line,))
    return shares


def share_remaining(
    db: "Database", plan: dict[str, int] | None, now: datetime, lane: str
) -> int:
    """Minutes still owed to ONE lane today (0 for a lane that is not in the plan)."""
    if not plan:
        return remaining(db, now)[0]
    owed = int(plan.get(str(lane), 0))
    spent = int((_today(db, now).get("per_lane") or {}).get(str(lane), {}).get("minutes", 0))
    return max(0, owed - spent)


def ensure_config(db: "Database") -> None:
    if get_config(db, CONFIG_KEY) is None:
        set_config(db, CONFIG_KEY, dict(DEFAULTS))


def config(db: "Database") -> dict[str, Any]:
    raw = get_config(db, CONFIG_KEY) or {}
    merged = dict(DEFAULTS)
    if isinstance(raw, dict):
        merged.update({k: raw[k] for k in DEFAULTS if k in raw})
    return merged


def _day(now: datetime) -> str:
    return pkt_date(now if now.tzinfo else now.replace(tzinfo=timezone.utc))


def minutes_today(db: "Database", now: datetime) -> int:
    """Today's budget: the owner's number, clamped to the agreed range."""
    cfg = config(db)
    raw = get_config(db, BUDGET_KEY)
    minutes = None
    if isinstance(raw, dict) and raw.get("day") == _day(now):
        try:
            minutes = int(raw.get("minutes"))
        except (TypeError, ValueError):
            minutes = None
    if minutes is None:
        minutes = int(cfg["default_minutes"])
    return max(int(cfg["min_minutes"]), min(int(cfg["max_minutes"]), minutes))


def set_minutes_today(db: "Database", minutes: int, now: datetime) -> int:
    """Owner (or a plan) chooses today's number; the range is still enforced."""
    cfg = config(db)
    clamped = max(int(cfg["min_minutes"]), min(int(cfg["max_minutes"]), int(minutes)))
    set_config(db, BUDGET_KEY, {"day": _day(now), "minutes": clamped})
    return clamped


def _today(db: "Database", now: datetime) -> dict[str, Any]:
    raw = get_config(db, TODAY_KEY)
    if not isinstance(raw, dict) or raw.get("day") != _day(now):
        return {
            "day": _day(now), "spent_minutes": 0, "follows": 0, "sessions": 0,
            "per_lane": {},
        }
    per_lane = raw.get("per_lane")
    return {
        "day": _day(now),
        "spent_minutes": int(raw.get("spent_minutes") or 0),
        "follows": int(raw.get("follows") or 0),
        "sessions": int(raw.get("sessions") or 0),
        "per_lane": per_lane if isinstance(per_lane, dict) else {},
    }


def remaining(db: "Database", now: datetime) -> tuple[int, int]:
    """(minutes left today, follows left today)."""
    cfg = config(db)
    today = _today(db, now)
    minutes_left = max(0, minutes_today(db, now) - today["spent_minutes"])
    follows_left = max(0, int(cfg["max_follows"]) - today["follows"])
    return minutes_left, follows_left


def run_session(
    db: "Database",
    page: Any,
    account_id: str,
    *,
    now: datetime,
    sleep: Callable[[float], None] | None = None,
    step_s: float = STEP_S,
    minutes: int | None = None,
    plan: dict[str, int] | None = None,
) -> str | None:
    """One ambient session for one account, or None when nothing is owed.

    Returns the digest line it wrote, so a caller can name the act. Nothing is
    recorded when the session could not start: a failure is not activity.
    """
    sleep = sleep or time.sleep  # looked up at call time so a test can replace it
    cfg = config(db)
    if not cfg["enabled"]:
        return None
    minutes_left, follows_left = remaining(db, now)
    if minutes_left < MIN_USEFUL_SESSION_MIN:
        return None
    # O8b: each lane owns a slice of the day. A lane that has spent its share
    # stops even when the global pool still has minutes for the others.
    lane_left = share_remaining(db, plan, now, account_id)
    if lane_left < MIN_USEFUL_SESSION_MIN:
        return None
    session_minutes = int(minutes if minutes is not None else cfg["session_minutes"])
    session_minutes = max(
        MIN_USEFUL_SESSION_MIN, min(session_minutes, minutes_left, lane_left)
    )

    try:
        page.goto(REELS_URL, wait_until="domcontentloaded")
    except Exception:  # noqa: BLE001 — an unreachable surface is not activity
        return None

    # A person does not tap Down every 8.000 s and never pauses on one reel. The
    # target time is fixed (so the budget stays honest), the pace is not: each
    # wait is drawn fresh, and one reel in eight is watched through.
    target_s = session_minutes * 60.0
    pace = random.Random(hashlib.sha256(f"{_day(now)}:{account_id}:{session_minutes}".encode()).hexdigest()[:16])
    elapsed = 0.0
    while elapsed < target_s:
        if pace.random() > 0.12:
            try:
                page.keyboard.press("ArrowDown")
            except Exception:  # noqa: BLE001 — a dead page ends the scroll, not the day
                break
        wait = min(step_s * pace.uniform(0.5, 1.45), target_s - elapsed)
        sleep(wait)
        elapsed += wait

    follows = 0
    if follows_left:
        from outreach import warming
        from outreach.engagement import perform_act

        for handle in warming.niche_handles(db):
            if follows >= follows_left:
                break
            try:
                outcome = perform_act(page, "follow", handle)
            except Exception:  # noqa: BLE001
                continue
            if outcome.ok:
                follows += 1

    today = _today(db, now)
    today["spent_minutes"] += session_minutes
    today["follows"] += follows
    today["sessions"] += 1
    per_lane = dict(today.get("per_lane") or {})
    mine = dict(per_lane.get(account_id) or {})
    mine["minutes"] = int(mine.get("minutes", 0)) + session_minutes
    mine["follows"] = int(mine.get("follows", 0)) + follows
    per_lane[account_id] = mine
    today["per_lane"] = per_lane
    set_config(db, TODAY_KEY, today)
    my_share = (plan or {}).get(account_id)
    mine_note = (
        f" ({mine['minutes']} of {my_share} min for this lane,"
        f" {today['spent_minutes']}/{minutes_today(db, now)} min today)"
        if my_share is not None
        else f" ({today['spent_minutes']}/{minutes_today(db, now)} min today)"
    )
    line = (
        f"AMBIENT [{account_id}]: {session_minutes} min reels, {follows} follow(s)"
        + mine_note
    )
    with db.conn:
        db.conn.execute("INSERT INTO digest(body) VALUES (?)", (line,))
    return line


def status_line(db: "Database", now: datetime) -> str:
    cfg = config(db)
    if not cfg["enabled"]:
        return "AMBIENT: off"
    minutes_left, follows_left = remaining(db, now)
    line = (
        f"AMBIENT: {minutes_today(db, now)} min budget today"
        f" ({minutes_left} left), {follows_left}/{cfg['max_follows']} follows left"
    )
    cached = get_config(db, PLAN_KEY)
    if isinstance(cached, dict) and cached.get("day") == _day(now):
        plan = cached.get("plan") or {}
        spent = (_today(db, now).get("per_lane") or {})
        parts = []
        for lane in sorted(plan):
            used = int((spent.get(lane) or {}).get("minutes", 0))
            parts.append(f"{lane} {used}/{plan[lane]}")
        if parts:
            line += " | plan " + " / ".join(parts)
    return line
