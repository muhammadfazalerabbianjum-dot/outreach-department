"""O1 (2026-11-13): the engagement stage — the caller the machinery never had.

`engagement.py`, `warmup.py`, `warming.py` and `comments.py` were built, tested
and **unwired**: `grep -rn "post_comment(\\|warm_up(\\|warm_account(" src/` found
no caller, so no account ever warmed up, and `warmup_config` / `warming_config`
sat NULL forever. This module is the missing caller and the only one.

Laws it enforces:

1. **Disarmed means silent.** `config.engage_enabled` seeds `False` and nothing
   in this module runs without it. Owner arming is the switch, never a default.
2. **One act per account per tick, hard.** `engagement.can_engage` decides
   (status, daily cap, 90 s spacing) BEFORE a page is touched, so a refusal
   costs no browser work and can never half-log an act.
3. **Window-gated.** An act happens only inside the lead's ADR-9 window: an
   account that comments at 03:00 local is not a local customer.
4. **An act is evidence.** Each act lands in `engagement_log` with its
   ADR-15 screenshot (the called modules already own that write; this module
   never writes an act row itself).
5. **Optional stage, never a blocker.** Nothing here may stop a send: the
   scheduler wraps the call, records one throttled row per failure class, and
   the cycle continues to dispatch.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from outreach.store import get_config, set_config

if TYPE_CHECKING:
    from outreach.store import Database

ENGAGE_KEY = "engage_enabled"
#: Owner order 2026-11-15: the WHOLE engagement lane (comments, story replies,
#: warm-ups, ambient scrolling) runs at night only. Local wall clock, and the
#: band may cross midnight — start > end means "overnight".
ENGAGE_WINDOW_KEY = "engage_window"
DEFAULT_ENGAGE_WINDOW = {"start": "20:55", "end": "10:00"}
#: Owner order 2026-11-15: a manual switch for "PC is needed right now". Kept
#: separate from the send queue so pausing engagement never stops a send.
ENGAGE_PAUSE_KEY = "engage_paused"
_MAX_COMMENT_CANDIDATES = 3  # bounded page visits per account per tick
_LOG_EVERY_S = 1800.0  # one refusal row per (account, reason) per 30 min


@dataclass(frozen=True)
class EngageReport:
    """What one engagement stage did. `acted` is the only success signal."""

    armed: bool
    acts: tuple[str, ...] = ()
    refusals: tuple[str, ...] = ()

    @property
    def acted(self) -> bool:
        return bool(self.acts)


def ensure_engage_config(db: "Database") -> None:
    """Seed the arming switch OFF and the night window once (idempotent)."""
    if get_config(db, ENGAGE_KEY) is None:
        set_config(db, ENGAGE_KEY, False)
    if get_config(db, ENGAGE_WINDOW_KEY) is None:
        set_config(db, ENGAGE_WINDOW_KEY, dict(DEFAULT_ENGAGE_WINDOW))


def engage_window(db: "Database") -> dict[str, str]:
    """The configured band, falling back to the owner's 20:55-10:00."""
    stored = get_config(db, ENGAGE_WINDOW_KEY)
    rule = stored if isinstance(stored, dict) else dict(DEFAULT_ENGAGE_WINDOW)
    return {
        "start": str(rule.get("start") or DEFAULT_ENGAGE_WINDOW["start"]),
        "end": str(rule.get("end") or DEFAULT_ENGAGE_WINDOW["end"]),
    }


def _minutes_of_day(value: str) -> int:
    hour, _, minute = str(value).partition(":")
    return int(hour) * 60 + int(minute or 0)


def engagement_window_open(db: "Database", now: datetime) -> bool:
    """Is the engagement lane allowed to act at this LOCAL instant?

    A band where start > end crosses midnight (20:55 -> 10:00 is the owner's).
    A malformed band refuses (fail-closed): a broken row must not become
    "always on".
    """
    rule = engage_window(db)
    try:
        start = _minutes_of_day(rule["start"])
        end = _minutes_of_day(rule["end"])
    except (TypeError, ValueError):
        return False
    local = now.astimezone() if now.tzinfo is not None else now
    minute_of_day = local.hour * 60 + local.minute
    if start <= end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end


def set_engage_paused(db: "Database", *, reason: str, now: datetime) -> None:
    """Owner pause: stop commenting/scrolling now, for any reason."""
    set_config(
        db,
        ENGAGE_PAUSE_KEY,
        {
            "reason": str(reason or "paused by owner").strip(),
            "since": now.isoformat(),
        },
    )


def clear_engage_paused(db: "Database") -> None:
    set_config(db, ENGAGE_PAUSE_KEY, None)


def engage_paused(db: "Database") -> str | None:
    """The owner's pause reason, or None when the lane is not paused."""
    stored = get_config(db, ENGAGE_PAUSE_KEY)
    if not stored:
        return None
    if isinstance(stored, dict):
        return str(stored.get("reason") or "paused by owner")
    return str(stored)


def _log_day_once(db: "Database", key: str, body: str, now: datetime) -> None:
    """One digest line per key per PKT day — a shut lane must not narrate itself.

    The stage runs every cycle; the window is shut for ~14 hours a day and a
    pause can last a week, so a 30-minute throttle would still be ~28 rows a
    day. Once a day is the whole point of the row: "the lane is idle, here is
    why".
    """
    from outreach.caps import pkt_date

    stamped = f"engage_logged:{key}:{pkt_date(now)}"
    if get_config(db, stamped):
        return
    with db.conn:
        db.conn.execute("INSERT INTO digest(body) VALUES (?)", (body,))
    set_config(db, stamped, True)


def engage_armed(db: "Database") -> bool:
    return bool(get_config(db, ENGAGE_KEY))


def _log_once(db: "Database", key: str, body: str, now: datetime) -> None:
    """One digest row per key per 30 min — refusals must not flood the digest."""
    stored = get_config(db, f"engage_log_at:{key}")
    if stored:
        try:
            last = datetime.fromisoformat(str(stored))
        except ValueError:
            last = None
        if last is not None and (now - last).total_seconds() < _LOG_EVERY_S:
            return
    with db.conn:
        db.conn.execute("INSERT INTO digest(body) VALUES (?)", (body,))
    set_config(db, f"engage_log_at:{key}", now.isoformat())


def _log(db: "Database", body: str) -> None:
    with db.conn:
        db.conn.execute("INSERT INTO digest(body) VALUES (?)", (body,))


def _comment_candidates(
    db: "Database", now: datetime, account_id: str
) -> list[tuple[int, str, str | None]]:
    """ROUTED IG leads with a usable handle and no logged comment yet.

    Per LEAD, not per account: the owner's law is "no comment, no send", so one
    logged comment on the lead's own post satisfies it — the same lead must not
    be re-commented by a second account.
    """
    from outreach.accounts import assigned_account
    from outreach.dispatcher import in_window
    from outreach.ig_sender import ig_handle
    from outreach.warmup import comment_state

    rows = db.conn.execute(
        "SELECT l.id, l.instagram, l.region FROM leads l"
        " WHERE l.state = 'ROUTED'"
        " AND l.instagram IS NOT NULL AND TRIM(l.instagram) <> ''"
        " ORDER BY l.id LIMIT ?",
        (_MAX_COMMENT_CANDIDATES * 8,),
    ).fetchall()
    found: list[tuple[int, str, str | None]] = []
    for row in rows:
        # O3: a lead is a candidate when it has no comment yet, or when its
        # comment went stale (past the 48 h expiry) and must be refreshed
        # before any DM. A `waiting` or `ready` lead is never re-commented.
        if comment_state(db, int(row["id"]), now) not in ("none", "stale"):
            continue
        if not row["region"] or not in_window(db, str(row["region"]), now):
            continue
        owner = assigned_account(db, int(row["id"]))
        if owner is not None and owner != account_id:
            continue  # L1: another account owns this lead
        try:
            handle = ig_handle(row["instagram"])
        except ValueError:
            continue
        found.append((int(row["id"]), handle, row["region"]))
        if len(found) >= _MAX_COMMENT_CANDIDATES:
            break
    return found


#: Instrumentation (2026-11-17): six profiles read "no post" twice in one
#: evening, 4-14 s apart, and the digest could not say whether the grid was
#: really empty, the selector had drifted, or the page was a wall. One bounded
#: read of what the browser actually had settles it with numbers.
_GRID_SNAPSHOT_JS = """() => ({
  url: location.href,
  articles: document.querySelectorAll('article').length,
  anchors: document.querySelectorAll('article a[href]').length,
  body_len: ((document.body && document.body.innerText) || '').length,
})"""


def _grid_snapshot(page: Any) -> dict[str, Any]:
    """What the browser sees at the moment the grid read came back empty."""
    try:
        value = page.evaluate(_GRID_SNAPSHOT_JS)
    except Exception:  # noqa: BLE001 - a diagnostic never breaks the lane
        return {}
    return value if isinstance(value, dict) else {}


def _session_flags(page: Any) -> dict[str, Any]:
    """The transport/state flags the send path already uses (login, dead, ban)."""
    from outreach.ig_sender import _probe_state

    try:
        value = _probe_state(page)
    except Exception:  # noqa: BLE001 - a diagnostic never breaks the lane
        return {}
    return value if isinstance(value, dict) else {}


def _grid_detail(snapshot: dict[str, Any], flags: dict[str, Any]) -> str:
    """One terminal-friendly line: where we were, what was there, who we were."""
    if snapshot:
        parts = [
            f"url={snapshot.get('url', '?')}",
            f"articles={snapshot.get('articles')}",
            f"anchors={snapshot.get('anchors')}",
            f"body_len={snapshot.get('body_len')}",
        ]
    else:
        parts = ["snapshot=unavailable"]
    if flags:
        parts += [
            f"login={flags.get('login')}",
            f"dead={flags.get('dead')}",
            f"ban={flags.get('ban')}",
        ]
    return " ".join(parts)


def _comment_one(
    db: "Database",
    page: Any,
    account_id: str,
    lead_id: int,
    handle: str,
    *,
    now: datetime,
    evidence_dir: str | Path,
    transport: Any,
) -> str | None:
    """Comment on the lead's latest post. Returns the act label or None.

    None means "not this lead": no post, no caption, or the draft fell under
    the confidence gate (which queues it for the owner instead of posting).
    A raised `EngagementRefused` is the account's problem, and the caller
    stops the account.
    """
    from outreach import comments, warmup
    from outreach.followers import read_followers, stamp_followers

    try:
        posts = warmup.discover_posts(page, handle, limit=1)
    except Exception as exc:  # noqa: BLE001 — a surface fault is not an act
        raise RuntimeError(f"post discovery failed for {handle}: {exc}") from exc
    # O7 (2026-11-14): the profile is already open, so read the follower count
    # here and put it on the lead. `unknown` is a legal answer and refuses
    # nothing; it is never a guess. Both readers ignore a drifted surface.
    try:
        stamp_followers(db, lead_id, read_followers(page), now)
    except Exception as exc:  # noqa: BLE001 — a count is not worth an act
        _log_once(
            db, f"{account_id}:followers",
            f"ENGAGE SKIPPED [followers_unreadable] lead {lead_id}: {exc}", now,
        )
    if not posts:
        snapshot = _grid_snapshot(page)
        flags = _session_flags(page)
        detail = _grid_detail(snapshot, flags)
        if flags.get("login") or flags.get("dead") or flags.get("ban"):
            # A wall is not a verdict about the restaurant: say "session", park
            # nothing, and leave the lead exactly as it was.
            _log_once(
                db,
                f"{account_id}:session",
                f"ENGAGE SKIPPED [session] lead {lead_id}: the profile did not"
                f" render for us - no comment attempted | {detail}",
                now,
            )
            return None
        _log_day_once(
            db,
            f"no_post:{handle}",
            f"COMMENT SKIPPED [no_post] {handle}: no post or reel on the profile"
            f" - nothing to comment on, so this lead cannot be warmed | {detail}",
            now,
        )
        return None
    post = comments.read_post(page, posts[0])
    if not comments._context(post):
        _log_once(
            db,
            f"{account_id}:no_context",
            f"ENGAGE SKIPPED [no_context] {handle}: post has no caption",
            now,
        )
        return None
    outcome = comments.post_comment(
        db,
        page,
        account_id,
        lead_id,
        post,
        now=now,
        evidence_dir=evidence_dir,
        transport=transport,
    )
    if outcome.decision == "posted":
        _log(db, f"ENGAGE [comment] lead {lead_id} by {account_id}: posted")
        return f"comment:lead {lead_id}"
    _log_once(
        db,
        f"{account_id}:queued",
        f"ENGAGE SKIPPED [queued_review] lead {lead_id}: {outcome.reason}",
        now,
    )
    return None


#: One ambient session per cycle, sized to the engage stage's 300 s budget
#: (4 minutes = 240 s leaves room for the act that ran before it).
AMBIENT_CYCLE_MINUTES = 4


def _story_one(
    db: "Database",
    page: Any,
    account_id: str,
    lead_id: int,
    handle: str,
    *,
    now: datetime,
    evidence_dir: Path,
    transport: Any,
) -> str | None:
    """O4: reply to this lead's live story as the warm first touch.

    The reply text is the SAME comment drafter (confidence gate, copy vet, owner
    queue), fed with what the story actually shows. No readable text means no
    reply — the lane falls through to a comment on a real post.
    """
    from functools import partial

    from outreach import comments, story

    if not story.story_enabled(db):
        return None
    outcome = story.reply_to_story(
        page,
        handle,
        draft=partial(comments.draft_comment, transport=transport),
        now=now,
        evidence_dir=evidence_dir,
    )
    if outcome.decided != "posted":
        _log_once(
            db,
            f"{account_id}:story:{outcome.decided}",
            f"STORY SKIPPED [{outcome.decided}] lead {lead_id}: {outcome.reason}",
            now,
        )
        return None
    story.record_story_reply(
        db,
        account_id=account_id,
        lead_id=lead_id,
        reply=outcome.reply,
        handle=handle,
        now=now,
    )
    _log(db, f"ENGAGE [story_reply:lead {lead_id}] by {account_id}")
    return f"story_reply:lead {lead_id}"


def run_engage_stage(
    db: "Database",
    now: datetime,
    *,
    page: Any = None,
    page_factory: Callable[[str], Any] | None = None,
    evidence_dir: str | Path | None = None,
    transport: Any = None,
    sleep_fn: Callable[[float], None] | None = None,
    root: str | Path | None = None,
) -> EngageReport:
    """Run at most one engagement act per eligible IG account.

    `page` is a live page shared by every account (tests, single account);
    `page_factory(account_id)` returns a context manager yielding THAT
    account's own session (the engine). Login isolation is not negotiable: a
    comment typed by `ig2` must never be typed inside `ig1`'s profile
    (ADR-13), so the factory is per account and the browser opens once per
    acting account, not per act.
    """
    ensure_engage_config(db)
    from outreach import ambient

    armed = engage_armed(db)
    ambient_on = bool(ambient.config(db)["enabled"])
    # Two owner switches BEFORE any browser work (2026-11-15): the night window
    # and the manual pause. Both refuse the whole lane, and neither touches the
    # send queue.
    paused = engage_paused(db)
    pause_note: str | None = None
    if paused:
        # Owner decision 2026-11-19: a pause stops every PUBLIC act (comments,
        # story replies, DM warm-up) but no longer silences ambient scrolling -
        # the accounts must stay warm through an industry switch. With ambient
        # switched off the old guarantee stands: a paused lane opens no browser.
        _log_day_once(
            db,
            f"owner_pause:{paused[:40]}",
            f"ENGAGE PAUSED by owner: {paused}"
            + (
                " - ambient only, no comments, no story replies, no DM warm-up"
                if ambient_on
                else ""
            )
            + " (resume with `command_center resume --engage`)",
            now,
        )
        if not ambient_on:
            return EngageReport(armed, refusals=("paused_by_owner",))
        armed = False  # the message lane stays shut; `_run_account` takes ambient
        pause_note = "paused_by_owner_ambient_only"
    if not engagement_window_open(db, now):
        rule = engage_window(db)
        _log_day_once(
            db,
            "window",
            f"ENGAGE WINDOW: closed (runs {rule['start']}-{rule['end']} local)"
            " - no comments, no scrolling",
            now,
        )
        return EngageReport(armed, refusals=("outside_engage_window",))
    if not armed and not ambient_on:
        return EngageReport(False, refusals=("not_armed",))
    # O8 (owner ruling 2026-11-14): ambient time has its OWN switch, because the
    # owner said yes to scrolling while the message lane stays disarmed. It never
    # sends a message and never comments, so a shut message lane must not stop it.

    from outreach.accounts import eligible_accounts
    from outreach.engagement import EngagementRefused, can_engage
    from outreach.scheduler import open_session_regions

    if not open_session_regions(db, now):
        # 2026-11-16: this refusal was silent. The comment lane looked dead for
        # two nights while it was only waiting for the owner's ADR-9 windows,
        # and every dispatch pass then parked the same leads [not_warmed_up].
        _log_day_once(
            db,
            "no_open_window",
            "ENGAGE WAITING [no_open_window]: comments only run inside an open"
            " session window (na 13:00-23:00, me 14:00-20:00 UTC)",
            now,
        )
        return EngageReport(armed, refusals=("no_open_window",))

    from outreach.ig_sender import EVIDENCE_DIR

    shots = Path(evidence_dir) if evidence_dir is not None else Path(EVIDENCE_DIR)
    from outreach.accounts import health_reason

    accounts = []
    blocked: list[str] = []
    for candidate in eligible_accounts(db, "ig"):
        # Live 2026-11-15: ig4 has no profile and ig2/ig3 are logged out, yet all
        # three were "eligible". A lane that cannot be signed in never opens a
        # browser here — it is named once and the fleet carries on.
        unhealthy = health_reason(db, str(candidate["id"]), root=root)
        if unhealthy is not None:
            health = "profile_missing" if "not on disk" in unhealthy else "no_session"
            blocked.append(f"{candidate['id']}:{health}")
            _log_once(
                db,
                f"{candidate['id']}:health",
                f"ENGAGE SKIPPED [{health}] {unhealthy}",
                now,
            )
            continue
        reason = can_engage(db, candidate["id"], now)
        if reason is None:
            accounts.append(candidate)
        else:
            blocked.append(f"{candidate['id']}:{reason}")
    if not accounts:
        return EngageReport(
            armed,
            refusals=(tuple(blocked) + ((pause_note,) if pause_note else ()))
            or ("no_account_can_act",),
        )

    acts: list[str] = []
    refusals: list[str] = blocked + ([pause_note] if pause_note else [])

    # O8 live fix 2026-11-14: three accounts each starting a fresh 5-minute
    # session put the engage stage at 31 minutes and tripped `STAGE OVERRUN`.
    # The stage budget is 300 s, so ambient gets one session's worth per CYCLE
    # across the whole fleet and the rest of the accounts wait for the next tick.
    cycle_ambient_minutes = [0]

    # O8b (owner request 2026-11-15): today's minutes are SPLIT across the lanes
    # that can act, unevenly, with a new split each day. Built once per cycle so
    # every account reads the same plan (and the owner sees it in the digest).
    ambient_plan: dict[str, int] = {}
    if ambient_on and accounts:
        ambient_plan = ambient.daily_split(db, now, [str(a["id"]) for a in accounts])

    def _run_account(account_id: str, target_page: Any) -> None:
        """One act for this account, in priority order, then stop.

        Comment first: that is the act the owner's law needs before any DM.
        Warm-up next (it is the same lead), standalone warming last (it is
        account health, not the funnel).
        """
        from outreach import warmup, warming

        def _run_ambient() -> str | None:
            """Spend this account's session on ambient time, out of one budget."""
            left = AMBIENT_CYCLE_MINUTES - cycle_ambient_minutes[0]
            if left < 1:
                return None  # this cycle's slice is spent; the next tick is soon
            try:
                line = ambient.run_session(
                    db, target_page, account_id, now=now, sleep=sleep_fn,
                    minutes=left, plan=ambient_plan,
                )
                if line is not None:
                    cycle_ambient_minutes[0] += left
                return line
            except Exception as exc:  # noqa: BLE001 — ambient is never a blocker
                refusals.append(f"{account_id}:ambient_error")
                _log_once(
                    db, f"{account_id}:ambient",
                    f"AMBIENT SKIPPED [{exc}] {account_id}", now,
                )
                return None

        try:
            done = None
            if not armed:
                # ambient-only pass: the message lane is shut, the scrolling the
                # owner approved is not. No act, no comment, no warm-up.
                if _run_ambient() is not None:
                    acts.append(f"ambient:{account_id}")
                return
            for lead_id, handle, _region in _comment_candidates(db, now, account_id):
                done = _comment_one(
                    db, target_page, account_id, lead_id, handle,
                    now=now, evidence_dir=shots, transport=transport,
                )
                if done:
                    break
            if done is None:
                # O4 (owner ruling 2026-11-14): a story reply is a warm touch,
                # so it is tried before any further browser work on this lead.
                for lead_id, handle, _region in _comment_candidates(
                    db, now, account_id
                ):
                    done = _story_one(
                        db, target_page, account_id, lead_id, handle,
                        now=now, evidence_dir=shots, transport=transport,
                    )
                    if done:
                        break
            if done is None and warmup.warmup_enabled(db):
                for lead_id, _handle, region in _comment_candidates(db, now, account_id):
                    outcomes = warmup.warm_up(
                        db, target_page, account_id, lead_id,
                        now=now, evidence_dir=shots, region=region,
                    )
                    if outcomes and outcomes[0].ok:
                        done = f"warmup:{outcomes[0].act}:lead {lead_id}"
                        _log(db, f"ENGAGE [{done}] by {account_id}")
                        break
            if done is None:
                outcomes = warming.warm_account(
                    db, target_page, account_id, now=now, evidence_dir=shots,
                )
                if outcomes and outcomes[0].ok:
                    done = f"warming:{outcomes[0].act}"
                    _log(db, f"ENGAGE [{done}] by {account_id}")
            # O8 (owner ruling 2026-11-14): ambient time is spent INSIDE the
            # account's own session, after the act that matters, and only out of
            # a budget the owner set (60-120 min/day, 5 follows a day).
            if _run_ambient() is not None:
                acts.append(f"ambient:{account_id}")
            if done is not None:
                acts.append(done)
            else:
                refusals.append(f"{account_id}:nothing_owed")
        except EngagementRefused as exc:
            refusals.append(f"{account_id}:refused")
            _log_once(
                db, f"{account_id}:refused",
                f"ENGAGE SKIPPED [{exc}] {account_id}", now,
            )

    if page is not None:
        for account in accounts:
            _run_account(str(account["id"]), page)
    elif page_factory is not None:
        # Live 2026-11-15: `ProfileMissing("profile 'Outreach-ig4' is not on disk")`
        # aborted the WHOLE stage, so an account the owner never logged in silenced
        # every other account. One account's missing session is a fact about that
        # account (the F2 lesson, one level up): name it, keep the fleet moving.
        from outreach.fleet import ProfileMissing

        for account in accounts:
            account_id = str(account["id"])
            try:
                with page_factory(account_id) as live:
                    _run_account(account_id, live)
            except ProfileMissing as exc:
                refusals.append(f"{account_id}:profile_missing")
                _log_once(
                    db,
                    f"{account_id}:profile",
                    f"ENGAGE SKIPPED [no browser profile] {account_id}: {exc}",
                    now,
                )
    else:
        raise ValueError("engage stage needs a page or a page_factory")

    return EngageReport(armed, tuple(acts), tuple(refusals))
