"""Website-menu evidence arrow (owner order 2026-11-12).

The scraper's menu pass writes one JSONL record per site:
`{place_id, name, url, dishes: [{name, description, price, category}], error}`.
That is the strongest dish signal this department can get - it is the restaurant
saying, on its own site, "this is what we serve" - so it enters `data_json` as
first-class evidence with its SOURCE attached, and the dish gate still decides
last (`validate_dish` is unchanged).

The scraper folder is READ-ONLY (ADR-3). This module only reads JSONL files and
writes our own `leads.data_json`; it never creates a lead, never changes a
state, never touches the scraper cache.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from outreach.store import Database

METHOD = "site menu page"
#: Navigation / chrome, never a plate. The upstream parser filters most of
#: this; refusing it again here is defense in depth (owner order 2026-11-12) -
#: one "Order Online" quoted at a restaurateur costs more than this list.
MENU_JUNK_NAMES = frozenset(
    {
        "home", "menu", "menus", "our menu", "order online", "order now",
        "order", "online ordering", "contact", "contact us", "about",
        "about us", "our story", "login", "log in", "sign in", "sign up",
        "cart", "checkout", "reservations", "reserve", "book now", "gift cards",
        "gift card", "careers", "jobs", "gallery", "photos", "blog", "news",
        "locations", "location", "hours", "delivery", "catering", "privacy",
        "privacy policy", "terms", "terms of service", "faq", "reviews",
        "find us", "instagram", "facebook", "twitter", "tiktok", "yelp",
        "skip to content", "view cart", "add to cart", "shop", "shop now",
    }
)
_MAX_ITEMS_PER_LEAD = 60


@dataclass(frozen=True)
class MenuReport:
    files: int
    records: int
    matched: int
    items: int
    leads_updated: int
    refused_junk: int


def menu_files(scraper_db: str | Path) -> list[Path]:
    """Every menu-pass JSONL beside the scraper cache DB, newest first."""
    cache = Path(scraper_db).parent
    return sorted(cache.glob("menu_site_results*.jsonl"), reverse=True)


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def normalize_items(dishes: Iterable[object], url: str) -> tuple[list[dict], int]:
    """Usable items with provenance, plus the count of junk refused."""
    usable: list[dict] = []
    seen: set[str] = set()
    junk = 0
    for raw in dishes or ():
        name = _clean(raw.get("name") if isinstance(raw, dict) else raw)
        key = name.casefold()
        if (
            not name
            or len(name) < 2
            or not any(ch.isalpha() for ch in name)
            or len(name.split()) > 6
            or key in MENU_JUNK_NAMES
            or key in seen
        ):
            junk += 1
            continue
        seen.add(key)
        payload = raw if isinstance(raw, dict) else {}
        usable.append(
            {
                "name": name,
                "description": _clean(payload.get("description"))[:400],
                "price": _clean(payload.get("price"))[:24],
                "category": _clean(payload.get("category"))[:80],
                "source_url": url,
                "method": METHOD,
            }
        )
        if len(usable) >= _MAX_ITEMS_PER_LEAD:
            break
    return usable, junk


def _payload_of(db: "Database", lead_id: int) -> dict:
    row = db.conn.execute(
        "SELECT data_json FROM leads WHERE id=?", (lead_id,)
    ).fetchone()
    try:
        payload = json.loads((row["data_json"] if row else "") or "{}") or {}
    except (TypeError, ValueError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def import_menu_items(
    db: "Database",
    scraper_db: str | Path,
    *,
    now: datetime | None = None,
    paths: list[Path] | None = None,
) -> MenuReport:
    """Merge menu-pass items into each lead's `data_json.menu_items`.

    Idempotent: an item already present (casefolded name) is skipped, so a
    re-run after tomorrow's pass only adds what is new and never duplicates.
    """
    moment = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    files = paths if paths is not None else menu_files(scraper_db)
    lead_ids = {
        str(row["place_id"]): int(row["id"])
        for row in db.conn.execute("SELECT id, place_id FROM leads")
    }
    records = matched = total_items = updated = refused = 0
    for path in files:
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            records += 1
            lead_id = lead_ids.get(str(record.get("place_id") or ""))
            if lead_id is None:
                continue
            matched += 1
            url = str(record.get("url") or "")
            items, junk = normalize_items(record.get("dishes") or [], url)
            refused += junk
            if not items:
                continue
            payload = _payload_of(db, lead_id)
            existing = [
                item for item in (payload.get("menu_items") or [])
                if isinstance(item, dict)
            ]
            known = {_clean(item.get("name")).casefold() for item in existing}
            fresh = [item for item in items if item["name"].casefold() not in known]
            if not fresh:
                continue
            payload["menu_items"] = existing + fresh
            payload["menu_evidence"] = {
                "method": METHOD,
                "source_url": url,
                "imported_at": moment,
                "items": len(existing) + len(fresh),
            }
            with db.conn:
                db.conn.execute(
                    "UPDATE leads SET data_json=? WHERE id=?",
                    (json.dumps(payload), lead_id),
                )
            updated += 1
            total_items += len(fresh)
    return MenuReport(
        files=len(files),
        records=records,
        matched=matched,
        items=total_items,
        leads_updated=updated,
        refused_junk=refused,
    )
