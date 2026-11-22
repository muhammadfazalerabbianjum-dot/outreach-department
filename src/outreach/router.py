"""Routing engine (B8, B29 law): IG first, email fallback, WA exception.

B29 (owner research 2026-10-31): cold WhatsApp first-touches are dead —
scraped public numbers are ORDER LINES staffed by cashiers, not owners,
so sales DMs there get ignored or marked as spam. The lane order is now:

  1. active Instagram (last post <= 30 days) -> IG DM, every market;
  2. no/ inactive IG -> email (both markets; Gulf's primary door);
  3. no IG, no email -> WhatsApp ONLY via the order-line exception:
     the Canada lane (`na`) AND the listing itself promotes WhatsApp
     orders (`order_line` in the payload) — the opener must acknowledge
     the channel (owner spec copy, `_ORDERLINE_ACK` in templates);
  4. anything else -> park: "no viable channel — park".

Automation is unchanged (owner decision 2026-10-31: "same flow we have
right now"): windows, caps, ladder and probes all stand; only the lane
law flipped. A 30-day post date derives ig activity automatically
(`effective_ig_inactive`); the operator's `ig_inactive=1` always wins,
and a missing date keeps pre-B29 behaviour until the scraper exports it    (A:\\scraper enrichment gap, DECISIONS 2026-10-31).

`apply_routes` moves every QUALIFIED, never-contacted lead through the
machine: ROUTED (channel stored on the lead) or UNREACHABLE (+history).

`reroute_stale_routes` is the cutover repair the engine runs on boot:
leads already ROUTED by the pre-B29 phone-wins law carry a stale
`route_channel`, and dispatch trusts that column, so the law re-routes
them here (or parks them UNREACHABLE) before the first send can fire.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from outreach.machine import transition

if TYPE_CHECKING:
    from outreach.store import Database

_ACTOR = "router"
_IG_ACTIVE_DAYS = 30  # owner spec: last post older than ~30d = inactive
_WA_EXCEPTION_REGIONS = ("na", "canada")  # the exception is the Canada lane's

# D4 (packet B, 2026-11-01): scraped platform guest inboxes are not a
# contactable channel — B29 made email a primary lane, so a restaurantguru
# or linkedin address would burn a first touch into a platform inbox that
# reaches nobody at the restaurant. A junk address is treated exactly like
# NO email by the lane law (falls back to the WA order-line exception or
# parks), and the claim seam refuses to claim it as defense in depth.
# Live audit 2026-11-01: 0 junk addresses in the ROUTED pool — hygiene +
# future-proofing (old rows: 3 restaurantguru.com, 3 linkedin.com).
PLATFORM_EMAIL_DOMAINS = frozenset({
    "restaurantguru.com",
    "linkedin.com",
    "tripadvisor.com",
    "opentable.com",
    "yelp.com",
    "doordash.com",
    "ubereats.com",
    "facebook.com",
    "instagram.com",
    "google.com",
    "grubhub.com",
    "skipthedishes.com",
})


# Contamination hold (2026-11-03): the enrichment refresh filled emails that
# mostly do not belong to the business — 113 free mailboxes, 190 addresses on
# a domain unrelated to the lead's own website (an agency, a vendor, another
# company entirely), and URL-encoded garbage. Cold-emailing those is
# deliverability and brand damage, so the lane is QUARANTINED: an address is
# only a channel when it sits on the lead's own website domain, or the lead
# carries an explicit verification flag. Everything else parks with a reason.
FREE_MAILBOX_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "aol.com",
    "protonmail.com", "proton.me", "mail.com", "gmx.com", "yandex.com",
    "zoho.com", "fastmail.com",
})

# Social links that are structurally NOT a profile: tracking pixels, template
# footers, post/reel permalinks. `@wix` is the classic footer artefact.
_JUNK_SOCIAL_MARKERS = (
    "facebook.com/tr", "/2008", "/embed", "@wix", "linktr.ee", "bit.ly",
    "utm_source", "powered by",
)
_JUNK_PROFILE_PATH = re.compile(
    r"instagram\.com/(p|reel|reels|tv|stories|explore)/", re.IGNORECASE
)
_URL_HOST = re.compile(r"https?://([^/]+)", re.IGNORECASE)


def _bare_domain(value: str | None) -> str:
    """'https://www.Urbano.om/menu' -> 'urbano.om'; a bare domain passes."""
    text = unquote(str(value or "")).strip().lower()
    if not text:
        return ""
    match = _URL_HOST.search(text)
    host = match.group(1) if match else text.split("/")[0]
    return host.split(":")[0].lstrip("@").replace("www.", "")


def email_lane_ok(
    email: str | None,
    website: str | None,
    verified: Any = None,
) -> bool:
    """Is this address evidence-backed enough to email? (fail-closed)

    `verified` truthy (the scraper's explicit confirmation) wins; otherwise
    the address domain must belong to the lead's own website domain. Platform
    inboxes are junk even when "verified" — they are never the business.
    """
    text = unquote(str(email or "")).strip().lower()
    if "@" not in text:
        return False
    domain = text.rsplit("@", 1)[1].strip()
    if not domain or is_platform_email(text):
        return False
    if isinstance(verified, str):
        verified = verified.strip().lower() in ("1", "true", "yes", "verified")
    if verified:
        return True
    web = _bare_domain(website)
    if not web or domain in FREE_MAILBOX_DOMAINS:
        return False  # no evidence, or an unverifiable free mailbox
    return domain == web or domain.endswith("." + web) or web.endswith("." + domain)


def sanitize_instagram(value: Any) -> str:
    """Return a usable IG profile handle/link, or '' when the value is junk.

    Contamination hold 2026-11-03: the refresh put tracking pixels
    (`facebook.com/tr?id=...`), template footers (`@wix`) and post permalinks
    (`instagram.com/p/...`) into social fields. None of those is a profile, so
    none is a lane — the lead falls through the lane law instead.
    """
    original = str(value or "").strip()
    if not original:
        return ""
    text = unquote(original).strip()
    low = text.lower()
    if any(marker in low for marker in _JUNK_SOCIAL_MARKERS):
        return ""
    if _JUNK_PROFILE_PATH.search(low):
        return ""
    if low.startswith("http") and "instagram.com" not in low:
        return ""  # a link to anywhere else is not an IG handle
    return original


def is_platform_email(email: str | None) -> bool:
    """True when the address belongs to a platform, not the restaurant.

    Domain match is case-insensitive and includes subdomains
    (mail.restaurantguru.com); a local part that merely contains the
    domain string is not junk (someone@restaurantguru.com.evil.ca).
    """
    text = str(email or "").strip().lower()
    if "@" not in text:
        return False
    domain = text.rsplit("@", 1)[1]
    return any(
        domain == blocked or domain.endswith("." + blocked)
        for blocked in PLATFORM_EMAIL_DOMAINS
    )


@dataclass(frozen=True)
class RouteDecision:
    channel: str | None
    greeting_name: str | None
    reason: str


@dataclass(frozen=True)
class RoutingReport:
    routed: int
    unreachable: int
    by_channel: dict


@dataclass(frozen=True)
class RerouteReport:
    rerouted: int  # stale route_channel corrected (state stays ROUTED)
    parked: int  # no viable channel: ROUTED -> UNREACHABLE


def _last_posted(payload: dict[str, Any]) -> str | None:
    """The scraped last-post date, tolerating both payload shapes:
    top-level `ig_last_posted_date` (spec field) or the scraper's
    `social_activity.instagram.last_post_date` (its native activity
    object)."""
    raw = payload.get("ig_last_posted_date")
    if raw:
        return str(raw)
    activity = payload.get("social_activity") or {}
    ig = activity.get("instagram") if isinstance(activity, dict) else None
    if isinstance(ig, dict) and ig.get("last_post_date"):
        return str(ig["last_post_date"])
    return None


def effective_ig_inactive(
    payload: dict[str, Any], operator_flag: Any, now: datetime
) -> Any:
    """B29: derive IG inactivity from the 30-day rule.

    Precedence: operator `ig_inactive=1` wins (ADR-53 authority, tested);
    otherwise a scraped post date older than 30 days means inactive, a
    fresh one means active; a MISSING date keeps today's behaviour
    (fail-open to the handle) until the scraper exports dates.
    """
    if operator_flag is not None:
        try:
            if int(operator_flag) == 1:
                return 1
        except (TypeError, ValueError):
            pass
    raw = _last_posted(payload)
    if raw:
        try:
            stamp = datetime.fromisoformat(raw)
        except ValueError:
            stamp = None
        if stamp is not None:
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age_days = (
                now.astimezone(timezone.utc) - stamp.astimezone(timezone.utc)
            ).total_seconds() / 86400.0
            return 1 if age_days > _IG_ACTIVE_DAYS else 0
    return operator_flag


def route_lead(
    *,
    phone: str | None,
    instagram: str | None,
    email: str | None,
    ig_inactive: int | str | None,
    name: str | None = None,
    ig_class: str | None = None,
    order_line: bool | None = None,
    region: str | None = None,
    website: str | None = None,
    email_verified: Any = None,
) -> RouteDecision:
    """Pick the single lane a lead can be reached on (pure, no DB I/O).

    B29 law (owner research 2026-10-31): an ACTIVE instagram wins in
    every market, email is the fallback, and WhatsApp is reachable only
    through the order-line exception (Canada lane + `order_line` evidence).
    A DEAD instagram link (B20 verdict) is no link at all; SUSPECT keeps
    the lane for exactly one attempt.
    """
    greeting_name = name.strip().split()[0] if name and name.strip() else None
    handle = sanitize_instagram(instagram)  # junk link = no handle (hold law)
    if ig_class == "DEAD":
        handle = ""  # structurally unusable: never route IG on a dead link
    inactive = ig_inactive is not None and str(ig_inactive) == "1"
    if handle and not inactive:
        return RouteDecision("ig", greeting_name, "active instagram first touch")
    if email and str(email).strip() and email_lane_ok(email, website, email_verified):
        return RouteDecision("email", greeting_name, "no active instagram; email present")
    if (
        phone
        and str(phone).strip()
        and order_line
        and (region or "").lower() in _WA_EXCEPTION_REGIONS
    ):
        return RouteDecision(
            "wa",
            greeting_name,
            "order-line exception: no instagram, no email; listing promotes"
            " WhatsApp orders",
        )
    if phone and str(phone).strip() and not order_line:
        return RouteDecision(
            None,
            greeting_name,
            "no viable channel: no active instagram, no email;"
            " whatsapp order line not established",
        )
    if order_line and (region or "").lower() not in _WA_EXCEPTION_REGIONS:
        return RouteDecision(
            None,
            greeting_name,
            "no viable channel: no active instagram, no email;"
            " order-line exception is canada-only",
        )
    return RouteDecision(
        None, greeting_name, "no viable channel: no active instagram, no email"
    )


def route_from_row(row: Any, now: datetime) -> RouteDecision:
    """The lane law over one `leads` row — the SSOT every routing site
    shares (apply_routes, the engine-start reroute, `outreach plan`):
    parse the payload once, derive IG activity from the 30-day rule,
    ask route_lead."""
    payload: dict[str, Any] = {}
    raw_json = row["data_json"]
    if raw_json:
        try:
            payload = json.loads(raw_json) or {}
        except json.JSONDecodeError:  # corrupted payload treated as unknown
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return route_lead(
        phone=row["phone"],
        instagram=row["instagram"],
        email=row["email"],
        ig_inactive=effective_ig_inactive(payload, payload.get("ig_inactive"), now),
        name=row["name"],
        ig_class=row["ig_class"],
        order_line=bool(payload.get("order_line")),
        region=row["region"],
        website=payload.get("website"),
        email_verified=payload.get("email_verified"),
    )


def apply_routes(
    db: "Database", *, run_id: str | None = None, now: datetime | None = None
) -> RoutingReport:
    """Route every QUALIFIED, never-contacted lead (id order), once."""
    now = now or datetime.now(timezone.utc)
    rows = db.conn.execute(
        "SELECT id, name, phone, instagram, email, data_json, ig_class, region"
        " FROM leads WHERE state='QUALIFIED' AND previously_contacted=0"
        + (" AND run_id=?" if run_id is not None else "")
        + " ORDER BY id",
        (run_id,) if run_id is not None else (),
    ).fetchall()
    routed = unreachable = 0
    by_channel: dict[str, int] = {}
    for row in rows:
        decision = route_from_row(row, now)
        if decision.channel is None:
            transition(
                db,
                int(row["id"]),
                "UNREACHABLE",
                reason=f"no usable channel: {decision.reason}",
                actor=_ACTOR,
                park_kind="lane_law",
            )
            unreachable += 1
            continue
        with db.conn:
            db.conn.execute(
                "UPDATE leads SET route_channel=? WHERE id=?",
                (decision.channel, int(row["id"])),
            )
        transition(
            db,
            int(row["id"]),
            "ROUTED",
            reason=f"routed to {decision.channel}: {decision.reason}",
            actor=_ACTOR,
        )
        routed += 1
        by_channel[decision.channel] = by_channel.get(decision.channel, 0) + 1
    return RoutingReport(
        routed=routed, unreachable=unreachable, by_channel=by_channel
    )


def reroute_stale_routes(
    db: "Database", *, now: datetime | None = None
) -> "RerouteReport":
    """B29 cutover repair: re-evaluate every ROUTED, never-contacted lead
    under the current lane law (owner spec 2026-10-31).

    Live incident 2026-10-31: the 824 leads routed by the pre-B29
    phone-wins law all carry `route_channel='wa'`, and dispatch trusts
    that column — without this pass the engine would keep firing the
    cold WhatsApp first-touches the owner outlawed. A lead with no
    viable channel moves ROUTED -> UNREACHABLE exactly like
    apply_routes; a changed channel just updates the column (state stays
    ROUTED; state_history records state edges only). Idempotent: a
    second run reports zeros.
    """
    now = now or datetime.now(timezone.utc)
    rows = db.conn.execute(
        "SELECT id, name, phone, instagram, email, data_json, ig_class,"
        " region, route_channel FROM leads"
        " WHERE state='ROUTED' AND previously_contacted=0 ORDER BY id"
    ).fetchall()
    rerouted = parked = 0
    for row in rows:
        decision = route_from_row(row, now)
        if decision.channel is None:
            transition(
                db,
                int(row["id"]),
                "UNREACHABLE",
                reason=f"no usable channel under the B29 lane law: {decision.reason}",
                actor=_ACTOR,
                park_kind="lane_law",
            )
            parked += 1
        elif decision.channel != row["route_channel"]:
            with db.conn:
                db.conn.execute(
                    "UPDATE leads SET route_channel=? WHERE id=?",
                    (decision.channel, int(row["id"])),
                )
            rerouted += 1
    return RerouteReport(rerouted=rerouted, parked=parked)

@dataclass(frozen=True)
class StrandedReport:
    recovered: int
    still_stranded: int


def reroute_stranded(
    db: "Database", *, now: datetime | None = None
) -> StrandedReport:
    """Revive UNREACHABLE leads the lane law can now reach (2026-11-16).

    Live audit: 867 rows sat in UNREACHABLE while 842 of them carry a phone
    and 286 carry a handle `sanitize_instagram` accepts — so the state was a
    ONE-WAY DOOR. `reroute_stale_routes` only re-reads ROUTED, and cleaning
    (link repair) or a later import can give a parked row a handle after the
    verdict was written. Re-deciding the row under the same pure lane law is
    the only repair that may leave this state: nothing here invents a channel,
    and a phone-only row still resolves to None (cold WhatsApp is outlawed),
    so this pass cannot manufacture a WA first touch.

    Idempotent: a second run recovers nothing.
    """
    now = now or datetime.now(timezone.utc)
    rows = db.conn.execute(
        "SELECT id, name, phone, instagram, email, data_json, ig_class, region,"
        " route_channel FROM leads"
        " WHERE state='UNREACHABLE' AND previously_contacted=0"
        "   AND park_kind='lane_law'"
        " ORDER BY id"
    ).fetchall()
    recovered = 0
    for row in rows:
        # The WHERE clause is the whole filter: only a row the LANE LAW parked
        # (`park_kind='lane_law'`) is re-decided here. A detector verdict is a
        # fact about the world, not a missing channel, and reviving one would
        # re-attempt the same refusal on every engine boot (leads 346 and 2955
        # were revived exactly that way before this column existed).
        decision = route_from_row(row, now)
        if decision.channel is None:
            continue
        # One transaction: a lane without the state edge (or a crash between
        # the two writes) would leave a parked lead wearing a channel it does
        # not have, and the countdown would count it as sendable.
        with db.conn:
            db.conn.execute(
                "UPDATE leads SET route_channel=? WHERE id=?",
                (decision.channel, int(row["id"])),
            )
            transition(
                db,
                int(row["id"]),
                "ROUTED",
                reason=(
                    "stranded repair 2026-11-16: the lane law now reaches this"
                    f" lead -> {decision.channel} ({decision.reason})"
                ),
                actor=_ACTOR,
                commit=False,
            )
        recovered += 1
    return StrandedReport(recovered=recovered, still_stranded=len(rows) - recovered)
