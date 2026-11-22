"""v1.1 batch 7: the pre-DM warm-up step.

Blueprint batch 7: a promising lead (ICP score at or above
`warmup_config.icp_min`) is followed and gets a like on one or two of its
recent posts BEFORE the first DM, on the same day, at least
`warmup_config.lead_min` (30) minutes before it. The DM claim then waits for
`dm_after` instead of messaging a cold profile.

Three laws shape this module:

  * **Pacing (L6)** - one act per call. The engagement gate allows one act
    every 90 s, so a warm-up is a small state machine that resumes on later
    ticks; `warm_up` returns [] when the account is still inside its gap
    rather than bursting.
  * **L1** - acts go through `engagement.engage`, so an account can never
    warm a lead another account owns, and warming never consumes the
    assignment the first DM claim will write.
  * **No silent rollout** - the claim-side gate reads
    `warmup_config.enabled`, which seeds to False. Until the owner turns it
    on, `claim_next` behaves byte-identically to before this batch.

Nothing here writes DM copy: the warm-up is follow/like only (L4).
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from outreach.engagement import (
    ActOutcome,
    EngagementRefused,
    can_engage,
    engage,
)
from outreach.ig_sender import ig_handle
from outreach.store import get_config, set_config

if TYPE_CHECKING:
    from outreach.store import Database

WARMUP_ICP_MIN_DEFAULT = 60
WARMUP_LEAD_MIN = 30
WARMUP_LIKES = 2
WARMUP_KEY = "warmup_config"
IG_PROFILE_URL = "https://www.instagram.com/{handle}/"
#: Every anchor in the grid; the filter below decides what is a post. Live
#: 2026-11-15: this used to require `/p/`, so a restaurant that posts REELS only
#: had no "post" to comment on — its DM could then never satisfy the owner's
#: "no comment, no send" law, and the refusal was silent.
_POST_LINK_SELECTOR = "article a[href]"
#: Permalink shapes Instagram uses for a publishable post.
_POST_PATHS = ("/p/", "/reel/")
#: The grid paints after `domcontentloaded` (it is an SPA), so a read taken the
#: instant the document arrives sees an empty page. Live 2026-11-16: six
#: comment attempts skipped `[no_post]` at 4-5 s per profile - faster than the
#: grid can render - so "no post" was a verdict about a page that had not
#: painted yet. Bounded on purpose: a profile that never paints still returns
#: [] (the old verdict) after exactly one deadline, never a hang.
_GRID_WAIT_S = 12.0
_GRID_POLL_S = 0.5
#: Instagram's own "nothing here" copy ends the wait early instead of burning
#: the whole deadline on a profile that genuinely has no posts.
_EMPTY_PROFILE_RE = re.compile(r"no posts yet|hasn't posted yet", re.IGNORECASE)


def _is_post_href(href: str) -> bool:
    """A publishable post permalink (grid photo OR reel), not a tag/highlight."""
    return any(path in str(href or "") for path in _POST_PATHS)


def ensure_warmup_config(db: "Database") -> None:
    """Seed the warm-up switch (off) and its two numbers, once."""
    if get_config(db, WARMUP_KEY) is None:
        set_config(
            db,
            WARMUP_KEY,
            {
                "enabled": False,
                "icp_min": WARMUP_ICP_MIN_DEFAULT,
                "lead_min": WARMUP_LEAD_MIN,
            },
        )


def _config(db: "Database") -> dict[str, Any]:
    raw = get_config(db, WARMUP_KEY) or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "icp_min": int(raw.get("icp_min", WARMUP_ICP_MIN_DEFAULT)),
        "lead_min": int(raw.get("lead_min", WARMUP_LEAD_MIN)),
    }


def is_promising(db: "Database", lead_id: int) -> bool:
    """ICP gate: only a judged lead with a real score is worth warming."""
    row = db.conn.execute(
        "SELECT icp_score FROM leads WHERE id=?", (lead_id,)
    ).fetchone()
    if row is None or row["icp_score"] is None:
        return False
    return int(row["icp_score"]) >= _config(db)["icp_min"]


def _profile_says_empty(page: Any) -> bool:
    """True only when Instagram itself says the profile has no posts.

    A surface that cannot be read is NOT an answer: `False` lets the wait run
    to its deadline instead of inventing an empty profile.
    """
    read = getattr(page, "inner_text", None)
    if read is None:
        return False
    try:
        return bool(_EMPTY_PROFILE_RE.search(read("body") or ""))
    except Exception:  # noqa: BLE001 - a probe never breaks discovery
        return False


def discover_posts(
    page: Any,
    handle: str,
    *,
    limit: int = 5,
    deadline_s: float = _GRID_WAIT_S,
    poll_s: float = _GRID_POLL_S,
    clock: Any = time.monotonic,
    sleep: Any = time.sleep,
) -> list[str]:
    """Recent post permalinks from the profile grid, deduped and capped.

    Instagram renders the grid as `article` anchors; the first `limit` unique
    permalinks are the ones the warm-up will like. The grid paints after
    `domcontentloaded`, so this waits for it: bounded by `deadline_s`, querying
    the DOM fresh on every poll, and returning [] (fail-closed, exactly as
    before) if the profile never paints or says it has no posts.
    """
    page.goto(IG_PROFILE_URL.format(handle=handle), wait_until="domcontentloaded")
    deadline = clock() + deadline_s
    links = page.locator(_POST_LINK_SELECTOR)
    while not links.count():
        if _profile_says_empty(page) or clock() >= deadline:
            break
        sleep(poll_s)
        links = page.locator(_POST_LINK_SELECTOR)  # re-read: the grid may have painted
    seen: list[str] = []
    for index in range(links.count()):
        href = links.nth(index).get_attribute("href") or ""
        if not _is_post_href(href):
            continue
        url = href if href.startswith("http") else f"https://www.instagram.com{href}"
        url = url.split("?")[0].rstrip("/") + "/"
        if url not in seen:
            seen.append(url)
        if len(seen) >= limit:
            break
    return seen


def _row(db: "Database", lead_id: int) -> Any:
    return db.conn.execute(
        "SELECT * FROM warmups WHERE lead_id=?", (lead_id,)
    ).fetchone()


def _next_act(row: Any, posts: list[str]) -> tuple[str, str | None]:
    """The next act this warm-up owes, or ("", None) when it is complete."""
    follows = 0 if row is None else int(row["follows"])
    likes = 0 if row is None else int(row["likes"])
    if follows == 0:
        return "follow", None
    if likes < min(WARMUP_LIKES, len(posts)):
        return "like", posts[likes]
    return "", None


def _save(
    db: "Database",
    *,
    lead_id: int,
    account_id: str,
    act: str,
    posts: list[str],
    now: datetime,
    lead_min: int,
) -> None:
    row = _row(db, lead_id)
    follows = 0 if row is None else int(row["follows"])
    likes = 0 if row is None else int(row["likes"])
    follows += 1 if act == "follow" else 0
    likes += 1 if act == "like" else 0
    stamp = now.astimezone(timezone.utc)
    with db.conn:
        db.conn.execute(
            "INSERT INTO warmups(lead_id, account_id, follows, likes, warmed_at,"
            " dm_after, post_urls) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(lead_id) DO UPDATE SET account_id=excluded.account_id,"
            " follows=excluded.follows, likes=excluded.likes,"
            " warmed_at=excluded.warmed_at, dm_after=excluded.dm_after,"
            " post_urls=excluded.post_urls",
            (
                lead_id,
                account_id,
                follows,
                likes,
                stamp.isoformat(),
                (stamp + timedelta(minutes=lead_min)).isoformat(),
                json.dumps(posts),
            ),
        )


def warm_up(
    db: "Database",
    page: Any,
    account_id: str,
    lead_id: int,
    *,
    now: datetime,
    evidence_dir: str | Path,
    post_urls: list[str] | None = None,
    region: str | None = None,
) -> list[ActOutcome]:
    """Run the next owed act for this lead, or return [] when it must wait.

    [] has three meanings and they are all "do nothing now": not promising,
    warm-up already complete, or the engagement gate refused (gap/cap/status).
    A performed act returns one outcome; a refused control returns one
    outcome with `ok=False` and is not saved as progress.
    """
    if not is_promising(db, lead_id):
        return []

    cfg = _config(db)
    row = _row(db, lead_id)
    posts = post_urls
    if posts is None and row is not None and row["post_urls"]:
        posts = list(json.loads(row["post_urls"] or "[]"))
    if not posts:
        handle_row = db.conn.execute(
            "SELECT instagram FROM leads WHERE id=?", (lead_id,)
        ).fetchone()
        handle = ig_handle(handle_row["instagram"] if handle_row else None)
        if not handle:
            return []
        posts = discover_posts(page, handle)

    act, target = _next_act(row, posts)
    if not act:
        return []
    if can_engage(db, account_id, now) is not None:
        return []  # pacing/cap: the next tick owns the next act

    try:
        outcome = engage(
            db, page, account_id, lead_id, act,
            now=now, evidence_dir=evidence_dir, region=region, target=target,
        )
    except EngagementRefused:
        return []
    if not outcome.ok:
        return [outcome]

    _save(
        db, lead_id=lead_id, account_id=account_id, act=act, posts=posts,
        now=now, lead_min=cfg["lead_min"],
    )
    return [outcome]


def dm_ready_at(db: "Database", lead_id: int) -> datetime | None:
    """When the first DM may go out, or None when this lead was never warmed."""
    row = _row(db, lead_id)
    if row is None or not row["dm_after"]:
        return None
    moment = datetime.fromisoformat(str(row["dm_after"]))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def warmup_pending(db: "Database", lead_id: int, now: datetime) -> bool:
    """True while the first DM must wait for this lead's warm-up window."""
    due = dm_ready_at(db, lead_id)
    if due is None:
        return False
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return moment < due


def warmup_enabled(db: "Database") -> bool:
    return _config(db)["enabled"]


# --- O2 (2026-11-13): "no comment, no send" ------------------------------------

class UnwarmedLeadError(RuntimeError):
    """A DM was asked for a lead whose own post has no logged comment.

    Owner law: one genuine public comment first, then the DM. Failing closed
    here means no copy exists for such a lead, so no path (claim, manual run,
    future caller) can produce a cold touch by accident.
    """


UNWARMED_SKIP_PREFIX = "PARKED [not_warmed_up]"
_UNWARMED_REPEAT_S = 1800.0


def warmup_gate_enabled(db: "Database") -> bool:
    """The gate arms WITH the comment lane, and only then.

    A gate that refuses every DM while the means to satisfy it is switched off
    would brick the live lane the moment it shipped. So the law is armed by the
    same switch that arms comments (`config.engage_enabled`): disarmed means
    the lane behaves as before, armed means an unwarmed lead is never claimed
    and never rendered.
    """
    from outreach.engage_stage import engage_armed

    return engage_armed(db)


#: O4 (owner ruling 2026-11-14: "story reply counts as warmup yes"). The warm
#: touch is any logged, successful public reply of ours — a comment on their own
#: post, or a reply to their story. One vocabulary, one law.
WARM_ACTS = ("comment", "story_reply")
_WARM_SQL = "act IN ('comment', 'story_reply')"


def warmed_up(db: "Database", lead_id: int) -> bool:
    """True only for a LOGGED, successful warm touch on this lead.

    Derived, never stored: one row in `engagement_log` is the whole fact, so
    there is no second writer and no column to drift (the IL-7 lesson).
    """
    row = db.conn.execute(
        "SELECT 1 FROM engagement_log"
        f" WHERE lead_id=? AND {_WARM_SQL} LIMIT 1",
        (lead_id,),
    ).fetchone()
    return row is not None


def assert_warmed(db: "Database", lead_id: int) -> None:
    """The render-side half of the law: raise rather than produce cold copy."""
    if not warmup_gate_enabled(db) or warmed_up(db, lead_id):
        return
    raise UnwarmedLeadError(
        f"lead {lead_id} has no logged comment on its own post —"
        " owner law: no comment, no send"
    )


def record_unwarmed_skip(db: "Database", lead_id: int, now: datetime) -> None:
    """One `PARKED [not_warmed_up]` row per lead per 30 min.

    Throttled per LEAD so a queue full of unwarmed leads reports as a count in
    the digest instead of burying it, and the dispatch loop keeps walking
    (the R9 lesson: a per-lead fact must never stop the lane).
    """
    prefix = f"{UNWARMED_SKIP_PREFIX} lead {lead_id}"
    since = (now - timedelta(seconds=_UNWARMED_REPEAT_S)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    existing = db.conn.execute(
        "SELECT 1 FROM digest WHERE substr(body, 1, ?) = ?"
        " AND substr(created_at, 1, 19) >= ? LIMIT 1",
        (len(prefix), prefix, since),
    ).fetchone()
    if existing:
        return
    with db.conn:
        db.conn.execute(
            "INSERT INTO digest(body) VALUES (?)",
            (
                f"{prefix}: no comment logged on its own post"
                " (law: no comment, no send)",
            ),
        )


# --- O3 (2026-11-14): the comment -> DM interval -------------------------------

COMMENT_WINDOW_KEY = "comment_window"
DEFAULT_COMMENT_GAP_H = 12.0
DEFAULT_COMMENT_EXPIRE_H = 48.0
_PARK_REPEAT_S = 1800.0


def comment_window(db: "Database") -> tuple[float, float]:
    """(gap_h, expire_h) — 12 h is the shortest credible gap between a public
    comment and a DM; past 48 h the comment has gone stale and a DM would land
    cold on a post nobody remembers."""
    raw = get_config(db, COMMENT_WINDOW_KEY) or {}
    gap = float(raw.get("gap_h", DEFAULT_COMMENT_GAP_H))
    expire = float(raw.get("expire_h", DEFAULT_COMMENT_EXPIRE_H))
    if gap < 0 or expire <= gap:
        # a nonsense pair must not silently disable the law: fall back whole
        return DEFAULT_COMMENT_GAP_H, DEFAULT_COMMENT_EXPIRE_H
    return gap, expire


def last_comment_at(db: "Database", lead_id: int) -> datetime | None:
    """The newest warm touch (comment or story reply, per the owner's O4 ruling)."""
    row = db.conn.execute(
        "SELECT acted_at FROM engagement_log"
        f" WHERE lead_id=? AND {_WARM_SQL}"
        " ORDER BY acted_at DESC, id DESC LIMIT 1",
        (lead_id,),
    ).fetchone()
    if row is None:
        return None
    return _parse_stamp(row["acted_at"])


def _parse_stamp(raw: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def comment_state(db: "Database", lead_id: int, now: datetime) -> str:
    """`none` | `waiting` | `ready` | `stale` — the whole O3 machine.

    One function, one source of truth: the claim asks it, the engagement stage
    asks it, and the pin proves the two boundaries.
    """
    last = last_comment_at(db, lead_id)
    if last is None:
        return "none"
    gap_h, expire_h = comment_window(db)
    age_h = (now - last).total_seconds() / 3600.0
    if age_h < gap_h:
        return "waiting"
    if age_h <= expire_h:
        return "ready"
    return "stale"


def record_comment_park(
    db: "Database", lead_id: int, state: str, now: datetime
) -> None:
    """One `PARKED [comment_<state>] lead N` row per lead per 30 min."""
    label = "not_warmed_up" if state == "none" else f"comment_{state}"
    prefix = f"PARKED [{label}] lead {lead_id}"
    if state == "none":
        # Live 2026-11-16: 408 unwarmed leads x one row per 30 min = 1,843
        # rows in 2.5 h, which buried the IG REPLY rows that matter. The
        # lead is still NAMED (the owner needs to see which ones), but once
        # per PKT day instead of twice an hour. Comment states keep the
        # 30-minute throttle: those are few and they self-clear.
        from outreach.caps import PKT

        midnight = now.astimezone(PKT).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        since = midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    else:
        since = (now - timedelta(seconds=_PARK_REPEAT_S)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
    existing = db.conn.execute(
        "SELECT 1 FROM digest WHERE substr(body, 1, ?) = ?"
        " AND substr(created_at, 1, 19) >= ? LIMIT 1",
        (len(prefix), prefix, since),
    ).fetchone()
    if existing:
        return
    detail = {
        "none": "no comment logged on its own post (law: no comment, no send)",
        "waiting": "the comment is too fresh to follow with a DM",
        "stale": "the comment is past its expiry: re-comment before sending",
    }.get(state, f"state {state}")
    with db.conn:
        db.conn.execute(
            "INSERT INTO digest(body) VALUES (?)", (f"{prefix}: {detail}",)
        )
