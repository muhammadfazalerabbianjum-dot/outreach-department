"""Scraper read-only adapter (ADR-3) — thin, typed mapping over the SSOT.

Evidence: DISCOVERY.md D1 — outreach payload lives inline for identity
columns and inside packed `data_json` for contact/qualification fields.
The connection is opened with URI `?mode=ro`: ANY write attempt raises
(circle evidence for gate #6). This adapter never writes anywhere.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from outreach.scraper_labels import ScraperSignals, read_signals

#: Default = this machine's layout. Point the adapter at any read-only scraper
#: checkout with OUTREACH_SCRAPER_ROOT; the cache DB lives inside it.
SCRAPER_ROOT = Path(os.environ.get("OUTREACH_SCRAPER_ROOT", "A:/scraper"))
SCRAPER_DB = SCRAPER_ROOT / "cache" / "scraper_cache.db"

SCRAPER_COLUMNS: tuple[str, ...] = (
    "id",
    "place_id",
    "name",
    "tier_assigned",
    "outreach_status",
    "outreach_stage",
    "last_contact_ts",
    "linkedin_url",
    "predicted_email",
    "dm_opened",
    "is_unsubscribed",
    "ig_inactive",
    "exported_at",
    "exported_wa_at",
    "exported_ig_at",
    "is_archived",
    "run_id",
    "vertical",
    "data_json",
)


def open_scraper(db_path: str | Path = SCRAPER_DB) -> sqlite3.Connection:
    path = Path(db_path)
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(frozen=True)
class LeadRow:
    """One scraper lead, normal for outreach consumption (no mutation)."""

    place_id: str
    name: str | None
    run_id: str | None
    city: str | None
    rating: float | None
    review_count: int | None
    tier: int | None
    phone: str | None
    instagram: str | None
    email: str | None
    website: str | None
    gmaps_url: str | None
    menu: tuple[dict[str, Any], ...]
    review_samples: tuple[str, ...]
    cuisine: str | None
    lead_type: str | None
    is_chain: bool | None
    is_ghost_kitchen: bool | None
    is_multi_location: bool | None
    is_owner_run: bool | None
    is_on_delivery_platform: bool | None
    halal_status: bool | None
    ig_inactive: int | None
    outreach_stage: str | None
    outreach_status: str | None
    last_contact_ts: int | None
    exported_at: int | None
    exported_wa_at: int | None
    exported_ig_at: int | None
    predicted_email: str | None
    linkedin_url: str | None
    is_unsubscribed: int | None
    raw_json: str | None
    # the scraper's post-2026-11-04 data labels, read but not yet consumed
    # (packet law: prep only, re-ingestion waits on BULK_PASS_REPORT)
    signals: ScraperSignals = field(default_factory=ScraperSignals)


def _row_to_lead(row: sqlite3.Row) -> LeadRow:
    payload: dict[str, Any] = {}
    if row["data_json"] is not None:
        try:
            payload = json.loads(row["data_json"])
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"scraper row place_id={row['place_id']}: corrupt data_json ({exc})"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(
                f"scraper row place_id={row['place_id']}: data_json is not an object"
            )
    social = payload.get("social") or {}
    tier = payload.get("tier")
    if tier is None:  # inline mirror column, per D1
        tier = row["tier_assigned"]
    ig_inactive = row["ig_inactive"]
    if ig_inactive is None:
        ig_inactive = payload.get("ig_inactive")
    return LeadRow(
        place_id=str(row["place_id"]),
        name=row["name"],
        run_id=row["run_id"],
        city=payload.get("city"),
        rating=payload.get("rating"),
        review_count=payload.get("review_count"),
        tier=tier,
        phone=payload.get("phone"),
        instagram=social.get("instagram"),
        email=payload.get("email"),
        website=payload.get("website"),
        gmaps_url=payload.get("google_maps_url"),
        menu=tuple(payload.get("menu_items") or ()),
        review_samples=tuple(payload.get("review_samples") or ()),
        cuisine=payload.get("cuisine"),
        lead_type=payload.get("lead_type"),
        is_chain=payload.get("is_chain"),
        is_ghost_kitchen=payload.get("is_ghost_kitchen"),
        is_multi_location=payload.get("is_multi_location"),
        is_owner_run=payload.get("is_owner_run"),
        is_on_delivery_platform=payload.get("is_on_delivery_platform"),
        halal_status=payload.get("halal_status"),
        ig_inactive=ig_inactive,
        outreach_stage=row["outreach_stage"],
        outreach_status=row["outreach_status"],
        last_contact_ts=row["last_contact_ts"],
        exported_at=row["exported_at"],
        exported_wa_at=row["exported_wa_at"],
        exported_ig_at=row["exported_ig_at"],
        predicted_email=row["predicted_email"] or payload.get("predicted_email"),
        linkedin_url=row["linkedin_url"] or payload.get("linkedin_url"),
        is_unsubscribed=row["is_unsubscribed"],
        raw_json=row["data_json"],
        signals=read_signals(payload),
    )


def fetch_leads(
    db_path: str | Path = SCRAPER_DB,
    *,
    limit: int | None = None,
    run_id: str | None = None,
) -> list[LeadRow]:
    """Read every live (non-archived) scraper lead, place_id-ordered.

    Read-only by contract: mode=ro connection, closed in `finally`. A
    missing file raises naturally (loud, never skipped). `run_id` scopes
    the read to one scraper run (B20 intake); None = every run.
    """
    cols = ", ".join(SCRAPER_COLUMNS)
    sql = (
        f"SELECT {cols} FROM scraped_places "
        "WHERE COALESCE(is_archived, 0) = 0"
    )
    params: tuple[Any, ...] = ()
    if run_id is not None:
        sql += " AND run_id = ?"
        params = (run_id,)
    sql += " ORDER BY place_id"
    if limit is not None:
        sql += " LIMIT ?"
        params = (*params, limit)
    conn = open_scraper(db_path)
    try:
        return [_row_to_lead(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()
