"""v1.1 account registry, L1 assignment ledger and per-account ramp caps.

Batches 0/2/3 of the frozen v1.1 blueprint (`v1.1 blueprint.md`):

  * **L1 — one lead, one account, ever.** `lead_accounts` (PK on lead_id) is
    written at first claim; a second account asking for the same lead gets
    `AssignmentConflict`, never a silent reassignment.
  * **L2 — per-account caps with an ADR-8 ramp.** Each account ramps 5/day in
    week 1, 10/day in week 2, 15/day after; a halted account has capacity 0.
    The fleet total is the sum of per-account caps — a daily target can NEVER
    raise an account's cap.
  * **L3 (partial) — behaviour separation.** Accounts carry their own Chrome
    profile dir, status and window offset; selection is by canal + status +
    capacity + offset order, so the fleet never selects the same account by
    accident.

Nothing here sends: selection is a pure read + one assignment write. The
v1.0 single-account lane keeps byte-identical behaviour because the seeded
fleet has exactly one account per channel (`ig1`/`wa1`, the live profile).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from outreach.caps import PKT, pkt_date
from outreach.store import get_config

if TYPE_CHECKING:
    from outreach.store import Database

LEGACY_PROFILE = "Outreach"  # ADR-13: the owner's one logged-in profile
ACTIVE_STATUSES = ("WARMING", "ACTIVE", "HEALTHY")
HALTED = "HALTED"
FULL_CAP = 15  # ADR-8 steady-state cap per account
#: Owner ruling 2026-11-11: a new account's first week is ENGAGEMENT WARM-UP
#: ONLY (follows/likes, no DMs at all), then 10/day, then the full cap. The
#: week-1 DM cap is therefore 0, not the old 5.
DM_WARMUP_ONLY_WEEKS = 1
RAMP_CAPS = {2: 10}  # week -> daily DM cap; week >= 3 -> FULL_CAP


class AssignmentConflict(Exception):
    """L1: this lead already belongs to a different account."""


class NoAccountAvailable(Exception):
    """No eligible account for the channel right now (all full/halted)."""


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def seed_fleet(db: "Database", *, now: datetime | None = None) -> tuple[str, ...]:
    """Idempotently register the live single account as `ig1` and `wa1`.

    The owner's existing profile IS account 1 on both channels, so the cutover
    is a no-op for behaviour: same profile dir, same cap, same lead flow. The
    pre-fleet history in `message_log` is attributed to those ids (the only
    account that existed then) so per-account day counts start truthful.
    """
    created: list[str] = []
    ig_start = get_config(db, "ig_warmup_start")
    for channel, account_id in (("ig", "ig1"), ("wa", "wa1")):
        row = db.conn.execute(
            "SELECT 1 FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        if row:
            continue
        warmup = ig_start if channel == "ig" else None
        with db.conn:
            db.conn.execute(
                "INSERT INTO accounts(id, channel, profile_dir, status, ramp_week,"
                " warmup_start, window_offset_min, created_at)"
                " VALUES (?, ?, ?, 'ACTIVE', 1, ?, 0, ?)",
                (account_id, channel, LEGACY_PROFILE, warmup, _now_iso(now)),
            )
        created.append(account_id)
    for channel, account_id in (("ig", "ig1"), ("wa", "wa1")):
        if db.conn.execute(
            "SELECT 1 FROM message_log WHERE channel=? AND account_id=''",
            (channel,),
        ).fetchone():
            with db.conn:
                db.conn.execute(
                    "UPDATE message_log SET account_id=? WHERE channel=?"
                    " AND account_id=''",
                    (account_id, channel),
                )
    return tuple(created)


def register_account(
    db: "Database",
    account_id: str,
    channel: str,
    profile_dir: str,
    *,
    warmup_start: str | None = None,
    window_offset_min: int = 0,
    status: str = "WARMING",
    now: datetime | None = None,
) -> str:
    """Register one fleet account (v1.1 batch 4 seam; the CLI is batch 12).

    The profile name is validated against the ADR-13 lane BEFORE the row
    lands: an account can never be pointed at an owner profile, and a
    duplicate id is refused rather than silently re-pointed at a different
    session. `window_offset_min` is the account's own stagger inside the
    region windows (L3: the fleet never fires all at once).
    """
    from outreach.fleet import validate_profile_name

    profile = validate_profile_name(profile_dir)
    if channel not in ("ig", "wa"):
        raise ValueError(f"channel must be 'ig' or 'wa', got {channel!r}")
    if get_account(db, account_id) is not None:
        raise ValueError(f"account {account_id!r} already exists")
    with db.conn:
        db.conn.execute(
            "INSERT INTO accounts(id, channel, profile_dir, status, ramp_week,"
            " warmup_start, window_offset_min, created_at)"
            " VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            (
                account_id,
                channel,
                profile,
                status,
                # no stamp supplied => the ramp starts today: week 1 is
                # engagement warm-up only, never full capacity by omission
                warmup_start or pkt_date(now or datetime.now(timezone.utc)),
                int(window_offset_min),
                _now_iso(now),
            ),
        )
    return account_id


def get_account(db: "Database", account_id: str) -> Any:
    return db.conn.execute(
        "SELECT * FROM accounts WHERE id=?", (account_id,)
    ).fetchone()


def _weeks_since(start: str | None, now: datetime) -> int:
    if not start:
        return 99  # no ramp stamp: not a warming account
    try:
        began = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    except ValueError:
        return 99
    if began.tzinfo is None:
        began = began.replace(tzinfo=timezone.utc)
    days = (now.astimezone(timezone.utc) - began).total_seconds() / 86400
    return max(1, int(days // 7) + 1)


def cap_for(db: "Database", account: Any, now: datetime) -> int:
    """L2: 0 → 10 → 15 by account age; halted = 0; a daily target never wins.

    Week 1 is engagement warm-up only (owner ruling 2026-11-11), so a freshly
    stamped account contributes 0 DM capacity: it can follow and like through
    the engagement queue, and `pick_account` will not hand it a lead.
    An account with no ramp stamp keeps the pre-fleet behaviour (full cap).
    """
    if account is None or account["status"] == HALTED:
        return 0
    week = _weeks_since(account["warmup_start"], now)
    if week <= DM_WARMUP_ONLY_WEEKS:
        return 0
    return RAMP_CAPS.get(week, FULL_CAP)


def _day_of(sent_at: str) -> str:
    dt = datetime.fromisoformat(str(sent_at).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PKT).date().isoformat()


def sends_today(db: "Database", account_id: str, channel: str, now: datetime) -> int:
    """Sends this account landed on the given PKT day (assignment-attributed)."""
    day = pkt_date(now)
    rows = db.conn.execute(
        "SELECT sent_at FROM message_log WHERE channel=? AND account_id=?",
        (channel, account_id),
    ).fetchall()
    return sum(1 for r in rows if _day_of(r["sent_at"]) == day)


def assigned_account(db: "Database", lead_id: int) -> str | None:
    """L1: the account this lead belongs to (empty when never claimed)."""
    row = db.conn.execute(
        "SELECT account_id FROM lead_accounts WHERE lead_id=?", (lead_id,)
    ).fetchone()
    return str(row["account_id"]) if row else None


def assign(
    db: "Database", lead_id: int, account_id: str, *, now: datetime | None = None
) -> bool:
    """Write the L1 assignment exactly once; a different account is refused.

    Returns True when this call created the assignment, False when it already
    existed for the SAME account (idempotent claim retry). Raises
    `AssignmentConflict` for any other account — there is no reassignment path
    in the claim; B9 redistribution owns whatever the owner approves.
    """
    existing = assigned_account(db, lead_id)
    if existing is not None:
        if existing == account_id:
            return False
        raise AssignmentConflict(
            f"lead {lead_id} is assigned to {existing}; {account_id} may not touch it (L1)"
        )
    with db.conn:  # ledger + the readable mirror land together
        db.conn.execute(
            "INSERT INTO lead_accounts(lead_id, account_id, assigned_at)"
            " VALUES (?, ?, ?)",
            (lead_id, account_id, _now_iso(now)),
        )
        db.conn.execute(
            "UPDATE leads SET account_id=? WHERE id=?", (account_id, lead_id)
        )
    return True


def _window_rule(db: "Database", region: str | None) -> dict[str, Any] | None:
    windows = get_config(db, "session_windows") or {}
    rule = (windows or {}).get(region or "")
    return rule if isinstance(rule, dict) else None


def _minutes(hhmm: object) -> int:
    parts = str(hhmm).split(":")
    return int(parts[0]) * 60 + int(parts[1])


def account_window_open(
    db: "Database", region: str | None, account: Any, now: datetime
) -> bool:
    """L3 stagger: is THIS account inside its own slice of the region window?

    The region window stays the outer law (`in_window`, including a B27
    recovery extension); the account's `window_offset_min` pushes its START
    later while every account still stops at the shared window end. An
    offset of 0 is the legacy account and keeps byte-identical behaviour.
    An offset that eats the whole window never opens (fail-closed: a
    misconfigured account parks, it never sends outside hours).
    """
    from outreach.dispatcher import in_window

    if account is None or account["status"] == HALTED:
        return False
    if not in_window(db, region, now):
        return False
    offset = int(account["window_offset_min"] or 0)
    if offset <= 0:
        return True
    rule = _window_rule(db, region)
    if rule is None:
        return False
    start = _minutes(rule["start"]) + offset
    end = _minutes(rule["end"])
    if start >= end:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    pkt = now.astimezone(PKT)
    return pkt.hour * 60 + pkt.minute >= start


def seconds_until_any_account_open(
    db: "Database",
    region: str | None,
    now: datetime,
    channels: Any = None,
) -> float | None:
    """Wake math for the stagger: 0.0 = some account may send now,
    > 0 = that many seconds to the next account window, None = no active
    account can ever act in this region (every relevant lane halted/full).

    `channels` narrows the question to the lanes that actually hold work in
    the region: the WA account being inside its window is not work when the
    queued leads are all IG (that mistake would wake=0 with nothing to do).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    wanted = tuple(
        channel for channel in ("ig", "wa") if channels is None or channel in channels
    )
    accounts = [
        account
        for channel in wanted
        for account in eligible_accounts(db, channel)
        # an account already at its own cap is not wake-able work (the claim
        # loop would refuse it, so wake=0 would hot-spin the engine)
        if sends_today(db, account["id"], channel, now) < cap_for(db, account, now)
    ]
    if not accounts:
        return None
    rule = _window_rule(db, region)
    pkt = now.astimezone(PKT)
    now_min = pkt.hour * 60 + pkt.minute
    waits: list[float] = []
    for account in accounts:
        if account_window_open(db, region, account, now):
            return 0.0
        offset = int(account["window_offset_min"] or 0)
        if rule is None or offset <= 0:
            continue
        start = _minutes(rule["start"]) + offset
        end = _minutes(rule["end"])
        if start >= end or now_min >= start:
            continue
        waits.append((start - now_min) * 60.0)
    return min(waits) if waits else None


def eligible_accounts(db: "Database", channel: str) -> list[Any]:
    """Active accounts of one channel, staggered by window offset then id."""
    marks = ",".join("?" for _ in ACTIVE_STATUSES)
    return list(
        db.conn.execute(
            f"SELECT * FROM accounts WHERE channel=? AND status IN ({marks})"
            " ORDER BY window_offset_min, id",
            (channel, *ACTIVE_STATUSES),
        ).fetchall()
    )


def channel_cap(db: "Database", channel: str, now: datetime) -> int | None:
    """L2 fleet ceiling: the sum of the channel's ACTIVE account caps.

    None when the channel has no active account, so `caps.current_cap` can
    fall back to the legacy single-lane number instead of pretending a dead
    lane has capacity.
    """
    accounts = eligible_accounts(db, channel)
    if not accounts:
        return None
    return sum(cap_for(db, account, now) for account in accounts)


#: What the DISK says about an account's browser profile. `unknown` is not a
#: failure: the one account that is really signed in reads `unknown` while its
#: browser holds the cookie DB open (measured on ig1, 2026-11-15).
def profile_health(
    db: "Database", account_id: str, *, root: str | Path | None = None
) -> str:
    """`signed_in` / `no_session` / `unknown` / `missing` / `n/a`, never raises."""
    from outreach.fleet import (
        ProfileMissing,
        ProfileRefused,
        instagram_session,
    )

    account = get_account(db, account_id)
    if account is None:
        return "missing"
    if account["channel"] != "ig":
        return "n/a"  # WhatsApp sends from a phone session, not a Chrome profile
    try:
        return instagram_session(str(account["profile_dir"]), root)
    except (ProfileMissing, ProfileRefused):
        return "missing"


def usable_account(
    db: "Database", account_id: str, *, root: str | Path | None = None
) -> bool:
    """Can this account actually act? Only a MISSING profile or a proven
    logged-out one is refused — `unknown` keeps the lane, because excluding it
    would stop the only account that sends."""
    return profile_health(db, account_id, root=root) not in ("missing", "no_session")


def health_reason(
    db: "Database", account_id: str, *, root: str | Path | None = None
) -> str | None:
    """The sentence to show for an unusable lane, or None when it can act."""
    health = profile_health(db, account_id, root=root)
    if health == "missing":
        return f"profile for {account_id} is not on disk (never created or signed in)"
    if health == "no_session":
        account = get_account(db, account_id)
        profile = str(account["profile_dir"]) if account is not None else "?"
        return f"no Instagram session in {profile} (logged out)"
    return None


def pick_account(
    db: "Database",
    channel: str,
    lead_id: int,
    now: datetime,
    *,
    region: str | None = None,
    root: str | Path | None = None,
) -> Any:
    """Two-phase claim, phase 1: which account may send to this lead NOW.

    An assigned lead keeps its account (L1) and is refused — not silently
    moved — when that account is halted, at cap, or outside its own
    staggered window. An unassigned lead takes the first eligible account
    with room AND an open stagger slice, and the assignment is persisted.
    `region=None` keeps the pre-batch-4 behaviour (no stagger filter) for
    manual/command_center runs.
    """
    current = assigned_account(db, lead_id)
    if current is not None:
        account = get_account(db, current)
        if account is None or account["status"] == HALTED:
            raise NoAccountAvailable(
                f"lead {lead_id} is assigned to halted/unknown account {current} (L1)"
            )
        # Live 2026-11-15: the registry said four senders, the disk said one. A
        # lane that is not signed in never takes a send (it would meet a login
        # wall while every counter said healthy).
        unhealthy = health_reason(db, current, root=root)
        if unhealthy is not None:
            raise NoAccountAvailable(f"account {current} cannot send: {unhealthy}")
        if sends_today(db, current, channel, now) >= cap_for(db, account, now):
            raise NoAccountAvailable(f"account {current} is at its daily cap")
        if region is not None and not account_window_open(
            db, region, account, now
        ):
            raise NoAccountAvailable(
                f"account {current} is outside its staggered window (L1: parked)"
            )
        return account
    unhealthy_lanes: list[str] = []
    for account in eligible_accounts(db, channel):
        unhealthy = health_reason(db, str(account["id"]), root=root)
        if unhealthy is not None:
            # a lane with no login is not a sender, whatever its row says; the
            # reason is carried into the refusal so the operator sees WHICH lane
            # and WHY instead of "every account is at cap"
            unhealthy_lanes.append(f"{account['id']}: {unhealthy}")
            continue
        if sends_today(
            db, account["id"], channel, now
        ) >= cap_for(db, account, now):
            continue  # L2: this one is full; the fleet keeps going on the others
        if region is not None and not account_window_open(
            db, region, account, now
        ):
            continue  # L3: this account's stagger slice has not opened yet
        assign(db, lead_id, account["id"], now=now)
        return account
    if unhealthy_lanes:
        raise NoAccountAvailable(
            f"no {channel} account can send: " + "; ".join(unhealthy_lanes)
        )
    raise NoAccountAvailable(f"every {channel} account is at cap or halted")
