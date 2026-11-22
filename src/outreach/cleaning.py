"""Data cleaning (B20): E.164 phones, IG link repair, region detection.

Pure, deterministic, stdlib-only (ADR-1). Every rule is fixture-tested in
tests/test_intake_driver.py:

- ``to_e164`` normalizes a phone to ``+<cc><national>`` per lane country.
  A number whose country cannot be proven keeps its raw digits — a WhatsApp
  link is attempted at send time, never pre-judged as dead (spec 2a).
- ``repair_instagram`` canonicalizes an IG link and classifies it
  VALID_FORMAT / SUSPECT / DEAD (spec 2b). DEAD never routes to IG (falls
  through to email per ADR-5); SUSPECT is kept as-is for exactly one send
  attempt (the sender's IG_DEAD path owns the aftermath).
- ``resolve_regions`` applies the region rule-ordered ladder
  (city lane map -> phone country code -> address country). Nothing
  resolvable is held in REGION_UNRESOLVED for the owner — the system
  never guesses a region and never messages a held lead (addendum 3).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from outreach.store import get_config, set_config

if TYPE_CHECKING:
    from outreach.store import Database

# lane country calling codes (digits, no '+'); longest match wins
_CC_TO_REGION: dict[str, str] = {
    "968": "me",  # Oman
    "1": "na",  # Canada + USA (NANP)
}

# seeded into config as `country_region_map` (ADR-9: config rows, not code)
COUNTRY_REGION_SEED: dict[str, str] = {
    "canada": "na",
    "united states": "na",
    "united states of america": "na",
    "usa": "na",
    "oman": "me",
}

_JUNK_IDENTITIES = frozenset(
    {
        "unknown name", "unknown", "unnamed", "no name", "none", "null",
        "n a", "na", "tbd", "test", "0",
    }
)


def junk_identity_reason(name: str | None) -> str | None:
    """Return "junk_name" when a lead has no usable identity, else None.

    Live origin 2026-11-04: the 5-lead Denver supply test produced a Tier-1 row
    named "Unknown Name" with no address, phone, website or handle. It was
    counted toward the target and the judge would have accepted it, because
    nothing in the pipeline asked whether the name meant anything.

    Only whole-token placeholders are junk: "Unknown Brewing Co" and "NA Grill"
    are real businesses and stay.
    """
    key = re.sub(r"[^0-9a-z]+", " ", (name or "").casefold()).strip()
    if not key or key in _JUNK_IDENTITIES:
        return "junk_name"
    return None


_IG_HOST = re.compile(
    r"(?:https?://)?(?:www[.]|m[.])?instagram[.]com/([^/?# ]+)", re.IGNORECASE
)
#: Path prefixes that are a ROUTE, never a profile. `instagram.com/m/<handle>`
#: is Instagram's mobile shell: LIVE 2026-11-11, the parser read `m` as the
#: handle, so such a lead was VALID_FORMAT and the sender would have opened
#: the profile `m` instead of the restaurant.
_IG_ROUTE_PREFIXES = frozenset({"m"})
_IG_CONTENT_SEGMENTS = frozenset({"p", "reel", "reels", "tv", "stories", "explore"})
#: Instagram's OWN pages, not a restaurant profile. LIVE 2026-11-09: the miner
#: stored `https://about.instagram.com/about-us` (the site footer link) as lead
#: 347's handle with class VALID_FORMAT, so the sender opened Instagram's own
#: about page, classified it `ig_dead`, and burnt a lead + a send slot.
_IG_SITE_SEGMENTS = frozenset(
    {
        "about",
        "about-us",
        "help",
        "legal",
        "privacy",
        "terms",
        "developer",
        "developers",
        "directory",
        "accounts",
        "session",
        "challenge",
        "api",
        "web",
    }
)
_IG_HANDLE = re.compile(r"[A-Za-z0-9._]{1,30}")


def to_e164(phone: str | None, region: str | None) -> str:
    """Normalize to E.164 for the lead's lane; raw digits when unprovable."""
    if not phone:
        return ""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if not digits:
        return ""
    if str(phone).lstrip().startswith("+"):
        return "+" + digits
    if digits.startswith("00"):  # international prefix written long-form
        return "+" + digits[2:]
    cc = next(
        (code for code in sorted(_CC_TO_REGION, key=len, reverse=True)
         if _CC_TO_REGION[code] == region),
        None,
    )
    if cc is None:
        return digits  # no proven country code: never guess a prefix
    if digits.startswith(cc) and len(digits) > len(cc) + 7:
        return "+" + digits  # already carries this lane's country code
    return "+" + cc + digits  # national format: prefix the lane code


def repair_instagram(raw: str | None) -> tuple[str | None, str | None]:
    """(canonical value, class): VALID_FORMAT | SUSPECT | DEAD, or (None, None)."""
    if raw is None:
        return None, None
    value = raw.strip()
    if not value:
        return None, None

    handle: str | None = None
    host = _IG_HOST.search(value)
    if host:
        tail = value[host.start():].split("://", 1)[-1]
        segments = [seg for seg in tail.split("/")[1:] if seg]
        while segments and segments[0].casefold() in _IG_ROUTE_PREFIXES:
            segments.pop(0)  # `m` is the mobile route, not the handle
        if not segments:
            # `instagram.com/` or `instagram.com/m/`: no profile to DM
            return value, "DEAD"
        segment = segments[0].split("?")[0].split("#")[0]
        if segment.casefold() in _IG_SITE_SEGMENTS:
            # Instagram's own site page: there is no profile here to DM
            return value, "DEAD"
        if segment.casefold() in _IG_CONTENT_SEGMENTS:
            # post/reel/story link: no profile handle to DM — one attempt only
            return value, "SUSPECT"
        handle = segment
    else:
        me_m = re.search(r"ig\.me/m/([^/?#\s]+)", value, re.IGNORECASE)
        if me_m:
            handle = me_m.group(1)
        elif re.search(r"ig\.me/", value, re.IGNORECASE):
            return value, "SUSPECT"  # ig.me short code, target unknown
    if handle is None:
        candidate = value[1:] if value.startswith("@") else value
        if _IG_HANDLE.fullmatch(candidate):
            handle = candidate
    if handle is None or not _IG_HANDLE.fullmatch(handle) or handle.endswith("."):
        return value, "DEAD"
    return f"https://instagram.com/{handle}", "VALID_FORMAT"


@dataclass(frozen=True)
class RegionReport:
    resolved: int
    held: int
    rejected: int = 0  # junk identity caught before any region question


def _ensure_country_map(db: "Database") -> dict[str, str]:
    if get_config(db, "country_region_map") is None:
        set_config(db, "country_region_map", dict(COUNTRY_REGION_SEED))
    return get_config(db, "country_region_map", {})


def _region_from_address(address: str, country_map: dict[str, str]) -> str | None:
    segments = [seg.strip().casefold() for seg in address.split(",")]
    for seg in reversed(segments):
        if seg in country_map:
            return country_map[seg]
    low = address.casefold()
    for country, region in country_map.items():
        if country in low:
            return region
    return None


def _region_from_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    if str(phone).lstrip().startswith("+") or str(phone).lstrip().startswith("00"):
        digits = "".join(ch for ch in str(phone) if ch.isdigit())
        if digits.startswith("00"):
            digits = digits[2:]
        for code in sorted(_CC_TO_REGION, key=len, reverse=True):
            if digits.startswith(code) and len(digits) >= len(code) + 7:
                return _CC_TO_REGION[code]
    return None  # no '+' = no proven country of origin: never guess


def resolve_regions(db: "Database", *, run_id: str | None = None) -> RegionReport:
    """Rule-ordered region ladder for NEW, never-contacted, region-less leads.

    city lane map (applied at import, re-checked here) -> phone country code
    (only when written in international form) -> address country (config map).
    Nothing left: hold in REGION_UNRESOLVED for the owner (addendum 3).
    `run_id` scopes the pass to one scraper run (B20 intake); None = all.
    """
    from outreach.machine import transition

    country_map = _ensure_country_map(db)
    resolved = held = rejected = 0
    scope = (
        "WHERE state='NEW' AND region IS NULL AND previously_contacted=0"
        + (" AND run_id=?" if run_id is not None else "")
    )
    rows = db.conn.execute(
        "SELECT id, name, city, region, phone, data_json FROM leads " + scope,
        (run_id,) if run_id is not None else (),
    ).fetchall()
    lane_map: dict[str, str] = get_config(db, "city_lane_map", {})
    for row in rows:
        # 2026-11-16 (lead 3149): a placeholder name is not a region question.
        # Holding junk in REGION_UNRESOLVED asked the owner to place a business
        # that does not exist in the data; junk identity is rejected here.
        if junk_identity_reason(row["name"]) is not None:
            transition(
                db, int(row["id"]), "REJECTED",
                reason=f"junk identity: placeholder name {row['name']!r}",
                actor="region-resolver",
            )
            rejected += 1
            continue
        region: str | None = None
        city = (row["city"] or "").split(",")[0].strip().casefold() or None
        if city:
            region = lane_map.get(city)
        if region is None:
            region = _region_from_phone(row["phone"])
        if region is None and row["data_json"]:
            try:
                payload: dict[str, Any] = json.loads(row["data_json"]) or {}
            except json.JSONDecodeError:
                payload = {}
            region = _region_from_address(str(payload.get("address") or ""),
                                          country_map)
        if region is None:
            transition(
                db, int(row["id"]), "REGION_UNRESOLVED",
                reason="no region signal: city, phone country code and"
                       " address country all missing",
                actor="region-resolver",
            )
            held += 1
            continue
        with db.conn:
            db.conn.execute(
                "UPDATE leads SET region=? WHERE id=?", (region, int(row["id"]))
            )
        resolved += 1
    return RegionReport(resolved=resolved, held=held, rejected=rejected)
