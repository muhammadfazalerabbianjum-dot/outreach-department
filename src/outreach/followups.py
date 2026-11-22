"""Follow-up ladder (B14, amended by B24 / ADR-10 amendment 2026-10-29).

The ladder NEVER dies. Rungs are calendar-fixed on the first send:

    day 3 -> day 7 -> day 14 -> day 30 -> every 6 months (180 days), forever

(owner directive 2026-10-29: "we never stop following up"). NO_REPLY
expiry is gone from the machine path — a lead sits in FOLLOWUP until a
reply hands it to the owner or a channel dies. The reply-cancel law is
untouched (B13's exactly-once cancel: any reply kills every pending row).

Anchor law: the rung calendar hangs off the FIRST send
(`anchor = touch-1 due_at - 3d`, falling back to `MIN(message_log.sent_at)`).
Arming always jumps to the first rung whose date is still in the future,
so a stalled engine skips lapsed rungs instead of replaying them as a
burst — the same "missed windows are skipped, never dumped" law the
scheduler keeps for sends.

Day-7 IG escalation (B24): when the day-7 touch of a WhatsApp-routed lead
goes out and the lead has a usable Instagram, ONE escalation row
(`channel='ig'`) is armed. It rides the exact same laws as any IG send —
queue/template gate, ADR-8 halt + warmup ramp, daily cap, the 180–300 s
human gap, the rolling 4/hour slot, ADR-9 region windows — and never
re-arms: one shot, while the WA ladder keeps walking independently.

Rung copy (owner directive 2026-10-30): ladder rungs ship the owner's
TEMPORARY follow-up line (`templates/followup.txt`, one frozen sentence,
hash-pinned) — previously every rung re-sent the opener, so leads got the
same message twice on the same topic (live: Eddy Lebanese cuisine et al).
First sends are untouched (ADR-6 opener law) and the day-7 IG escalation
— the first Instagram contact for that lead — still ships the opener.
Resends are real sends: they respect the daily caps, land in message_log,
and pause with a paused queue.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from outreach.caps import register_send, send_allowed
from outreach.templates import (
    MessageHold,
    assert_sendable_template,
    render_followup,
    render_for_lead,
)
from outreach.variants import render_lane

if TYPE_CHECKING:
    from outreach.store import Database

_FIRST_TOUCH_AT = timedelta(days=3)  # day 3, armed at commit_send

# Rung offsets (days after the first send). Ruling B (v1.2 packet,
# 2026-11-05): TWO follow-ups - day 3 and day 7. The earlier 14/30/180
# cadence is retired; re-arming it is a separate recorded owner ruling.
#: Owner ruling 2026-11-09: a rung arriving more than three days after the
#: day it was earned is not a follow-up any more, it is a mass-mailer. The
#: copy is engineered to read like one person remembering you; a day-3 touch
#: landing on day 13 breaks that promise, so those rows are EXPIRED
#: (`expired_reason='expired_overdue'`), the lead's ladder closes, and nothing
#: re-arms. Rungs inside the grace window are still owed and drain first.
EXPIRE_GRACE_H = 72.0
_RUNG_DAYS = (3, 7)
MAX_LADDER_TOUCH = len(_RUNG_DAYS)


def expire_overdue_rungs(
    db: "Database", now: datetime, *, grace_h: float = EXPIRE_GRACE_H
) -> list[int]:
    """Expire pending rungs more than `grace_h` past their due date.

    Returns the expired follow-up ids. Nothing is re-armed (the caller never
    passes them to `_arm_next`), so an expired lead's ladder is closed, and
    the reason is durable: an operator reading the table sees WHY the ladder
    stopped rather than guessing at a missing row. Idempotent - an expired
    row is no longer `pending`.
    """
    cutoff = (now - timedelta(hours=grace_h)).astimezone(timezone.utc)
    # Only rungs that could still be SENT are in scope: a row hanging off a
    # lead that already left SENT/FOLLOWUP (replied, terminal) belongs to the
    # reply/state machine, and expiring it here would erase that handover trail.
    rows = db.conn.execute(
        "SELECT f.id FROM followups f JOIN leads l ON l.id = f.lead_id"
        " WHERE f.status='pending' AND f.due_at < ?"
        " AND l.state IN ('SENT', 'FOLLOWUP')",
        (cutoff.isoformat().replace("+00:00", "Z"),),
    ).fetchall()
    expired = [int(r["id"]) for r in rows]
    if not expired:
        return []
    with db.conn:
        db.conn.executemany(
            "UPDATE followups SET status='expired', expired_reason='expired_overdue'"
            " WHERE id=? AND status='pending'",
            [(fid,) for fid in expired],
        )
    return expired


def rung_days(touch: int) -> int:
    """Days after the first send when rung `touch` is due: day 3, then day 7.

    A touch past the cap is a law violation, not a calendar question, so it
    raises instead of inventing a day-14.
    """
    if touch < 1:
        raise ValueError(f"touch must be >= 1, got {touch}")
    if touch > MAX_LADDER_TOUCH:
        raise ValueError(
            f"the ladder is capped at two follow-ups (day 3, day 7); touch"
            f" {touch} is not armed (Ruling B 2026-11-05)"
        )
    return _RUNG_DAYS[touch - 1]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _anchor(db: "Database", lead_id: int, now: datetime) -> datetime:
    """The lead's first-send instant (the rung calendar's origin)."""
    row = db.conn.execute(
        "SELECT due_at FROM followups WHERE lead_id=? AND touch=1"
        " ORDER BY id LIMIT 1",
        (lead_id,),
    ).fetchone()
    if row is not None:
        return datetime.fromisoformat(row["due_at"]) - _FIRST_TOUCH_AT
    sent = db.conn.execute(
        "SELECT MIN(sent_at) FROM message_log WHERE lead_id=?", (lead_id,)
    ).fetchone()[0]
    if sent:
        return datetime.fromisoformat(str(sent))
    return now  # no history at all: arm relative to now (never before)


def _next_rung_after(
    last_touch: int, anchor: datetime, now: datetime
) -> tuple[int, datetime] | None:
    """First rung strictly after `last_touch` whose date is still ahead.

    None once the two-touch cap is reached (Ruling B): the ladder is over,
    and a lapsed rung is skipped rather than replayed as a burst.
    """
    touch = last_touch + 1
    if touch > MAX_LADDER_TOUCH:
        return None
    due = anchor + timedelta(days=rung_days(touch))
    while due <= now:
        touch += 1  # lapsed while the engine was down: skip, never burst
        if touch > MAX_LADDER_TOUCH:
            return None
        due = anchor + timedelta(days=rung_days(touch))
    return touch, due


def email_rung_allowed(db: "Database", lead_row: Any) -> bool:
    """Can this lead's EMAIL rung send right now? One rule, two call sites.

    LIVE LESSON 2026-11-04: the contamination quarantine was enforced on
    first touch (`dispatcher.claim_next`) but not on the ladder, which
    re-sends the same address on day 3/7/14/... Fifteen rungs fired that
    day and fourteen carried a free mailbox or an unrelated domain. The
    claim loop asked; the ladder never did.

    Mirrored by `scheduler.seconds_to_next_wake`, so a refused rung is not
    wake work and can never pin the engine to wake=0.
    """
    from outreach.dispatcher import email_enabled
    from outreach.router import email_lane_ok

    if not email_enabled(db):
        return False  # the owner disarmed the lane (ADR-5 gate)
    try:
        payload = json.loads(lead_row["data_json"] or "{}") or {}
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return email_lane_ok(
        lead_row["email"],
        payload.get("website"),
        payload.get("email_verified"),
    )


def schedule_ladder_on_sent(db: "Database", lead_id: int, now: datetime) -> None:
    """Arm touch 1 (day 3) exactly once when a lead hits SENT."""
    exists = db.conn.execute(
        "SELECT COUNT(*) FROM followups WHERE lead_id=?", (lead_id,)
    ).fetchone()[0]
    if exists:
        return
    with db.conn:
        db.conn.execute(
            "INSERT INTO followups(lead_id, touch, due_at, status)"
            " VALUES (?, 1, ?, 'pending')",
            (lead_id, _iso(now + _FIRST_TOUCH_AT)),
        )


def _arm_ig_escalation(db: "Database", lead_id: int, now: datetime) -> bool:
    """Arm the one-shot day-7 IG escalation row; True when armed.

    Eligible: a WhatsApp-routed lead with a usable Instagram handle (a
    DEAD link class is no link at all — the router says so, B20). Idempotent
    per lead: an existing escalation row is never duplicated.
    """
    row = db.conn.execute(
        "SELECT route_channel, instagram, ig_class FROM leads WHERE id=?",
        (lead_id,),
    ).fetchone()
    if row is None or row["route_channel"] != "wa":
        return False
    handle = (row["instagram"] or "").strip()
    if not handle or row["ig_class"] == "DEAD":
        return False
    exists = db.conn.execute(
        "SELECT 1 FROM followups WHERE lead_id=? AND channel='ig' LIMIT 1",
        (lead_id,),
    ).fetchone()
    if exists:
        return False
    with db.conn:
        db.conn.execute(
            "INSERT INTO followups(lead_id, touch, channel, due_at, status)"
            " VALUES (?, 2, 'ig', ?, 'pending')",
            (lead_id, _iso(now)),
        )
    return True


def process_due_followups(
    db: "Database",
    now: datetime,
    *,
    senders: dict[str, Callable[..., "object"]],
    regions: tuple[str, ...] | None = None,
    probe_blocked: tuple[str, ...] = (),
) -> list[int]:
    """Run every due follow-up once through the channel adapter.

    Adapter contract (wired to the real senders at B15):
    ``senders[channel](db, lead_id, now=now)`` — transport only; the
    ladder does its own bookkeeping (message_log, caps, state machine).
    Nothing is armed ahead of a failed touch — the row stays pending for
    the next run. Owner law (f) (HANDOFF §7): a resend is skipped while
    the global 180–300 s send gap is running, and every resend that does
    go out arms the gap for the next one. `regions` narrows the pass to
    open session windows (ADR-9): None = no filter (owner/manual runs).

    A row's channel is `followups.channel` when stamped (the day-7 IG
    escalation), else the lead's own `route_channel`. Successful ladder
    rows arm the next rung (first future one); the day-2 ladder row also
    arms the one-shot IG escalation. Escalation rows never arm anything.
    """
    from outreach import dispatcher as _disp
    from outreach.dispatcher import (
        _SLOT_CHANNELS,
        _SLOT_MAX_PER_HOUR,
        _assert_queue_live,
        arm_send_gap,
        send_gap_remaining,
        sends_in_last_hour,
    )
    from outreach.caps import ensure_ig_warmup_start
    from outreach.store import get_config

    if regions is not None and not regions:
        return []  # no open window anywhere: nothing may send (ADR-9)
    # Owner ruling 2026-11-09: drop the mass-mailer tail BEFORE anything is
    # sent, so the grace-window rungs are the ones that drain first.
    expire_overdue_rungs(db, now)
    processed: list[int] = []
    rows_sql = (
        "SELECT f.id AS fid, f.touch, f.channel AS fchannel, f.lead_id,"
        " l.route_channel, l.state"
        " FROM followups f JOIN leads l ON l.id = f.lead_id"
        " WHERE f.status='pending' AND f.due_at <= ?"
    )
    params: list[object] = [_iso(now)]
    if regions is not None:
        marks = ", ".join("?" for _ in regions)
        rows_sql += f" AND l.region IN ({marks})"
        params.extend(regions)
    rows_sql += " ORDER BY f.due_at"
    rows = db.conn.execute(rows_sql, params).fetchall()
    for row in rows:
        if row["state"] not in ("SENT", "FOLLOWUP"):
            continue  # replied/cancelled mid-window — B13 owns those
        is_escalation = row["fchannel"] is not None
        if not is_escalation and int(row["touch"]) > MAX_LADDER_TOUCH:
            # Ruling B: a rung armed under the retired 14/30 cadence must
            # never fire. Expire it (auditable) instead of leaving a row
            # pending forever; the escalation row (touch 2, channel-stamped)
            # is the same day-7 touch on another channel, not a third rung.
            with db.conn:
                db.conn.execute(
                    "UPDATE followups SET status='expired' WHERE id=?",
                    (row["fid"],),
                )
            continue
        channel = row["fchannel"] or row["route_channel"]
        if channel in probe_blocked:
            # Reply-guard (owner 2026-10-30): this channel's reply probe has
            # not confirmed its threads recently (live: probe outage while
            # rungs came due). The row STAYS pending — retried on the very
            # next pass after a successful probe — never fired blind.
            continue
        sender = senders.get(channel)
        if sender is None:
            continue  # channel not wired into this run (B15 responsibility)
        try:
            _assert_queue_live(db)
            assert_sendable_template(db)
        except Exception:  # noqa: BLE001 — paused/refused queue halts all
            break
        if channel == "wa" and get_config(db, "wa_paused"):
            continue  # owner directive 2026-11-02: paused lane — the rung
            # stays pending, retried when the owner re-arms WhatsApp
        if channel == "ig" and get_config(db, "ig_halted"):
            continue  # ADR-8: halted lane — no Instagram usage at all
        if send_gap_remaining(db, now) > 0:
            continue  # owner law (f): the 180–300 s break is still running
        if channel in _SLOT_CHANNELS and (
            sends_in_last_hour(db, channel, now) >= _SLOT_MAX_PER_HOUR
        ):
            continue  # owner law (c): 4 per hour-slot per platform (rolling)
        if channel == "ig":
            # ADR-8 bootstrap (same as claim_next): a real, in-window IG
            # attempt starts the ramp — otherwise cap 0 deadlocks the lane.
            ensure_ig_warmup_start(db, now)
        if not send_allowed(db, channel, now):
            continue  # capped — stays pending for a later window
        from outreach.variants import render_select

        # Every column a render reads: the day-7 IG escalation renders the
        # opener, and a row missing `city` raised sqlite3.Row's IndexError
        # instead of the MessageHold the renderer was written to raise.
        lead = db.conn.execute(render_select(), (row["lead_id"],)).fetchone()
        if channel == "email" and not email_rung_allowed(db, lead):
            continue  # quarantined address / disarmed lane: rung stays pending
        if is_escalation:
            # first IG contact for this lead: the opener law still binds (Q7)
            try:
                message = render_lane(db, channel, lead, now=now)
            except MessageHold:
                continue  # no blessed opener: skip, invent nothing (Q7)
        else:
            # ladder rung: the owner's 4-stage follow-up set (owner paste
            # 2026-10-30 evening) — stage by touch; only a fill stage with
            # a blank lead name holds (never invent a restaurant)
            try:
                message = render_followup(lead, int(row["touch"]))
            except MessageHold:
                continue  # rung stays pending until the name exists
        # THE WALL also judges ladder rungs (owner order 2026-11-07: every
        # pre-dispatch body). Layer 1 only: these bytes are owner-pinned, and
        # the model's taste must never retire the owner's own sentence. A
        # veto leaves the row pending and the lead untouched.
        from outreach.proofread import screen

        verdict = screen(
            db,
            message.text,
            lead_id=int(row["lead_id"]),
            variant=getattr(message, "variant", None) or channel,
            dish=(lead["extracted_dish"] or "").strip() or None,
            now=now,
        )
        if not verdict.ok:
            continue  # held: reason in proofread_log + digest
        started = _disp._session_clock()  # session entry: the gap below
        # counts from the COMMIT instant (module ref so the test seam patches)
        try:
            outcome = sender(db, row["lead_id"], now=now, message=message)
        except Exception as exc:  # noqa: BLE001 — same containment law as
            # dispatch (2026-10-29 IG crash class): one bad transport must
            # never brick the ladder pass — record, skip, keep walking
            from outreach.scheduler import _record_sender_error

            _record_sender_error(db, channel, row["lead_id"], exc, now)
            continue  # row stays pending: retried on a later pass
        if getattr(outcome, "state", "") != "sent":
            continue  # transport failure → unchanged, retried later

        with db.conn:
            # 2026-11-13 (audit F4, found live): the ladder wrote its own
            # message_log row and never moved the account's health clock, so
            # two rungs sent and `accounts.last_ok_at` stayed NULL. One helper
            # now stamps both send paths, and the rung row names its account.
            account_id = _disp.stamp_account_health(db, row["lead_id"], now)
            db.conn.execute(
                "INSERT INTO message_log(lead_id, channel, template_hash,"
                " dish, screenshot, body, sent_at, account_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["lead_id"],
                    channel,
                    message.template_hash,
                    lead["extracted_dish"],
                    # ADR-15 (Phase-1 audit 2026-11-03): link the evidence
                    # the sender already captured — the old hardcoded None
                    # left 13 live rung rows unlinked on 10-30. Email
                    # outcomes carry no screenshot attr -> None (by
                    # construction, ADR-5).
                    (
                        str(outcome.screenshot)
                        if getattr(outcome, "screenshot", None)
                        else None
                    ),
                    message.text,  # 2026-11-04: keep the text, not just its hash
                    _disp.utc_stamp(now),  # same clock the ladder decided with
                    account_id or "",
                ),
            )
            db.conn.execute(
                "UPDATE followups SET status='done' WHERE id=?", (row["fid"],)
            )
        register_send(db, channel, now)
        # owner law (f): pace the NEXT send from the session's real commit
        # instant. LIVE LESSONS 2026-11-03: arming from the tick's `now`
        # let a cold boot (>= the drawn gap) eat the whole break — two live
        # rungs landed 58.7 s apart on 10-30. Same law as the senders.
        arm_send_gap(db, channel, _disp.commit_instant(now, started))
        if row["state"] == "SENT" and not is_escalation:
            # first touch: the machine steps once (escalation rows always
            # find the lead already in FOLLOWUP)
            from outreach.machine import transition

            transition(
                db, row["lead_id"], "FOLLOWUP", actor="followup-ladder"
            )
        if not is_escalation:
            # next rung, calendar-anchored (skipping any lapsed one); None
            # past the two-touch cap means the ladder is finished
            anchor = _anchor(db, row["lead_id"], now)
            nxt = _next_rung_after(row["touch"], anchor, now)
            if nxt is not None:
                touch, due = nxt
                with db.conn:
                    db.conn.execute(
                        "INSERT INTO followups(lead_id, touch, due_at, status)"
                        " VALUES (?, ?, ?, 'pending')",
                        (row["lead_id"], touch, _iso(due)),
                    )
            if row["touch"] == 2:
                # day-7 miss on WhatsApp: arm the one-shot IG escalation
                _arm_ig_escalation(db, row["lead_id"], now)
        processed.append(row["lead_id"])
    return processed


def backfill_ladder(db: "Database", now: datetime) -> int:
    """Arm the missing rung for every SENT/FOLLOWUP lead (B24 backfill).

    Idempotent: leads that already have a pending row, leads outside
    SENT/FOLLOWUP (replied, dead, rejected...) and leads whose pending row
    was cancelled by a reply are left untouched. A lead whose day-7 touch
    already completed (pre-amendment history) gets both its next rung and
    the day-7 IG escalation when eligible.
    """
    armed = 0
    rows = db.conn.execute(
        "SELECT id, state FROM leads WHERE state IN ('SENT', 'FOLLOWUP')"
        " ORDER BY id"
    ).fetchall()
    for row in rows:
        lead_id = int(row["id"])
        pending = db.conn.execute(
            "SELECT 1 FROM followups WHERE lead_id=? AND status='pending'"
            " LIMIT 1",
            (lead_id,),
        ).fetchone()
        if pending:
            continue  # an armed ladder owns this lead already
        last = db.conn.execute(
            "SELECT MAX(touch) AS k FROM followups"
            " WHERE lead_id=? AND channel IS NULL AND status='done'",
            (lead_id,),
        ).fetchone()
        last_touch = int(last["k"] or 0)
        anchor = _anchor(db, lead_id, now)
        nxt = _next_rung_after(last_touch, anchor, now)
        if nxt is not None:
            touch, due = nxt
            with db.conn:
                db.conn.execute(
                    "INSERT INTO followups(lead_id, touch, due_at, status)"
                    " VALUES (?, ?, ?, 'pending')",
                    (lead_id, touch, _iso(due)),
                )
            armed += 1
        if last_touch >= 2:
            # day-7 already completed before the amendment: the IG
            # escalation is due now (arms once, only when eligible)
            _arm_ig_escalation(db, lead_id, now)
    return armed
