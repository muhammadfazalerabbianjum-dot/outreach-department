"""ICP qualification judge (AI role #1) — single-branch, fail-closed.

Blueprint: an independent, thriving, single-branch restaurant is the only
QUALIFIED verdict; anything else is REJECTED with a reason. Rating/review
thresholds (≥4.0 / ≥100) are hints — the model judges with a confidence
score. A semantically invalid verdict is treated like a dead model: the
queue is PAUSED + a HALT recorded (never partially trusted).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from outreach.chains import HARDCODED_CHAINS
from outreach.cleaning import junk_identity_reason
from outreach.ollama_adapter import (
    DEFAULT_MODEL,
    MAX_RETRIES,
    REQUEST_TIMEOUT_S,
    ask_with_guard,
    fail_closed_pause,
)
from outreach.store import get_config

if TYPE_CHECKING:
    from outreach.store import Database

JUDGE_MODEL = DEFAULT_MODEL
QUALIFICATION_OPTIONS = {"temperature": 0}  # corpus agreement needs stability

_ACTOR = "icp-judge"
_ALLOWED_REJECT_REASONS = frozenset(
    {
        "chain", "multi_location", "hotel", "ghost_kitchen", "corporate",
        "closed", "dormant", "low_rating", "not_restaurant", "other",
    }
)
# deterministic rendering of the enum for the judge prompt (sorted: iteration
# order of a frozenset varies per process, and temperature=0 needs a stable
# prompt for corpus agreement)
_PROMPT_REJECT_REASONS = json.dumps(sorted(_ALLOWED_REJECT_REASONS))

# Model-observed synonyms (probe evidence, 2026-10-22): the judge occasionally
# answers outside the canonical enum with an obvious equivalent. Canonicalize;
# any OTHER non-empty label keeps its verbatim text (B20 live evidence: a
# verdict that already says REJECT must not crash intake / fail-closed-pause
# the queue over label drift) — empty or non-string reasons still fail closed.
_REJECT_ALIASES = {
    "low_review_count": "dormant",
    "low_reviews": "low_rating",
    "not_enough_reviews": "dormant",
    "permanently_closed": "closed",
    "out_of_business": "closed",
    "food_court": "not_restaurant",
    "mall_food_court": "not_restaurant",
    "franchise": "chain",
    "virtual_kitchen": "ghost_kitchen",
    "delivery_only": "ghost_kitchen",
    "hotel_restaurant": "hotel",
    "resort": "hotel",
}


class QueuePaused(RuntimeError):
    """The queue is PAUSED (fail-closed); judging may not run."""


@dataclass(frozen=True)
class IcpVerdict:
    qualified: bool
    confidence: float
    reject_reason: str | None


@dataclass(frozen=True)
class QualifyReport:
    judged: int
    qualified: int
    rejected: int


def parse_verdict(payload: Any) -> IcpVerdict:
    """Validate the judge's JSON verdict; ValueError explains any violation."""
    if not isinstance(payload, dict):
        raise ValueError("icp judge verdict: payload is not an object")
    if "qualified" not in payload or "confidence" not in payload:
        raise ValueError("icp judge verdict: missing required keys")
    qualified = payload["qualified"]
    if not isinstance(qualified, bool):
        raise ValueError("icp judge verdict: 'qualified' must be a boolean")
    confidence = payload["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("icp judge verdict: 'confidence' must be a number")
    if not 0.0 <= float(confidence) <= 1.0:
        raise ValueError("icp judge verdict: 'confidence' out of range")
    reject_reason = payload.get("reject_reason")
    if qualified:
        if reject_reason is not None:
            raise ValueError(
                "icp judge verdict: a pass verdict carries no reject_reason"
            )
        return IcpVerdict(True, float(confidence), None)
    if not isinstance(reject_reason, str) or not reject_reason.strip():
        raise ValueError("icp judge verdict: a rejection demands reject_reason")
    # Known synonyms canonicalize; any other non-empty label is kept verbatim
    # (telemetry). The verdict already says REJECT — the safe direction — so
    # label drift (observed live: "on_delivery_platform", derived from our own
    # scraped-flag line) must never crash intake or pause the whole queue.
    canonical = _REJECT_ALIASES.get(reject_reason, reject_reason)
    return IcpVerdict(False, float(confidence), canonical)


def _lead_prompt(row: Any) -> str:
    payload: dict[str, Any] = {}
    if row["data_json"]:
        try:
            payload = json.loads(row["data_json"]) or {}
        except json.JSONDecodeError:
            payload = {}
    samples = payload.get("review_samples") or []
    sample_lines = "\n".join(f"- {str(s)[:200]}" for s in samples[:5]) or "- (none)"
    def flag(key):  # noqa: ANN001 — tiny local renderer for nullable booleans
        value = payload.get(key)
        return "unknown" if value is None else str(value).lower()

    return f"""You are the qualification judge for a restaurant-outreach department.
Judge whether this lead is a QUALIFIED outreach target.

QUALIFIED means ALL of:
- an independent, owner-run restaurant with a single location (no chain, no
  franchise, no multi-location brand)
- thriving: clearly active and well-reputed right now
- a real sit-down restaurant/bistro/cafe (NOT inside a hotel, mall food court
  or resort; NOT a ghost/virtual/delivery-only kitchen)

REJECT if the lead is or is inside: a chain or franchise, a multi-location
brand, a hotel or resort, a ghost kitchen, delivery-only, corporate-run,
closed, dormant (too few reviews / no recent activity), of poor reputation,
or not a real restaurant.

Evidence hints (NOT rules): rating >= 4.0 and review_count >= 100 suggest
health. Scraped booleans may be null — judge the whole picture and set your
confidence honestly.

Reply with STRICT JSON only and no commentary, exactly these keys:
{{"qualified": <true|false>, "confidence": <number 0.0-1.0>,
  "reject_reason": null when qualified, else exactly one of:
  {_PROMPT_REJECT_REASONS}}}
Use "other" if none of those fits; never invent a new reason value.

LEAD:
name: {row["name"]}
city: {row["city"]}
category: {payload.get("category") or "unknown"}
cuisine: {payload.get("cuisine") or "unknown"}
rating: {payload.get("rating") or "unknown"}
review_count: {payload.get("review_count") or "unknown"}
scraped flags: is_chain={flag("is_chain")} is_owner_run={flag("is_owner_run")} \
is_multi_location={flag("is_multi_location")} \
is_ghost_kitchen={flag("is_ghost_kitchen")} \
is_on_delivery_platform={flag("is_on_delivery_platform")} halal={flag("halal_status")}
recent review samples:
{sample_lines}"""


def _assert_queue_live(db: "Database") -> None:
    if get_config(db, "queue_status") == "PAUSED":
        raise QueuePaused("queue is PAUSED (fail-closed); qualification may not run")


def judge_lead(
    db: "Database",
    lead_id: int,
    *,
    transport: Any = None,
    sleep_s: float = 0.5,
    timeout_s: float = REQUEST_TIMEOUT_S,
    retries: int = MAX_RETRIES,
) -> str:
    """Judge one NEW lead; transition it QUALIFIED or REJECTED (with reason).

    `timeout_s` is threaded to the socket so a live smoke test can bound a
    wedged model instead of hanging (R12).
    """
    _assert_queue_live(db)
    row = db.conn.execute(
        "SELECT id, name, city, data_json FROM leads WHERE id=?", (lead_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown lead id={lead_id}")
    verdict: IcpVerdict
    try:
        raw = ask_with_guard(
            db,
            _lead_prompt(row),
            model=JUDGE_MODEL,
            expect_json=True,
            transport=transport,
            sleep_s=sleep_s,
            options=QUALIFICATION_OPTIONS,
            timeout_s=timeout_s,
            retries=retries,
        )
        verdict = parse_verdict(raw)
    except ValueError as exc:
        fail_closed_pause(db, reason=f"icp {exc}", component="icp")
        raise

    qual_json = json.dumps(
        {
            "qualified": verdict.qualified,
            "confidence": verdict.confidence,
            "reject_reason": verdict.reject_reason,
            "model": JUDGE_MODEL,
        }
    )
    with db.conn:
        db.conn.execute(
            "UPDATE leads SET icp_score=?, qualification_json=? WHERE id=?",
            (verdict.confidence, qual_json, lead_id),
        )
    if verdict.qualified:
        return transition_return(db, lead_id, "QUALIFIED")
    return transition_return(db, lead_id, "REJECTED", reason=verdict.reject_reason)


def transition_return(db: "Database", lead_id: int, to_state: str, *, reason: str | None = None) -> str:
    """Machine-blessed state move with the judge as actor."""
    from outreach.machine import transition

    return transition(db, lead_id, to_state, reason=reason, actor=_ACTOR)


def _rule_reject_reason(name: str | None, payload: Any, group_size: int) -> str | None:
    """Deterministic multi-location brain (B20, owner-explicit).

    Precedence: multi_location (scraped flag / branch marker proving >2
    locations / brand group >2 within the run) -> chain (scraped flag or the
    scraper's own HARDCODED_CHAINS names) -> corporate_account (hotel,
    resort, airport or franchise affiliations). None = the LLM judges it.
    """
    if junk_identity_reason(name) is not None:
        # 2026-11-04: no identity means nothing to judge. Rejecting here keeps
        # a placeholder row out of the model call and out of the send lane.
        return "junk_data"
    if payload.get("is_multi_location") is True:
        return "multi_location"
    if _marker_locations(name) > 2 or group_size > 2:
        return "multi_location"
    if payload.get("is_chain") is True:
        return "chain"
    raw_cf = (name or "").casefold()
    if any(entry in raw_cf for entry in HARDCODED_CHAINS):
        return "chain"
    if _AFFILIATION.search(raw_cf):
        return "corporate_account"
    return None


_BRAND_STRIP = frozenset(
    {
        "the", "branch", "branches", "location", "locations", "outlet",
        "outlets", "restaurant", "restaurants", "cafe", "cafes", "coffee",
        "kitchen", "grill", "bar", "bars", "eatery", "food", "co",
        "company", "inc", "ltd", "number", "no", "and", "first", "second",
        "third", "fourth", "fifth",
    }
)
_ORDINAL_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
_AFFILIATION = re.compile(
    r"\b(hotel|resort|airport|marriott|hilton|hyatt|sheraton|intercontinental"
    r"|holiday inn|four seasons|ritz|crowne|rotana|kempinski|anantara"
    r"|pullman|mercure|novotel|best western|ramada|wyndham)\b"
)
_ORDINAL_TOKEN = re.compile(r"\d{1,2}(?:st|nd|rd|th)?")


def _brand_key(name: str | None) -> str:
    """Marker/suffix-stripped brand key: 'Al Ghad (2nd Branch)' -> 'al ghad'."""
    if not name:
        return ""
    from outreach.importer import normalize_name

    without_parens = re.sub(r"\([^)]*\)", " ", name)
    tokens = [
        t
        for t in normalize_name(without_parens).split()
        if t not in _BRAND_STRIP and not _ORDINAL_TOKEN.fullmatch(t)
    ]
    return " ".join(tokens)


def _marker_locations(name: str | None) -> int:
    """Highest location count a branch marker proves (1 = no marker)."""
    if not name:
        return 1
    cf = name.casefold()
    best = 1
    found = re.search(r"\b(\d+)(?:st|nd|rd|th)?\s*(?:branch|location|outlet)", cf)
    if found:
        best = max(best, int(found.group(1)))
    words = re.search(
        r"\b(first|second|third|fourth|fifth)\s+(?:branch|location|outlet)", cf
    )
    if words:
        best = max(best, _ORDINAL_WORDS[words.group(1)])
    return best


def qualify_new_leads(
    db: "Database",
    *,
    transport: Any = None,
    sleep_s: float = 0.5,
    limit: int | None = None,
    run_id: str | None = None,
    timeout_s: float = REQUEST_TIMEOUT_S,
    retries: int = MAX_RETRIES,
) -> QualifyReport:
    """Judge every NEW, never-contacted lead (id order), fail-closed on any fault.

    B20: the deterministic multi-location brain runs FIRST (brand grouping
    across the run, branch markers, the scraper's chain list, affiliations)
    — obvious rejects never cost an LLM call; everything else goes to the
    judge unchanged. `run_id` scopes the pass to one scraper run.
    """
    from outreach.ollama_adapter import model_window_open

    if not model_window_open():
        # Owner order 2026-11-15: no model call before 20:55. Judging is a model
        # pass, so it simply does not run; the leads stay NEW for the night.
        return QualifyReport(judged=0, qualified=0, rejected=0)
    scope = " WHERE state='NEW' AND previously_contacted=0"
    params: list[Any] = []
    if run_id is not None:
        scope += " AND run_id=?"
        params.append(run_id)
    order = " ORDER BY id" + (" LIMIT ?" if limit is not None else "")
    if limit is not None:
        params.append(limit)
    rows = db.conn.execute(
        "SELECT id, name, data_json FROM leads" + scope + order, tuple(params)
    ).fetchall()

    # brand groups come from the WHOLE scope (all states): locations already
    # rejected in an earlier pass still prove the brand has >2 locations
    group_sql = (
        "SELECT name FROM leads" + (" WHERE run_id=?" if run_id is not None else "")
    )
    groups: Counter[str] = Counter(
        key
        for (nm,) in db.conn.execute(
            group_sql, (run_id,) if run_id is not None else ()
        )
        if (key := _brand_key(nm))
    )

    judged = qualified = rejected = 0
    for row in rows:
        payload: dict[str, Any] = {}
        if row["data_json"]:
            try:
                payload = json.loads(row["data_json"]) or {}
            except json.JSONDecodeError:
                payload = {}
        name = row["name"]
        reason = _rule_reject_reason(name, payload, groups.get(_brand_key(name), 1))
        if reason is not None:
            from outreach.machine import transition

            transition(
                db, int(row["id"]), "REJECTED",
                reason=reason, actor="icp-rules",
            )
            judged += 1
            rejected += 1
            continue
        state = judge_lead(
            db,
            int(row["id"]),
            transport=transport,
            sleep_s=sleep_s,
            timeout_s=timeout_s,
            retries=retries,
        )
        judged += 1
        if state == "QUALIFIED":
            qualified += 1
        else:
            rejected += 1
    return QualifyReport(judged=judged, qualified=qualified, rejected=rejected)
