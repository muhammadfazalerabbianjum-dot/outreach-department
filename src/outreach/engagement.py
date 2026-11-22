"""v1.1 batch 5: the engagement core (follow / like), pacing and logging.

Frozen blueprint (`v1.1 blueprint.md`, batch 5) plus its laws:

  * **L6 - pacing.** Every act belongs to one account and lands at least
    `min_gap_s` (90 s) after that account's previous act, capped at
    `per_day` (30) acts on the PKT day. Acts are interleaved with the send
    windows by the caller, never fired as a burst: `can_engage` is the one
    gate, so the engine parks instead of flooding.
  * **L5 - likes and follows are ungated.** Only comments (batch 6) carry a
    confidence score, so `confidence` stays NULL here. Every act is still
    logged with its target and a screenshot (ADR-15).
  * **L1 - one lead, one account.** Acting on a lead assigned to another
    account raises `AssignmentConflict` before any browser work; engagement
    never creates an assignment (the first DM claim owns that).

The TCP/HTTP surface is a page object (playwright in the senders, a fake in
tests). No browser is started here, so this module is safe to call from a
tick that has already decided the account is free.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from outreach.accounts import (
    ACTIVE_STATUSES,
    assigned_account,
    get_account,
)
from outreach.caps import PKT, pkt_date
from outreach.ig_sender import ig_handle
from outreach.store import get_config, set_config
from outreach.wa_sender import _take_screenshot

if TYPE_CHECKING:
    from outreach.store import Database

ENGAGEMENT_DAILY_MAX = 30
ENGAGEMENT_MIN_GAP_S = 90
CAPS_KEY = "engagement_caps"

IG_PROFILE_URL = "https://www.instagram.com/{handle}/"
# Two spellings per control: IG ships both a text button and an aria-labelled
# icon depending on the surface (profile header vs post overlay).
_FOLLOW_SELECTORS = (
    'button:has-text("Follow")',
    'button[aria-label^="Follow"]',
)
_LIKE_SELECTORS = (
    'svg[aria-label="Like"]',
    'button[aria-label="Like"]',
)


class EngagementRefused(RuntimeError):
    """The account may not act right now; the message says why."""


@dataclass(frozen=True)
class ActOutcome:
    """What one act did: `ok=False` means nothing was clicked or logged."""

    ok: bool
    act: str
    target: str
    reason: str = ""


def ensure_engagement_config(db: "Database") -> None:
    """Seed `engagement_caps` once (idempotent, like the daily caps)."""
    if get_config(db, CAPS_KEY) is None:
        set_config(
            db,
            CAPS_KEY,
            {"per_day": ENGAGEMENT_DAILY_MAX, "min_gap_s": ENGAGEMENT_MIN_GAP_S},
        )


def _caps(db: "Database") -> tuple[int, int]:
    raw = get_config(db, CAPS_KEY) or {}
    return (
        int(raw.get("per_day", ENGAGEMENT_DAILY_MAX)),
        int(raw.get("min_gap_s", ENGAGEMENT_MIN_GAP_S)),
    )


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _parse(stamp: Any) -> datetime:
    moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    return _utc(moment)


def acts_today(db: "Database", account_id: str, now: datetime) -> int:
    """Acts this account landed on the PKT day (same day law as sends)."""
    day = pkt_date(now)
    rows = db.conn.execute(
        "SELECT acted_at FROM engagement_log WHERE account_id=?", (account_id,)
    ).fetchall()
    return sum(1 for r in rows if pkt_date(_parse(r["acted_at"])) == day)


def last_act_at(db: "Database", account_id: str) -> datetime | None:
    row = db.conn.execute(
        "SELECT acted_at FROM engagement_log WHERE account_id=?"
        " ORDER BY acted_at DESC, id DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    return _parse(row["acted_at"]) if row else None


def ready_at(db: "Database", account_id: str, now: datetime) -> datetime | None:
    """When this account may act again, or None when it has never acted.

    Wake math for the warming queue: `now >= ready_at` is the pacing half of
    the answer (`can_engage` adds caps and status).
    """
    last = last_act_at(db, account_id)
    if last is None:
        return None
    _, gap = _caps(db)
    return last + timedelta(seconds=gap)


def can_engage(db: "Database", account_id: str, now: datetime) -> str | None:
    """None when an act is allowed now, else the refusal reason."""
    account = get_account(db, account_id)
    if account is None:
        return f"unknown account {account_id}"
    if account["status"] not in ACTIVE_STATUSES:
        return f"account {account_id} is {account['status']}"
    per_day, _ = _caps(db)
    used = acts_today(db, account_id, now)
    if used >= per_day:
        return f"cap: {used}/{per_day} acts for {account_id} on {pkt_date(now)}"
    due = ready_at(db, account_id, now)
    if due is not None and _utc(now) < due:
        remaining = (due - _utc(now)).total_seconds()
        return f"gap: {remaining:.0f}s left of the {_caps(db)[1]}s act spacing"
    return None


def _find(page: Any, selectors: tuple[str, ...]) -> Any:
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count():
            return locator.first
    return None


def perform_act(page: Any, act: str, target: str) -> ActOutcome:
    """Click one real control on `page`; unknown target/act fails closed.

    `follow` takes a handle and opens its profile; `like` takes the post's
    absolute URL (the caller knows which post it warmed up on). A surface
    without the control returns ok=False so the caller logs nothing rather
    than recording an act that never happened.
    """
    if act == "follow":
        url = IG_PROFILE_URL.format(handle=target)
        selectors = _FOLLOW_SELECTORS
    elif act == "like":
        if not str(target).startswith("http"):
            raise ValueError(f"like needs the post URL, got {target!r}")
        url = target
        selectors = _LIKE_SELECTORS
    else:
        raise ValueError(f"unknown engagement act {act!r}")
    page.goto(url, wait_until="domcontentloaded")
    control = _find(page, selectors)
    if control is None:
        return ActOutcome(False, act, target, f"no {act} control at {url}")
    control.click()
    return ActOutcome(True, act, target)


def engage(
    db: "Database",
    page: Any,
    account_id: str,
    lead_id: int,
    act: str,
    *,
    now: datetime,
    evidence_dir: str | Path,
    region: str | None = None,
    target: str | None = None,
) -> ActOutcome:
    """Run one act for one account on one lead, or refuse before the browser.

    Order matters: L1 (who owns the lead) and pacing are decided from the DB
    BEFORE a page is touched, so a refusal costs no browser work and can
    never produce a half-logged act.
    """
    reason = can_engage(db, account_id, now)
    if reason is not None:
        raise EngagementRefused(reason)

    owner = assigned_account(db, lead_id)
    if owner is not None and owner != account_id:
        from outreach.accounts import AssignmentConflict

        raise AssignmentConflict(
            f"lead {lead_id} is assigned to {owner}; {account_id} may not touch"
            " it (L1)"
        )

    if region is not None:
        from outreach.accounts import account_window_open

        if not account_window_open(db, region, get_account(db, account_id), now):
            raise EngagementRefused(
                f"account {account_id} is outside its staggered window"
            )

    row = db.conn.execute(
        "SELECT instagram FROM leads WHERE id=?", (lead_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown lead id={lead_id}")
    handle = ig_handle(row["instagram"])
    if act == "follow":
        if not handle:
            raise EngagementRefused(
                f"lead {lead_id} has no usable instagram handle"
            )
        target_value = handle
    elif act == "like":
        # A like needs the POST (batch 7 supplies the permalink it found).
        if not target:
            raise EngagementRefused(
                f"like on lead {lead_id} needs the post URL, not the handle"
            )
        target_value = target
    else:
        raise ValueError(f"unknown engagement act {act!r}")

    outcome = perform_act(page, act, target_value)
    if not outcome.ok:
        return outcome

    shot = _take_screenshot(page, evidence_dir, lead_id, f"engage_{act}")
    with db.conn:
        db.conn.execute(
            "INSERT INTO engagement_log(account_id, lead_id, act, target,"
            " confidence, screenshot, acted_at) VALUES (?, ?, ?, ?, NULL, ?, ?)",
            (account_id, lead_id, act, outcome.target, str(shot), _utc(now).isoformat()),
        )
    return outcome
