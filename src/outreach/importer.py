"""Dedupe + importer (B3): scraper leads → outreach.db, machine-blessed.

ADR-3 dedupe order: place_id → normalized phone → fuzzy name+city.
D2 contacted-elsewhere signals never enter the active queue — mirrored with
the `previously_contacted` flag (migration v2), never abused states.
Region lanes (na/me) resolve from the `city_lane_map` config row seeded
from DISCOVERY D3 evidence (ASSUMED fills are DECISIONS.md DT-3).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING

from outreach.cleaning import junk_identity_reason
from outreach.machine import transition
from outreach.scraper_adapter import SCRAPER_DB, LeadRow, fetch_leads
from outreach.store import get_config, set_config

if TYPE_CHECKING:
    from outreach.store import Database

FUZZY_NAME_RATIO = 0.94

# location/branch designators that are allowed to sit as the single extra
# token inside a containment dup ("The Street Eatery" vs "The Street Eatery SW")
_DESIGNATION_TOKENS = frozenset(
    {"sw", "nw", "ne", "se", "2", "ii", "south", "north", "east", "west",
     "mall", "branch", "downtown", "market"}
)


def _name_duplicate(name_a: str, name_b: str) -> bool:
    """Fuzzy name decision (city already matched by caller).

    Blueprint rule 'fuzzy name+city' implemented as: token-set equality
    (Arabic/English dual listings are the same restaurant in either order),
    token containment with at most one extra designator token, or a strict
    SequenceMatcher ratio (typo pairs) — never a loose ratio alone.
    """
    tokens_a = set(name_a.split())
    tokens_b = set(name_b.split())
    if not tokens_a or not tokens_b:
        return False
    if tokens_a == tokens_b:
        return True
    if len(tokens_a) >= 2 and len(tokens_b) >= 2:
        smaller, larger = (
            (tokens_a, tokens_b) if len(tokens_a) <= len(tokens_b)
            else (tokens_b, tokens_a)
        )
        extra = larger - smaller
        if smaller <= larger and len(extra) <= 1 and extra <= _DESIGNATION_TOKENS:
            return True
    return SequenceMatcher(None, name_a, name_b).ratio() >= FUZZY_NAME_RATIO

_LANE_SEED: dict[str, tuple[str, ...]] = {
    "na": (
        "austin", "houston", "dallas", "san antonio", "miami", "denver",
        "toronto",
        "ottawa", "windsor", "calgary", "edmonton", "vancouver", "surrey",
        "seattle", "portland", "montreal", "winnipeg",
    ),
    "me": ("salalah",),
}


@dataclass(frozen=True)
class ImportReport:
    total_rows: int
    created: int
    refreshed: int
    dups: int
    skipped_contacted: int
    skipped_junk: int = 0
    unknown_regions: tuple[str, ...] = ()


def normalize_name(value: str | None) -> str:
    """Casefolded, punctuation-collapsed name key."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z]+", " ", value.casefold())).strip()


def normalize_phone(value: str | None) -> str:
    """Digits-only comparison key (originals preserved on the lead)."""
    if not value:
        return ""
    return "".join(ch for ch in value if ch.isdigit())


def normalize_city(value: str | None) -> str | None:
    """Scraper-matching key: first comma segment, casefolded ("Calgary, Canada")."""
    if not value:
        return None
    token = value.split(",")[0].strip().casefold()
    return token or None


def ensure_lane_map(db: "Database") -> None:
    """Seed the city -> lane map, and keep it current as the code seed grows.

    A DB seeded before a city joined `_LANE_SEED` would otherwise never learn
    it, and every lead from that city would import with region NULL (the
    2026-11-04 Denver case: `unknown_regions=('denver',)` on a live refresh).
    Merging is additive: an entry the map already holds is never overwritten.
    """
    flat = {
        city: region
        for region, cities in _LANE_SEED.items()
        for city in cities
    }
    existing = get_config(db, "city_lane_map")
    if existing is None:
        set_config(db, "city_lane_map", flat)
        return
    missing = {city: region for city, region in flat.items() if city not in existing}
    if missing:
        set_config(db, "city_lane_map", {**existing, **missing})


def _contacted_signal(row: LeadRow) -> bool:
    """Real contact evidence from the scraper row, never an export stamp.

    The D4 ruling (2026-11-04) established with the scraper's own columns that
    `exported_at == exported_wa_at == exported_ig_at` marks a LIST EXPORT (two
    bulk stamps over 357 rows, `outreach_status=NULL`,
    `outreach_stage='not_contacted'`, `dm_opened=0`), not three channel sends.
    Those stamps stayed in this predicate, so every refresh (`sync` re-imports
    the whole run) re-flagged the guard-cleared leads: on 2026-11-09 all 185
    ROUTED leads carried `previously_contacted=1` again while the scraper still
    said `not_contacted`, which is what made the low-lead scan report 0
    dispatch-ready. Contact evidence is a contact timestamp, a status the
    scraper actually recorded, an unsubscribe, or a stage past not_contacted.
    """
    return bool(
        row.last_contact_ts
        or row.is_unsubscribed
        or row.outreach_status
        or (row.outreach_stage not in (None, "", "not_contacted"))
    )


def refresh_leads(
    db: "Database",
    db_path: str | Path = SCRAPER_DB,
    *,
    limit: int | None = None,
    run_id: str | None = None,
) -> ImportReport:
    """Import/refresh every non-archived scraper lead into outreach.db.

    Idempotent: a place_id already known is refreshed in place (never against
    its state); new rows collide-checked in blueprint order; a collision is
    inserted and moved to DUP via `transition()` so the machine records it.
    `run_id` scopes the pass to one scraper run (B20 intake); every lead
    remembers the run that brought it (provenance for the watcher).
    """
    ensure_lane_map(db)
    lane_map: dict[str, str] = get_config(db, "city_lane_map", {})
    rows = [r for r in fetch_leads(db_path, limit=limit, run_id=run_id)]

    place_index: dict[str, int] = {}
    phone_index: dict[str, int] = {}
    name_city_index: list[tuple[str, str | None, int]] = []
    for existing in db.conn.execute(
        "SELECT id, place_id, phone, name, city FROM leads"
    ):
        lead_id = int(existing["id"])
        place_index[existing["place_id"]] = lead_id
        p_norm = normalize_phone(existing["phone"])
        if p_norm:
            phone_index.setdefault(p_norm, lead_id)
        name_city_index.append(
            (normalize_name(existing["name"]), normalize_city(existing["city"]), lead_id)
        )

    created = refreshed = dups = skipped = junk = 0
    unknown: set[str] = set()

    for row in rows:
        if junk_identity_reason(row.name) is not None:
            # 2026-11-04: a placeholder row is not a lead. It would otherwise
            # be counted toward a run target and could be judged and messaged.
            junk += 1
            continue
        city_norm = normalize_city(row.city)
        if city_norm and city_norm not in lane_map:
            unknown.add(city_norm)
        lane = lane_map.get(city_norm) if city_norm else None
        contacted = _contacted_signal(row)

        if row.place_id in place_index:
            with db.conn:
                db.conn.execute(
                    "UPDATE leads SET name=?, category=?, city=?, region=?,"
                    " phone=?, instagram=?, email=?, data_json=?,"
                    " previously_contacted=?, run_id=? WHERE place_id=?",
                    (
                        row.name, None, row.city, lane, row.phone,
                        row.instagram, row.email, row.raw_json,
                        1 if contacted else 0, row.run_id, row.place_id,
                    ),
                )
            refreshed += 1
            continue

        phone_norm = normalize_phone(row.phone)
        collision_phone = phone_index.get(phone_norm) if phone_norm else None
        collision_reason: str | None = None
        if collision_phone is not None:
            collision_reason = f"phone matches lead #{collision_phone}"
        else:
            name_norm = normalize_name(row.name)
            if name_norm and city_norm:
                for other_name, other_city, other_id in name_city_index:
                    if other_city != city_norm or not other_name:
                        continue
                    if _name_duplicate(name_norm, other_name):
                        collision_reason = (
                            f"fuzzy name+city matches lead #{other_id}"
                        )
                        break

        if collision_reason is not None:
            new_id = db.create_lead(
                row.place_id, name=row.name, city=row.city, region=lane,
                phone=row.phone, instagram=row.instagram, email=row.email,
                data_json=row.raw_json,
                previously_contacted=1 if contacted else 0,
                run_id=row.run_id,
            )
            transition(db, new_id, "DUP", reason=collision_reason)
            dups += 1
        else:
            new_id = db.create_lead(
                row.place_id, name=row.name, city=row.city, region=lane,
                phone=row.phone, instagram=row.instagram, email=row.email,
                data_json=row.raw_json,
                previously_contacted=1 if contacted else 0,
                run_id=row.run_id,
            )
            created += 1
            if contacted:
                skipped += 1

        # register new rows for in-batch collisions (progressive indexes)
        place_index[row.place_id] = new_id
        if phone_norm:
            phone_index.setdefault(phone_norm, new_id)
        name_city_index.append((normalize_name(row.name), city_norm, new_id))

    return ImportReport(
        total_rows=len(rows),
        created=created,
        refreshed=refreshed,
        dups=dups,
        skipped_contacted=skipped,
        skipped_junk=junk,
        unknown_regions=tuple(sorted(unknown)),
    )
