"""O7 (2026-11-14): the follower count a brand filter needs.

Read while the account is already on the profile (the comment act), stored on
the lead as `data_json.ig_followers` plus the read time, and used by O6's
`brand_account` rule. Three shapes matter: `1,234` / `1 234`, `12.4K`, `1.2M`.

Unreadable is `unknown`, never a guess: a wrong "4,900" would let a brand
through the filter, and a wrong "51,000" would silently drop a real restaurant.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from outreach.store import Database

UNKNOWN = "unknown"

_COUNT_RE = re.compile(r"([\d][\d\s,. ]*)\s*([KkMm])?")
_FOLLOWERS_HINT = re.compile(r"followers?", re.IGNORECASE)


def parse_follower_count(text: str | None) -> int | None:
    """`"12.4K followers"` → 12400; anything ambiguous → None (never a guess)."""
    if not text:
        return None
    match = _COUNT_RE.search(str(text))
    if match is None:
        return None
    raw, suffix = match.group(1).strip(), (match.group(2) or "").upper()
    raw = raw.replace(" ", "").replace("\u00a0", "")
    if suffix:
        # "12.4K": the dot is a decimal point
        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            return None
        multiplier = 1_000 if suffix == "K" else 1_000_000
        return int(round(value * multiplier))
    # no suffix: thousands separators, never decimals
    digits = raw.replace(",", "").replace(".", "")
    if not digits.isdigit():
        return None
    return int(digits)


def read_followers(page: Any) -> str | int:
    """Read the header count from the profile page, or `"unknown"`.

    The header renders as `12.4K followers` (sometimes split across the link's
    title/aria-label), so the first element whose text mentions "followers" is
    the one parsed. Nothing else on the page is trusted.
    """
    selectors = (
        'header a[href$="/followers/"]',
        'a[href*="/followers"] span',
        "header ul li",
    )
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = locator.count()
        except Exception:  # noqa: BLE001 — a surface without the header is not a crash
            continue
        for index in range(min(count, 5)):
            node = locator.nth(index)
            text = ""
            for reader in ("inner_text", "get_attribute"):
                try:
                    text = (
                        node.inner_text()
                        if reader == "inner_text"
                        else node.get_attribute("title") or ""
                    )
                except Exception:  # noqa: BLE001
                    text = ""
                if text:
                    break
            if not text or not _FOLLOWERS_HINT.search(str(text)):
                continue
            parsed = parse_follower_count(str(text))
            if parsed is not None:
                return parsed
    return UNKNOWN


def stamp_followers(
    db: "Database", lead_id: int, value: str | int, now: datetime | None = None
) -> str | int:
    """Store the read on the lead (idempotent upsert inside `data_json`)."""
    row = db.conn.execute(
        "SELECT data_json FROM leads WHERE id=?", (lead_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown lead id={lead_id}")
    try:
        payload = json.loads(row["data_json"] or "{}") or {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload["ig_followers"] = value
    payload["ig_followers_at"] = stamp.isoformat()
    with db.conn:
        db.conn.execute(
            "UPDATE leads SET data_json=? WHERE id=?", (json.dumps(payload), lead_id)
        )
    return value
