"""outreach.db — the department's single source of truth (blueprint).

SQLite per ADR-1/ADR-3: own DB, never the scraper's. Migrations are a
versioned, idempotent ledger applied inside one transaction each.
Append-only `state_history` is enforced by DB triggers, not convention.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

MIGRATION_TABLES = {
    "leads",
    "state_history",
    "daily_sends",
    "message_log",
    "followups",
    "digest",
    "config",
    "engagement_log",
    "comment_review",
    "warmups",
    "warming_progress",
    "account_health",
}

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

_MIGRATION_V1_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY,
        place_id TEXT NOT NULL UNIQUE,
        name TEXT,
        category TEXT,
        city TEXT,
        region TEXT,
        phone TEXT,
        instagram TEXT,
        email TEXT,
        data_json TEXT,
        icp_score REAL,
        qualification_json TEXT,
        reject_reason TEXT,
        state TEXT NOT NULL DEFAULT 'NEW',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS state_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id INTEGER NOT NULL REFERENCES leads(id),
        from_state TEXT NOT NULL,
        to_state TEXT NOT NULL,
        reason TEXT,
        actor TEXT NOT NULL DEFAULT 'system',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS state_history_no_update
    BEFORE UPDATE ON state_history
    BEGIN
        SELECT RAISE(ABORT, 'state_history is append-only');
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS state_history_no_delete
    BEFORE DELETE ON state_history
    BEGIN
        SELECT RAISE(ABORT, 'state_history is append-only');
    END
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_sends (
        channel TEXT NOT NULL CHECK (channel IN ('wa', 'ig', 'email')),
        date TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0 CHECK (count >= 0 AND count <= 20),
        PRIMARY KEY (channel, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS message_log (
        id INTEGER PRIMARY KEY,
        lead_id INTEGER NOT NULL REFERENCES leads(id),
        channel TEXT NOT NULL CHECK (channel IN ('wa', 'ig', 'email')),
        template_hash TEXT NOT NULL,
        dish TEXT,
        sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        screenshot TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS followups (
        id INTEGER PRIMARY KEY,
        lead_id INTEGER NOT NULL REFERENCES leads(id),
        touch INTEGER NOT NULL CHECK (touch IN (1, 2)),
        due_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'done', 'cancelled', 'expired')),
        cancelled_by_reply INTEGER NOT NULL DEFAULT 0
            CHECK (cancelled_by_reply IN (0, 1)),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS digest (
        id INTEGER PRIMARY KEY,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
)

MIGRATIONS: dict[int, tuple[str, tuple[str, ...]]] = {
    1: ("baseline tables + append-only triggers", _MIGRATION_V1_STATEMENTS),
    2: (
        "leads.previously_contacted (B3 importer: contacted-elsewhere signal, stays out of the active queue)",
        ("ALTER TABLE leads ADD COLUMN previously_contacted INTEGER NOT NULL DEFAULT 0",),
    ),
    3: (
        "leads.extracted_dish + leads.dish_json (B6 dish extractor verdict storage)",
        (
            "ALTER TABLE leads ADD COLUMN extracted_dish TEXT",
            "ALTER TABLE leads ADD COLUMN dish_json TEXT",
        ),
    ),
    4: (
        "leads.route_channel (B8 router: wa|ig|email decided per lead)",
        ("ALTER TABLE leads ADD COLUMN route_channel TEXT NOT NULL DEFAULT ''",),
    ),
    5: (
        "leads.run_id + phone_e164 + ig_class (B20 intake: scraper-run provenance,"
        " E.164-normalized phone, repaired IG link class)",
        (
            "ALTER TABLE leads ADD COLUMN run_id TEXT",
            "ALTER TABLE leads ADD COLUMN phone_e164 TEXT",
            "ALTER TABLE leads ADD COLUMN ig_class TEXT",
        ),
    ),
    6: (
        "followups rebuild (B24 / ADR-10 amendment: infinite ladder — touch no"
        " longer capped at 2, channel stamp for the day-7 IG escalation row)",
        (
            """
            CREATE TABLE followups_v6 (
                id INTEGER PRIMARY KEY,
                lead_id INTEGER NOT NULL REFERENCES leads(id),
                touch INTEGER NOT NULL CHECK (touch >= 1),
                channel TEXT,
                due_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'done', 'cancelled', 'expired')),
                cancelled_by_reply INTEGER NOT NULL DEFAULT 0
                    CHECK (cancelled_by_reply IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )
            """,
            """
            INSERT INTO followups_v6
                (id, lead_id, touch, channel, due_at, status,
                 cancelled_by_reply, created_at)
            SELECT id, lead_id, touch, NULL, due_at, status,
                   cancelled_by_reply, created_at
            FROM followups
            """,
            "DROP TABLE followups",
            "ALTER TABLE followups_v6 RENAME TO followups",
        ),
    ),
    7: (
        "leads.account_id (v1.1 seam, phase-2 item 2: per-account identity for"
        " multi-account lanes — additive only; v1.0 never reads or writes it)",
        ("ALTER TABLE leads ADD COLUMN account_id TEXT NOT NULL DEFAULT ''",),
    ),
    8: (
        "v1.1 account registry: accounts + lead_accounts (L1 assignment ledger)"
        " + message_log.account_id stamp (batches 0/2/3, additive)",
        (
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                channel TEXT NOT NULL CHECK (channel IN ('ig', 'wa')),
                profile_dir TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('WARMING', 'ACTIVE', 'HEALTHY',
                                      'HEALTH_ALARMED', 'HALTED')),
                ramp_week INTEGER NOT NULL DEFAULT 1 CHECK (ramp_week >= 1),
                warmup_start TEXT,
                window_offset_min INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                last_ok_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS lead_accounts (
                lead_id INTEGER PRIMARY KEY REFERENCES leads(id),
                account_id TEXT NOT NULL REFERENCES accounts(id),
                assigned_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )
            """,
            "ALTER TABLE message_log ADD COLUMN account_id TEXT NOT NULL DEFAULT ''",
            "CREATE INDEX IF NOT EXISTS idx_lead_accounts_account"
            " ON lead_accounts(account_id)",
        ),
    ),
    9: (
        "message_log.body (2026-11-04 audit trail: the rendered text the"
        " recipient actually read, not just the template hash)",
        ("ALTER TABLE message_log ADD COLUMN body TEXT",),
    ),
    10: (
        "engagement_log (v1.1 batch 5: one row per follow/like act, with its"
        " target, confidence and ADR-15 screenshot)",
        (
            """
            CREATE TABLE IF NOT EXISTS engagement_log (
                id INTEGER PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES accounts(id),
                lead_id INTEGER REFERENCES leads(id),
                act TEXT NOT NULL
                    CHECK (act IN ('follow', 'like', 'comment')),
                target TEXT NOT NULL DEFAULT '',
                confidence REAL,
                screenshot TEXT,
                acted_at TEXT NOT NULL,
                detail TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_engagement_account_time"
            " ON engagement_log(account_id, acted_at)",
        ),
    ),
    # 2026-11-16 (owner review): a park is a CLASS, not prose. The stranded-lead
    # revive used to substring-match the state_history reason text ("no viable
    # channel"), which any future wording change would have silently broken.
    # `leads.park_kind` is written by the code that parks, so the filter reads a
    # discrete field. The backfill below is the ONE structural mapping: every
    # currently parked row whose newest UNREACHABLE edge was written by the
    # router (= the lane law) is 'lane_law'; detector verdicts stay empty.
    20: (
        "leads.park_kind (parked rows carry a machine-readable class, so the"
        " revive filter never parses a reason string)",
        (
            "ALTER TABLE leads ADD COLUMN park_kind TEXT NOT NULL DEFAULT ''",
            """
            UPDATE leads SET park_kind = 'lane_law'
             WHERE state = 'UNREACHABLE'
               AND EXISTS (
                   SELECT 1 FROM state_history h
                    WHERE h.lead_id = leads.id
                      AND h.to_state = 'UNREACHABLE'
                      AND h.actor = 'router'
                      AND h.id = (SELECT MAX(id) FROM state_history
                                   WHERE lead_id = leads.id
                                     AND to_state = 'UNREACHABLE')
               )
            """,
        ),
    ),
    11: (
        "comment_review (v1.1 batch 6: every comment the confidence gate or"
        " the copy veto refused, kept for the owner instead of posted)",
        (
            """
            CREATE TABLE IF NOT EXISTS comment_review (
                id INTEGER PRIMARY KEY,
                account_id TEXT NOT NULL,
                lead_id INTEGER,
                post_url TEXT NOT NULL DEFAULT '',
                caption TEXT,
                draft TEXT NOT NULL,
                confidence REAL,
                why TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_comment_review_status"
            " ON comment_review(status, created_at)",
        ),
    ),
    12: (
        "warmups (v1.1 batch 7: one row per warmed lead; `dm_after` is the"
        " earliest legal first touch, 30 min after the last warm-up act)",
        (
            """
            CREATE TABLE IF NOT EXISTS warmups (
                lead_id INTEGER PRIMARY KEY REFERENCES leads(id),
                account_id TEXT NOT NULL REFERENCES accounts(id),
                follows INTEGER NOT NULL DEFAULT 0,
                likes INTEGER NOT NULL DEFAULT 0,
                warmed_at TEXT NOT NULL,
                dm_after TEXT NOT NULL,
                post_urls TEXT NOT NULL DEFAULT '[]'
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_warmups_dm_after"
            " ON warmups(dm_after)",
        ),
    ),
    13: (
        "warming_progress (v1.1 batch 8: per-account, per-handle progress of"
        " the standalone warming queue, so a follow is never repeated and a"
        " drained queue stops looking like work)",
        (
            """
            CREATE TABLE IF NOT EXISTS warming_progress (
                account_id TEXT NOT NULL REFERENCES accounts(id),
                handle TEXT NOT NULL,
                follows INTEGER NOT NULL DEFAULT 0,
                likes INTEGER NOT NULL DEFAULT 0,
                post_urls TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (account_id, handle)
            )
            """,
        ),
    ),
    14: (
        "account_health (v1.1 batch 9: every health signal an account emitted,"
        " and whether it halted that account or was only recorded)",
        (
            """
            CREATE TABLE IF NOT EXISTS account_health (
                id INTEGER PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES accounts(id),
                signal TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT 'recorded'
                    CHECK (action IN ('recorded', 'halted')),
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_account_health_account"
            " ON account_health(account_id, created_at)",
        ),
    ),
    15: (
        "owner_inbox (v1.1 batch 10.6: a reply is a live conversation handed to"
        " the owner, so it is stored until the owner acknowledges it - a toast"
        " or a digest line can be suppressed, this row cannot)",
        (
            """
            CREATE TABLE IF NOT EXISTS owner_inbox (
                id INTEGER PRIMARY KEY,
                lead_id INTEGER NOT NULL REFERENCES leads(id),
                channel TEXT NOT NULL CHECK (channel IN ('wa', 'ig', 'email')),
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                acknowledged_at TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_owner_inbox_unread"
            " ON owner_inbox(acknowledged_at, created_at)",
        ),
    ),
    16: (
        "proofread_log (the wall: every judged render, the hold queue for"
        " vetoed bodies, and the per-variant veto rate)",
        (
            """
            CREATE TABLE IF NOT EXISTS proofread_log (
                id INTEGER PRIMARY KEY,
                lead_id INTEGER NOT NULL REFERENCES leads(id),
                variant TEXT NOT NULL,
                dish TEXT,
                body TEXT NOT NULL,
                ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
                layers TEXT NOT NULL,
                reason TEXT NOT NULL,
                vetoed INTEGER NOT NULL CHECK (vetoed IN (0, 1)),
                created_at TEXT NOT NULL,
                cleared_at TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_proofread_open"
            " ON proofread_log(vetoed, cleared_at)",
            "CREATE INDEX IF NOT EXISTS idx_proofread_variant"
            " ON proofread_log(variant, created_at)",
        ),
    ),
    17: (
        "followups.expired_reason + owner_inbox conversation columns (2026-11-09:"
        " an expired rung must say why, and a reply row must carry the account, the"
        " handle and what the lead actually wrote - a notification string is not a"
        " conversation)",
        (
            "ALTER TABLE followups ADD COLUMN expired_reason TEXT",
            "ALTER TABLE owner_inbox ADD COLUMN reply_text TEXT",
            "ALTER TABLE owner_inbox ADD COLUMN account_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE owner_inbox ADD COLUMN sender_handle TEXT NOT NULL DEFAULT ''",
            "CREATE INDEX IF NOT EXISTS idx_followups_status_due"
            " ON followups(status, due_at)",
        ),
    ),
    18: (
        "proofread_log tone telemetry (owner ruling 2026-11-11: the model's"
        " tone view is ADVISORY - recorded for the owner, never a veto input,"
        " and never computed for owner copy)",
        (
            "ALTER TABLE proofread_log ADD COLUMN tone_score REAL",
            "ALTER TABLE proofread_log ADD COLUMN tone_note TEXT",
        ),
    ),
    # O4 (owner ruling 2026-11-14: a story reply counts as warm-up): the act
    # vocabulary gains `story_reply`. SQLite cannot ALTER a CHECK, so the table
    # is rebuilt in one transaction with its rows copied — no column changes and
    # nothing dropped from the old vocabulary.
    19: (
        "engagement_log.act gains 'story_reply' (O4: a story reply is a warm first touch)",
        (
            """
            CREATE TABLE engagement_log_story (
                id INTEGER PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES accounts(id),
                lead_id INTEGER REFERENCES leads(id),
                act TEXT NOT NULL
                    CHECK (act IN ('follow', 'like', 'comment', 'story_reply')),
                target TEXT NOT NULL DEFAULT '',
                confidence REAL,
                screenshot TEXT,
                acted_at TEXT NOT NULL,
                detail TEXT
            )
            """,
            "INSERT INTO engagement_log_story (id, account_id, lead_id, act,"
            " target, confidence, screenshot, acted_at, detail)"
            " SELECT id, account_id, lead_id, act, target, confidence, screenshot,"
            " acted_at, detail FROM engagement_log",
            "DROP TABLE engagement_log",
            "ALTER TABLE engagement_log_story RENAME TO engagement_log",
            "CREATE INDEX IF NOT EXISTS idx_engagement_account_time"
            " ON engagement_log(account_id, acted_at)",
        ),
    ),
}

SCHEMA_VERSION = max(MIGRATIONS)


class Database:
    """Connection wrapper: WAL-friendly local store + migration ledger."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        # 2026-11-13 (audit F6): WAL so a read-only dashboard can read while
        # the engine writes, and a busy_timeout so neither side fails instantly.
        # Both are idempotent; `delete` mode let a long dashboard read push the
        # engine's writer into SQLITE_BUSY, i.e. a skipped send cycle.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def migrate(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
        )
        applied = {
            row[0]
            for row in self.conn.execute("SELECT version FROM schema_migrations")
        }
        for version in sorted(MIGRATIONS):
            if version in applied:
                continue
            name, statements = MIGRATIONS[version]
            with self.conn:  # one atomic transaction per migration
                for statement in statements:
                    self.conn.execute(statement)
                self.conn.execute(
                    "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                    (version, name),
                )

    def create_lead(
        self,
        place_id: str,
        *,
        name: str | None = None,
        category: str | None = None,
        city: str | None = None,
        region: str | None = None,
        phone: str | None = None,
        instagram: str | None = None,
        email: str | None = None,
        data_json: str | None = None,
        previously_contacted: int = 0,
        run_id: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO leads(place_id, name, category, city, region, phone,"
            " instagram, email, data_json, previously_contacted, run_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                place_id, name, category, city, region, phone,
                instagram, email, data_json, previously_contacted, run_id,
            ),
        )
        return int(cur.lastrowid)


def set_config(db: Database, key: str, value: Any, *, atomic: bool = True) -> None:
    """Upsert one config row (JSON-serialized). Windows/caps/ramp live here.

    `atomic=False` runs the same upsert inside the CALLER's open transaction
    (e.g. commit_send's atomic block) instead of committing its own — an
    inner `with db.conn` there would land the ledger early.
    """
    statement = (
        "INSERT INTO config(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=(strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
    )
    if atomic:
        with db.conn:
            db.conn.execute(statement, (key, json.dumps(value)))
    else:
        db.conn.execute(statement, (key, json.dumps(value)))


def get_config(db: Database, key: str, default: Any = None) -> Any:
    row = db.conn.execute(
        "SELECT value FROM config WHERE key=?", (key,)
    ).fetchone()
    if row is None:
        return default
    return json.loads(row["value"])
