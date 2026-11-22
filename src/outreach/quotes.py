"""Deterministic review-quote quality gate (owner ruling 2026-11-11).

A lead's V2 opener quotes a guest review verbatim, so the QUOTE has to earn its
place: specific enough that the restaurant recognises itself. The owner's law:

  PASS: names a dish, a moment, or a story
        ("the kind you drive 40 minutes for", "ruined every other manoushe
        for me", "we ordered the Beef Nihari, Paya, Haleem…").
  REFUSE: rating words, service words, generic praise ("great food, friendly
        staff", "amazing", "delicious"), any star/rating mention, and
        "best in city" with no story behind it.

Every refusal is a NAMED class so a hold says exactly which law the quote
broke, and the gate is deterministic (no model): the same quote always gets the
same verdict, on every machine, with Ollama down.

This module decides nothing about the message: `variants.route_variant` uses it
to skip V2 when no quote passes, and the lead routes on to the next tier
(V7/V4 moment variants, then V3) instead of falling to generic V5.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Same dish truth the extractor uses: a quote "names a dish" only when it
#: uses a word the department already accepts as one. Imported rather than
#: duplicated so a lexicon fix cannot leave two answers to one question.
from outreach.dish_mining import _DISH_LEXICON

#: Longest quote V2 may carry. A quote is never truncated to fit: a cut quote
#: is a changed quote, so an over-long one is refused (`too_long`).
MAX_QUOTE_CHARS = 200
#: Below this a quote carries no detail worth reacting to.
MIN_QUOTE_WORDS = 5

#: Stars, scores, "5/5", "10/10", "rating".
_RATING = re.compile(
    r"(?:\b\d(?:\.\d)?\s*/\s*(?:5|10)\b|\b[1-5]\s*stars?\b|\bfive\s+stars?\b"
    r"|\bstar\s+rating\b|\bratings?\b|\b\d+%\b)",
    re.IGNORECASE,
)
#: Rating adjectives: punchy but contentless on their own.
_RATING_WORDS = frozenset(
    {
        "amazing", "delicious", "incredible", "fantastic", "excellent",
        "superb", "wonderful", "tasty", "yummy", "perfect", "outstanding",
        "awesome", "great", "good", "nice", "lovely", "best",
    }
)
#: Service / venue words: they describe the visit, never the plate.
_SERVICE_WORDS = frozenset(
    {
        "service", "staff", "server", "waiter", "waitress", "owner",
        "friendly", "polite", "hospitable", "professional", "attentive",
        "quick", "fast", "clean", "atmosphere", "ambiance", "ambience",
        "prices", "price", "cheap", "expensive", "value", "decor",
        "recommend", "recommended",
    }
)
#: A story or a moment: repetition, distance, time, family, history. These are
#: what make a quote "the kind you drive 40 minutes for".
_MOMENT = re.compile(
    r"(?:\b\d+\s*(?:minutes?|hours?|mins?|hrs?|years?|decades?|blocks?)\b"
    r"|\bdrive\b|\bdrove\b|\bdriving\b|\bqueue\b|\blineup\b|\bline\s+up\b"
    r"|\bevery\s+(?:time|week|sunday|friday|visit|meal)\b|\ball\s+the\s+time\b"
    r"|\btwice\b|\bthree\s+times\b|\bagain\s+and\s+again\b|\bsince\b"
    r"|\bgrew\s+up\b|\bback\s+home\b|\breminds?\s+me\b|\bruined\b"
    r"|\bfirst\s+time\b|\bused\s+to\b|\bmy\s+(?:wife|husband|kids|mother|father|family)\b"
    r"|\bfamily[\s-]owned\b|\bchildhood\b|\bfor\s+\d+\s+years\b)",
    re.IGNORECASE,
)
#: Superlative claims need a story or a dish behind them.
_SUPERLATIVE = re.compile(
    r"\b(?:best|worst|number\s+one|#1|better\s+than\s+any|one\s+of\s+the\s+best)\b",
    re.IGNORECASE,
)
#: Generic food nouns: "the food was good" is praise, not a dish.
_FOOD_NOUNS = frozenset(
    {"food", "meal", "meals", "dish", "dishes", "menu", "everything",
     "anything", "portions", "flavors", "flavours", "selection", "options"}
)
_TOKENS = re.compile(r"[a-z][a-z'\-]+")
#: Sentence boundary for splitting a review into quotable beats.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class QuoteVerdict:
    """`ok=True` means the quote may ship in V2. `klass` names the refusal."""

    ok: bool
    klass: str
    reason: str


@dataclass(frozen=True)
class QuotePick:
    """The chosen quote plus every refusal, so a hold can explain itself."""

    quote: str | None
    verdicts: tuple[tuple[str, QuoteVerdict], ...] = ()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _content_words(text: str) -> list[str]:
    return _TOKENS.findall(text.casefold())


def _names_a_dish(words: list[str]) -> bool:
    """Bigrams first ("beef nihari"), then single lexicon words."""
    for index in range(len(words) - 1):
        if f"{words[index]} {words[index + 1]}" in _DISH_LEXICON:
            return True
    return any(word in _DISH_LEXICON for word in words)


def judge_quote(text: str) -> QuoteVerdict:
    """One deterministic verdict per quote; classes are the owner's law list."""
    quote = _clean(text)
    words = _content_words(quote)
    if not quote:
        return QuoteVerdict(False, "empty", "empty quote")
    if "..." in quote or "…" in quote:
        # Live 2026-11-16: all 18 COPY_HOLD leads were held by ONE cause - the
        # scraper truncates long reviews with an ellipsis (review_samples shows
        # "...which simply wasn't …"), we quoted that sentence verbatim, and
        # the message lint bans ellipsis. A truncated source is not a quotable
        # sentence: refuse it here, by name, instead of letting it poison the
        # rendered message downstream.
        return QuoteVerdict(
            False, "truncated_source",
            "the review is cut off with an ellipsis - a truncation is not a quote",
        )
    if len(quote) > MAX_QUOTE_CHARS:
        return QuoteVerdict(
            False, "too_long",
            f"{len(quote)} chars: a quote is never truncated to fit",
        )
    if _RATING.search(quote):
        return QuoteVerdict(
            False, "rating_mention",
            "star/rating mention (owner ban: rating is not a story)",
        )
    dish = _names_a_dish(words)
    moment = bool(_MOMENT.search(quote))
    content = dish or moment
    services = [w for w in words if w in _SERVICE_WORDS]
    ratings = [w for w in words if w in _RATING_WORDS]
    food_nouns = [w for w in words if w in _FOOD_NOUNS]
    if ratings and not content and (food_nouns or not services):
        # A pile of adjectives with nothing behind them: the owner's
        # "great food, friendly staff", "amazing", "delicious" class.
        return QuoteVerdict(
            False, "generic_praise",
            f"generic praise ({', '.join(sorted(set(ratings))[:3])}) with no dish or story",
        )
    if services and not content:
        return QuoteVerdict(
            False, "service_word",
            f"service/venue words only ({', '.join(sorted(set(services))[:3])})",
        )
    if _SUPERLATIVE.search(quote) and not moment:
        # Owner law: "best in city" with no story behind it is refused, even
        # when it names the dish - the claim is the whole quote's content and
        # a claim is not a story. A rank without a moment stays a rating.
        return QuoteVerdict(
            False, "superlative_without_story",
            "superlative claim with no story behind it",
        )
    if (
        len(words) < MIN_QUOTE_WORDS
        and not content
        and not services
        and not ratings
    ):
        return QuoteVerdict(
            False, "too_short",
            f"{len(words)} words: no detail to react to",
        )
    if not content:
        return QuoteVerdict(
            False, "no_specific_detail",
            "no dish, moment or story: nothing specific to quote",
        )
    return QuoteVerdict(
        True, "specific",
        "names a dish" if dish and not moment else (
            "tells a moment" if moment and not dish else "names a dish and a moment"
        ),
    )


def _candidates(payload: dict[str, Any]) -> list[str]:
    """`review_quote` first (an explicit choice), then `review_samples`."""
    out: list[str] = []
    primary = payload.get("review_quote")
    if isinstance(primary, str) and primary.strip():
        out.append(_clean(primary))
    samples = payload.get("review_samples")
    if isinstance(samples, list):
        for sample in samples:
            if isinstance(sample, str) and sample.strip():
                text = _clean(sample)
                if text not in out:
                    out.append(text)
    return out


def _beats(text: str) -> list[str]:
    """A review split into its sentences — each a possible quote on its own."""
    return [beat for beat in _SENTENCE_END.split(_clean(text)) if beat.strip()]


def _best_beat(text: str) -> tuple[str | None, list[tuple[str, QuoteVerdict]]]:
    """The longest single sentence that passes, with every refusal recorded."""
    verdicts: list[tuple[str, QuoteVerdict]] = []
    best: str | None = None
    for beat in _beats(text):
        verdict = judge_quote(beat)
        verdicts.append((beat, verdict))
        if verdict.ok and (best is None or len(_content_words(beat)) > len(_content_words(best))):
            best = beat
    return best, verdicts


def select_quote(payload: dict[str, Any]) -> QuotePick:
    """First passing quote in payload order, with every refusal recorded.

    LIVE 2026-11-15 (holds 893/994/1921/153): whole reviews were being pasted
    as the quote, so a four-sentence guest review turned a first touch into a
    9-10 sentence paragraph and the wall refused it as `copy_lint`. A quote is
    now ONE sentence — the strongest passing sentence of the review — because
    choosing a sentence is selection, while cutting one is rewriting it.
    """
    verdicts: list[tuple[str, QuoteVerdict]] = []
    for candidate in _candidates(payload):
        beats = _beats(candidate)
        if len(beats) <= 1:
            verdict = judge_quote(candidate)
            verdicts.append((candidate, verdict))
            if verdict.ok:
                return QuotePick(candidate, tuple(verdicts))
            continue
        best, beat_verdicts = _best_beat(candidate)
        verdicts.extend(beat_verdicts)
        if best is not None:
            return QuotePick(best, tuple(verdicts))
    return QuotePick(None, tuple(verdicts))


__all__ = [
    "MAX_QUOTE_CHARS",
    "MIN_QUOTE_WORDS",
    "QuotePick",
    "QuoteVerdict",
    "judge_quote",
    "select_quote",
]
