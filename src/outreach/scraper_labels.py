"""Typed read of the scraper's fixed data labels (prep, no live effect).

Evidence: ``A:/scraper/DATA_QUALITY_HANDOFF.md`` §5 (acceptance) + §6.1
(2026-11-04 fix table). The scraper's contamination work changed what its
``data_json`` means in four places, and our adapter must read those labels
literally instead of re-deriving the same facts with different rules:

* ``social_activity[platform].method`` — an estimated post date is labelled
  ``estimate_google_search`` / ``estimate_insta_stalker``. A guess is not an
  observation, so a labelled estimate reads as *unknown* (never "inactive").
* ``needs_chain_review`` / ``classification="needs_review"`` — an AI-only
  chain verdict no longer persists as ``is_chain``; the tier stands and the
  lead stays in the pool, tagged. The flag is a review marker, not an exit.
* ``coordinates_unavailable`` — derived on read when lat/lon are absent. A
  stated absence, so nothing downstream may invent a location.
* ``owner_name`` / ``email`` — now evidence-backed upstream (literal name near
  owner/founder wording on the business's own page; own-domain or
  name-matched mailbox). Email evidence is judged by ONE rule: this module
  asks ``router.email_lane_ok`` rather than keeping a second opinion.

Deliberately additive (packet law): nothing in the pipeline consumes
``ScraperSignals`` yet. Re-ingestion is GO only on the scraper's
``BULK_PASS_REPORT`` plus §5 acceptance.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from outreach.router import email_lane_ok

# Methods that mean "we guessed this date": the value is preserved for
# telemetry but never trusted as an observation.
ESTIMATE_METHODS = frozenset({"estimate_google_search", "estimate_insta_stalker"})
# `fallback` is the scraper's default for "no method recorded" — equally a gap.
UNTRUSTED_METHODS = ESTIMATE_METHODS | {"fallback"}
REVIEW_CLASSIFICATION = "needs_review"
# Placeholders the scraper blanks (41 live leads once carried "Unknown Name");
# the adapter must not resurrect them if an older row still holds one.
OWNER_PLACEHOLDERS = frozenset(
    {
        "unknown name",
        "unknown",
        "unknown owner",
        "n/a",
        "na",
        "none",
        "null",
        "owner",
        "-",
    }
)


@dataclass(frozen=True)
class ScraperSignals:
    """The four labels, read once and typed. Read-only view of a payload."""

    tier: int | None = None
    ig_date_method: str | None = None
    ig_date_trusted: bool = False
    chain_review_pending: bool = False
    review_classification: str | None = None
    coordinates_unavailable: bool = True
    owner_name: str | None = None
    email: str | None = None
    email_own_domain: bool = False


def _activity(payload: dict[str, Any], platform: str) -> dict[str, Any]:
    activities = payload.get("social_activity") or {}
    if not isinstance(activities, dict):
        return {}
    activity = activities.get(platform) or {}
    return activity if isinstance(activity, dict) else {}


def post_date_method(payload: dict[str, Any], platform: str) -> str | None:
    """The label the scraper attached to this platform's post date."""
    method = _activity(payload, platform).get("method")
    return method if isinstance(method, str) and method.strip() else None


def is_trusted_method(method: str | None) -> bool:
    """Only a recorded observation is trusted; estimates and gaps are not."""
    return method is not None and method not in UNTRUSTED_METHODS


def trusted_post_date(
    payload: dict[str, Any], platform: str = "instagram"
) -> str | None:
    """The post date IFF the scraper observed it; None means unknown."""
    if not is_trusted_method(post_date_method(payload, platform)):
        return None
    value = _activity(payload, platform).get("last_post_date")
    return value if isinstance(value, str) and value.strip() else None


def chain_review_pending(payload: dict[str, Any]) -> bool:
    """AI-only chain verdict awaiting a human look (tier stands, lead stays)."""
    if payload.get("needs_chain_review") is True:
        return True
    return payload.get("classification") == REVIEW_CLASSIFICATION


def coordinates_unavailable(payload: dict[str, Any]) -> bool:
    """Absent coordinates: the label when stamped, else the values themselves."""
    if payload.get("coordinates_unavailable") is True:
        return True
    if payload.get("coordinates_unavailable") is False:
        return False
    return payload.get("latitude") is None and payload.get("longitude") is None


def owner_name(payload: dict[str, Any]) -> str | None:
    """Evidence-backed owner name, or None. Never a placeholder or a list."""
    value = payload.get("owner_name")
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or text.casefold() in OWNER_PLACEHOLDERS:
        return None
    return text


def email_own_domain(payload: dict[str, Any]) -> bool:
    """Email evidence, asked of the one rule that gates sending (ADR-5)."""
    email = payload.get("email")
    return email_lane_ok(
        email if isinstance(email, str) else None,
        payload.get("website") if isinstance(payload.get("website"), str) else None,
    )


def read_signals(payload: dict[str, Any] | None) -> ScraperSignals:
    """Read the four labels off one scraper payload. Pure, never mutating."""
    data: dict[str, Any] = payload if isinstance(payload, dict) else {}
    tier = data.get("tier")
    classification = data.get("classification")
    method = post_date_method(data, "instagram")
    return ScraperSignals(
        tier=tier if isinstance(tier, int) and not isinstance(tier, bool) else None,
        ig_date_method=method,
        ig_date_trusted=is_trusted_method(method)
        and trusted_post_date(data, "instagram") is not None,
        chain_review_pending=chain_review_pending(data),
        review_classification=(
            classification if isinstance(classification, str) else None
        ),
        coordinates_unavailable=coordinates_unavailable(data),
        owner_name=owner_name(data),
        email=data.get("email") if isinstance(data.get("email"), str) else None,
        email_own_domain=email_own_domain(data),
    )
