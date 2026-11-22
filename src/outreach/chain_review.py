"""Chain-verdict contradiction repair (contamination hold 2026-11-03).

The scraper's data-quality audit found 382 leads excluded with
`skip_reason="AI chain"`; 38 of them carry tier1-tier4 — i.e. the tiering
considered them valid prospects and an AI chain verdict silently deleted
them (sampled names: Peking Duck, El Taco De Oro, Shawarma King). The
orchestrator's ruling: **tier wins for now**, restore them to the pool,
route them normally, and TAG them for human review — never silently drop,
never silently include.

A genuine `is_chain=True` flag still wins (the flag is scraped evidence, the
verdict is inference); only flag-less, tier-valid rejects are restored.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from outreach.machine import transition

if TYPE_CHECKING:
    from outreach.store import Database

ACTOR = "chain_review"
TAG = "chain_verdict_contradiction"
_REASON = (
    "chain verdict contradicted by tier evidence (tier wins pending human"
    " review) — restored for outreach, tagged needs_review"
)


def _payload(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}") or {}
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def restore_tier_valid_chain_rejects(
    db: "Database", *, now: datetime | None = None
) -> list[int]:
    """REJECTED(chain) + tier evidence + no scraped chain flag -> QUALIFIED.

    Idempotent: a restored lead is QUALIFIED, so a second pass finds nothing.
    Returns the ids restored (patchable, deterministic, id-ordered).
    """
    rows = db.conn.execute(
        "SELECT id, reject_reason, data_json FROM leads"
        " WHERE state='REJECTED' AND reject_reason='chain' ORDER BY id"
    ).fetchall()
    restored: list[int] = []
    for row in rows:
        payload = _payload(row["data_json"])
        if payload.get("tier") is None:
            continue  # no tier evidence: the AI verdict stands
        if payload.get("is_chain") is True:
            continue  # scraped flag outranks the verdict: a real chain
        payload["needs_review"] = TAG
        with db.conn:
            db.conn.execute(
                "UPDATE leads SET data_json=? WHERE id=?",
                (json.dumps(payload), int(row["id"])),
            )
        transition(
            db, int(row["id"]), "QUALIFIED", reason=_REASON, actor=ACTOR
        )
        restored.append(int(row["id"]))
    return restored
