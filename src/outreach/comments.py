"""v1.1 batch 6: comment drafting behind the confidence gate (gate 13).

Frozen law L5 (`v1.1 blueprint.md`): a comment is drafted from the post the
account actually acts on - caption and alt text - by the local LLM, and it
carries a confidence score. Below the threshold (config
`comment_confidence_min`, default 0.8) it is skipped and queued for the owner,
never posted and never replaced with something generic. Above it, the comment
is posted and logged with its post context, its confidence and a screenshot
(ADR-15).

Law L4 still holds: the system does not write DM copy. Comments are the one
surface where it writes anything, which is exactly why they are gated, why
`_vet` runs before any click, and why a rejection reason is always recorded.

The LLM call is the production `ollama_adapter` path; tests inject a fake
transport so the JSON contract and the fail-closed pause are the real ones.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from outreach.engagement import ActOutcome, can_engage
from outreach.ollama_adapter import (
    DEFAULT_MODEL,
    ModelWindowClosed,
    OllamaDead,
    model_window_open,
    Transport,
    fail_closed_pause,
    generate,
)
from outreach.store import get_config, set_config
from outreach.wa_sender import _take_screenshot

if TYPE_CHECKING:
    from outreach.store import Database

COMMENT_CONFIDENCE_MIN = 0.8
CONFIDENCE_KEY = "comment_confidence_min"
MAX_COMMENT_CHARS = 300

_COMMENT_SELECTORS = (
    'textarea[aria-label="Add a comment…"]',
    'textarea[aria-label*="comment" i]',
)


class CommentRefused(RuntimeError):
    """The post gives no context, or the account may not act right now."""


@dataclass(frozen=True)
class Draft:
    comment: str
    confidence: float
    why: str


@dataclass(frozen=True)
class CommentOutcome:
    decision: str  # "posted" | "queued_review" | "failed"
    comment: str
    confidence: float
    reason: str = ""


def ensure_comment_config(db: "Database") -> None:
    """Seed the confidence threshold once (idempotent)."""
    if get_config(db, CONFIDENCE_KEY) is None:
        set_config(db, CONFIDENCE_KEY, COMMENT_CONFIDENCE_MIN)


def threshold(db: "Database") -> float:
    raw = get_config(db, CONFIDENCE_KEY)
    return COMMENT_CONFIDENCE_MIN if raw is None else float(raw)


def _context(post: dict[str, Any]) -> str:
    parts = [
        f"caption: {post.get('caption') or ''}".strip(),
        f"image alt text: {post.get('alt_text') or ''}".strip(),
    ]
    return "\n".join(p for p in parts if not p.endswith(":")).strip()


def _prompt(post: dict[str, Any]) -> str:
    return (
        "You write ONE short Instagram comment on a restaurant's own post, as a"
        " local who eats there. Use only what the post shows. No links, no"
        " em-dashes, no emojis, no hashtags, no sales pitch; at most 25 words."
        " If the post does not say enough to be specific, say so with low"
        " confidence instead of inventing detail.\n\n"
        f"{_context(post)}\n\n"
        'Reply ONLY with JSON: {"comment": "...", "confidence": 0.0-1.0,'
        ' "why": "what in the post this refers to"}.'
    )


def _vet(comment: str) -> str | None:
    """Copy laws as a refusal reason, or None when the draft may post.

    The owner's no-em-dash law applies to anything a person reads, and a
    placeholder or a link in a public comment is spam - both are refused
    before the browser is touched.
    """
    text = comment.strip()
    if not text:
        return "empty draft"
    if "\u2014" in text or "--" in text:
        return "em-dash in draft"
    if "{{" in text or "}}" in text or "None" in text:
        return "unfilled placeholder in draft"
    if "http" in text.lower():
        return "link in draft"
    if len(text) > MAX_COMMENT_CHARS:
        return f"draft over {MAX_COMMENT_CHARS} chars"
    return None


def draft_comment(
    post: dict[str, Any],
    *,
    transport: Transport | None = None,
    model: str = DEFAULT_MODEL,
) -> Draft:
    """Ask the local LLM for one comment, or refuse when there is no context."""
    if not _context(post):
        raise CommentRefused(
            "post has no caption or alt text - a comment would be generic (L5)"
        )
    if not model_window_open():
        raise CommentRefused(
            "local model window is closed (opens 20:55) - no comment drafted"
        )
    raw = generate(
        _prompt(post), model=model, expect_json=True, transport=transport
    )
    if not isinstance(raw, dict):
        raise ValueError(f"comment draft is not an object: {raw!r}")
    try:
        comment = str(raw["comment"])
        confidence = float(raw["confidence"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"comment draft is malformed: {raw!r}") from exc
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be within 0.0-1.0, got {confidence}")
    return Draft(comment=comment, confidence=confidence, why=str(raw.get("why") or ""))


def perform_comment(page: Any, post_url: str, text: str) -> ActOutcome:
    """Type the comment under the post and submit it; fail closed if absent."""
    page.goto(post_url, wait_until="domcontentloaded")
    box = None
    for selector in _COMMENT_SELECTORS:
        locator = page.locator(selector)
        if locator.count():
            box = locator.first
            break
    if box is None:
        return ActOutcome(False, "comment", post_url, f"no comment box at {post_url}")
    box.fill(text)
    box.press("Enter")
    return ActOutcome(True, "comment", post_url)


def _queue(
    db: "Database",
    *,
    account_id: str,
    lead_id: int,
    post: dict[str, Any],
    draft: Draft,
    why: str,
    now: datetime,
) -> None:
    with db.conn:
        db.conn.execute(
            "INSERT INTO comment_review(account_id, lead_id, post_url, caption,"
            " draft, confidence, why, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                account_id,
                lead_id,
                str(post.get("url") or ""),
                str(post.get("caption") or ""),
                draft.comment,
                draft.confidence,
                why,
                now.astimezone(timezone.utc).isoformat(),
            ),
        )


def post_comment(
    db: "Database",
    page: Any,
    account_id: str,
    lead_id: int,
    post: dict[str, Any],
    *,
    now: datetime,
    evidence_dir: str | Path,
    transport: Transport | None = None,
    model: str = DEFAULT_MODEL,
) -> CommentOutcome:
    """Draft, gate, then post one comment - or queue it. Never both."""
    if not _context(post):
        raise CommentRefused(
            "post has no caption or alt text - a comment would be generic (L5)"
        )
    reason = can_engage(db, account_id, now)
    if reason is not None:
        from outreach.engagement import EngagementRefused

        raise EngagementRefused(reason)

    try:
        draft = draft_comment(post, transport=transport, model=model)
    except ModelWindowClosed as exc:
        raise CommentRefused(str(exc)) from exc
    except OllamaDead as exc:
        fail_closed_pause(db, reason=str(exc), component="ollama")
        raise

    limit = threshold(db)
    if draft.confidence < limit:
        _queue(
            db, account_id=account_id, lead_id=lead_id, post=post, draft=draft,
            why=f"confidence {draft.confidence:.2f} < {limit:.2f}", now=now,
        )
        return CommentOutcome(
            "queued_review", draft.comment, draft.confidence,
            f"confidence {draft.confidence:.2f} < {limit:.2f}",
        )

    veto = _vet(draft.comment)
    if veto is not None:
        _queue(
            db, account_id=account_id, lead_id=lead_id, post=post, draft=draft,
            why=veto, now=now,
        )
        return CommentOutcome("queued_review", draft.comment, draft.confidence, veto)

    outcome = perform_comment(page, str(post["url"]), draft.comment)
    if not outcome.ok:
        return CommentOutcome(
            "failed", draft.comment, draft.confidence, outcome.reason
        )

    shot = _take_screenshot(page, evidence_dir, lead_id, "comment")
    detail = json.dumps(
        {
            "post_url": post.get("url"),
            "caption": post.get("caption"),
            "alt_text": post.get("alt_text"),
            "why": draft.why,
        }
    )
    with db.conn:
        db.conn.execute(
            "INSERT INTO engagement_log(account_id, lead_id, act, target,"
            " confidence, screenshot, acted_at, detail)"
            " VALUES (?, ?, 'comment', ?, ?, ?, ?, ?)",
            (
                account_id,
                lead_id,
                str(post.get("url") or ""),
                draft.confidence,
                str(shot),
                now.astimezone(timezone.utc).isoformat(),
                detail,
            ),
        )
    return CommentOutcome("posted", draft.comment, draft.confidence)


# --- O1 (2026-11-13): read the post the account actually acts on --------------

_CAPTION_SELECTORS = (
    'meta[property="og:description"]',
    'meta[name="description"]',
)
_ALT_SELECTORS = ('meta[property="og:image:alt"]',)


def _meta(page: Any, selectors: tuple[str, ...]) -> str:
    """First non-empty meta `content` among `selectors`, or ''."""
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if not locator.count():
                continue
            value = locator.first.get_attribute("content")
        except Exception:  # noqa: BLE001 — a surface without meta is not a crash
            continue
        if value and str(value).strip():
            return str(value).strip()
    return ""


def read_post(page: Any, url: str) -> dict[str, Any]:
    """Open one post and return the context a comment may cite.

    Instagram's post pages carry the caption in `og:description`
    ("1,234 likes, 56 comments - handle on 12 November: 'text'"), which is
    readable without a session and is the only text the page guarantees. When
    it is absent the post is returned with an empty caption, and
    `draft_comment` refuses instead of inventing context (L5).
    """
    page.goto(url, wait_until="domcontentloaded")
    return {
        "url": url,
        "caption": _meta(page, _CAPTION_SELECTORS),
        "alt_text": _meta(page, _ALT_SELECTORS),
    }
