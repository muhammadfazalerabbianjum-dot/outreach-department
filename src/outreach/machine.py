"""The lead state machine (blueprint, batch 1 core).

NEW → QUALIFIED → ROUTED → SENT → FOLLOWUP → REPLIED
Exits: REJECTED (ICP fail w/ reason), UNREACHABLE, WA_DEAD, IG_DEAD,
CHANNEL_BANNED (drains back to ROUTED on email), NO_REPLY, DUP.
Hold: REGION_UNRESOLVED (B20 — no region signal; owner releases to NEW;
never claimable: only ROUTED leads can dispatch).
Holds (2026-11-13): COPY_HOLD (the message lint refused the render) and
TRANSPORT_HOLD (Instagram would not load either DM surface). Both are
parks, not verdicts: a repair moves the lead back to ROUTED.
Park (2026-11-14, O6): DEPRIORITIZED — the targeting policy moved it out of
who we message (delivery-only, chain, brand account). Same shape: one repair
edge back to ROUTED, and nothing but ROUTED is ever claimable.

Every transition is validated against the frozen map, applied with the
history row in one transaction, and appended to `state_history`
(append-only at the DB level).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from outreach.store import Database

TERMINAL_STATES = frozenset(
    {"REPLIED", "REJECTED", "UNREACHABLE", "WA_DEAD", "IG_DEAD", "NO_REPLY", "DUP"}
)

ALLOWED: dict[str, frozenset[str]] = {
    "NEW": frozenset({"QUALIFIED", "REJECTED", "DUP", "REGION_UNRESOLVED"}),
    # Contamination hold 2026-11-03 (orchestrator ruling): a REJECTED lead may
    # return to QUALIFIED ONLY through the dedicated tier-contradiction repair
    # (`chain_review.restore_tier_valid_chain_rejects`) — the verdict was
    # wrong, the scraped tier was right. No dispatch path can re-qualify a
    # rejected lead on its own.
    "REJECTED": frozenset({"QUALIFIED"}),
    # Revive edge 2026-11-16: UNREACHABLE means "no LAWFUL channel at the
    # moment routing looked", not "no contact data". At that audit 842 of the
    # 867 parked rows carried a phone and 286 a handle that
    # `sanitize_instagram` accepts (on 2026-11-21: 554 lane-law rows, 536
    # phone-bearing), so a later repair/import can make the law reach them. Only
    # `router.reroute_stranded` may use this edge, and only after the same
    # pure lane law re-decides the row (never a hand-set channel).
    "UNREACHABLE": frozenset({"ROUTED"}),
    # B20 hold: owner sets the region and the lead rejoins as NEW — or the row
    # turns out to be junk (2026-11-16: lead 3149 "Unknown Name" with no phone
    # and no handle sat here for weeks), in which case it is REJECTED.
    "REGION_UNRESOLVED": frozenset({"NEW", "REJECTED"}),
    "QUALIFIED": frozenset({"ROUTED", "UNREACHABLE"}),  # UNREACHABLE = no usable channel at routing (B8)
    "ROUTED": frozenset(
        {
            "SENT",
            "UNREACHABLE",
            "WA_DEAD",
            "IG_DEAD",
            "CHANNEL_BANNED",
            "DUP",
            "COPY_HOLD",
            "TRANSPORT_HOLD",
            "DEPRIORITIZED",
        }
    ),
    # O6 (2026-11-14): targeting parked it (delivery-only, chain, brand...).
    # A park, never a verdict: one repair edge returns it to ROUTED.
    "DEPRIORITIZED": frozenset({"ROUTED"}),
    # Parks, never verdicts: the account may be fine (transport) and the lead
    # may be fine (copy). Both return to ROUTED only by an explicit repair,
    # so a poison row cannot be re-claimed in the same tick — or ever.
    "COPY_HOLD": frozenset({"ROUTED"}),
    "TRANSPORT_HOLD": frozenset({"ROUTED"}),
    "SENT": frozenset(
        {"FOLLOWUP", "REPLIED", "UNREACHABLE", "WA_DEAD", "IG_DEAD"}
    ),
    "FOLLOWUP": frozenset(
        {"REPLIED", "NO_REPLY", "UNREACHABLE", "WA_DEAD", "IG_DEAD"}
    ),
    "CHANNEL_BANNED": frozenset({"ROUTED"}),
}

_REASON_REQUIRED = frozenset({"REJECTED", "DUP"})
# Holds whose reason the OWNER must be able to read on the row itself
# (2026-11-16: 18 COPY_HOLD leads carried reject_reason NULL because the
# reason only ever went to state_history, so `holds` was the only place to
# learn why a lead was stuck). Same string, now on both rows.
_REASON_ON_ROW = frozenset(
    {"REJECTED", "COPY_HOLD", "TRANSPORT_HOLD", "REGION_UNRESOLVED"}
)


class InvalidTransition(ValueError):
    """The requested edge does not exist in the frozen machine."""


class MissingReason(ValueError):
    """The target state (REJECTED/DUP) refuses to fire without a reason."""


def _now_sql() -> str:
    return "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def _stamp_sql(at: datetime | None) -> str:
    """The row timestamp: the caller's clock when given, SQLite's otherwise.

    One format, matching `dispatcher.utc_stamp` (…T%H:%M:%SZ), because the
    reply measurement compares these against `message_log.sent_at`.
    """
    if at is None:
        return "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
    moment = at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def transition(
    db: "Database",
    lead_id: int,
    to_state: str,
    *,
    reason: str | None = None,
    actor: str = "system",
    commit: bool = True,
    at: datetime | None = None,
    park_kind: str | None = None,
) -> str:
    """Move a lead one legal edge; return the new state.

    Atomic: state update (+reject_reason when applicable) and the append-only
    history row commit together or not at all — unless commit=False, where
    the caller's transaction owns the commit (dispatcher's atomic send).

    `at` stamps both rows from the caller's clock instead of SQLite's wall
    clock; a detector that decided at `now` must not be recorded hours later
    (the reply measurement reads these stamps against `message_log.sent_at`).
    """
    stamp = _stamp_sql(at)
    row = db.conn.execute(
        "SELECT state FROM leads WHERE id=?", (lead_id,)
    ).fetchone()
    if row is None:
        raise InvalidTransition(f"unknown lead id={lead_id}")
    from_state = row["state"]

    if to_state not in ALLOWED.get(from_state, frozenset()):
        raise InvalidTransition(f"{from_state} cannot transition to {to_state}")

    if to_state in _REASON_REQUIRED and not (reason and reason.strip()):
        raise MissingReason(f"{to_state} requires a reject reason")

    with db.conn if commit else _NullContext():
        # `park_kind` is the park's CLASS, written only by the code that parks
        # (`lane_law` = routing found no lawful channel; `verdict` = the platform
        # refused). It is cleared on every other edge, so a stale class can never
        # survive a repair, and the revive reads this field instead of prose.
        kind = (park_kind or "") if to_state == "UNREACHABLE" else ""
        if to_state in _REASON_ON_ROW and (reason or "").strip():
            db.conn.execute(
                "UPDATE leads SET state=?, reject_reason=?, park_kind=?, "
                "updated_at=? WHERE id=?",
                (to_state, reason, kind, stamp, lead_id),
            )
        else:
            db.conn.execute(
                "UPDATE leads SET state=?, park_kind=?, updated_at=? WHERE id=?",
                (to_state, kind, stamp, lead_id),
            )
        db.conn.execute(
            "INSERT INTO state_history(lead_id, from_state, to_state, reason, actor,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (lead_id, from_state, to_state, reason, actor, stamp),
        )
    return to_state


class _NullContext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc_info: object) -> None:
        return None
