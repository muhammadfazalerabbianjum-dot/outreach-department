"""Variant engine (v11-batch1, owner packet C 2026-11-02): the owner's
9-variant IG first-touch packet — byte-pinned bodies + routing priority +
P.S. rotation + banned-phrase lint + bounded V1 generation — sitting
behind the existing render seam.

Laws:
- Owner scripts are byte-pinned copy (V11_BATCH1_OWNER_SCRIPTS.md). The
  no-em-dash standing law was applied to that copy with the B29
  punctuation-only precedent (em dash -> comma, never a word moved); the
  byte-pin tests fail on any other drift.
- RULE 2: no observed specific -> V5 only, never fabricate. RULE 4:
  owner known -> "Salam {owner}," replaces the bare "Salam!". RULE 5:
  one superlative max, periods not ellipses, one no-pressure phrase max.
  Banned phrases are lint-enforced at the message level, never
  auto-repaired: lint fail = refuse + digest row.
- V1 is the ONLY composed copy: bounded generation through the local
  Ollama adapter, gate = confidence >= 0.8 AND grounded in the post text
  AND the model's own genericness self-verify (would this sentence make
  sense to a different restaurant? yes -> fail). Every attempt is logged
  in `variant_generations` with post context, confidence and verdict.
  A dead model degrades to the routing fallback and NEVER pauses the
  queue (unlike the ICP/dish judges: no send decision rides on it).
- P.S. rotation: never identical in two consecutive sends to the same
  segment (segment = city); V5 capped at <=30% of the daily IG cap.
- Config switch `variant_engine` (off by default): off = the frozen v1.0
  copy lane; on = this engine for IG first-touch only. WA and email
  keep the v1.0 frozen copy until the owner pastes new copy (ADR-6).
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from outreach.ollama_adapter import DEFAULT_MODEL, ask_with_guard
from outreach.store import get_config, set_config

#: Every column a render may read off the lead row.
#:
#: LIVE 2026-11-09: `_ig_row` selected id/name/route_channel/extracted_dish/
#: instagram/ig_class/data_json but NOT `city`, and `route_variant` reads
#: `lead_row["city"]`. A `sqlite3.Row` raises `IndexError: No item with that
#: key` for a missing column - not the MessageHold the render was written to
#: raise - so every first touch died inside the IG sender
#: (`SENDER ERROR [ig] lead 284`) while ladder rungs, which never render an
#: opener, kept sending. A sender now selects with `render_select()` so the
#: row always carries what the renderer reads.
RENDER_COLUMNS: tuple[str, ...] = (
    "id",
    # `state` is here because `dispatcher._routed_row` selects the commit row
    # with this same list (single source) and still has to assert ROUTED.
    "state",
    "name",
    "city",
    "region",
    "route_channel",
    "extracted_dish",
    "data_json",
    "instagram",
    "phone",
    "phone_e164",
    "email",
    "ig_class",
)


def render_select() -> str:
    """SELECT carrying every RENDER_COLUMNS, keyed by lead id (`?`)."""
    return "SELECT " + ", ".join(RENDER_COLUMNS) + " FROM leads WHERE id=?"
from outreach.templates import (
    MessageHold,
    RenderedMessage,
    TemplateError,
)

if TYPE_CHECKING:
    from outreach.store import Database

# ---------------------------------------------------------------------------
# byte-pinned owner copy (V11_BATCH1_OWNER_SCRIPTS.md; em dash -> comma only)
# ---------------------------------------------------------------------------

VARIANT_BODIES: dict[str, str] = {
    "V1": "Salam! {{observed_moment}}",
    "V2": (
        "Salam! One of {{restaurant}}'s reviews said \"{{review_quote}}\","
        " that's a big claim from a stranger. Is it fair?"
    ),
    "V3": (
        "Salam! Quick question I ask every spot that impresses me in"
        " {{city}}, what's the one dish your regulars order without even"
        " looking at the menu? Saw {{restaurant}}'s {{rating}} from"
        " {{reviews}} reviews and had to ask."
    ),
    "V4": (
        "Salam! Seeing a halal spot this busy in {{city}} is a good sign."
        " Is the {{dish}} what people come for, or does something else"
        " sell out first?"
    ),
    "V5": (
        "Salam! Genuinely curious, what made you pick {{city}} for"
        " {{restaurant}}? MashaAllah, that kind of move takes real guts."
    ),
    "V6": (
        "Salam! The {{dish}} in your latest post looks like the one to"
        " order. Is it on all week, or only certain days?"
    ),
    "V7": (
        "Salam! Your reviews call the {{dish}} the closest thing to"
        " back-home cooking in {{city}}. Is that fair, or is another dish"
        " closer to it?"
    ),
    "V8": (
        "Salam! {{arabic_name}}, beautiful name for a restaurant."
        " Genuinely curious, what does it mean to you? There's always a"
        " story behind a name like that."
    ),
    "V9": (
        "Salam! {{restaurant}} keeps coming up when people in {{city}}"
        " argue about the best {{dish}}. Where do you think you sit in"
        " that argument?"
    ),
}

# v1.2 pack (owner paste 2026-11-05): PS-A/PS-B now name the product
# (restaurant systems: ordering, counter, kitchen); PS-C stays product-free
# on purpose so rotation keeps one honest, un-pitched slot. The owner's
# {dish} is the rotation's own `{{ps_subject}}` slot - the dish when the
# variant had one, the variant's question subject otherwise.
PS_ROTATION: dict[str, str] = {
    "PS-A": (
        "P.S. full transparency, I build restaurant systems for restaurants."
        " Ordering, counter, kitchen. But genuinely curious about the"
        " {{ps_subject}} first \U0001f604"
    ),
    "PS-B": (
        "P.S. I build restaurant systems, so I keep an eye on spots like this."
        " The {{ps_subject}} question is the real one though \U0001f604"
    ),
    "PS-C": (
        "P.S. disclosure: I'm in the restaurant-app world. The question is"
        " genuine though, the {{ps_subject}} looked too good not to ask"
        " \U0001f604"
    ),
}
PS_ORDER = ("PS-A", "PS-B", "PS-C")

BANNED_PHRASES = (
    "stopped my scroll",
    "looks unreal",
    "always good seeing a spot doing this well",
    # owner ruling 2026-11-11: the opener reacts to the quote's CONTENT; the
    # existence of reviews is never the thing we react to.
    "saw your reviews",
    "saw your review",
    "read your reviews",
    "seen your reviews",
    "noticed your reviews",
)
_SUPERLATIVES = re.compile(
    r"\b(best|unfair|unreal|amazing|incredible|closest|real deal)\b",
    re.IGNORECASE,
)
_NO_PRESSURE = re.compile(
    r"\b(no pressure|take your time|whenever you like|no rush)\b",
    re.IGNORECASE,
)
_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")
_POSTFIELDS_MIN_WORD = 4
_V1_MIN_CONFIDENCE = 0.8
_V5_SHARE = 0.30  # owner rule: V5 <= 30% of daily IG sends (cap-based)
_PS_QUESTIONS = {  # "question subject" for PS slots on non-dish variants
    "V1": "latest post", "V2": "that review", "V3": "menu",
    "V4": "community", "V5": "story", "V8": "name",
}

# routing requires (owner packet): signal key -> payload key
_V14_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "V1": ("ig_last_post",),
    "V2": ("review_quote",),
    "V7": ("heritage_signal",),
    "V4": ("community_signal",),
    "V6": ("ig_last_post", "comment_first_feasible"),
    "V9": ("rivalry_signal",),
}


class LintBlocked(TemplateError):
    """Message-level lint law: banned phrase / ellipsis / double
    superlative / missing P.S. or question / inexact Arabic copy."""


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------


def seed_variant_tables(db: "Database", *, reseed: bool = False) -> int:
    """Create + seed `variants`, `variant_ps` and `variant_generations` /
    `variant_history`; idempotent, and a drifted stored body RAISES (copy
    changes only via the owner-paste process, never silent).

    `reseed=True` is that paste path: it installs the pinned copy over a
    differing stored row and leaves one digest line naming what changed. It
    returns the number of rows it rewrote, so a plain call (0) and a real
    copy change are distinguishable in the caller's output.
    """
    with db.conn:
        db.conn.execute(
            "CREATE TABLE IF NOT EXISTS variants ("
            " variant_id TEXT PRIMARY KEY, body TEXT NOT NULL,"
            " requires TEXT NOT NULL, ps_binding TEXT NOT NULL)"
        )
        db.conn.execute(
            "CREATE TABLE IF NOT EXISTS variant_ps ("
            " ps_id TEXT PRIMARY KEY, body TEXT NOT NULL)"
        )
        db.conn.execute(
            "CREATE TABLE IF NOT EXISTS variant_generations ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER,"
            " post_text TEXT, composed TEXT, confidence REAL,"
            " text_hash TEXT, verdict TEXT, created_at TEXT)"
        )
        db.conn.execute(
            "CREATE TABLE IF NOT EXISTS variant_history ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, segment TEXT,"
            " variant_id TEXT, ps_binding TEXT, lead_id INTEGER,"
            " at_iso TEXT)"
        )
    _PS_NAMES = {"PS-A", "PS-B", "PS-C"}
    changed: list[str] = []
    for vid, body in VARIANT_BODIES.items():
        row = db.conn.execute(
            "SELECT body FROM variants WHERE variant_id=?", (vid,)
        ).fetchone()
        if row is None:
            with db.conn:
                db.conn.execute(
                    "INSERT INTO variants(variant_id, body, requires,"
                    " ps_binding) VALUES (?, ?, ?, ?)",
                    (vid, body, json.dumps(_V14_REQUIREMENTS.get(vid, ())),
                     "rotation"),
                )
        elif row[0] != body:
            if not reseed:
                raise TemplateError(
                    f"variant drift: {vid} body differs from the pinned owner"
                    " copy (fix only via owner paste)"
                )
            with db.conn:
                db.conn.execute(
                    "UPDATE variants SET body=? WHERE variant_id=?", (body, vid)
                )
            changed.append(vid)
    for ps_id, body in PS_ROTATION.items():
        row = db.conn.execute(
            "SELECT body FROM variant_ps WHERE ps_id=?", (ps_id,)
        ).fetchone()
        if row is None:
            with db.conn:
                db.conn.execute(
                    "INSERT INTO variant_ps(ps_id, body) VALUES (?, ?)",
                    (ps_id, body),
                )
        elif row[0] != body:
            if not reseed:
                raise TemplateError(
                    f"variant_ps drift: {ps_id} body differs from the pinned"
                    " owner copy"
                )
            with db.conn:
                db.conn.execute(
                    "UPDATE variant_ps SET body=? WHERE ps_id=?", (body, ps_id)
                )
            changed.append(ps_id)
    if changed:
        with db.conn:
            db.conn.execute(
                "INSERT INTO digest(body) VALUES (?)",
                ("VARIANT COPY reseeded from the owner paste: "
                 + ", ".join(changed),),
            )
    return len(changed)


# ---------------------------------------------------------------------------
# lint (message level; refuse, never auto-repair)
# ---------------------------------------------------------------------------


def lint(
    text: str, owner: str | None = None, arabic_name: str | None = None
) -> None:
    """Raise LintBlocked listing every violation when the message breaks
    the owner's guardrail rules; return quietly when clean."""
    problems: list[str] = []
    folded = text.casefold()
    for phrase in BANNED_PHRASES:
        if phrase.casefold() in folded:
            problems.append(f"banned phrase: {phrase!r}")
    if "..." in text or "\u2026" in text:
        problems.append("ellipsis")
    if "\u2014" in text:
        # Ruling A (v1.2 pack, 2026-11-05): the standing no-em-dash law is
        # enforced mechanically, so a future owner paste cannot reintroduce
        # the character by accident. Refuse, never rewrite.
        problems.append("em dash (standing law: none in any message copy)")
    if len(_SUPERLATIVES.findall(text)) > 1:
        problems.append("more than one superlative")
    if len(_NO_PRESSURE.findall(text)) > 1:
        problems.append("more than one no-pressure phrase")
    if "P.S." not in text:
        problems.append("missing P.S.")
    if "?" not in text:
        problems.append("missing question mark")
    if arabic_name and _ARABIC_RE.search(arabic_name or ""):
        if arabic_name not in text:
            problems.append("arabic name not copied byte-exact")
    if problems:
        raise LintBlocked("lint blocked: " + "; ".join(problems))


# ---------------------------------------------------------------------------
# routing (owner packet priority, deterministic on the lead payload)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteChoice:
    variant_id: str
    fills: dict[str, str]


def _payload(lead_row: Any) -> dict[str, Any]:
    try:
        payload = json.loads(lead_row["data_json"] or "{}") or {}
    except (ValueError, TypeError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def route_variant(
    lead_row: Any, *, exclude: frozenset[str] = frozenset()
) -> RouteChoice:
    """The owner packet routing priority, 1..7, deterministic.

    Excluded variants (no-repeat law / V5 cap / generation failure) are
    skipped in priority order; the last resort is V5 unless V5 itself is
    excluded or its fills are empty (MessageHold: never fabricate).
    """
    payload = _payload(lead_row)
    name = (lead_row["name"] or "").strip() if lead_row is not None else ""
    city = (lead_row["city"] or "").strip() if lead_row is not None else ""
    if not name or not city:
        raise MessageHold(
            "variant engine cannot fill {{restaurant}}/{{city}}: name or"
            " city blank — never invent (hold, Q7)"
        )
    dish = (lead_row["extracted_dish"] or "").strip()
    rating = payload.get("rating")
    try:
        rating_f = float(rating) if rating is not None else None
    except (TypeError, ValueError):
        rating_f = None
    reviews = payload.get("review_count")
    try:
        reviews_n = int(reviews) if reviews is not None else None
    except (TypeError, ValueError):
        reviews_n = None
    arabic_name = name if _ARABIC_RE.search(name) else ""
    # owner packet priority: 1 = latest IG post -> V1, or V6 when
    # comment-first is feasible; 2 = V8; 3 = V2; 4 = V7; 5 = V4;
    # 6 = V3/V9; 7 = V5.
    priority = ("V6", "V1", "V8", "V2", "V7", "V4", "V3", "V9", "V5")
    post = str(payload.get("ig_last_post") or "").strip()
    comment_first = bool(payload.get("comment_first_feasible"))
    for vid in priority:
        if vid in exclude:
            continue
        if vid == "V6":
            if not (comment_first and post and dish):
                continue  # the DM line names the dish — never fabricate
            return RouteChoice(vid, {})
        if vid == "V1":
            if comment_first or not post:
                continue  # V6 owns the post when comment-first is feasible
            return RouteChoice(vid, {})
        if vid == "V8":
            if not arabic_name:
                continue
            return RouteChoice(vid, {"{{arabic_name}}": arabic_name})
        if vid == "V2":
            # Owner ruling 2026-11-11: a quote ships only when it is SPECIFIC
            # (a dish, a moment, a story). Rating words, service words and
            # generic praise are refused by name, and a lead whose quotes all
            # fail routes on to the moment tier rather than to generic V5.
            from outreach.quotes import select_quote

            picked = select_quote(payload)
            if picked.quote is None:
                continue
            return RouteChoice(vid, {"{{review_quote}}": picked.quote})
        if vid == "V7" and not payload.get("heritage_signal"):
            continue
        if vid == "V7":
            if not dish:
                continue  # never fabricate the dish the copy celebrates
            return RouteChoice(vid, {"{{dish}}": dish})
        if vid == "V4" and not payload.get("community_signal"):
            continue
        if vid == "V4":
            if not dish:
                continue
            return RouteChoice(vid, {"{{dish}}": dish})
        if vid in ("V3", "V9"):
            if not (rating_f is not None and rating_f >= 4.5
                    and reviews_n is not None and reviews_n > 0):
                continue
            if vid == "V9" and not payload.get("rivalry_signal"):
                continue
            if vid == "V3" and payload.get("rivalry_signal"):
                continue  # priority 6's V9 clause when rivalry reads natural
            if vid == "V9" and not dish:
                continue
            fills: dict[str, str] = {
                "{{rating}}": str(rating).strip(),
                "{{reviews}}": str(reviews).strip(),
            }
            if vid == "V9":
                fills["{{dish}}"] = dish
            return RouteChoice(vid, fills)
        if vid == "V5":
            return RouteChoice(vid, {})
    raise MessageHold(
        "no owner variant is fillable for this lead (never fabricate,"
        " rule 2): hold at ROUTED"
    )


def _fallback_after(
    lead_row: Any, failed: str
) -> RouteChoice:
    """V1 generation failure -> fall back to V2/V3/V5 per routing."""
    return route_variant(lead_row, exclude=frozenset({failed}))


# ---------------------------------------------------------------------------
# bounded V1 generation (the ONLY copy the model may write)
# ---------------------------------------------------------------------------


def _compose_prompt(post_text: str) -> str:
    return (
        "One sentence, at most ~20 words, for a restaurant-outreach DM. Use"
        " ONE concrete detail from THIS latest post (the dish, the flame,"
        " the pour, the sound) and end with a short question about exactly"
        " that detail. Rules: no superlatives, no ellipses, no banned"
        " marketing phrases, periods instead of ellipses, must be readable"
        " to this exact restaurant only.\n"
        "Reply STRICT JSON only: {\"sentence\": <str>, \"confidence\":"
        " <0.0-1.0>}\n\nPOST:\n" + post_text[:600]
    )


def _generic_prompt(sentence: str, post_text: str) -> str:
    return (
        "Self-verify for outreach copy. Question: would this sentence make"
        " the same sense if sent to a DIFFERENT restaurant? Answer"
        " strictly JSON: {\"generic\": <true|false>, \"confidence\":"
        " <0.0-1.0>}. true = generic (reject).\n\nSENTENCE:\n"
        + sentence + "\n\nPOST:\n" + post_text[:600]
    )


def _concrete_noun_ok(sentence: str, post_text: str) -> bool:
    """Gate: the composed line must reference a concrete noun from the post
    (a shared content word the genericness check alone does not prove)."""
    words = re.findall(r"[A-Za-z\u0600-\u06FF]{%d,}" % _POSTFIELDS_MIN_WORD, post_text)
    post_tokens = {w.casefold() for w in words} - {
        "about", "their", "restaurant", "orders", "tonight", "friends",
    }
    sent_tokens = {w.casefold() for w in re.findall(
        r"[A-Za-z\u0600-\u06FF]{%d,}" % _POSTFIELDS_MIN_WORD, sentence
    )}
    return bool(post_tokens & sent_tokens)


def _compose_moment(
    db: "Database", post_text: str, *, transport: Any = None
) -> tuple[str, float]:
    """Model composes; code gates. Picks up the existing fail-closed
    adapter (retries 3x, strict JSON, never invents a continuation)."""
    verdict = ask_with_guard(
        db,
        _compose_prompt(post_text),
        model=DEFAULT_MODEL,
        expect_json=True,
        transport=transport,
        options={"temperature": 0},
    )
    if not isinstance(verdict, dict) or "sentence" not in verdict or "confidence" not in verdict:
        raise ValueError("compose verdict missing keys")
    confidence = float(verdict["confidence"])
    sentence = str(verdict["sentence"]).strip()
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("compose confidence out of range")
    return sentence, confidence


def _log_generation(
    db: "Database",
    lead_id: int,
    post_text: str,
    composed: str,
    confidence: float | None,
    verdict: str,
) -> None:
    with db.conn:
        db.conn.execute(
            "INSERT INTO variant_generations(lead_id, post_text, composed,"
            " confidence, text_hash, verdict, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                lead_id,
                post_text[:400],
                composed[:400],
                confidence,
                None if not composed
                else hashlib.sha256(composed.encode()).hexdigest(),
                verdict,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


# ---------------------------------------------------------------------------
# P.S. rotation + V5 share cap
# ---------------------------------------------------------------------------


def _last_for_segment(db: "Database", segment: str) -> dict[str, str | None]:
    seed_variant_tables(db)  # self-sufficiency: rotation works on a fresh DB
    row = db.conn.execute(
        "SELECT variant_id, ps_binding FROM variant_history"
        " WHERE segment=? ORDER BY id DESC LIMIT 1",
        (segment,),
    ).fetchone()
    if row is None:
        return {"variant_id": None, "ps_binding": None}
    return {"variant_id": row[0], "ps_binding": row[1]}


def next_ps(
    db: "Database", segment: str, *, last_used: str | None = None,
    rng: random.Random | None = None,
) -> str:
    """Rotation law: never identical in two consecutive sends to the same
    segment; ties broken by the fixed order, otherwise a free draw."""
    last = last_used if last_used is not None else (
        _last_for_segment(db, segment) or {}
    ).get("ps_binding")
    if not last:
        return (rng or random.Random()).choice(PS_ORDER)
    rest = [ps_id for ps_id in PS_ORDER if ps_id != last]
    return (rng or random.Random()).choice(rest)


def v5_sends_today(db: "Database", now: datetime) -> int:
    """V5 sends already logged today (PKT day, caps convention)."""
    seed_variant_tables(db)
    day = (now.astimezone(timezone.utc) + timedelta(hours=5)).strftime(
        "%Y-%m-%d"
    )
    v5_hash = hashlib.sha256(VARIANT_BODIES["V5"].encode()).hexdigest()
    row = db.conn.execute(
        "SELECT COUNT(*) FROM message_log WHERE channel='ig'"
        " AND substr(sent_at,1,10)=? AND template_hash=?",
        (day, v5_hash),
    ).fetchone()
    return int(row[0])


def v5_share_ok(db: "Database", now: datetime) -> bool:
    """<=30% of the daily IG cap (ASSUMED denominator = the cap, not the
    partial sends so far: 'a share of daily sends' reads stable; documented
    in DECISIONS 2026-11-02)."""
    from outreach.caps import current_cap

    used = v5_sends_today(db, now)
    cap = current_cap(db, "ig", now)
    return (used + 1) <= _V5_SHARE * cap


def _log_history(
    db: "Database", segment: str, variant_id: str, ps_binding: str,
    lead_id: int, now: datetime,
) -> None:
    with db.conn:
        db.conn.execute(
            "INSERT INTO variant_history(segment, variant_id, ps_binding,"
            " lead_id, at_iso) VALUES (?, ?, ?, ?, ?)",
            (segment, variant_id, ps_binding, lead_id,
             now.astimezone(timezone.utc).isoformat()),
        )


# ---------------------------------------------------------------------------
# render (the lane entry; config-switched v1.0 vs engine)
# ---------------------------------------------------------------------------


def _fill(text: str, fills: dict[str, str]) -> str:
    rendered = text
    for token, value in fills.items():
        rendered = rendered.replace(token, value)
    if "{{" in rendered:
        raise TemplateError(
            "variant engine placeholder drift: unresolved '{{' remains"
        )
    return rendered


def _subject_for(variant_id: str, dish: str) -> str:
    """PS slot subject: the dish when the variant had one, else the
    variant's question subject (owner note on static variants)."""
    if dish:
        return dish
    return _PS_QUESTIONS.get(variant_id, "the spot")


def _gate_compose(sentence: str, confidence: float, post_text: str) -> None:
    """Code gate on the composed line: confidence floor, concrete grounding,
    and the lint floor the whole message must clear later (banned /
    ellipsis / superlative count). A violation here is a MESSAGE-level
    lint failure: refuse + digest, never auto-repair (law above)."""
    if confidence < _V1_MIN_CONFIDENCE:
        raise ValueError(f"compose confidence {confidence} < 0.8")
    if not _concrete_noun_ok(sentence, post_text):
        raise ValueError("composed line not grounded in the post text")
    problems: list[str] = []
    folded = sentence.casefold()
    for phrase in BANNED_PHRASES:
        if phrase.casefold() in folded:
            problems.append(f"banned phrase: {phrase!r}")
    if "..." in sentence or "\u2026" in sentence:
        problems.append("ellipsis")
    if len(_SUPERLATIVES.findall(sentence)) > 1:
        problems.append("more than one superlative")
    if problems:
        raise LintBlocked("composed line blocked: " + "; ".join(problems))


#: Owner order 2026-11-12: when the dish came from the restaurant's own menu,
#: the sentence must say so instead of implying a post. Only V4 and V7 carry a
#: source-implying verb; every other variant is source-neutral and untouched.
MENU_DISH_BODIES: dict[str, str] = {
    # R5 (owner approval 2026-11-14, "apply all"): these two are the same V4/V7
    # sentences the owner approved, with the menu qualifier where the dish is
    # named. The old wording ("Salam brother", "looks like the real deal") was the
    # copy the owner retired; a menu-sourced send must not keep it.
    "V4": (
        "Salam! Seeing a halal spot this busy in {{city}} is a good sign."
        " Is the {{dish}} on your menu what people come for, or does"
        " something else sell out first?"
    ),
    "V7": (
        "Salam! Your reviews call the {{dish}} on your menu the closest"
        " thing to back-home cooking in {{city}}. Is that fair, or is"
        " another dish closer to it?"
    ),
}


def _dish_on_menu(dish: str, menu_items: object) -> bool:
    """Is this dish literally on the restaurant's own menu?"""
    if not dish:
        return False
    folded = dish.casefold()
    for item in menu_items or ():
        name = str((item or {}).get("name", "") if isinstance(item, dict) else "").casefold()
        if name and folded in name:
            return True
    return False


def render_ig_lead(
    db: "Database",
    lead_row: Any,
    *,
    now: datetime | None = None,
    transport: Any = None,
) -> RenderedMessage:
    """One IG first-touch through the 9-variant packet (engine ON)."""
    if now is None:
        now = datetime.now(timezone.utc)
    seed_variant_tables(db)
    payload = _payload(lead_row)
    segment = (lead_row["city"] or "").strip()
    last = _last_for_segment(db, segment)

    choice = _apply_no_repeat(db, lead_row, route_variant(lead_row), last)

    composed_line: str | None = None
    confidence: float | None = None
    sentence: str = ""
    if choice.variant_id == "V1":
        post_text = str(payload.get("ig_last_post") or "").strip()
        try:
            sentence, confidence = _compose_moment(
                db, post_text, transport=transport
            )
            generic = ask_with_guard(
                db,
                _generic_prompt(sentence, post_text),
                model=DEFAULT_MODEL,
                expect_json=True,
                transport=transport,
                options={"temperature": 0},
            )
            if not (isinstance(generic, dict)
                    and generic.get("generic") is False):
                raise ValueError("genericness self-verify rejected the line")
            _gate_compose(sentence, confidence, post_text)
        except LintBlocked:
            _log_generation(
                db, int(lead_row["id"]), post_text, sentence, confidence,
                "lint_blocked",
            )
            with db.conn:
                db.conn.execute(
                    "INSERT INTO digest(body) VALUES (?)",
                    (f"VARIANT LINT BLOCK [lead {lead_row['id']}]: composed"
                     " V1 line failed the owner guardrail lint; send"
                     " refused for owner review (never auto-reparied)",),
                )
            raise
        except Exception as exc:  # noqa: BLE001 — model/gate failure
            _log_generation(
                db, int(lead_row["id"]), post_text, "", None,
                f"fallback:{type(exc).__name__}",
            )
            choice = _apply_no_repeat(
                db, lead_row,
                route_variant(lead_row, exclude=frozenset({"V1"})),
                last,
            )
        else:
            composed_line = sentence
            _log_generation(
                db, int(lead_row["id"]), post_text, sentence, confidence,
                "accepted",
            )

    dish = (lead_row["extracted_dish"] or "").strip()
    if choice.variant_id == "V5" and not v5_share_ok(db, now=now):
        # cap hit: prefer the next eligible variant, else V5 still never
        # silently refuses the lead (the rung holds instead)
        choice = _apply_no_repeat(
            db, lead_row, route_variant(lead_row, exclude=frozenset({"V5"})),
            last,
        )
        if choice.variant_id == "V5":
            raise MessageHold(
                "V5 share cap reached and no other variant fills this"
                " lead - hold at ROUTED (never spam thin-data copy)"
            )

    fills = dict(choice.fills)
    if choice.variant_id == "V2":
        quoted = fills.get("{{review_quote}}") or ""
        # The opener must carry the quote it reacts to, byte for byte: a
        # paraphrase is our words pretending to be the guest's.
        if quoted and quoted not in _fill(VARIANT_BODIES["V2"], {**fills,
                "{{city}}": (lead_row["city"] or "").strip(),
                "{{restaurant}}": (lead_row["name"] or "").strip()}):
            raise MessageHold(
                "V2 opener does not carry the selected quote verbatim - hold"
                " (never quote a guest approximately)"
            )
    fills["{{city}}"] = (lead_row["city"] or "").strip()
    fills["{{restaurant}}"] = (lead_row["name"] or "").strip()
    if composed_line is not None:
        fills["{{observed_moment}}"] = composed_line
    body = VARIANT_BODIES[choice.variant_id]
    if _dish_on_menu(dish, payload.get("menu_items") or ()):
        body = MENU_DISH_BODIES.get(choice.variant_id, body)
    text = _fill(body, fills)

    # RULE 4: owner known -> "Salam {owner}," replaces the bare "Salam!"
    # CONTAMINATION HOLD 2026-11-03: the enrichment fabricated owner names
    # ("Dan Gilbert" on 17 unrelated restaurants), so a name fills only when
    # the owner explicitly re-arms the feature AND the lead carries scraper
    # evidence. Otherwise the bare "Salam!" opener stands — never a guess.
    owner = str(payload.get("owner_name") or "").strip()
    if owner and _owner_fill_allowed(db, payload) and text.startswith("Salam!"):
        text = f"Salam {owner.split()[0]}," + text[len("Salam!"):]

    ps_id = next_ps(db, segment, last_used=last.get("ps_binding"))
    ps_subject = _subject_for(choice.variant_id, dish)
    ps_body = _fill(PS_ROTATION[ps_id], {"{{ps_subject}}": ps_subject})
    text = f"{text}\n{ps_body}"

    arabic = choice.fills.get("{{arabic_name}}")
    lint(text, arabic_name=(arabic if _ARABIC_RE.search(arabic or "") else None))

    template_hash = hashlib.sha256(body.encode()).hexdigest()
    _log_history(db, segment, choice.variant_id, ps_id,
                 int(lead_row["id"]), now)
    return RenderedMessage(
        text=text, template_hash=template_hash,
        variant=choice.variant_id,
    )


def _owner_fill_allowed(db: "Database", payload: dict[str, Any]) -> bool:
    """Owner-name fills: OFF by default, and evidence-gated when re-armed."""
    if not int(get_config(db, "owner_fill_enabled", 0) or 0):
        return False
    evidence = payload.get("owner_verified")
    if isinstance(evidence, str):
        evidence = evidence.strip().lower() in ("1", "true", "yes", "verified")
    return bool(evidence)


def _apply_no_repeat(
    db: "Database", lead_row: Any, choice: RouteChoice,
    last: dict[str, str | None],
) -> RouteChoice:
    """Never the same variant twice in a row to the same segment: tried
    first, then the packet priority again with it excluded."""
    sentinel = object()
    for _ in range(3):  # bounded retries, then keep the current choice
        if last.get("variant_id") != choice.variant_id:
            return choice
        try:
            choice = route_variant(
                lead_row, exclude=frozenset({choice.variant_id})
            )
        except MessageHold:
            return choice  # nothing else eligible: never empty (documented)
    return choice


def all_possible_openers(
    db: "Database", lead_row: Any, *, now: datetime | None = None
) -> list[str]:
    """Every opener this lead could EVER have been shipped, for direction guards.

    Live 2026-11-15: a thread held *"Salam! New Spice Grill's 4.5 stars from 795
    reviews…"* — copy from the FROZEN template family (`templates/social_nodish.txt`),
    shipped while `variant_engine` was off — while the probe compared only against
    the variant lane that is on TODAY. Our own DM therefore read as a restaurant
    reply: two leads flipped to REPLIED, their ladders cancelled, both handed to
    the owner as unanswered.

    So this renders **every** pinned variant body, whether or not it is eligible
    right now (eligibility depends on today's data and config, and the thread
    carries yesterday's answer). A body whose placeholders cannot be filled from
    what we still know contributes nothing rather than a guess. Each opener is
    paired with every P.S. binding, because the shipped text carries one.

    Model-free and deterministic: V1 is composed from the observed post, so it
    has no static body to compare and is skipped here (the fuzzy match in
    `replies._most_words_are_ours` covers an edited copy).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if lead_row is None:
        return []
    seed_variant_tables(db)
    payload = _payload(lead_row)
    name = (lead_row["name"] or "").strip()
    city = (lead_row["city"] or "").strip()
    dish = (lead_row["extracted_dish"] or "").strip()
    quote = ""
    try:
        from outreach.quotes import select_quote

        quote = select_quote(payload).quote or ""
    except Exception:  # noqa: BLE001 - a missing quote just leaves V2 out
        quote = ""
    base = {
        "{{restaurant}}": name,
        "{{city}}": city,
        "{{rating}}": str(payload.get("rating") or "").strip(),
        "{{reviews}}": str(payload.get("review_count") or "").strip(),
        "{{dish}}": dish,
        "{{review_quote}}": quote,
        "{{arabic_name}}": name if _ARABIC_RE.search(name) else "",
    }
    on_menu = _dish_on_menu(dish, payload.get("menu_items") or ())
    copies: list[str] = []
    for variant_id, body in VARIANT_BODIES.items():
        if variant_id == "V1":
            continue  # composed from the post: no static body exists
        if any(not payload.get(key) for key in _V14_REQUIREMENTS.get(variant_id, ())):
            continue  # a variant whose evidence is gone never shipped either
        template = MENU_DISH_BODIES.get(variant_id, body) if on_menu else body
        try:
            opener = _fill(template, dict(base))
        except Exception:  # noqa: BLE001 - an unfillable body contributes none
            continue
        if "{{" in opener:
            continue  # a placeholder we cannot fill: never half a sentence
        for ps_body in PS_ROTATION.values():
            try:
                ps = _fill(ps_body, {"{{ps_subject}}": _subject_for(variant_id, dish)})
            except Exception:  # noqa: BLE001
                copies.append(opener)
                continue
            copies.append(opener + chr(10) + ps)
    return copies


def render_lane(
    db: "Database", channel: str, lead_row: Any, *, now: datetime | None = None
) -> Any:
    """THE SEAM: v1.0 frozen copy for wa/email, and for ig when the engine
    is off; packet variants for ig when `variant_engine` is set.

    O2 (2026-11-13): the second enforcement point of "no comment, no send".
    A claim can be bypassed by a manual run or a future caller; rendering
    cannot, because every IG render passes through here.
    """
    if channel == "ig":
        from outreach.warmup import assert_warmed

        key = lead_row["id"] if "id" in lead_row.keys() else lead_row["lead_id"]
        assert_warmed(db, int(key))
    engine_on = bool(get_config(db, "variant_engine")) and channel == "ig"
    if not engine_on:
        from outreach.templates import render_for_lead

        return render_for_lead(channel, lead_row)
    return render_ig_lead(db, lead_row, now=now)


def render_lane_opener(
    db: "Database", channel: str, lead_row: Any, *, now: Any = None
) -> str | None:  # unchanged signature; docstring below
    """Reply-probe seam: the opener text the direction guard keys on, via
    the SAME lane the send used (variant copy must match what shipped).
    Returns None when the lane refuses to render (MessageHold et al)."""
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        return render_lane(db, channel, lead_row, now=now).text
    except Exception:  # noqa: BLE001 — matching never blocks a probe pass
        return None
