"""Dish extractor (AI role #2) — model proposes, deterministic gate verifies.

Blueprint: {{dish}} = signature/most-mentioned dish; the validation gate is
CODE (not model trust): grounded in THIS lead's menu/review evidence, never a
generic non-dish word, sane token shape. No valid dish → the no-dish variant
(expected to dominate: menu_items exist on only a minority of scraper rows).
Fail-closed inherited from `ask_with_guard`; a semantically broken verdict is
itself a pause.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from outreach.icp import _assert_queue_live
from outreach.ollama_adapter import (
    DEFAULT_MODEL,
    MAX_RETRIES,
    REQUEST_TIMEOUT_S,
    ask_with_guard,
    fail_closed_pause,
    model_window_open,
)
from outreach.store import get_config

if TYPE_CHECKING:
    from outreach.store import Database

DISH_MODEL = DEFAULT_MODEL
DISH_OPTIONS = {"temperature": 0}
_ACTOR = "dish-extractor"

# single standing words that are never a dish candidate on their own
_GENERIC_DISH_WORDS = frozenset(
    {
        "food", "menu", "menus", "meal", "meals", "dish", "dishes", "drink",
        "drinks", "beverage", "beverages", "dessert", "desserts", "signature",
        "special", "specials", "lunch", "dinner", "brunch", "breakfast",
        "service", "restaurant", "item", "items", "everything", "anything",
    }
)
_STOPWORDS = frozenset(
    {"the", "a", "an", "of", "on", "with", "and", "is", "at", "their",
     "its", "in", "for", "my", "our", "his", "her"}
)
_MAX_TOKENS = 6


@dataclass(frozen=True)
class DishVerdict:
    dish: str | None
    confidence: float
    model_raw: str | None
    source: str  # "validated" | "fallback" | "low_confidence"


@dataclass(frozen=True)
class ExtractReport:
    extracted: int
    with_dish: int
    no_dish: int


def parse_dish_verdict(payload: Any) -> tuple[str | None, float]:
    """Strict schema: dish str|null + confidence in 0..1. ValueError otherwise."""
    if not isinstance(payload, dict):
        raise ValueError("dish extractor verdict: payload is not an object")
    if "dish" not in payload or "confidence" not in payload:
        raise ValueError("dish extractor verdict: missing required keys")
    dish = payload["dish"]
    if dish is not None and (not isinstance(dish, str) or not dish.strip()):
        raise ValueError("dish extractor verdict: 'dish' must be a string or null")
    confidence = payload["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("dish extractor verdict: 'confidence' must be a number")
    if not 0.0 <= float(confidence) <= 1.0:
        raise ValueError("dish extractor verdict: 'confidence' out of range")
    return (
        dish.strip() if isinstance(dish, str) else None,
        float(confidence),
    )


def _ok_shape(tokens: list[str]) -> bool:
    if not 1 <= len(tokens) <= _MAX_TOKENS:
        return False
    for token in tokens:
        if token.isnumeric():
            return False  # a numeric token is never part of a candidate dish
        if not any(ch.isalpha() for ch in token):
            return False
    return True


def _canon(candidate: str, menu: list[dict[str, Any]]) -> str:
    casefold = candidate.casefold()
    for item in menu:
        name = str(item.get("name") or "").strip()
        if name and name.casefold() == casefold:
            return name  # canonical casing from the menu itself
    return candidate


def validate_dish(
    candidate: str | None,
    menu: list[dict[str, Any]],
    reviews: list[str],
) -> str:
    """Return the usable dish name, or "" when the gate rejects the candidate."""
    if candidate is None:
        return ""
    normalized = re.sub(r"\s+", " ", candidate.casefold()).strip()
    if not normalized:
        return ""
    tokens = re.split(r"[\s\-]+", normalized)
    core_tokens = [t for t in tokens if t not in _STOPWORDS]
    if not core_tokens or not _ok_shape(tokens):
        return ""
    if len(core_tokens) == 1 and core_tokens[0].casefold() in _GENERIC_DISH_WORDS:
        return ""

    evidence_strings = [str(s) for s in reviews]
    for item in menu:
        evidence_strings.append(str(item.get("name") or ""))
        evidence_strings.append(str(item.get("description") or ""))
    token_set = set(tokens)
    grounded = any(
        token_set - set(re.split(r"[\s\-]+", s.casefold())) == set()
        for s in evidence_strings
        if s.strip()
    )
    if not grounded:
        return ""
    return _canon(re.sub(r"\s+", " ", candidate).strip(), menu)


def _dish_prompt(row: Any, menu: list[dict[str, Any]], reviews: list[str]) -> str:
    menu_lines = "\n".join(
        f"- {item.get('name')}: {item.get('description') or ''} ({item.get('category') or ''})".strip(" :")
        for item in menu[:15]
    ) or "(no menu available)"
    review_lines = "\n".join(f"- {str(r)[:200]}" for r in reviews[:8]) or "(none)"
    return f"""You are the dish extractor for a restaurant-outreach department.
From this restaurant's menus and recent guest reviews, name the SINGLE
signature or most-mentioned dish that the outreach message could celebrate.

Rules:
- The dish must be a real, specific dish discussed in THIS evidence (menu
  items, descriptions, or review mentions).
- Never generic non-dishes: "food", "menu", "meal", "drinks", "specials",
  "desserts", rambling sentences, or anything not grounded in the evidence.
- If nothing qualifies, or the evidence is empty, reply dish: null.

Reply with STRICT JSON only and no commentary, exactly these keys:
{{"dish": <dish name string or null>, "confidence": <number 0.0-1.0>}}

LEAD:
name: {row["name"]}
MENU:
{menu_lines}
RECENT REVIEW MENTIONS:
{review_lines}"""


# States in which a dish is worth extracting: both are post-qualification, i.e.
# the lead can actually be sent. ROUTED matters because production holds 186 of
# them and ZERO QUALIFIED rows (measured 2026-11-07) — a QUALIFIED-only pass
# could not run at all.
_SEND_READY_STATES = ("QUALIFIED", "ROUTED")


def _store_verdict(db: "Database", lead_id: int, verdict: DishVerdict) -> None:
    """Persist one verdict exactly one way (SSOT for the annotation shape)."""
    dish_json = json.dumps(
        {
            "dish": verdict.dish,
            "confidence": verdict.confidence,
            "model_raw": verdict.model_raw,
            "source": verdict.source,
            "model": DISH_MODEL if verdict.source not in (
                "deterministic", "menu", "no_evidence"
            ) else None,
        }
    )
    with db.conn:
        db.conn.execute(
            "UPDATE leads SET extracted_dish=?, dish_json=? WHERE id=?",
            (verdict.dish, dish_json, lead_id),
        )


def extract_dish(
    db: "Database",
    lead_id: int,
    *,
    transport: Any = None,
    sleep_s: float = 0.5,
    assert_live: bool = True,
    use_model: bool = True,
    timeout_s: float = REQUEST_TIMEOUT_S,
    retries: int = MAX_RETRIES,
) -> DishVerdict:
    """Extract + gate the dish for one send-ready lead; annotation, not an event.

    Order (2026-11-07): the lead's own review text is mined deterministically
    first, so a healthy extraction makes NO model call and cannot stall on an
    Ollama outage. The model stays as the fallback when the text yields nothing.
    A lead with no evidence at all is annotated no-dish without a model call:
    nothing could ground a dish, so a model could only invent one.
    """
    if assert_live:
        # Dispatch is forbidden while the queue is paused; annotating a dish is
        # not dispatch (no send, no state change), so an explicit backfill may
        # pass assert_live=False and run during an owner pause (2026-11-07).
        _assert_queue_live(db)
    row = db.conn.execute(
        "SELECT id, name, state, data_json FROM leads WHERE id=?", (lead_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown lead id={lead_id}")
    if row["state"] not in _SEND_READY_STATES:
        raise ValueError(
            f"lead {lead_id} is not QUALIFIED or ROUTED (state={row['state']})"
        )

    payload: dict[str, Any] = {}
    if row["data_json"]:
        try:
            payload = json.loads(row["data_json"]) or {}
        except json.JSONDecodeError:
            payload = {}
    menu = payload.get("menu_items") or []
    reviews = payload.get("review_samples") or []

    # Lazy import: dish_mining imports this module's gate, so a module-level
    # import here would be circular.
    from outreach.dish_mining import propose_dish_from_menu, propose_dish_from_reviews

    lead_name = str(row["name"] or "")
    mined = propose_dish_from_reviews(reviews, menu, lead_name)
    source = "deterministic"
    if not mined and menu:
        # Owner order 2026-11-12: the restaurant's own website menu is the
        # strongest evidence we have, and it must work with ZERO reviews.
        mined = propose_dish_from_menu(menu, lead_name)
        source = "menu"
    if mined:
        verdict = DishVerdict(
            dish=mined, confidence=1.0, model_raw=mined, source=source
        )
        _store_verdict(db, lead_id, verdict)
        return verdict
    if not menu and not reviews:
        verdict = DishVerdict(
            dish=None, confidence=0.0, model_raw=None, source="no_evidence"
        )
        _store_verdict(db, lead_id, verdict)
        return verdict
    if not use_model or not model_window_open():
        # A deterministic-only pass (the backfill), or the owner's model window
        # still being shut (2026-11-15: nothing may call the model before
        # 20:55), must never touch Ollama: a
        # downed model aborted the first real backfill run outright (measured
        # 2026-11-07), and there is nothing fail-closed to protect here -- the
        # lead simply falls back to the no-dish copy.
        verdict = DishVerdict(
            dish=None, confidence=0.0, model_raw=None, source="no_model"
        )
        _store_verdict(db, lead_id, verdict)
        return verdict

    raw = ask_with_guard(
        db,
        _dish_prompt(row, menu, reviews),
        model=DISH_MODEL,
        expect_json=True,
        transport=transport,
        sleep_s=sleep_s,
        options=DISH_OPTIONS,
        timeout_s=timeout_s,
        retries=retries,
    )
    try:
        model_dish, confidence = parse_dish_verdict(raw)
    except ValueError as exc:
        fail_closed_pause(db, reason=f"dish {exc}", component="dishes")
        raise

    validated = validate_dish(model_dish, menu, [str(r) for r in reviews])
    source = "validated" if validated else "fallback"
    if validated and confidence < float(
        get_config(db, "dish_confidence_threshold", 0.7)
    ):
        # a wrong dish is worse than no dish (addendum 5): below the
        # configured confidence the lead falls back to the no-dish variant
        validated = ""
        source = "low_confidence"
    verdict = DishVerdict(
        dish=validated or None,
        confidence=confidence,
        model_raw=model_dish,
        source=source,
    )
    _store_verdict(db, lead_id, verdict)
    return verdict


def extract_dishes(
    db: "Database",
    *,
    transport: Any = None,
    sleep_s: float = 0.5,
    limit: int | None = None,
    run_id: str | None = None,
    assert_live: bool = True,
    use_model: bool = True,
    timeout_s: float = REQUEST_TIMEOUT_S,
    retries: int = MAX_RETRIES,
) -> ExtractReport:
    """Annotate every send-ready lead that has no dish yet (id order).

    `run_id` scopes the pass to one scraper run (B20 intake); None = all.
    Already-annotated rows are skipped, so re-running is free and idempotent.
    """
    scope = (
        " WHERE state IN ('QUALIFIED', 'ROUTED')"
        " AND (extracted_dish IS NULL OR TRIM(extracted_dish) = '')"
    ) + (
        " AND run_id=?" if run_id is not None else ""
    )
    params: tuple[Any, ...] = (run_id,) if run_id is not None else ()
    ids = [
        int(r[0])
        for r in db.conn.execute(
            "SELECT id FROM leads" + scope + " ORDER BY id"
            + (" LIMIT ?" if limit is not None else ""),
            (*params, limit) if limit is not None else params,
        )
    ]
    extracted = with_dish = no_dish = 0
    for lead_id in ids:
        verdict = extract_dish(
            db, lead_id, transport=transport, sleep_s=sleep_s,
            assert_live=assert_live, use_model=use_model, timeout_s=timeout_s,
            retries=retries,
        )
        extracted += 1
        if verdict.dish:
            with_dish += 1
        else:
            no_dish += 1
    return ExtractReport(extracted=extracted, with_dish=with_dish, no_dish=no_dish)
