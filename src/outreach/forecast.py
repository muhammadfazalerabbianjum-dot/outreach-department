"""Forward contract: what the next three days will actually claim.

Owner order 2026-11-11. The countdown says how much supply is left; this file
says WHICH leads that supply becomes, in the order the dispatcher will claim
them. It exists because two departments have to agree on that order before it
happens, and because a lead forecast as "dish" that arrives as "moment" is a
counting bug, not luck.

Fixed paths, schema v1, same contract rules as the countdown pair:

    <repo>/DISPATCH_FORECAST.md
    <repo>/DISPATCH_FORECAST.json

Both files come from this module's ONE payload, written on the same tick as
the countdown and fed with the countdown's measured rate (`countdown.write`
passes its payload in), so the two documents can never disagree about capacity.

Honesty rules inherited from the countdown, plus one of its own:

  * the claim order is `dispatcher.claim_priority` itself, not a copy of it:
    dish first, then strong evidence, then lead id, exactly as `claim_next`
    sorts;
  * a lead parked by an open wall hold or a junk identity is NOT forecast -
    it is not claimable today, and pretending otherwise would inflate supply;
  * the tier is measured (dish / review-quote / moment), and a quote only
    counts as review-quote when it PASSES the owner's specificity gate;
  * an enrichment marker the lead data does not carry is an omitted key, never
    a guessed timestamp.

`record_divergence` closes the loop at claim time: a lead whose tier at claim
is lower than its forecast tier lands in the digest named "forecast lied",
because that is the counting-bug class this file exists to catch.

A forecast is an expectation, not a promise: it does not model the session
windows, the rolling 4/hour slot, template gates or account ownership. The
claims are ordered exactly as the dispatcher orders them, and the DAY buckets
assume the effective daily rate of that day - nothing more. When two lanes are
live at once the forecast follows the FIRST live lane only (`channel` says
which); per-lane forecasts are a later contract revision, not a guess here.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from outreach import countdown
from outreach.caps import pkt_date
from outreach.store import get_config, set_config

if TYPE_CHECKING:
    from outreach.store import Database

SCHEMA_VERSION = 1
ROOT = countdown.ROOT
JSON_PATH = ROOT / "DISPATCH_FORECAST.json"
MD_PATH = ROOT / "DISPATCH_FORECAST.md"
DAYS = 3

TIER_DISH = "dish"
TIER_QUOTE = "review-quote"
TIER_MOMENT = "moment"
TIER_RANK = {TIER_MOMENT: 1, TIER_QUOTE: 2, TIER_DISH: 3}
CLAIM_ORDER = (
    "dispatcher.claim_priority (dish first, then strong evidence, then lead id)"
)
#: Config key holding today's forecast tier per lead, used only by the
#: claim-time divergence check. Values are the tier strings above.
TODAY_KEY = "dispatch_forecast_today"
#: Keys an enriched lead payload may carry. Absent => the claim omits the key.
_MARKER_KEYS = ("synced_at", "enriched_at", "last_sync_at", "scraper_synced_at")
DIVERGENCE_PREFIX = "FORECAST DIVERGENCE"


def _payload_of(data_json: Any) -> dict[str, Any]:
    try:
        payload = json.loads(data_json or "{}") or {}
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def has_reviews(payload: dict[str, Any]) -> bool:
    """Review evidence: a sample we could quote, or a counted rating."""
    samples = payload.get("review_samples")
    if isinstance(samples, list) and any(
        isinstance(sample, str) and sample.strip() for sample in samples
    ):
        return True
    try:
        return int(payload.get("review_count") or 0) > 0
    except (TypeError, ValueError):
        return False


def lead_tier(dish: str | None, payload: dict[str, Any]) -> str:
    """dish > review-quote (only a PASSING quote) > moment."""
    if str(dish or "").strip():
        return TIER_DISH
    from outreach.quotes import select_quote

    if payload and select_quote(payload).quote is not None:
        return TIER_QUOTE
    return TIER_MOMENT


def _enriched(payload: dict[str, Any]) -> str | None:
    for key in _MARKER_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _claimable(db: "Database") -> list[Any]:
    """ROUTED leads the dispatcher could reach, in the dispatcher's order.

    Excluded (with the dispatcher's own reasons): a lane that is paused or
    halted, a junk identity, and a lead parked by an open wall hold. Session
    windows and account ownership are NOT modelled (see the module docstring).
    """
    from outreach.cleaning import junk_identity_reason
    from outreach.countdown import _lane_state
    from outreach.dispatcher import claim_priority

    lanes = _lane_state(db)
    held = {
        int(row[0])
        for row in db.conn.execute(
            "SELECT DISTINCT lead_id FROM proofread_log WHERE vetoed=1"
            " AND cleared_at IS NULL"
        )
    }
    rows = []
    for row in db.conn.execute(
        "SELECT id, name, route_channel, extracted_dish, data_json"
        " FROM leads WHERE state='ROUTED'"
    ):
        if lanes.get(str(row["route_channel"] or "")) != "live":
            continue
        if junk_identity_reason(row["name"]) is not None:
            continue
        if int(row["id"]) in held:
            continue
        rows.append(row)
    return sorted(rows, key=claim_priority)


def _capacity_today_remaining(db: "Database", now: datetime, channel: str) -> int:
    """Room left TODAY: the live cap minus what already went out today."""
    from outreach.caps import current_cap, ensure_caps

    if countdown._lane_state(db).get(channel) != "live":
        return 0
    ensure_caps(db)  # self-sufficiency: a cold DB seeds its cap before reading
    used_row = db.conn.execute(
        "SELECT count FROM daily_sends WHERE channel=? AND date=?",
        (channel, pkt_date(now)),
    ).fetchone()
    used = int(used_row[0]) if used_row else 0
    return max(0, current_cap(db, channel, now) - used)


def _sends_today(db: "Database", now: datetime, channel: str) -> int:
    """How many sends already landed on this PKT day for the lane."""
    row = db.conn.execute(
        "SELECT count FROM daily_sends WHERE channel=? AND date=?",
        (channel, pkt_date(now)),
    ).fetchone()
    return int(row[0]) if row else 0


def _day_rate(db: "Database", day: datetime) -> int:
    """Effective DM rate on that day (accounts ramp between days)."""
    return int(countdown.daily_capacity(db, day)["total"])


def _claim_entry(row: Any) -> dict[str, Any]:
    payload = _payload_of(row["data_json"])
    entry: dict[str, Any] = {
        "lead_id": int(row["id"]),
        "tier": lead_tier(row["extracted_dish"], payload),
        "has_reviews": has_reviews(payload),
    }
    marker = _enriched(payload)
    if marker is not None:
        entry["enriched"] = marker
    return entry


def build(
    db: "Database", now: datetime, *, countdown_payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The whole forecast payload. Reads only; writes one config key."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    rows = _claimable(db)
    lanes = countdown._lane_state(db)
    live_channels = [channel for channel, state in lanes.items() if state == "live"]
    channel = "ig" if "ig" in live_channels else (
        live_channels[0] if live_channels else "ig"
    )
    caps = [_capacity_today_remaining(db, now, channel)]
    for offset in range(1, DAYS):
        caps.append(_day_rate(db, now + timedelta(days=offset)))

    days: list[dict[str, Any]] = []
    cursor = 0
    for offset in range(DAYS):
        day = pkt_date(now + timedelta(days=offset))
        take = min(max(0, caps[offset]), len(rows) - cursor)
        claims = [_claim_entry(row) for row in rows[cursor:cursor + take]]
        cursor += take
        days.append(
            {
                "day": day,
                "offset": offset,
                "capacity": int(caps[offset]),
                "claims_count": len(claims),
                "claims": claims,
            }
        )

    forecast_claims = [claim for day in days for claim in day["claims"]]
    debt = [
        claim for claim in forecast_claims
        if claim["tier"] == TIER_MOMENT and not claim["has_reviews"]
    ]
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "day": pkt_date(now),
        "claim_order": CLAIM_ORDER,
        "channel": channel,
        "daily_rate": (
            int(countdown_payload["daily_send_rate_effective"])
            if countdown_payload else caps[0]
        ),
        # 2026-11-13 (ownership audit): "rate" and "capacity" were being read as
        # a contradiction - supply said 15/day while this file said 7, and both
        # were true: 15 is the cap, 7 is what is LEFT of today. The pair now
        # states both numbers under names that cannot be confused.
        "cap_total": (
            int(countdown_payload["daily_send_rate_effective"])
            if countdown_payload else caps[0]
        ),
        "sent_today": _sends_today(db, now, channel),
        "today_capacity_remaining": int(caps[0]),
        "days": days,
        "enrichment_debt": len(debt),
        "enrichment_debt_detail": {"moment_without_reviews": len(debt)},
    }
    tiers = {tier: 0 for tier in (TIER_DISH, TIER_QUOTE, TIER_MOMENT)}
    for claim in forecast_claims:
        tiers[claim["tier"]] = tiers.get(claim["tier"], 0) + 1
    payload["tiers"] = tiers
    # O10 (2026-11-14): the contract also carries the reply rate per
    # attribution dimension, so "is the overhaul working?" is answered by the
    # same file that says what will ship next - measured, not believed.
    from outreach.attribution import contract as _attribution_contract

    payload["reply_rate_by"] = _attribution_contract(db, now, channel=channel)
    # O5 (2026-11-14): how much of the routed queue is timed on the venue's own
    # hours. "unknown" is the honest answer until the scraper fields land, and
    # an unknown clock changes nothing about the send order.
    from outreach.clock import status_line as _clock_line

    payload["send_clock"] = _clock_line(db, now)
    set_config(
        db,
        TODAY_KEY,
        {str(claim["lead_id"]): claim["tier"] for claim in days[0]["claims"]},
    )
    return payload


def render_markdown(payload: dict[str, Any]) -> str:
    """Human twin of the JSON: every number comes from the same dict."""
    days = payload["days"]
    lines = [
        "# Dispatch forecast",
        "",
        "Contract v1 — fixed path, schema v1, same rules as the countdown pair.",
        "Machine twin: `DISPATCH_FORECAST.json` (same numbers, same day).",
        "",
        f"- generated_at: {payload['generated_at']}",
        f"- day: {payload['day']}",
        f"- claim order: {payload['claim_order']}",
        f"- effective daily rate: {payload['daily_rate']}/day",
    ]
    for day in days:
        label = "today" if day["offset"] == 0 else f"+{day['offset']}"
        lines += [
            "",
            f"## {label} {day['day']} - {day['claims_count']} claim(s),"
            f" capacity {day['capacity']}",
        ]
        if not day["claims"]:
            lines.append("- (no claim: supply or capacity is exhausted)")
        for claim in day["claims"]:
            detail = [f"lead {claim['lead_id']}", claim["tier"]]
            detail.append("reviews yes" if claim["has_reviews"] else "no reviews")
            if "enriched" in claim:
                detail.append(f"enriched {claim['enriched']}")
            lines.append("- " + ", ".join(detail))
    tiers = payload["tiers"]
    lines += [
        "",
        "## Tiers",
        f"- dish: {tiers.get(TIER_DISH, 0)}",
        f"- review-quote: {tiers.get(TIER_QUOTE, 0)}",
        f"- moment: {tiers.get(TIER_MOMENT, 0)}",
        "",
        f"## Enrichment debt: {payload['enrichment_debt']}",
        "- moment-or-below with no reviews, no dish and no passing quote",
    ]
    if payload.get("send_clock"):
        lines += ["", f"## {payload['send_clock']}"]
    by_dimension = payload.get("reply_rate_by") or {}
    if by_dimension:
        lines += ["", "## Reply rate by attribution (72h, first touch)"]
        for dimension, rows in by_dimension.items():
            lines += ["", f"### {dimension}"]
            if not rows:
                lines.append("- (no decided first touches yet)")
                continue
            for row in rows:
                rate = row.get("rate")
                label = "n/a" if rate is None else f"{round(100 * rate)}%"
                late = f", late {row['late']}" if row.get("late") else ""
                lines.append(
                    f"- {row['bucket']}: {label}"
                    f" ({row['replies']}/{row['decided']}){late}"
                )
    return "\n".join(lines) + "\n"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _digest(db: "Database", payload: dict[str, Any]) -> None:
    tiers = payload["tiers"]
    total = sum(day["claims_count"] for day in payload["days"])
    body = (
        f"3-day outlook: {total} claims, tiers"
        f" dish {tiers.get(TIER_DISH, 0)} / review-quote {tiers.get(TIER_QUOTE, 0)}"
        f" / moment {tiers.get(TIER_MOMENT, 0)},"
        f" enrichment debt {payload['enrichment_debt']}."
    )
    with db.conn:
        db.conn.execute("INSERT INTO digest(body) VALUES (?)", (body,))


def write(
    db: "Database",
    now: datetime,
    *,
    countdown_payload: dict[str, Any] | None = None,
    json_path: Path = JSON_PATH,
    md_path: Path = MD_PATH,
) -> dict[str, Any]:
    """Write both forecast files and the preflight digest row."""
    payload = build(db, now, countdown_payload=countdown_payload)
    _write_atomic(md_path, render_markdown(payload))
    _write_atomic(json_path, json.dumps(payload, indent=2) + "\n")
    _digest(db, payload)
    return payload


def lead_tier_of(row: Any) -> str:
    """The tier of one lead row (the dispatcher's seam, same function)."""
    return lead_tier(row["extracted_dish"], _payload_of(row["data_json"]))


def record_divergence(
    db: "Database", lead_id: int, actual_tier: str, now: datetime
) -> str | None:
    """Log when the claim disagrees with today's forecast (drop = a lie).

    A HIGHER tier than forecast is not a lie worth a row (the forecast only
    promised an order); a LOWER one means the file counted something it did
    not have, which is the bug class the forecast exists to surface.
    """
    stored = get_config(db, TODAY_KEY) or {}
    expected = stored.get(str(lead_id)) if isinstance(stored, dict) else None
    if not expected:
        return None
    if TIER_RANK.get(actual_tier, 0) >= TIER_RANK.get(str(expected), 0):
        return None
    prefix = f"{DIVERGENCE_PREFIX} lead {lead_id}:"
    body = (
        f"{prefix} forecast {expected}, claim-time {actual_tier}"
        " (forecast lied - counting bug class)"
    )
    existing = db.conn.execute(
        "SELECT 1 FROM digest WHERE substr(body, 1, ?) = ? LIMIT 1",
        (len(prefix), prefix),
    ).fetchone()
    if existing:
        return None  # one row is enough to raise the class; no storm
    with db.conn:
        db.conn.execute("INSERT INTO digest(body) VALUES (?)", (body,))
    return body


__all__ = [
    "CLAIM_ORDER",
    "JSON_PATH",
    "MD_PATH",
    "SCHEMA_VERSION",
    "TIER_DISH",
    "TIER_MOMENT",
    "TIER_QUOTE",
    "build",
    "has_reviews",
    "lead_tier",
    "lead_tier_of",
    "record_divergence",
    "render_markdown",
    "write",
]
