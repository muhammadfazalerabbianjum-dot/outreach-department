"""Template engine — byte-exact frozen outreach copy (ADR-6).

Law: the two variant files under `templates/` are the copy, byte-for-byte.
The ONLY sanctioned fills are the placeholders as typed by the owner,
normalized to moustaches at freeze time (`(dish)` ⇒ {{dish}}, `[name]` ⇒
{{name}}, `[Restaurant]` ⇒ {{restaurant}}, `[platform]` ⇒ {{platform}} —
token syntax only; never a word of copy). Every rendered byte elsewhere
must equal the file. sha256 of each file is pinned in PINNED_SHA256 — any
drift outside the fills is a test failure, not a silent rewrite.

Variant set (owner voice edit 2026-10-29; previously owner-pasted
2026-10-23/24 + owner-delegated no-dish pair):
- `social`         — WA + IG, "Salam!" opener that ends in a question, {{dish}} required;
- `email`          — Salam greeting, all four fills, {{dish}} required;
- `social_nodish`  — rating/review-anchored opener (no dish available);
- `email_nodish`   — same for email; both reuse the owner's frozen P.S/rows;
- `followup`/`followup_recap` — the LIVE ladder: touch 1 = day-3 offer,
  touch 2 = day-7 recap, and that is the end of it (owner ruling 2026-11-07:
  two touches, days 3 and 7; `MAX_LADDER_TOUCH`). Every rung ships its stage
  instead of re-sending the opener (live: leads were getting the same message
  twice on the same topic). First sends and the day-7 IG escalation keep the
  opener.
- `followup_value`/`followup_soft` — RETIRED copy (day-14 value, day-30/180
  soft from the old 4-stage set). Kept pinned and undeleted because historical
  `message_log` rows attribute themselves by hash; they are never armed, and
  `render_followup` refuses touch 3 or higher. A leftover pending row from the
  old cadence is expired by `followups`, not sent.

Law (owner directive 2026-10-30): NO message copy may contain an em dash —
pinned by test_no_template_contains_an_em_dash; swap only via owner paste.

Leads without a dish AND without rating/review evidence HOLD at ROUTED —
nothing is invented (Q7).

Owner gate: `template_status` PROVISIONAL until "owner_confirmed";
`assert_sendable_template` is the dispatcher-side check. The email channel
additionally honors the `email_enabled` gate (ADR-5) at the dispatcher.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from outreach.store import Database

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
DEFAULT_PLATFORM = "Google Maps"  # Q6 provisional (leads carry no source)

# byte-drift gate — update ONLY via an owner copy edit (paste or preview
# edit — ADR-6 sanctioned path, logged in DECISIONS.md)
PINNED_SHA256: dict[str, str] = {
    # R5 (owner approval 2026-11-14): "apply all" - the pitch-from-first-line and
    # the ellipses are gone; the transparency line moved to touch 2.
    "social": "5baa95d26cfe148f35664b145c525988b75e366bf755b6773e60fd00049fa5ff",
    "email": "b89e5a6b04fc1c347a7864f4be9a2203cd2eaa63d1e5bb9cbf7d0a7aa35c8d2c",
    "social_nodish": "5df93412248c96e98a95f794ffa80a72f8a3662e7dbc2b5c4e5739cb72549982",
    "email_nodish": "db4d7561b97200537b16a8f1bf27d7570430a5eb5ce1dd07871fefaef9e4ba47",
    "followup": "e03d8d12aa9203a53b8691d7da533280c2b2a9291975af40e06d0b5b7d97e6d0",
    "followup_recap": "5c45f6d99cf71b0c4a61d4db2c122af137a22182b412b902c0fac8230a9d16de",
    "followup_value": "ba0d074f59cb5230830f7f5ab2a36156dc5a444e50a79097f8544e6fdc3dcc2a",
    "followup_soft": "a858aaed23ae59802e881851d399880a199810ab8edd1c5f4e1a91ea51c673a3",
}
_VARIANT_FILES = {
    "social": "social.txt",
    "email": "email.txt",
    "social_nodish": "social_nodish.txt",
    "email_nodish": "email_nodish.txt",
    "followup": "followup.txt",
    "followup_recap": "followup_recap.txt",
    "followup_value": "followup_value.txt",
    "followup_soft": "followup_soft.txt",
}
# ladder touch -> owner stage file. Ruling B (v1.2 packet, 2026-11-05):
# TWO follow-ups, day 3 and day 7. Day-14/day-30 stages stay pinned below as
# retired copy (historical message_log rows resolve their hashes), but no
# rung arms them: a third touch needs its own recorded owner ruling.
MAX_LADDER_TOUCH = 2
_FOLLOWUP_VARIANT: dict[int, str] = {
    1: "followup",
    2: "followup_recap",
}
_CHANNEL_VARIANT = {"wa": "social", "ig": "social", "email": "email"}
_CHANNEL_NODISH_VARIANT = {
    "wa": "social_nodish",
    "ig": "social_nodish",
    "email": "email_nodish",
}


class TemplateError(RuntimeError):
    """Template machinery misused or drifted (dangling/unknown placeholders)."""


class MessageHold(TemplateError):
    """Lead cannot fill the frozen file (no dish / no greeting name).

    The lead HELDS at ROUTED until the owner blesses the missing variant —
    never invent copy (ADR-6).
    """


class EmailPendingOwner(RuntimeError):
    """ADR-5: email stays disabled until the owner registers creds/switch."""


class TemplateUnconfirmed(RuntimeError):
    """template_status is not 'owner_confirmed' — nothing may ship yet."""


@dataclass(frozen=True)
class RenderedMessage:
    text: str
    template_hash: str
    variant: str


# B29 order-line acknowledgment (owner spec paste 2026-10-31): the ONLY
# legal WhatsApp first touch opens by acknowledging the channel. Owner
# copy with the em dash swapped to a comma (standing no-em-dash law,
# punctuation-only precedent); pinned by test_ack_copy_is_the_owner_spec_line_without_em_dash.
_ORDERLINE_ACK = (
    "Salam! I know this is your order line, not ordering today, promise \U0001f604"
)


def _first_name(name: str | None) -> str:
    if not name:
        return ""
    token = name.strip().split()[0] if name.strip() else ""
    return token


def _load_variant(variant: str) -> str:
    filename = _VARIANT_FILES.get(variant)
    if filename is None:
        raise TemplateError(f"unknown template variant {variant!r}")
    return (_TEMPLATES_DIR / filename).read_text(encoding="utf-8")


def _fill(text: str, fills: dict[str, str]) -> str:
    rendered = text
    for token, value in fills.items():
        rendered = rendered.replace(token, value)
    if "{{" in rendered:
        raise TemplateError(
            "template placeholder drift: unresolved '{{' remains after fills"
        )
    return rendered


def render_message(
    channel: str,
    *,
    name: str | None = None,
    dish: str | None = None,
    variant: str | None = None,
    restaurant: str | None = None,
    platform: str | None = None,
    rating: str | None = None,
    reviews: str | None = None,
    owner: str | None = None,
    menu_items: Any = None,
    order_line: bool = False,
    _raw_override: str | None = None,
) -> RenderedMessage:
    """Render one frozen-copy message for wa/ig/email.

    Dish present → the owner's dish-centric variant. Dish missing → the
    owner-delegated no-dish variant (QUESTIONS.md Q7), which reaches for
    the lead's OWN Google rating + review count as the opener evidence;
    without that data the lead still HOLDS (nothing is invented).
    Raw byte integrity is checked against the pinned hash before any fill.
    """
    if variant is None:
        if channel not in _CHANNEL_VARIANT:
            raise TemplateError(f"unsupported channel {channel!r}")
        dish_missing = dish is None or not str(dish).strip()
        resolved_variant = (
            _CHANNEL_NODISH_VARIANT[channel]
            if dish_missing
            else _CHANNEL_VARIANT[channel]
        )
    else:
        resolved_variant = variant

    raw = _raw_override if _raw_override is not None else _load_variant(resolved_variant)
    template_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if (
        variant in PINNED_SHA256 or resolved_variant in PINNED_SHA256
    ) and template_hash != PINNED_SHA256[resolved_variant] and _raw_override is None:
        raise TemplateError(
            f"template drift: {resolved_variant} hash {template_hash} != pinned"
            " (copy must change only via the owner-paste process)"
        )

    # B29 fills (optional placeholders only — frozen bytes stay frozen):
    # {{owner}} greets the owner's own first name (a template that greets
    # {{owner}} without owner data HOLDS — never "Hi guys", never a restaurant);
    # {{dish_phrase}} carries the dish provenance: menu-listed dishes stay
    # plain, review-sourced dishes carry the review evidence phrasing.
    if "{{owner}}" in raw and not _first_name(owner):
        raise MessageHold(
            "template greets {{owner}} but the lead has no owner_name — "
            "hold (nothing invented, spec 2026-10-31)"
        )
    owner_first = _first_name(owner)
    dish_plain = str(dish).strip() if dish is not None else ""
    on_menu = any(
        dish_plain
        and dish_plain.casefold() in str((item or {}).get("name", "")).casefold()
        for item in (menu_items or ())
    )
    dish_phrase = (
        dish_plain
        if on_menu
        else (
            f"{dish_plain} your reviews keep mentioning"
            if dish_plain
            else ""
        )
    )
    # Owner order 2026-11-12: the copy's verb must fit the EVIDENCE SOURCE -
    # "Saw the koobideh on your menu" for a menu item, "Saw the koobideh post"
    # for a review/post mention. One fill, both renders pinned by test.
    dish_context = " on your menu" if on_menu else " post"
    first_name = _first_name(name)
    full_name = (name or "").strip()
    if resolved_variant.endswith("_nodish"):
        if rating is None or not str(rating).strip() or reviews is None or not str(reviews).strip():
            raise MessageHold(
                "no dish AND no rating/review evidence to open with — "
                "nothing invented (QUESTIONS.md Q7) — lead holds"
            )
        if resolved_variant.startswith("email") and (not first_name or not full_name):
            raise MessageHold(
                "greeting name missing: lead name too blank to fill "
                "{{name}}/{{restaurant}} — lead holds"
            )
        fills = {
            "{{name}}": first_name,
            "{{restaurant}}": full_name,
            "{{platform}}": (platform or DEFAULT_PLATFORM).strip(),
            "{{rating}}": str(rating).strip(),
            "{{reviews}}": str(reviews).strip(),
            "{{owner}}": owner_first,
            "{{dish_phrase}}": dish_phrase,
            "{{dish_context}}": dish_context,
        }
        text = _fill(raw, fills)
        if channel == "wa" and order_line:
            text = f"{_ORDERLINE_ACK} {text}"  # B29: acknowledge the order line
        return RenderedMessage(
            text=text, template_hash=template_hash, variant=resolved_variant
        )
    if dish is None or not str(dish).strip():
        raise MessageHold(
            "no extracted dish: the owner's copy is dish-centric and no "
            "no-dish wording is owner-blessed (QUESTIONS.md Q7) — lead holds"
        )
    if resolved_variant == "email" and (not first_name or not full_name):
        raise MessageHold(
            "greeting name missing: lead name too blank to fill "
            "{{name}}/{{restaurant}} — lead holds"
        )
    fills = {
        "{{name}}": first_name,
        "{{restaurant}}": full_name,
        "{{platform}}": (platform or DEFAULT_PLATFORM).strip(),
        "{{dish}}": str(dish).strip(),
        "{{owner}}": owner_first,
        "{{dish_phrase}}": dish_phrase,
        "{{dish_context}}": dish_context,
    }
    text = _fill(raw, fills)
    if channel == "wa" and order_line:
        text = f"{_ORDERLINE_ACK} {text}"  # B29: acknowledge the order line
    return RenderedMessage(
        text=text, template_hash=template_hash, variant=resolved_variant
    )


def render_for_lead(channel: str, lead_row: Any) -> RenderedMessage:
    """One call site law: channel + lead row ⇒ the right frozen variant.

    Pulls the opener evidence (rating/review_count) from the lead's own
    scraper payload — never fabricated — and renders dish-first, falling
    back to the rating-anchored no-dish variant when there is no dish.
    """
    import json

    try:
        payload = json.loads(lead_row["data_json"] or "{}") or {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    rating = payload.get("rating")
    reviews = payload.get("review_count")
    owner = str(payload.get("owner_name") or "").strip() or None
    order_line = bool(payload.get("order_line"))
    return render_message(
        channel,
        name=lead_row["name"],
        dish=lead_row["extracted_dish"],
        rating=None if rating is None else str(rating),
        reviews=None if reviews is None else str(reviews),
        owner=owner,
        menu_items=payload.get("menu_items") or (),
        order_line=order_line,
    )


def frozen_family(channel: str, lead_row: Any) -> list[str]:
    """Every frozen-family render this lead could have been shipped.

    Direction guards need the whole family, not today's pick. Live 2026-11-15:
    leads 86 and 109 held *"Salam! New Spice Grill's 4.5 stars from 795
    reviews…"* in their threads - the no-dish variant, rendered before dish
    extraction existed for them - while `render_for_lead` now renders the
    dish-first variant. The thread's copy therefore read as a restaurant reply.

    Renders both the dish-first and the rating-anchored variant from the lead's
    own evidence; a variant that cannot fill (no dish, no rating, no owner name)
    contributes nothing instead of a guess.
    """
    import json

    try:
        payload = json.loads(lead_row["data_json"] or "{}") or {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    rating = payload.get("rating")
    reviews = payload.get("review_count")
    renders: list[str] = []
    for variant in {_CHANNEL_VARIANT[channel], _CHANNEL_NODISH_VARIANT[channel]}:
        try:
            renders.append(
                render_message(
                    channel,
                    variant=variant,
                    name=lead_row["name"],
                    dish=lead_row["extracted_dish"],
                    rating=None if rating is None else str(rating),
                    reviews=None if reviews is None else str(reviews),
                    owner=str(payload.get("owner_name") or "").strip() or None,
                    menu_items=payload.get("menu_items") or (),
                ).text
            )
        except (TemplateError, MessageHold, KeyError):
            continue  # a family member the lead cannot fill contributes none
    return renders


def render_followup(lead_row: Any, touch: int) -> RenderedMessage:
    """The owner's follow-up ladder, capped at two touches (Ruling B).

    touch 1 = day-3 offer (no fills), 2 = day-7 recap ({{restaurant}}). The
    v1.0 ladder ran to day 14/30/180; the research-backed law is two
    follow-ups, so touch 3+ is a refusal, not a fourth stage. Every stage
    file is hash-pinned: drift is a test failure, not a silent rewrite. A
    fill stage with a blank lead name HOLDS (MessageHold, the rung stays
    pending): never invent a restaurant. Never a newline: both senders
    forbid an Enter inside type() (WhatsApp/IG composer = send).
    """
    if int(touch) > MAX_LADDER_TOUCH:
        raise TemplateError(
            f"the ladder is capped at two follow-ups (day 3, day 7); touch"
            f" {touch} is refused (Ruling B 2026-11-05)"
        )
    variant = _FOLLOWUP_VARIANT.get(int(touch), "followup_soft")
    raw = _load_variant(variant)
    template_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if template_hash != PINNED_SHA256[variant]:
        raise TemplateError(
            f"template drift: {variant} hash {template_hash} != pinned"
            " (copy must change only via the owner-paste process)"
        )
    fills: dict[str, str] = {}
    if "{{restaurant}}" in raw:
        name = ""
        if lead_row is not None:
            try:
                name = (lead_row["name"] or "").strip()
            except (IndexError, KeyError, TypeError):
                name = ""
        if not name:
            raise MessageHold(
                f"follow-up stage {variant} needs {{restaurant}} but the lead"
                " has no name — rung holds (nothing invented)"
            )
        fills["{{restaurant}}"] = name
    return RenderedMessage(
        text=_fill(raw, fills), template_hash=template_hash, variant=variant
    )


def ensure_template_status(db: "Database") -> str:
    """Seed `template_status` as PROVISIONAL until the owner confirms copy."""
    from outreach.store import get_config, set_config

    status = get_config(db, "template_status")
    if status is None:
        set_config(db, "template_status", "PROVISIONAL")
        return "PROVISIONAL"
    return str(status)


def assert_sendable_template(db: "Database") -> str:
    """Dispatcher contract: refuse to build any sendable message before the
    owner confirms the frozen copy. Returns the status when clear."""
    from outreach.store import get_config

    status = get_config(db, "template_status")
    if status != "owner_confirmed":
        raise TemplateUnconfirmed(
            f"template_status={status!r}: the frozen copy is not owner-confirmed"
            " — no message may ship (QUESTIONS.md Q5)"
        )
    return status
