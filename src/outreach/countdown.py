"""Supply countdown: the stable, contract-style handoff of "how much is left".

Owner order 2026-11-09: third-party scripts and external agents consume this
file, so the PATH is a contract, not a convenience:

    <repo>/SUPPLY_COUNTDOWN.md      (human)
    <repo>/SUPPLY_COUNTDOWN.json    (machine, schema v1)

Both paths live next to `outreach.db`, both are written from ONE payload in one
pass, so the Markdown and the JSON can never disagree. The JSON key set is
versioned (`schema_version: 1`): renaming a key requires a migration, not a
patch.

Every number is measured from the department's own DB. When an input cannot be
measured the key is OMITTED rather than estimated - a missing key is honest, a
guessed number is not (the one case today: a sending rate of 0 makes "days of
supply" a division by zero, so the key is absent and `rate_note` says why).

The file is (re)written on the first engine tick after 00:00 PKT and refreshed
once at 23:00 PKT. `refills_needed` is true below 14 days of supply; both files
then carry the line `SCRAPER NEEDED: N days of supply at current rate` and the
owner gets a toast.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from outreach.caps import PKT, pkt_date
from outreach.store import get_config, set_config

if TYPE_CHECKING:
    from outreach.store import Database

SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parents[2]
MD_PATH = ROOT / "SUPPLY_COUNTDOWN.md"
JSON_PATH = ROOT / "SUPPLY_COUNTDOWN.json"
ALERT_PREFIX = "SCRAPER NEEDED"

DAY_STAMP = "countdown_day"           # PKT day whose 00:00 write landed
REFRESH_STAMP = "countdown_refreshed_day"  # PKT day whose 23:00 refresh landed
HISTORY_KEY = "countdown_history"     # {day: dispatch_ready total}, ≤ 14 days
REFRESH_HOUR = 23                     # local (PKT) hour of the daily refresh
REFILL_THRESHOLD_DAYS = 14            # below this, the scraper is needed
TREND_DAYS = 7
_HISTORY_DAYS = 14


def _int(conn: "Database", sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None else 0


def _lane_state(db: "Database") -> dict[str, str]:
    """`live` / `paused` / `halted` / `disarmed` per channel (config only)."""
    return {
        "ig": "halted" if get_config(db, "ig_halted") else "live",
        "wa": "paused" if get_config(db, "wa_paused") else "live",
        "email": "live" if get_config(db, "email_enabled") else "disarmed",
    }


def _claimable(db: "Database", lanes: dict[str, str]) -> dict[str, int]:
    """ROUTED leads a first touch could actually claim today, per channel.

    Same boundary the claim loop enforces: a junk-identity row is never
    claimed, and a lane that is paused/halted/disarmed claims nothing.
    """
    from outreach.cleaning import junk_identity_reason

    counts = {"ig": 0, "wa": 0, "email": 0}
    dish = 0
    for row in db.conn.execute(
        "SELECT name, route_channel, extracted_dish FROM leads WHERE state='ROUTED'"
    ):
        channel = str(row["route_channel"] or "")
        if channel not in counts or lanes.get(channel) != "live":
            continue
        if junk_identity_reason(row["name"]) is not None:
            continue
        counts[channel] += 1
        if str(row["extracted_dish"] or "").strip():
            dish += 1  # a dish is a dish on any live lane
    return {"counts": counts, "with_dish": dish}


def definitions(db: "Database") -> dict[str, Any]:
    """The four supply numbers, each with the set it counts (owner ruling 2026-11-16).

    One word ("supply") was answering three different questions and the answers
    were read as one: 123 sendable is not 259 usable, and "no contact channel"
    never meant UNREACHABLE (842 of those 867 rows carry a phone). Every number
    below is a separate SELECT over a named state set, and the label travels
    WITH the number into both files, so no reader has to guess the denominator.
    """
    lanes = _lane_state(db)
    claimable = _claimable(db, lanes)
    by_state = {
        str(row["state"]): int(row["n"])
        for row in db.conn.execute(
            "SELECT state, COUNT(*) n FROM leads GROUP BY state"
        )
    }
    usable_states = (
        "ROUTED", "COPY_HOLD", "TRANSPORT_HOLD", "FOLLOWUP", "SENT", "REPLIED",
    )
    phone_only = 0
    no_channel = 0
    for row in db.conn.execute(
        "SELECT state, phone, phone_e164, instagram FROM leads"
    ):
        phone = str(row["phone"] or "").strip()
        handle = str(row["instagram"] or "").strip()
        e164 = str(row["phone_e164"] or "").strip()
        if not phone and not handle:
            no_channel += 1
        if (
            str(row["state"]) == "UNREACHABLE"
            and e164.startswith("+")
            and not handle
        ):
            phone_only += 1  # a real number the lane law cannot legally use
    return {
        "sendable_today": {
            "n": sum(claimable["counts"].values()),
            "set": "state='ROUTED' on a lane that is live (ig/wa/email), junk "
                   "identity excluded - the only number dispatch spends",
            "by_lane": dict(claimable["counts"]),
        },
        "usable_stock": {
            "n": sum(by_state.get(s, 0) for s in usable_states),
            "set": "states " + ", ".join(usable_states) + " - every lead still "
                   "worth a touch; includes COPY_HOLD/TRANSPORT_HOLD (parked, "
                   "repairable) and the post-first-touch ladder",
        },
        "phone_only_parked": {
            "n": phone_only,
            "set": "state='UNREACHABLE' with a valid phone_e164 and no IG - "
                   "contactable in principle, no lawful cold lane (B29: "
                   "WhatsApp first touch is order-line/Canada only)",
        },
        "no_channel": {
            "n": no_channel,
            "set": "no phone AND no instagram in our data, any state - the only "
                   "honest reading of \"no contact channel\"",
        },
        "by_state": by_state,
    }


def _capacity(db: "Database", now: datetime, lanes: dict[str, str]) -> dict[str, Any]:
    """Effective daily send rate: signed-in accounts × their OWN ramp cap.

    Never the fleet ambition. A halted account, or any account on a lane
    that is paused/disarmed, contributes 0 — so arming ig2–ig4 visibly
    raises the number and nothing else does.
    """
    from outreach.accounts import cap_for

    breakdown: list[dict[str, Any]] = []
    total = 0
    for row in db.conn.execute(
        "SELECT id, channel, status, warmup_start FROM accounts ORDER BY channel, id"
    ):
        channel = str(row["channel"])
        cap = cap_for(db, row, now) if lanes.get(channel) == "live" else 0
        total += cap
        breakdown.append(
            {
                "account": str(row["id"]),
                "channel": channel,
                "status": str(row["status"]),
                "daily_cap": int(cap),
            }
        )
    return {"total": total, "breakdown": breakdown}


def _history(db: "Database", day: str, total: int) -> dict[str, int]:
    raw = get_config(db, HISTORY_KEY) or {}
    history = {str(k): int(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    history[day] = int(total)
    kept = sorted(history.items())[-_HISTORY_DAYS:]
    set_config(db, HISTORY_KEY, dict(kept))
    return dict(kept)


def daily_capacity(db: "Database", now: datetime) -> dict[str, Any]:
    """Public seam: one day's DM capacity (awake lanes x per-account ramps).

    The dispatch forecast calls this for each day of its window, so the two
    contracts measure capacity with the same arithmetic.
    """
    return _capacity(db, now, _lane_state(db))


def build(db: "Database", now: datetime) -> dict[str, Any]:
    """The whole payload, read-only (no config writes, no files)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    day = pkt_date(now)
    lanes = _lane_state(db)
    supply = _claimable(db, lanes)
    capacity = daily_capacity(db, now)
    in_flight = {
        "ladder_pending": _int(
            db, "SELECT COUNT(*) FROM followups WHERE status='pending'"
        ),
        "held_by_wall": _int(
            db,
            "SELECT COUNT(*) FROM proofread_log WHERE vetoed=1 AND cleared_at IS NULL",
        ),
        "in_conversation": _int(
            db, "SELECT COUNT(*) FROM leads WHERE state='REPLIED'"
        ),
    }
    ready_total = sum(supply["counts"].values())
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "day": day,
        "dispatch_ready": supply["counts"],
        "lanes": lanes,
        "with_dish": supply["with_dish"],
        "in_flight": in_flight,
        # a row with no captured text is not a restaurant replying (owner
        # ruling 2026-11-11): it never inflates the reply count
        "replies_pending_ack": _int(
            db,
            "SELECT COUNT(*) FROM owner_inbox WHERE acknowledged_at IS NULL"
            " AND TRIM(COALESCE(reply_text, '')) != ''",
        ),
        "daily_send_rate_effective": capacity["total"],
        # the other half of the same fact: what already went out today, so a
        # reader can never mistake the cap for the remaining room
        "sent_today": {
            lane: _int(
                db,
                "SELECT COALESCE(MAX(count), 0) FROM daily_sends"
                " WHERE channel=? AND date=?",
                (lane, day),
            )
            for lane in ("ig", "wa", "email")
        },
        "rate_breakdown": capacity["breakdown"],
        "definitions": definitions(db),
    }
    rate = capacity["total"]
    if rate > 0:
        days = round(ready_total / rate, 2)
        payload["days_of_supply_remaining"] = days
        refills = days < REFILL_THRESHOLD_DAYS
        payload["refills_needed"] = refills
        if refills:
            payload["refill_alert"] = (
                f"{ALERT_PREFIX}: {days:g} days of supply at current rate"
            )
    else:
        payload["rate_note"] = (
            "no account can send (0/day): days of supply is undefined, "
            "not infinite - fix the lane, then read this file"
        )
    return payload


def render_markdown(payload: dict[str, Any]) -> str:
    """The human twin of the JSON — every number comes from the same dict."""
    ready = payload["dispatch_ready"]
    lanes = payload["lanes"]
    flight = payload["in_flight"]
    lines = [
        "# Supply countdown",
        "",
        "Contract v1 — this path never changes and these JSON keys never rename",
        "without a versioned migration. Machine twin: `SUPPLY_COUNTDOWN.json`",
        f"(same numbers, same day). Schema version: {payload['schema_version']}.",
        "",
        f"- generated_at: {payload['generated_at']}",
        f"- day: {payload['day']}",
        "",
        "## Dispatch-ready (claimable for a first touch today)",
        f"- ig: {ready['ig']} leads ({lanes['ig']})",
        f"- wa: {ready['wa']} leads ({lanes['wa']})",
        f"- email: {ready['email']} leads ({lanes['email']})",
        f"- with a verified dish: {payload['with_dish']}",
        "",
        "## In flight",
        f"- ladder pending: {flight['ladder_pending']}",
        f"- held by the proofread wall: {flight['held_by_wall']}",
        f"- in active conversation: {flight['in_conversation']}",
        f"- replies waiting for the owner: {payload['replies_pending_ack']}",
        "",
        "## Sending capacity",
        f"- effective daily send rate: {payload['daily_send_rate_effective']}/day",
    ]
    for row in payload["rate_breakdown"]:
        lines.append(
            f"  - {row['account']} ({row['channel']}, {row['status']}):"
            f" cap {row['daily_cap']}"
        )
    lines += ["", "## What the numbers mean (owner ruling 2026-11-16)"]
    for name in ("sendable_today", "usable_stock", "phone_only_parked", "no_channel"):
        block = payload["definitions"][name]
        lines.append(f"- {name}: {block['n']} — {block['set']}")
    lines += ["", "## Supply"]
    if "days_of_supply_remaining" in payload:
        lines.append(
            f"- days of supply remaining: {payload['days_of_supply_remaining']:g}"
        )
    else:
        lines.append(f"- days of supply remaining: unmeasurable — {payload['rate_note']}")
    trend = payload.get("supply_trend_7d") or []
    if trend:
        lines.append(
            "- trend (7d): "
            + ", ".join(f"{row['day']} {row['dispatch_ready']}" for row in trend)
        )
    else:
        lines.append(
            "- trend (7d): unmeasurable — no recorded day yet (this is the first)"
        )
    if payload.get("refill_alert"):
        lines += ["", payload["refill_alert"]]
    return "\n".join(lines) + "\n"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _digest(db: "Database", payload: dict[str, Any]) -> int:
    ready = payload["dispatch_ready"]
    supply = payload.get("days_of_supply_remaining")
    trend = payload.get("supply_trend_7d") or []
    defs = payload["definitions"]
    body = (
        f"SUPPLY COUNTDOWN {payload['day']}: {sum(ready.values())} lead(s) ready"
        f" ({payload['with_dish']} with dish) - "
        f"{payload['daily_send_rate_effective']}/day effective - "
        + (f"{supply:g} days of supply" if supply is not None else "days of supply n/a")
        + f" - replies awaiting ack: {payload['replies_pending_ack']}"
        + (f" - trend 7d: {len(trend)} day(s) recorded" if trend else "")
        + f" - coverage: usable {defs['usable_stock']['n']},"
        f" phone-only parked {defs['phone_only_parked']['n']},"
        f" no channel {defs['no_channel']['n']}"
    )
    with db.conn:
        cursor = db.conn.execute(
            "INSERT INTO digest(body) VALUES (?)",
            (body + (f" | {payload['refill_alert']}" if payload.get("refill_alert") else ""),),
        )
    return int(cursor.lastrowid or 0)


def _due(db: "Database", now: datetime) -> str | None:
    """Which write this tick owes: 'daily' (day rollover), 'refresh' (23:00).

    Both stamps are per PKT day, so a restarted engine re-writes the same day's
    file exactly once instead of on every tick, and the 23:00 refresh runs even
    when the 00:00 write happened outside a window.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    day = pkt_date(now)
    if str(get_config(db, DAY_STAMP) or "") != day:
        return "daily"
    hour = now.astimezone(PKT).hour
    if hour >= REFRESH_HOUR and str(get_config(db, REFRESH_STAMP) or "") != day:
        return "refresh"
    return None


def write(
    db: "Database",
    now: datetime,
    *,
    md_path: Path = MD_PATH,
    json_path: Path = JSON_PATH,
    notifier: Callable[[str], bool] | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    """Write both files when this tick owes a write; None when it does not.

    `force` is for an owner/manual regeneration (and the contract test): it
    ignores the day stamps but still records today's history point.
    """
    kind = "forced" if force else _due(db, now)
    if kind is None:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    payload = build(db, now)
    ready_total = sum(payload["dispatch_ready"].values())
    history = _history(db, payload["day"], ready_total)
    payload["supply_trend_7d"] = [
        {"day": day, "dispatch_ready": total}
        for day, total in sorted(history.items())[-TREND_DAYS:]
    ]
    _write_atomic(md_path, render_markdown(payload))
    _write_atomic(json_path, json.dumps(payload, indent=2) + "\n")
    day = payload["day"]
    if kind == "daily":
        set_config(db, DAY_STAMP, day)
        set_config(db, REFRESH_STAMP, "")
    elif kind == "refresh":
        set_config(db, REFRESH_STAMP, day)
    digest_id = _digest(db, payload)
    if kind == "refresh":
        # Owner ruling 2026-11-11: the 23:00 pass refreshes the files and the
        # digest row ONLY. The row is the record; advancing the toast cursor
        # past it keeps the standard drain from popping a balloon for it.
        from outreach.toast import STAMP_ID

        set_config(db, STAMP_ID, digest_id)
    alert = payload.get("refill_alert")
    if alert and kind == "daily":
        # ... and the refill toast itself is once per DAY, not once per write.
        if str(get_config(db, "refill_toast_day") or "") == payload["day"]:
            alert = None
    if alert:
        if notifier is not None:
            try:
                if notifier(alert):
                    set_config(db, "refill_toast_day", payload["day"])
                    # the digest row is the durable record; stamping the toast
                    # clock keeps the standard drain from popping the same
                    # alert twice inside its cooldown window.
                    from outreach.toast import STAMP_AT

                    set_config(db, STAMP_AT, now.isoformat())
            except Exception:  # noqa: BLE001 - a toast never sinks the write
                pass
    # Same tick, same measured payload (owner order 2026-11-11): the forward
    # contract is written beside the countdown, never on a separate schedule.
    from outreach.forecast import write as write_forecast

    try:
        write_forecast(
            db, now, countdown_payload=payload,
            json_path=json_path.with_name("DISPATCH_FORECAST.json"),
            md_path=json_path.with_name("DISPATCH_FORECAST.md"),
        )
    except Exception as exc:  # noqa: BLE001 - never sink the countdown write
        with db.conn:
            db.conn.execute(
                "INSERT INTO digest(body) VALUES (?)",
                (f"DISPATCH FORECAST WRITE ERROR: {repr(exc)[:300]}",),
            )
    return payload


def _is_live(db: "Database") -> bool:
    """Is this the department's real DB, the one the contract file describes?

    Tests (and dry runs) open `outreach.db` inside tmp dirs; letting them
    write the fixed paths would put fixture numbers into a file that external
    agents treat as truth. Only the real DB may regenerate the contract.
    """
    try:
        return Path(db.path).resolve() == (ROOT / "outreach.db").resolve()
    except OSError:  # pragma: no cover - unreadable path is not the live DB
        return False


def write_from_tick(db: "Database", now: datetime) -> dict[str, Any] | None:
    """Engine-loop seam: never raises (a countdown fault cannot park sends)."""
    from outreach.toast import sendast

    if not _is_live(db):
        return None
    try:
        return write(db, now, notifier=sendast)
    except Exception as exc:  # noqa: BLE001 - visibility instead of a crash
        try:
            with db.conn:
                db.conn.execute(
                    "INSERT INTO digest(body) VALUES (?)",
                    (f"SUPPLY COUNTDOWN WRITE ERROR: {repr(exc)[:300]}",),
                )
        except Exception:  # noqa: BLE001 - even the record is best-effort
            pass
        return None


__all__ = [
    "ALERT_PREFIX",
    "JSON_PATH",
    "MD_PATH",
    "SCHEMA_VERSION",
    "build",
    "render_markdown",
    "write",
    "write_from_tick",
]
