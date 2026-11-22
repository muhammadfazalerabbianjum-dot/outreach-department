"""Owner inbox (v1.1 batch 10.6): a reply stays visible until the owner says so.

The reply alert already goes two places - a digest row and a Windows balloon -
and both can vanish without anyone noticing: Focus Assist swallows balloons,
and a digest is only read by someone who already suspected something happened.
A reply is the one event where that silence has a cost, because it hands the
owner a live conversation.

So a reply writes one durable row here, on the engine's own clock, and the
only thing that clears it is an explicit acknowledgement. The toast stays a
nudge; this row is the record.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from outreach.store import Database


def _stamp(moment: datetime) -> str:
    at = moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(stamp: str) -> datetime:
    text = str(stamp).replace("Z", "+00:00")
    return datetime.fromisoformat(text)


#: The three channels the schema accepts, with the spellings callers use.
_CHANNELS = {"wa": "wa", "ig": "ig", "email": "email",
             "whatsapp": "wa", "instagram": "ig"}


def add_reply(
    db: "Database",
    *,
    lead_id: int,
    channel: str,
    body: str,
    now: datetime,
    reply_text: str | None = None,
    account_id: str = "",
    sender_handle: str = "",
) -> int:
    """Write the unacknowledged reply row; returns its id.

    Called only from `register_reply`'s exactly-once path, so a repeated
    detection cannot double-file the same conversation. The channel is
    canonicalised here because the table's CHECK is the real gate: callers
    pass "whatsapp" in places, and a rejected insert would lose the reply.
    """
    canonical = _CHANNELS.get(str(channel).strip().lower())
    if canonical is None:
        raise ValueError(f"unknown reply channel {channel!r}")
    with db.conn:
        cursor = db.conn.execute(
            "INSERT INTO owner_inbox(lead_id, channel, body, created_at,"
            " reply_text, account_id, sender_handle)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                lead_id,
                canonical,
                body,
                _stamp(now),
                reply_text,
                account_id or "",
                sender_handle or "",
            ),
        )
    return int(cursor.lastrowid)


def unread(db: "Database") -> list[dict]:
    """Every reply the owner has not acknowledged, oldest first."""
    return [
        dict(row)
        for row in db.conn.execute(
            "SELECT id, lead_id, channel, body, created_at, reply_text,"
            " account_id, sender_handle FROM owner_inbox"
            " WHERE acknowledged_at IS NULL ORDER BY created_at, id"
        )
    ]


def acknowledge(db: "Database", which: str = "all", *, now: datetime) -> int:
    """Mark replies read. `which` is "all" or a single inbox id.

    Keeps the rows: what was announced and when is history the owner may want,
    and deleting it would make a missed reply unauditable.
    """
    stamp = _stamp(now)
    with db.conn:
        if which == "all":
            cursor = db.conn.execute(
                "UPDATE owner_inbox SET acknowledged_at=?"
                " WHERE acknowledged_at IS NULL",
                (stamp,),
            )
        else:
            try:
                inbox_id = int(which)
            except (TypeError, ValueError):
                raise ValueError(f"unknown inbox target {which!r}") from None
            cursor = db.conn.execute(
                "UPDATE owner_inbox SET acknowledged_at=?"
                " WHERE id=? AND acknowledged_at IS NULL",
                (stamp, inbox_id),
            )
    return cursor.rowcount


def format_inbox(rows: list[dict], *, now: datetime) -> list[str]:
    """One line per unread reply, with how long it has been waiting."""
    if not rows:
        return ["no unread replies"]
    lines = [f"UNREAD REPLIES: {len(rows)}"]
    for row in rows:
        age = now - _parse(row["created_at"])
        hours = max(0, int(age.total_seconds() // 3600))
        lines.append(
            f"  [{row['id']}] {row['channel']:<5} lead {row['lead_id']:<5}"
            f" {hours}h ago  {row['body'][:90]}"
        )
    return lines


def has_captured_text(row: dict) -> bool:
    """Is this inbox row backed by words the lead actually wrote?

    Owner ruling 2026-11-11: a row with no quoted text must never read as
    "a restaurant replied". The probe now stores the bubble it saw; a legacy
    row that only carries our own alert string is not evidence of a reply.
    """
    return bool(str(row.get("reply_text") or "").strip())


def unverified(db: "Database") -> list[dict]:
    """Unacknowledged reply rows with nothing captured from the lead."""
    return [
        dict(row)
        for row in db.conn.execute(
            "SELECT id, lead_id, channel, created_at, body FROM owner_inbox"
            " WHERE acknowledged_at IS NULL"
            " AND TRIM(COALESCE(reply_text, '')) = '' ORDER BY id"
        )
    ]


def _history_row(db: "Database", lead_id: int, state: str, stamp: str, note: str) -> None:
    """Append a state_history row using whatever columns this DB actually has."""
    columns = [
        str(info[1])
        for info in db.conn.execute("PRAGMA table_info(state_history)")
    ]
    values: dict[str, object] = {
        "lead_id": lead_id,
        "state": state,
        "from_state": "REPLIED",
        "to_state": state,
        "actor": "inbox-audit",
        "reason": note,
        "created_at": stamp,
        "at": stamp,
    }
    usable = [column for column in columns if column in values]
    if not usable:
        return
    placeholders = ",".join("?" for _ in usable)
    db.conn.execute(
        f"INSERT INTO state_history({','.join(usable)}) VALUES ({placeholders})",
        tuple(values[column] for column in usable),
    )


def revert_unverified(
    db: "Database", *, now: datetime, prefix: str = "INBOX AUDIT REVERT"
) -> list[int]:
    """Undo a REPLIED that had no reply behind it (owner ruling 2026-11-11).

    REPLIED is terminal in the machine, so this is the documented repair path
    (same shape as the tier-contradiction repair): the lead returns to the
    last real pre-reply state, the inbox row STAYS as the audit record, and
    one digest row says exactly what was undone and why.
    """
    reverted: list[int] = []
    stamp = _stamp(now)
    for row in unverified(db):
        lead_id = int(row["lead_id"])
        lead = db.conn.execute(
            "SELECT state FROM leads WHERE id=?", (lead_id,)
        ).fetchone()
        if lead is None or lead["state"] != "REPLIED":
            continue  # nothing to repair, or a different path already did
        history_columns = {
            str(info[1]) for info in db.conn.execute("PRAGMA table_info(state_history)")
        }
        state_column = (
            "state" if "state" in history_columns
            else "to_state" if "to_state" in history_columns
            else ""
        )
        previous = None
        if state_column:
            previous = db.conn.execute(
                f"SELECT {state_column} AS state FROM state_history WHERE lead_id=?"
                f" AND {state_column} IN ('SENT', 'FOLLOWUP')"
                " ORDER BY id DESC LIMIT 1",
                (lead_id,),
            ).fetchone()
        back = str(previous["state"]) if previous else "SENT"
        note = (
            f"{prefix}: lead {lead_id} showed REPLIED with no captured reply"
            f" text (the last bubble was ours), reverted to {back};"
            f" inbox row {row['id']} kept for audit"
        )
        with db.conn:
            db.conn.execute("UPDATE leads SET state=? WHERE id=?", (back, lead_id))
            _history_row(db, lead_id, back, stamp, note)
            db.conn.execute(
                "INSERT INTO digest(body, created_at) VALUES (?, ?)", (note, stamp)
            )
        reverted.append(lead_id)
    return reverted


def revert_false_replies(
    db: "Database", *, now: datetime, prefix: str = "FALSE REPLY REPAIR"
) -> list[int]:
    """Undo a REPLIED whose newest bubble was OUR copy or a canned auto-reply.

    Live 2026-11-15: `owner_inbox` 16/17 held our own DM read back from two
    threads, and 15 held Instagram's "thanks for contacting us" autoresponder.
    All three had cancelled a ladder and handed the lead over as if a
    restaurant had answered. `REPLIED` is terminal in the machine, so this is
    the documented repair shape (`revert_unverified`): the lead returns to the
    last real pre-reply state, the inbox row STAYS as the audit record and is
    acknowledged (there is nothing for the owner to answer), and one digest row
    names exactly what was undone and why.
    """
    from outreach.replies import classify_newest, _ours_copy

    repaired: list[int] = []
    stamp = _stamp(now)
    rows = db.conn.execute(
        "SELECT * FROM owner_inbox WHERE acknowledged_at IS NULL ORDER BY id"
    ).fetchall()
    for row in rows:
        lead_id = int(row["lead_id"])
        text = str(row["reply_text"] or "")
        if not text.strip():
            continue  # an empty row is `revert_unverified`'s business
        lead = db.conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if lead is None or lead["state"] != "REPLIED":
            continue  # nothing to repair, or another path already did
        ours = ""
        try:
            ours = _ours_copy(db, str(row["channel"] or "ig"), lead, now=now)
        except Exception:  # noqa: BLE001 - an unrenderable family still judges text
            ours = ""
        verdict = classify_newest(text, ours, min_len=1)
        if verdict not in ("ours", "auto"):
            continue  # a human answer stays exactly where it is
        history_columns = {
            str(info[1]) for info in db.conn.execute("PRAGMA table_info(state_history)")
        }
        state_column = (
            "state" if "state" in history_columns
            else "to_state" if "to_state" in history_columns
            else ""
        )
        previous = None
        if state_column:
            previous = db.conn.execute(
                f"SELECT {state_column} AS state FROM state_history WHERE lead_id=?"
                f" AND {state_column} IN ('SENT', 'FOLLOWUP')"
                " ORDER BY id DESC LIMIT 1",
                (lead_id,),
            ).fetchone()
        back = str(previous["state"]) if previous else "SENT"
        why = "our own copy" if verdict == "ours" else "a canned auto-reply"
        note = (
            f"{prefix}: lead {lead_id} was REPLIED on {why}, not a reply;"
            f" reverted to {back}, inbox row {row['id']} kept for audit"
        )
        with db.conn:
            db.conn.execute("UPDATE leads SET state=? WHERE id=?", (back, lead_id))
            db.conn.execute(
                "UPDATE owner_inbox SET acknowledged_at=? WHERE id=?",
                (stamp, int(row["id"])),
            )
            _history_row(db, lead_id, back, stamp, note)
            db.conn.execute(
                "INSERT INTO digest(body, created_at) VALUES (?, ?)", (note, stamp)
            )
        repaired.append(lead_id)
    return repaired


def format_unverified(rows: list[dict]) -> list[str]:
    """What the owner sees for rows that are NOT replies (never a quote)."""
    return [
        f"  [{row['id']}] lead {row['lead_id']} {row['channel']:<5}"
        " UNVERIFIED - no text captured; not a reply"
        for row in rows
    ]
