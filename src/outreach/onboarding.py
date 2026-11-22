"""v1.1 batch 12: onboarding an account safely (`add_account`).

Blueprint batch 12: "Onboarding flow: `add_account` CLI (profile path, ramp
seed, owner confirmation)", done when onboarding account 2 works end to end.

The dangerous part of onboarding is not inserting a row; it is everything the
row can point at. So the order is fixed and every step fails closed:

  1. the owner must confirm (`--confirm`), because this decides who sends;
  2. the profile name must be inside the automation lane (ADR-13): an owner
     profile can never be registered;
  3. the profile must ALREADY exist on disk - Chrome happily creates a fresh
     profile on launch, and that is how a fleet ends up "healthy" while every
     account stares at a login wall;
  4. the id and the profile must not be in use by another account;
  5. only then does the registry row land, seeded to week 1 (cap 5) and
     WARMING, with the ramp clock starting today in PKT.

A registered but unsigned account is legitimate (it is exactly the state the
owner signs in from), so a missing session is a loud warning, not a refusal.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from outreach.accounts import get_account, register_account
from outreach.caps import pkt_date
from outreach.fleet import (
    instagram_session,
    require_profile,
    validate_profile_name,
)

if TYPE_CHECKING:
    from outreach.store import Database

ACCOUNT_ID_RE = re.compile(r"^(ig|wa)[0-9]+$")
MAX_OFFSET_MIN = 600  # ten hours: an offset beyond the window just parks


class OnboardingRefused(RuntimeError):
    """The account cannot be registered as asked; the message says why."""


def add_account(
    db: "Database",
    account_id: str,
    channel: str,
    profile_dir: str,
    *,
    offset_min: int = 0,
    now: datetime | None = None,
    root: str | None = None,
) -> dict[str, str]:
    """Register one account, or raise `OnboardingRefused`.

    Returns a small report the CLI prints: the account, its ramp start, its
    cap and the profile's session state.
    """
    moment = now or datetime.now(timezone.utc)
    if not ACCOUNT_ID_RE.match(str(account_id)):
        raise OnboardingRefused(
            f"account id {account_id!r} must look like 'ig2' or 'wa2'"
        )
    if channel not in ("ig", "wa"):
        raise OnboardingRefused(f"channel must be 'ig' or 'wa', got {channel!r}")
    if not str(account_id).startswith(channel):
        raise OnboardingRefused(
            f"account id {account_id!r} does not match channel {channel!r}"
        )
    if int(offset_min) < 0 or int(offset_min) >= MAX_OFFSET_MIN:
        raise OnboardingRefused(
            f"offset must be 0..{MAX_OFFSET_MIN - 1} minutes, got {offset_min}"
        )
    if get_account(db, account_id) is not None:
        raise OnboardingRefused(f"account {account_id!r} already exists")
    taken = db.conn.execute(
        "SELECT id FROM accounts WHERE profile_dir=?", (str(profile_dir),)
    ).fetchone()
    if taken is not None:
        raise OnboardingRefused(
            f"profile {profile_dir!r} is already used by {taken['id']!r}"
        )
    profile = validate_profile_name(profile_dir)  # ADR-13, before any row
    require_profile(profile, root)  # fail closed: never auto-create
    register_account(
        db,
        account_id,
        channel,
        profile,
        warmup_start=pkt_date(moment),
        window_offset_min=int(offset_min),
        status="WARMING",
        now=moment,
    )
    session = instagram_session(profile, root) if channel == "ig" else "n/a"
    return {
        "account": account_id,
        "channel": channel,
        "profile": profile,
        "ramp_start": pkt_date(moment),
        "cap_week_1": "5",
        "offset_min": str(int(offset_min)),
        "session": session,
    }
