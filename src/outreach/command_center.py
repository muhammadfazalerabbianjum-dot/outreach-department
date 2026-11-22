"""Command center (B16): THE one interface to the engine.

Everything reads the DB; the only writes are the engine pieces already
gated (pause/resume flip the queue row, `run` executes one scheduler
tick through the real sender maps). All three channels are wired: the
WA/IG entries are profile-owning adapters (they open the dedicated
Outreach profile per dispatch — ADR-13 — while email talks SMTP).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone

from outreach.caps import current_cap, pkt_date
from outreach.email_sender import resend_email, send_email_message
from outreach.ig_sender import resend_ig_via_profile, send_ig_via_profile
from outreach.scheduler import run_engine_tick
from outreach.store import Database, get_config, set_config
from outreach.wa_sender import resend_wa_via_profile, send_wa_via_profile

def _console_safe(line: str, *, encoding: str | None = None) -> str:
    """Owner copy carries emoji; this console is cp1252 and cannot print them.

    Replace only the characters the terminal cannot encode. The pinned copy
    stays byte-exact in `variants.PS_ROTATION`; this is display only, so a
    preview never dies on a character the owner typed on purpose.
    """
    target = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        line.encode(target)
    except UnicodeEncodeError:
        return line.encode(target, "replace").decode(target, "replace")
    return line


def _echo(line: object = "") -> None:
    """Print one line the owner's console can actually render (cp1252).

    Live 2026-11-15: `command_center plan` died with
    `UnicodeEncodeError: 'charmap' codec can't encode character '😄'`
    because the rendered copy carries the pinned emoji and `<stdout>` is cp1252.
    One choke point, because every command prints through here — `holds` had its
    own guard and `plan`/`alerts` did not.
    """
    print(_console_safe(str(line)))


real_senders = {
    "wa": send_wa_via_profile,
    "ig": send_ig_via_profile,
    "email": send_email_message,
}
real_resenders = {
    "wa": resend_wa_via_profile,
    "ig": resend_ig_via_profile,
    "email": resend_email,
}


def build_status(db: "Database", now: datetime) -> dict:
    """The digest: one honest dict of queue, caps and every gate."""
    counts = {
        row["state"]: row["n"]
        for row in db.conn.execute(
            "SELECT state, COUNT(*) AS n FROM leads GROUP BY state"
        )
    }
    caps = {}
    for channel in ("wa", "ig", "email"):
        day = pkt_date(now)
        row = db.conn.execute(
            "SELECT count FROM daily_sends WHERE channel=? AND date=?",
            (channel, day),
        ).fetchone()
        try:
            cap = current_cap(db, channel, now)
        except RuntimeError:
            cap = None  # a cold DB: the engine seeds caps on its next run
        caps[channel] = {
            "used": row[0] if row else 0,
            "cap": cap,
        }
    alerts = [
        row[0]
        for row in db.conn.execute(
            "SELECT body FROM digest ORDER BY id DESC LIMIT 10"
        )
    ]
    return {
        "now": now.isoformat(),
        "queue_status": get_config(db, "queue_status") or "LIVE",
        "template_status": get_config(db, "template_status"),
        "email_enabled": bool(get_config(db, "email_enabled")),
        "ig_halted": bool(get_config(db, "ig_halted")),
        "counts": counts,
        "ready_by_region": _dispatch_ready(db),
        "ingested_runs": get_config(db, "ingested_runs") or {},
        "caps": caps,
        "windows": get_config(db, "session_windows"),
        "engagement": _engagement_state(db, now),
        "alerts": alerts,
    }


def _engagement_state(db: "Database", now: datetime) -> dict:
    """The engagement lane at a glance: armed, inside its window, or paused."""
    from outreach.engage_stage import (
        engage_armed,
        engage_paused,
        engage_window,
        engagement_window_open,
    )

    rule = engage_window(db)
    return {
        "armed": engage_armed(db),
        "window": f"{rule['start']}-{rule['end']} local",
        "window_open": engagement_window_open(db, now),
        "paused": engage_paused(db),
    }


def _dispatch_ready(db: "Database") -> dict:
    from outreach.intake import dispatch_ready_by_region

    return dispatch_ready_by_region(db)


def build_plan(
    db: "Database", *, lead_id: int | None = None, now: datetime | None = None
) -> list[str]:
    """B29 spec item 5: the per-lead output — chosen channel + reason, the
    filled first touch, and the warm-handle note when ig_last_posted_date
    is older than 14 days. Recomputes the lane law live (route_lead is
    pure); PARKED leads print their park reason and no message."""
    import json as _json

    from outreach.router import route_from_row
    from outreach.templates import MessageHold, render_for_lead

    now = now or datetime.now(timezone.utc)
    sql = (
        "SELECT id, place_id, name, phone, instagram, email, data_json,"
        " ig_class, region, route_channel, extracted_dish FROM leads"
        " WHERE state IN ('ROUTED', 'UNREACHABLE')"
        " AND previously_contacted=0"
    )
    params: tuple = ()
    if lead_id is not None:
        sql += " AND id=?"
        params = (lead_id,)
    sql += " ORDER BY id"
    lines = [
        "| id | place | channel | reason | message | ig note |",
        "|---|---|---|---|---|---|",
    ]
    for row in db.conn.execute(sql, params):
        try:
            payload = _json.loads(row["data_json"] or "{}") or {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        decision = route_from_row(row, now)
        channel = decision.channel or "PARK"
        message = "-"
        if decision.channel:
            try:
                message = render_for_lead(decision.channel, row).text.replace(
                    "\n", " "
                )
            except MessageHold as exc:
                message = f"HOLD: {exc}"
        note = "-"
        raw = payload.get("ig_last_posted_date")
        if raw:
            try:
                stamp = datetime.fromisoformat(str(raw))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                age_days = (
                    now - stamp.astimezone(timezone.utc)
                ).total_seconds() / 86400.0
                if age_days > 14:
                    note = (
                        "warm handle, engage with their recent post"
                        " before DMing"
                    )
            except ValueError:
                pass
        lines.append(
            "| {id} | {place} | {channel} | {reason} | {message} | {note} |".format(
                id=row["id"],
                place=(row["place_id"] or "-").replace("|", "/"),
                channel=channel,
                reason=decision.reason,
                message=message.replace("|", "/"),
                note=note,
            )
        )
    if len(lines) == 2:
        lines.append("| - | - | - | no ROUTED or parked leads | - | - |")
    return lines


def main(
    argv: list[str] | None = None,
    *,
    db: "Database | None" = None,
    echo=_echo,
    transport=None,
) -> int:
    """`outreach status|run|pause|resume|alerts|intake` — one interface, that's it.

    `run [--now ISO]` executes ONE engine tick with the REAL sender maps
    (wa, ig and email all live; an unclaimed/unroutable lead is the only
    thing that stops the drain). A bad `--now` is refused loudly (rc 2),
    never half-run. `intake [--run ID] [--scraper PATH] [--limit N]` runs
    the B20 pipeline for every pending scraper run (or one run) — with no
    `--run` it only touches runs the watcher has not completed yet.
    """
    parser = argparse.ArgumentParser(prog="outreach")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    run_p = sub.add_parser("run")
    run_p.add_argument("--now", default=None)
    pause_p = sub.add_parser("pause")
    pause_p.add_argument(
        "--engage", action="store_true",
        help="pause the engagement lane (comments/scroll) instead of the send queue",
    )
    pause_p.add_argument(
        "--reason", default="paused by owner",
        help="why, recorded with the pause so `status` can say it",
    )
    resume_p = sub.add_parser("resume")
    resume_p.add_argument(
        "--engage", action="store_true",
        help="resume the engagement lane instead of the send queue",
    )
    alerts_p = sub.add_parser("alerts")
    alerts_p.add_argument("--limit", type=int, default=20)
    intake_p = sub.add_parser("intake")
    intake_p.add_argument("--run", default=None, help="one scraper run_id")
    intake_p.add_argument("--scraper", default=None, help="scraper DB path")
    intake_p.add_argument(
        "--limit", type=int, default=None, help="LLM leads per stage"
    )
    plan_p = sub.add_parser("plan")  # B29: per-lead channel+message output
    plan_p.add_argument("--lead", type=int, default=None, help="one lead id")
    sub.add_parser("fleet")  # v1.1 batch 11: accounts, targets, switches
    replies_p = sub.add_parser("replies")  # v1.1 batch 10.5: 72h measurement
    replies_p.add_argument("--hours", type=int, default=72)
    replies_p.add_argument("--channel", default=None, choices=("ig", "wa", "email"))
    replies_p.add_argument(
        "--by",
        default=None,
        choices=("commented_before_dm", "send_hour", "target_class", "touch_kind"),
        help="O10: one attribution table instead of the per-variant one",
    )
    inbox_p = sub.add_parser("inbox")  # v1.1 batch 10.6: durable reply alerts
    inbox_p.add_argument(
        "--ack", default=None, metavar="all|ID",
        help="acknowledge every unread reply, or one by inbox id",
    )
    inbox_p.add_argument(
        "--repair", action="store_true",
        help="undo REPLIED rows whose bubble was our own copy or an autoresponder",
    )
    holds_p = sub.add_parser("holds")  # R7: the wall's open holds, read-only
    holds_p.add_argument(
        "--clear",
        type=int,
        default=None,
        metavar="LEAD_ID",
        help="owner judgement: allow THIS lead to be claimed again",
    )
    holds_p.add_argument(
        "--log",
        action="store_true",
        help="write one digest inventory row (the results of record)",
    )
    holds_p.add_argument(
        "--release-copy",
        action="store_true",
        help="return COPY_HOLD leads to ROUTED (only those with no open veto)",
    )
    region_p = sub.add_parser("resolve-region")  # DT-14: release a held lead
    region_p.add_argument("lead", type=int, help="lead id in REGION_UNRESOLVED")
    region_p.add_argument(
        "--region", default=None, choices=("na", "me"),
        help="the region to set, then release the lead as NEW",
    )
    region_p.add_argument(
        "--reject", action="store_true",
        help="close it instead: a junk row (placeholder name / no channel)",
    )
    region_p.add_argument(
        "--reason", default=None, help="why (recorded on the row and the history)",
    )
    variants_p = sub.add_parser("variants")  # v1.2 pack: install a paste
    variants_p.add_argument(
        "--reseed", action="store_true",
        help="install the pinned copy over drifted stored rows (owner paste)",
    )
    add_p = sub.add_parser("add-account")  # v1.1 batch 12: onboarding
    add_p.add_argument("--id", required=True, help="account id, e.g. ig2")
    add_p.add_argument("--profile", required=True, help="lane profile dir")
    add_p.add_argument("--channel", default=None, choices=("ig", "wa"))
    add_p.add_argument("--offset", type=int, default=0, help="stagger minutes")
    add_p.add_argument(
        "--confirm", action="store_true",
        help="the owner's confirmation: this decides who sends",
    )
    args = parser.parse_args(argv)

    if args.cmd == "run" and args.now is not None:
        try:
            _parse_now(args.now)
        except ValueError as exc:
            echo(f"refused: --now is not an ISO timestamp ({exc})")
            return 2

    own_db = db is None
    if own_db:
        from pathlib import Path  # the repo-root DB

        db = Database(Path(__file__).resolve().parents[2] / "outreach.db")  # type: ignore[assignment]
    try:
        now = (
            datetime.now(timezone.utc)
            if args.cmd == "run" and args.now is None
            else (
                _parse_now(args.now)
                if args.cmd == "run"
                else datetime.now(timezone.utc)
            )
        )
        if args.cmd == "status":
            echo(json.dumps(build_status(db, now), indent=2, default=str))
        elif args.cmd == "pause":
            if args.engage:
                from outreach.engage_stage import (
                    engage_window,
                    set_engage_paused,
                )

                set_engage_paused(db, reason=args.reason, now=now)
                rule = engage_window(db)
                echo(
                    f"engagement lane PAUSED ({args.reason}) - comments and"
                    f" scrolling stop now; window runs {rule['start']}-{rule['end']}"
                    " local when resumed"
                )
            else:
                set_config(db, "queue_status", "PAUSED")
                echo("queue is PAUSED (no dispatch until resume)")
        elif args.cmd == "resume":
            if args.engage:
                from outreach.engage_stage import clear_engage_paused

                clear_engage_paused(db)
                echo("engagement lane resumed (night window still applies)")
            else:
                set_config(db, "queue_status", "LIVE")
                echo("queue is LIVE")
        elif args.cmd == "alerts":
            for row in db.conn.execute(
                "SELECT body FROM digest ORDER BY id DESC LIMIT ?",
                (args.limit,),
            ):
                echo(row[0])
        elif args.cmd == "intake":
            from outreach.intake import intake_run, pending_runs

            runs = (
                [args.run]
                if args.run
                else list(pending_runs(db, args.scraper))
            )
            reports = [
                intake_run(
                    db, run_id, transport=transport, db_path=args.scraper,
                    limit=args.limit,
                ).as_dict()
                for run_id in runs
            ]
            echo(json.dumps(reports, indent=2, default=str))
        elif args.cmd == "plan":
            for line in build_plan(db, lead_id=args.lead, now=now):
                echo(line)
        elif args.cmd == "fleet":
            from outreach.dashboard import build_fleet

            for line in build_fleet(db, now):
                echo(line)
        elif args.cmd == "replies":
            if getattr(args, "by", None):
                from outreach.attribution import attribution, format_attribution

                rows = attribution(
                    db, now, dimension=args.by, window_hours=args.hours,
                    channel=args.channel,
                )
                for line in format_attribution(
                    rows, window_hours=args.hours, dimensions=(args.by,)
                ):
                    echo(line)
                return 0
            from outreach.measure import format_measurement, reply_measurement

            rows = reply_measurement(
                db, now, window_hours=args.hours, channel=args.channel
            )
            for line in format_measurement(rows, window_hours=args.hours):
                echo(line)
        elif args.cmd == "inbox":
            from outreach.inbox import (
                acknowledge,
                format_inbox,
                revert_false_replies,
                unread,
            )

            if args.repair:
                fixed = revert_false_replies(db, now=now)
                echo(
                    "repaired false REPLIED on lead(s): "
                    + (", ".join(str(x) for x in fixed) if fixed else "none")
                )

            if args.ack is not None:
                try:
                    count = acknowledge(db, args.ack, now=now)
                except ValueError as exc:
                    echo(f"refused: {exc}")
                    return 2
                echo(f"acknowledged {count} reply(ies)")
            for line in format_inbox(unread(db), now=now):
                echo(line)
        elif args.cmd == "resolve-region":
            from outreach.machine import transition

            row = db.conn.execute(
                "SELECT id, name, state, region FROM leads WHERE id=?",
                (args.lead,),
            ).fetchone()
            if row is None:
                echo(f"refused: there is no lead {args.lead}")
                return 2
            if row["state"] != "REGION_UNRESOLVED":
                echo(
                    f"refused: lead {args.lead} is {row['state']}, not"
                    " REGION_UNRESOLVED (nothing to release)"
                )
                return 2
            if args.reject:
                reason = args.reason or (
                    f"junk identity: {row['name']!r} with no channel - released"
                    " from REGION_UNRESOLVED as junk"
                )
                transition(db, args.lead, "REJECTED", reason=reason, actor="owner")
                echo(f"lead {args.lead} REJECTED: {reason}")
            elif args.region:
                with db.conn:
                    db.conn.execute(
                        "UPDATE leads SET region=? WHERE id=?",
                        (args.region, args.lead),
                    )
                transition(
                    db, args.lead, "NEW",
                    reason=f"owner set region {args.region} (DT-14 release)",
                    actor="owner",
                )
                echo(
                    f"lead {args.lead} released: region={args.region}, state=NEW"
                    " (it rejoins qualification and routing from there)"
                )
            else:
                echo(
                    "refused: pass --region na|me to release it, or --reject if"
                    " the row is junk (no name, no phone, no handle)"
                )
                return 2
        elif args.cmd == "holds":
            from outreach.proofread import (
                clear_hold,
                format_holds,
                open_holds,
                release_copy_holds,
            )

            if args.release_copy:
                released = release_copy_holds(db, now=now)
                echo(
                    f"released {len(released)} COPY_HOLD lead(s) back to ROUTED"
                    + (": " + ", ".join(str(x) for x in released) if released else "")
                )
                held = db.conn.execute(
                    "SELECT id, reject_reason FROM leads WHERE state='COPY_HOLD'"
                    " ORDER BY id"
                ).fetchall()
                for row in held:
                    echo(
                        _console_safe(
                            f"  still held {row['id']}: "
                            f"{(row['reject_reason'] or 'no reason recorded')[:100]}"
                        )
                    )

            if args.clear is not None:
                cleared = clear_hold(db, args.clear, now=now)
                echo(f"cleared {cleared} open hold(s) for lead {args.clear}")
                if not cleared:
                    echo("refused: that lead has no open hold")
                    return 2
            holds = open_holds(db)
            lines = format_holds(holds)
            for line in lines:
                echo(_console_safe(line))
            if args.log:
                with db.conn:
                    db.conn.execute(
                        "INSERT INTO digest(body) VALUES (?)",
                        ("HOLDS INVENTORY\n" + "\n".join(lines),),
                    )
                echo("logged one HOLDS INVENTORY row")
        elif args.cmd == "variants":
            from outreach.templates import TemplateError
            from outreach.variants import PS_ROTATION, seed_variant_tables

            try:
                changed = seed_variant_tables(db, reseed=args.reseed)
            except TemplateError as exc:
                echo(f"refused: {exc}")
                echo("pass --reseed to install the pinned owner copy")
                return 2
            if args.reseed:
                echo(f"reseeded {changed} variant row(s) from the pinned copy")
            for ps_id, body in PS_ROTATION.items():
                echo(_console_safe(f"  {ps_id}: {body}"))
        elif args.cmd == "add-account":
            from outreach.fleet import ProfileMissing, ProfileRefused
            from outreach.onboarding import OnboardingRefused, add_account

            if not args.confirm:
                echo(
                    "refused: onboarding registers a sender - re-run with --confirm"
                    " once you have checked the id and profile."
                )
                return 2
            channel = args.channel or args.id[:2]
            try:
                report = add_account(
                    db, args.id, channel, args.profile,
                    offset_min=args.offset, now=now,
                )
            except (OnboardingRefused, ProfileRefused, ProfileMissing) as exc:
                echo(f"refused: {exc}")
                return 2
            echo(json.dumps(report, indent=2))
            if report["session"] not in ("signed_in", "n/a"):
                echo(
                    f"warning: profile {report['profile']} has no Instagram session"
                    f" ({report['session']}); it stays WARMING and sends nothing."
                )
            echo(
                f"next: sign in with `python interactive_login.py --account"
                f" {report['account']}`"
            )
        elif args.cmd == "run":
            tick = run_engine_tick(
                db,
                now,
                senders=dict(real_senders),
                resenders=dict(real_resenders),
            )
            echo(json.dumps(asdict(tick), indent=2, default=str))
        else:  # pragma: no cover - argparse enforces subcommand choices
            raise AssertionError("unreachable subcommand")
        return 0
    finally:
        if own_db:
            db.close()


def _parse_now(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


if __name__ == "__main__":  # pragma: no cover - module entry point
    import sys

    raise SystemExit(main(sys.argv[1:]))
