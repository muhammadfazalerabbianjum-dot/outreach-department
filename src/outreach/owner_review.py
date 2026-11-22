"""Owner final-review file (B24, owner directive 2026-10-29).

"if whatsapp and IG both dead you put them in a owner final review md file"

Rule as locked with the owner:
  - WhatsApp dead (state WA_DEAD) AND no usable Instagram handle
    (empty, or `ig_class = 'DEAD'` — a dead link is no link, B20), or
  - Instagram dead (state IG_DEAD) AND no WhatsApp number to fall back on.

Scope notes (reversible, logged in DECISIONS.md):
  - UNREACHABLE leads are NOT listed: nothing died. Careful with the
    wording (corrected 2026-11-16): UNREACHABLE means no LAWFUL channel at
    routing time, not no contact data — 842 of the 867 live rows carry a
    phone and 286 carry a usable handle. `router.reroute_stranded` revives
    those on the engine's boot pass; the rest are parked by the lane law.
  - WA_DEAD with a live Instagram is NOT listed either (it is not both
    dead); the pre-day-7 death gap is logged in QUESTIONS.md.

The file is regenerated wholesale on every pass and written atomically
(temp + os.replace): same `now` in, byte-identical file out — no append
drift, no duplicates, safe to re-run any number of times.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from outreach.store import Database

_HEADER = "# OWNER REVIEW — final review for leads where BOTH channels are dead"
_RULE = (
    "Rule: WhatsApp dead + no usable Instagram handle, or Instagram dead +"
    " no WhatsApp number (ADR-10 amendment, owner directive 2026-10-29)."
)
_NOTE = (
    "UNREACHABLE (no lawful channel at routing time) is not listed — nothing"
    " died; the engine re-checks these on every boot."
    " The system will never message these again: decide manually — find a"
    " new handle, re-route, or drop them."
)

_QUERY = (
    "SELECT id, name, city, region, state, phone, instagram, ig_class"
    " FROM leads WHERE state IN ('WA_DEAD', 'IG_DEAD') ORDER BY id"
)


def _reason(row: "object") -> str | None:
    """Both-channels-dead verdict, or None when a channel survives."""
    has_phone = bool(str(row["phone"] or "").strip())
    has_ig = bool(str(row["instagram"] or "").strip()) and (
        row["ig_class"] != "DEAD"
    )
    if row["state"] == "WA_DEAD" and not has_ig:
        return "WhatsApp dead + no usable Instagram"
    if row["state"] == "IG_DEAD" and not has_phone:
        return "Instagram dead + no WhatsApp number"
    return None


def write_owner_review(
    db: "Database", path: str | Path, now: datetime
) -> int:
    """Regenerate the review file; return the number of leads listed."""
    rows_out: list[str] = []
    for row in db.conn.execute(_QUERY):
        reason = _reason(row)
        if reason is None:
            continue
        last = db.conn.execute(
            "SELECT MAX(sent_at) FROM message_log WHERE lead_id=?",
            (row["id"],),
        ).fetchone()[0]
        rows_out.append(
            "| {id} | {name} | {city} | {region} | {state} | {reason} |"
            " {phone} | {instagram} | {last} |".format(
                id=row["id"],
                name=(row["name"] or "—").replace("|", "/"),
                city=row["city"] or "—",
                region=row["region"] or "—",
                state=row["state"],
                reason=reason,
                phone=row["phone"] or "—",
                instagram=row["instagram"] or "—",
                last=last or "—",
            )
        )

    lines = [
        _HEADER,
        "",
        f"Generated: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f" · {len(rows_out)} lead{'s' if len(rows_out) != 1 else ''}",
        "",
        _RULE,
        _NOTE,
        "",
    ]
    if rows_out:
        lines += [
            "| id | name | city | region | state | why | phone | instagram |"
            " last message |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        lines += rows_out
    else:
        lines += ["## None — no lead currently has both channels dead."]
    lines.append("")
    body = "\n".join(lines)

    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, target)  # atomic: readers never see a half file
    return len(rows_out)
