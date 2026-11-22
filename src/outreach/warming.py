"""v1.1 batch 8: the standalone warming queue, one act at a time.

A fleet account that has no lead to warm still has to look alive, and that
means acting: following a handful of niche accounts and liking their recent
posts. Those acts belong to no lead, so they cannot go through
`engagement.engage` (which resolves a lead's handle and enforces L1) — they go
through the same `perform_act` transport and the same `engagement_log` ledger
instead, writing `lead_id = NULL`.

Three laws shape this module:

  * **L6 - one budget per account.** Lead warm-up and standalone warming spend
    the SAME `can_engage` allowance (30 acts/day, 90 s apart), because they
    write the same ledger. An account can never double its activity by using
    two queues.
  * **L5 - likes and follows are ungated**, no copy is drafted or posted here.
    Every act keeps its ADR-15 screenshot.
  * **No silent rollout.** `warming_config.enabled` seeds False, and nothing
    is clicked while it is off. The owner arms it explicitly.

Targets are an owner/ops-supplied list of niche handles, not a scrape of the
explore feed: an Instagram feed selector is unstable and unbounded, while a
short list is reviewable and mockable. Each handle is warmed once (follow, then
up to `likes_per_handle` likes on its newest posts); when every handle is done
the queue is drained and reports no work rather than looping on old posts.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from outreach.engagement import ActOutcome, can_engage, perform_act
from outreach.ig_sender import ig_handle
from outreach.store import get_config, set_config
from outreach.warmup import discover_posts

if TYPE_CHECKING:
    from outreach.store import Database

WARMING_KEY = "warming_config"
WARMING_LIKES_PER_HANDLE = 2
WARMING_MAX_HANDLES = 20


def ensure_warming_config(db: "Database") -> None:
    """Seed the switch (off) and its inputs once, idempotently."""
    if get_config(db, WARMING_KEY) is None:
        set_config(
            db,
            WARMING_KEY,
            {
                "enabled": False,
                "handles": [],
                "likes_per_handle": WARMING_LIKES_PER_HANDLE,
            },
        )


def _config(db: "Database") -> dict[str, Any]:
    raw = get_config(db, WARMING_KEY) or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "likes": max(1, int(raw.get("likes_per_handle", WARMING_LIKES_PER_HANDLE))),
    }


def _clean(raw: Any) -> str:
    """One configured entry -> bare lowercase handle, or '' when unusable."""
    try:
        return ig_handle(str(raw or "")).lower()
    except ValueError:
        return ""


def niche_handles(db: "Database") -> list[str]:
    """The niche list: normalised, deduped in order, capped, empties dropped."""
    raw = (get_config(db, WARMING_KEY) or {}).get("handles") or []
    seen: list[str] = []
    for entry in raw:
        handle = _clean(entry)
        if handle and handle not in seen:
            seen.append(handle)
        if len(seen) >= WARMING_MAX_HANDLES:
            break
    return seen


def _progress(db: "Database", account_id: str, handle: str) -> Any:
    return db.conn.execute(
        "SELECT * FROM warming_progress WHERE account_id=? AND handle=?",
        (account_id, handle),
    ).fetchone()


def _save(
    db: "Database",
    *,
    account_id: str,
    handle: str,
    follows: int,
    likes: int,
    posts: list[str],
    now: datetime,
) -> None:
    with db.conn:
        db.conn.execute(
            "INSERT INTO warming_progress(account_id, handle, follows, likes,"
            " post_urls, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(account_id, handle) DO UPDATE SET"
            " follows=excluded.follows, likes=excluded.likes,"
            " post_urls=excluded.post_urls, updated_at=excluded.updated_at",
            (
                account_id,
                handle,
                follows,
                likes,
                json.dumps(posts),
                now.astimezone(timezone.utc).isoformat(),
            ),
        )


def _stored_posts(row: Any) -> list[str]:
    if row is None or not row["post_urls"]:
        return []
    try:
        value = json.loads(row["post_urls"])
    except ValueError:
        return []
    return [str(url) for url in value] if isinstance(value, list) else []


def _is_complete(row: Any, likes_per_handle: int) -> bool:
    """A handle is done once it is followed and its available posts are liked."""
    if row is None or not int(row["follows"]):
        return False
    posts = _stored_posts(row)
    return int(row["likes"]) >= min(likes_per_handle, len(posts))


def has_warming_work(db: "Database", account_id: str) -> bool:
    """Would another tick find anything to do for this account?

    Shared with the wake math: a drained queue must not promise work, or the
    engine wakes every 90 s to do nothing.
    """
    cfg = _config(db)
    if not cfg["enabled"]:
        return False
    for handle in niche_handles(db):
        if not _is_complete(_progress(db, account_id, handle), cfg["likes"]):
            return True
    return False


def _shot(page: Any, evidence_dir: str | Path, act: str, handle: str) -> Path:
    directory = Path(evidence_dir)
    directory.mkdir(parents=True, exist_ok=True)
    shot = directory / f"warming_{act}_{handle.replace('.', '_')}_{time.strftime('%Y%m%dT%H%M%S')}.png"
    page.screenshot(path=str(shot))
    return shot


def _log(
    db: "Database",
    *,
    account_id: str,
    act: str,
    target: str,
    handle: str,
    screenshot: Path,
    now: datetime,
) -> None:
    with db.conn:
        db.conn.execute(
            "INSERT INTO engagement_log(account_id, lead_id, act, target,"
            " confidence, screenshot, acted_at, detail)"
            " VALUES (?, NULL, ?, ?, NULL, ?, ?, ?)",
            (
                account_id,
                act,
                target,
                str(screenshot),
                now.astimezone(timezone.utc).isoformat(),
                f"warming:{handle}",
            ),
        )


def _run_act(
    db: "Database",
    page: Any,
    *,
    account_id: str,
    handle: str,
    act: str,
    target: str,
    now: datetime,
    evidence_dir: str | Path,
) -> ActOutcome:
    outcome = perform_act(page, act, target)
    if not outcome.ok:
        return outcome  # drifted surface: log nothing, retry on a later tick
    shot = _shot(page, evidence_dir, act, handle)
    _log(
        db, account_id=account_id, act=act, target=outcome.target,
        handle=handle, screenshot=shot, now=now,
    )
    return outcome


def warm_account(
    db: "Database",
    page: Any,
    account_id: str,
    *,
    now: datetime,
    evidence_dir: str | Path,
) -> list[ActOutcome]:
    """Run the NEXT owed act for this account, or [] when it must wait.

    [] means "do nothing now": warming is disabled, the niche list is empty,
    the queue is drained, or the engagement gate refuses (gap/cap/status).
    A performed act returns one outcome; a surface without the control returns
    one outcome with `ok=False` and is never recorded as progress.
    """
    cfg = _config(db)
    if not cfg["enabled"]:
        return []
    handles = niche_handles(db)
    if not handles:
        return []
    if can_engage(db, account_id, now) is not None:
        return []  # pacing/cap/status: the next tick owns the next act

    for handle in handles:
        row = _progress(db, account_id, handle)
        if row is None or not int(row["follows"]):
            outcome = _run_act(
                db, page, account_id=account_id, handle=handle,
                act="follow", target=handle, now=now, evidence_dir=evidence_dir,
            )
            if not outcome.ok:
                return [outcome]
            row = _progress(db, account_id, handle)
            _save(
                db, account_id=account_id, handle=handle,
                follows=int(row["follows"]) if row else 1, likes=0,
                posts=_stored_posts(row), now=now,
            )
            return [outcome]

        posts = _stored_posts(row)
        if not posts:
            posts = discover_posts(page, handle, limit=cfg["likes"])
            _save(
                db, account_id=account_id, handle=handle,
                follows=int(row["follows"]), likes=int(row["likes"]),
                posts=posts, now=now,
            )
            if not posts:
                continue  # nothing to like on this handle: next one, no act
        likes = int(row["likes"])
        if likes >= min(cfg["likes"], len(posts)):
            continue  # this handle is done

        outcome = _run_act(
            db, page, account_id=account_id, handle=handle,
            act="like", target=posts[likes], now=now, evidence_dir=evidence_dir,
        )
        if not outcome.ok:
            return [outcome]
        _save(
            db, account_id=account_id, handle=handle,
            follows=int(row["follows"]), likes=likes + 1, posts=posts, now=now,
        )
        return [outcome]

    return []
