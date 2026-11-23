# Outreach Department — the engineering deep dive

**Status:** first pass 2026-11-20, fact-checked against the source line by line on
2026-11-21. This is the long document: architecture, data model, laws, failure
modes, and the reasoning behind each of them. If you want the short version, read
the showcase `README.md` — it names every feature and points back here.

This document describes the **restaurant build (v1.0)**, frozen on 2026-11-19 ahead
of an industry switch. Where a fact is a measurement rather than a design claim, it
carries the date and the numbers it was measured with. Where something is broken,
it says so in the section where a reader would otherwise trust it.

---

## Contents

1. [What this is, and what it refuses to be](#1-what-this-is-and-what-it-refuses-to-be)
2. [The shape of the machine](#2-the-shape-of-the-machine)
3. [Module map](#3-module-map)
4. [The data model](#4-the-data-model)
5. [The state machine](#5-the-state-machine)
6. [Dispatch: who gets messaged, and in what order](#6-dispatch-who-gets-messaged-and-in-what-order)
7. [Channel internals](#7-channel-internals)
8. [The AI contract](#8-the-ai-contract)
9. [Personalisation: `{{dish}}` and review quotes](#9-personalisation-dish-and-review-quotes)
10. [The scraper contract](#10-the-scraper-contract)
11. [Scheduling, windows and pacing](#11-scheduling-windows-and-pacing)
12. [The engagement lane](#12-the-engagement-lane)
13. [Replies and human handover](#13-replies-and-human-handover)
14. [Failure taxonomy](#14-failure-taxonomy)
15. [Verification: the gate](#15-verification-the-gate)
16. [Owner switches and the pause law](#16-owner-switches-and-the-pause-law)
17. [Ops runbook](#17-ops-runbook)
18. [Decision log](#18-decision-log)
19. [Known limitations](#19-known-limitations)
20. [Glossary](#20-glossary)

---

## 1. What this is, and what it refuses to be

One owner, one queue, three channels. Scraped restaurant leads enter a SQLite
state machine and leave as personalised first touches on **Instagram → Email →
WhatsApp**, in that order, inside the owner's working windows, under hard daily
caps. WhatsApp is last and narrow on purpose: a cold DM to a scraped number
reaches a cashier on an order line, not an owner (B29, 2026-10-31, §7). Two
thousand rows do not get "processed"; they get *claimed*, one at a time, by a
claim loop that refuses to claim anything it cannot lawfully send.

The three refusals define the project more than the features do:

- **Not a scraper.** Scraping lives in a separate project (the scraper repo) which this
  system treats as read-only, per ADR-3. The only arrow is
  `python -m outreach.sync`.
- **Not a CRM.** There is no pipeline board, no deal stage, no per-contact history
  beyond what the state machine and `message_log` require. A reply is not a
  "conversation"; it is a handover trigger.
- **Not a LinkedIn bridge.** A previous build drifted into one and was archived at
  `_archive_prev_build/20261022-drift/`. ADR-14 says LinkedIn is *mounted, never
  rebuilt*, and any new LinkedIn code is a red batch with no exceptions.

The single strongest constraint: **AI decides exactly two things** — whether a lead
is in the ideal-customer profile, and which dish to name in the copy. It never
writes copy, never chooses who to message, never schedules, never replies. Every
message body is a frozen template with one hole punched in it.

### Why that constraint exists

Because outreach failure is rarely a writing problem. It is a *volume* problem and
a *judgement* problem: too many messages, to the wrong people, sent at the wrong
hour, with no record of what was said. A deterministic queue fixes volume, order
and record-keeping. The only part genuinely needing judgement is "is this a real
independent restaurant" and "what is this restaurant known for" — so those are the
only two places a model is allowed to speak, and both are wrapped in code that can
refuse the model's answer.

---

## 2. The shape of the machine

```
scraper DB (read-only)
     │  python -m outreach.sync
     ▼
  intake ──► cleaning ──► icp (AI #1) ──► routing ──► dispatcher
                                                        │
                       ┌────────────────────────────────┼─────────────────┐
                       ▼                                ▼                 ▼
                 IG sender                       email sender       WA sender
                       │                                │                 │
                       └──────────── commit_send ───────┴─────────────────┘
                                        │
                                        ▼
                        outreach.db (leads, daily_sends, message_log,
                                     followups, state_history, config)
                                        │
              ┌─────────────────────────┼──────────────────────────┐
              ▼                         ▼                          ▼
        replies probe              digest / alerts        SUPPLY_COUNTDOWN.json
        (handover)                 (owner surfaces)       DISPATCH_FORECAST.json
```

Two loops drive it. The **engine loop** (`engine_loop.py`) sleeps until the next
meaningful moment and runs one scheduler tick. The **tick** (`scheduler.py`) does
its work in a fixed order: poll replies, run the follow-up ladder, run the
engagement stage, dispatch fresh sends. Everything else — qualification,
extraction, refresh — is a stage that runs on its own schedule or on demand.

Nothing lives in memory between ticks. Wake time, caps, windows, pending
follow-ups and queue state are all derived from the database, so killing the
process at any instant loses nothing; the next process turns the same crank. This
is the property that makes a reboot-safe automation possible without a job queue.

---

## 3. Module map

Sixty-five Python files under `src/outreach/` (including `__init__.py`), flat,
grouped here by concern. The
grouping is documentation, not packaging — the code has no sub-packages on purpose
(ADR-1: stdlib-first, flat modules, no import gymnastics).

### Entry points and orchestration

| Module | Role |
|---|---|
| `engine_loop.py` | The OS-level runner. One pass per wake, one scheduler tick per pass, exceptions park the loop and land in the digest. |
| `scheduler.py` | The tick body: boot → replies → ladder → engage → dispatch → review. Owns `seconds_to_next_wake`. |
| `command_center.py` | The operator CLI: `status`, `run`, `pause`, `resume`, `alerts`, `intake`, `plan`, `fleet`, `replies`, `inbox`, `holds`, `resolve-region`, `variants`, `add-account`. |
| `watchdog.py` | The Task Scheduler check that re-spawns a *gone* engine and refuses to double-spawn a hung one. |
| `toast.py` | The optional local Windows balloon on new digest rows (config-flagged, throttled). |
| `setup_startup.py` | Installs/removes the Windows Startup launcher. |
| `startup_readiness.py` | Pre-flight checks the launcher runs before the engine starts. |
| `heartbeat.py` | Liveness stamp per stage, so "is it running" is a query, not a guess. |
| `health.py` | Lane and account health, feeding the digest. |

### Intake and cleaning

| Module | Role |
|---|---|
| `sync.py` | The only arrow out of the scraper project. Read-only adapter over the scraper DB. |
| `scraper_adapter.py`, `scraper_labels.py` | Field mapping and label normalisation for scraper rows. |
| `intake.py` | Row → lead, dedupe (`place_id` → normalised phone → fuzzy name+city), run provenance. |
| `importer.py` | Bulk import path (including the contacted-elsewhere signal). |
| `cleaning.py` | One junk-identity rule, asked at intake, judgement and claim. |
| `targeting.py` | The O6/O7 policy that parks delivery-only, chain and brand accounts. |

### Judgement (the two AI roles) and copy

| Module | Role |
|---|---|
| `ollama_adapter.py` | The department's only AI endpoint. Loopback-only, retries 3×, then PAUSE + HALT. |
| `icp.py` | AI role #1: single-branch, independent, thriving — or REJECTED with a reason. |
| `dishes.py` | AI role #2: propose a dish; a code gate verifies it. |
| `dish_mining.py` | Evidence mining from menus and reviews, plural folding, junk-word blocking. |
| `menu_sync.py` | Website-menu evidence into the dish pipeline. |
| `chains.py`, `chain_review.py` | Chain detection and the tier-contradiction repair that can restore a wrongly rejected lead. |
| `quotes.py` | Review-quote extraction for the quote tier. |
| `followers.py` | Reads a follower count while the page is already open; feeds the brand-account filter, `unknown` rather than a guess. |
| `templates.py`, `templates/`, `variants.py` | `templates.py` is the byte-exact renderer; `templates/` is the copy itself; `variants.py` is the nine-variant IG packet (byte-pinned, `V1` its only composed copy) and the lane render. |
| `proofread.py`, `proofread_log` | Copy lint plus the wall: `proofread.py` judges every pre-dispatch body, `proofread_log` (a table) holds every verdict and the veto queue. |
| `playbook.py` | The message ladder as data. |

### Dispatch, caps and pacing

| Module | Role |
|---|---|
| `dispatcher.py` | `claim_next` (writes nothing) + `commit_send` (one transaction). |
| `router.py` | Channel decision per lead, the boot-time stale reroute, and the stranded-lead revive. |
| `cold_probe.py` | The measured exit from the follower gate: three dish-bearing cold first touches a day, watched for a `Delivered` bubble. |
| `caps.py` | Daily caps, IG warm-up ramp, `set_cap` validation. |
| `followups.py` | The ladder: touch 1, day 3, day 7, with per-rung channel stamps. |
| `replan.py` | Re-derives the day after a pause, restart or repair. |
| `attribution.py`, `measure.py` | What each send carried, and whether it worked. |

### Channels

| Module | Role |
|---|---|
| `browser.py` | The one Chrome profile launcher (ADR-13: `--profile-directory=Outreach`). |
| `accounts.py`, `fleet.py` | The account registry: lanes, status, per-account ramps, the L1 assignment ledger, and the cookie-level session check. |
| `onboarding.py` | `add_account`: owner confirmation, lane-only profile, profile must already exist, then a `WARMING` row. |
| `replies.py` | Reply detection: the pure email matcher plus the WA and IG in-thread probes, and the probe-freshness guard the ladder reads. |
| `wa_sender.py` | WhatsApp Web sends through the real UI. |
| `ig_sender.py` | Instagram DM sends, including the `ig.me` shortcut and its fallback. |
| `email_sender.py` | Hostinger SMTP via `smtplib`, priority-3 only. |
| `inbox.py` | IMAP poll, parsed messages for the reply matcher. |
| `story.py`, `comments.py` | Engagement acts: story replies, public comments. |
| `warmup.py`, `warming.py`, `engagement.py` | Warm-up, warming progress, and the per-act ceiling. |
| `engage_stage.py` | The only caller of the engagement machinery. |
| `ambient.py` | The 60–120 min/day scrolling budget. |

### Reporting and contracts

| Module | Role |
|---|---|
| `countdown.py` | `SUPPLY_COUNTDOWN.md` + `.json`, schema v1, one payload. |
| `forecast.py` | `DISPATCH_FORECAST.md` + `.json`, schema v1, one payload, and the claim-time divergence check. |
| `targets.py` | The daily-target alarm: one below-half-pace alert per channel per PKT day (a target never raises a cap). |
| `dashboard.py` (plus the `digest` table every module writes to) | Owner-facing surfaces. |
| `alerts.py` | P0/P1 escalation out of the digest. |
| `agent_api.py` | Read-only JSON snapshot for a supervising agent (`mode=ro`, no writes, no imports from the dispatcher). |
| `owner_review.py` | Builds the owner's review queue (never published). |
| `retirement.py` | Cleanup of superseded artefacts. |

### Storage and clock

| Module | Role |
|---|---|
| `store.py` | Schema, numbered migrations, WAL, busy timeout, `schema_migrations`. |
| `machine.py` | The frozen transition map and the atomic `transition()`. |
| `clock.py` | PKT arithmetic; windows and daily caps are PKT, not machine-local. |
| `sync.py` | (also above) the scraper boundary. |

---

## 4. The data model

One SQLite file, `outreach.db`, in WAL mode with a 5 s `busy_timeout`, so a
read-only surface can read while the engine writes. There is one writer (the
engine). The strictly read-only surfaces — `agent_api`, the scraper adapter, the
cookie reads in `fleet` — open the file with `mode=ro`; tests build their own
temporary database and never touch this one.

### Core tables (migration 1)

```sql
leads(
  id INTEGER PRIMARY KEY,
  place_id TEXT NOT NULL UNIQUE,      -- dedupe key #1
  name, category, city, region,
  phone, instagram, email,
  data_json TEXT,                     -- the scraper row, verbatim
  icp_score REAL, qualification_json TEXT, reject_reason TEXT,
  state TEXT NOT NULL DEFAULT 'NEW',
  created_at, updated_at
)

state_history(
  id, lead_id, from_state, to_state, reason, actor, created_at
)   -- append-only, enforced by BEFORE UPDATE / BEFORE DELETE triggers

daily_sends(
  channel TEXT CHECK (channel IN ('wa','ig','email')),
  date TEXT, count INTEGER CHECK (count >= 0 AND count <= 20),
  PRIMARY KEY (channel, date)
)

message_log(
  id, lead_id, channel, template_hash, dish, sent_at, screenshot
)

followups(
  id, lead_id, touch, due_at, status, cancelled_by_reply, created_at
)

digest(id, body, created_at)          -- every owner-facing event, append-only
config(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at)
```

### Tables added later (numbered migrations)

| Migration | Adds | Why |
|---|---|---|
| 2 | `leads.previously_contacted` | Contacted elsewhere — imported, never queued. |
| 3 | `leads.extracted_dish`, `dish_json` | Dish verdict storage with its evidence. |
| 4 | `leads.route_channel` | The routing decision is recorded, not recomputed. |
| 5 | `leads.run_id`, `phone_e164`, `ig_class` | Scraper-run provenance, E.164 phones, repaired IG link classes. |
| 6 | `followups` rebuild | The ladder became infinite; `touch` no longer capped at 2, and a per-rung `channel` was added for the day-7 IG escalation. |
| 7 | `leads.account_id` | The v1.1 seam for multi-account lanes (additive; v1.0 never reads it). |
| 8 | `accounts`, `lead_accounts`, `message_log.account_id` | The account registry and assignment ledger. |
| 9 | `message_log.body` | What the recipient actually read, not just a template hash. |
| 10 | `engagement_log` | One row per follow/like act, with target, confidence and evidence. |
| 11 | `comment_review` | Comments the confidence gate or the copy veto refused, kept for the owner. |
| 12 | `warmups` | Per-lead warm-up record; `dm_after` is the earliest legal first touch. |
| 13 | `warming_progress` | Per-account, per-handle progress, so a follow is never repeated. |
| 14 | `account_health` | Every health signal an account emitted, and whether it halted the account. |
| 15 | `owner_inbox` | A reply stored as a live conversation until the owner acknowledges it. |
| 16 | `proofread_log` | The wall's ledger: every judged render, the veto queue, per-variant veto rate. |
| 17 | `followups.expired_reason` + owner_inbox conversation columns | Why a rung expired; and what the lead actually wrote, on which account. |
| 18 | `proofread_log.tone_score`, `.tone_note` | The model's tone view, recorded as advisory telemetry, never a veto input. |
| 19 | `engagement_log` rebuild | Adds `story_reply` to the act vocabulary (SQLite cannot `ALTER` a `CHECK`). |
| 20 | `leads.park_kind` | A park's machine-readable class, so the revive never parses a reason string. |

Every migration is a numbered tuple in `MIGRATIONS`, applied in order at open and
recorded in `schema_migrations`. Adding one is a code change plus a test; hand-
editing the file is forbidden by the manual.

Four tables are deliberately *not* migrations, because the engine that owns them
is optional: `variants`, `variant_ps`, `variant_generations` and `variant_history`
are created and seeded on first use by `variants.seed_variant_tables`, so a v1.0
install that never arms `variant_engine` never has them.

One trap worth naming: `MIGRATIONS` is a dict, so two entries with the same
number do not fail loudly — the later one silently wins. A `12:` duplicate of the
`story_reply` rebuild sat in this file and was dead code for that reason; it is
removed, and the versions are now 1–20 with no gaps.
A pin in the private suite counts the **version literals in the source** rather
than trusting the dict that swallowed them: the
pre-fix file yields 21 literals against 20 keys, and the surviving range is still
complete, so a range check alone would have passed. Same lesson as the gate's
stale-node-id pin — check the text when the object has already forgotten.

### Three invariants worth naming

1. **`state_history` is append-only at the database level**, not by convention.
   Triggers abort on UPDATE and DELETE. A transition and its history row commit in
   one transaction, so the audit trail cannot disagree with `leads.state`.
2. **`daily_sends` is the cap.** Caps are not counters in memory; they are rows
   with a primary key of `(channel, date)`. A crash cannot lose a send count.
3. **`config` is the only configuration store.** Windows, caps, switches, template
   owner-gate — all rows, read through `get_config` / `set_config`. There is no
   config file to drift out of sync with the database.

---

## 5. The state machine

The frozen map lives in `machine.py` as `ALLOWED: dict[str, frozenset[str]]`. A
transition not in the map raises `InvalidTransition`; the row is untouched.

```
NEW ──► QUALIFIED ──► ROUTED ──► SENT ──► FOLLOWUP ──► REPLIED
 │          │           │                    └────────► NO_REPLY
 │          │           │
 │          │           ├──► UNREACHABLE  (no lawful channel at routing)
 │          │           ├──► WA_DEAD / IG_DEAD
 │          │           ├──► CHANNEL_BANNED  (drains to ROUTED on email)
 │          │           ├──► COPY_HOLD     (the lint refused the render)
 │          │           ├──► TRANSPORT_HOLD (IG would not load either surface)
 │          │           └──► DEPRIORITIZED  (targeting policy)
 │          └──► UNREACHABLE
 ├──► REJECTED (ICP fail, reason required)
 ├──► DUP (reason required)
 └──► REGION_UNRESOLVED (owner sets the region → NEW, or junk → REJECTED)
```

### The edges that are not obvious

| Edge | Why it exists |
|---|---|
| `REJECTED → QUALIFIED` | Only through `chain_review.restore_tier_valid_chain_rejects`. The verdict was wrong and the scraped tier was right; no dispatch path can re-qualify a rejected lead on its own. |
| `UNREACHABLE → ROUTED` | "Unreachable" means *no lawful channel at the moment routing looked*, not *no data*. At the 2026-11-16 audit 842 of 867 parked rows carried a phone and 286 a handle the sanitizer accepts; on 2026-11-23 the pass still saw 555 lane-law rows, 537 phone-bearing, so a later repair or import can make the law reach them. Only `router.reroute_stranded` may use this edge, and only by re-running the lane law. |
| `COPY_HOLD → ROUTED`, `TRANSPORT_HOLD → ROUTED` | Parks, never verdicts. A poison row cannot be re-claimed in the same tick — or ever — without an explicit repair. |
| `DEPRIORITIZED → ROUTED` | Same shape: targeting parked it, a repair returns it. |
| `CHANNEL_BANNED → ROUTED` | A banned Instagram lane does not kill the lead; it re-enters routing where email can take it. |
| `FOLLOWUP → NO_REPLY` | The ladder's terminus, and **vestigial**: nothing writes it since the ADR-10 amendment removed the expiry pass. It stays in the frozen map so an old row remains legal to read. |

**Only `ROUTED` is claimable.** Every other state is either a terminal verdict, a
park awaiting repair, or a hold awaiting an owner. This single rule is why a lead
cannot be messaged twice by accident: after the first send it is `SENT`, and
`SENT` is not in the dispatcher's claim predicate.

### Reason discipline

`REJECTED` and `DUP` refuse to fire without a non-empty reason (`MissingReason`).
Since 2026-11-16 the reason is also written to the row itself for `REJECTED`,
`COPY_HOLD`, `TRANSPORT_HOLD` and `REGION_UNRESOLVED` — because 18 `COPY_HOLD`
leads had `reject_reason NULL`, and `holds` was the only place to learn why a lead
was stuck. A reason that lives only in history is a reason nobody reads.

`park_kind` records the *class* of an `UNREACHABLE` park (`lane_law` = routing
found no lawful channel; `verdict` = the platform refused) and is cleared on every
other edge, so a stale class can never survive a repair.

---

## 6. Dispatch: who gets messaged, and in what order

`dispatcher.py` splits preparation from commitment, and that split is the whole
safety story:

- **`claim_next` prepares a send and writes nothing.** It checks, in order:
  queue live, template confirmed, no armed send gap, the lane not paused, the
  targeting and venue-clock parks, the region window, the rolling 4/hour slot,
  account availability, the copy wall, and finally that the body renders. If any
  check fails it returns a refusal. Nothing is reserved, stamped or incremented.
- **`commit_send` lands the outcome atomically**: `daily_sends` increment +
  `message_log` row + `ROUTED → SENT` transition, in **one transaction**. Any
  refusal leaves the ledger and the state machine byte-identical.

That is the gate-#3 law: a failed send is indistinguishable from a send that never
happened.

### Ordering (tiers)

The claim sort is one function, `dispatcher.claim_priority`, and it returns two
numbers:

1. **dish first.** A lead with a verified `extracted_dish` claims before one
   without. The dish is the only personalised thing in the copy, so it is the
   only evidence that changes the order.
2. **strong evidence second.** Inside each dish group, a lead whose owner is
   known, or whose rating is ≥ 4.3 and review count ≥ 80, claims before a weak
   or unknown one (B29, 2026-10-31). Missing figures count as weak — no evidence
   is weak evidence.
3. **lead id last**, as the stable tie-break.

A lead with a dish is preferred, but a lead without one is never excluded — the
order changes, the set does not. That ruling (2026-11-07) is what stops the queue
from starving when the scraper returns a thin batch.

The three **tiers** readers see in the forecast — `dish`, `review-quote`,
`moment` — are `forecast.py`'s vocabulary for *describing* a lead, not the
claim sort. None of them drops a lead, and `forecast.py` imports
`claim_priority` rather than re-implementing it, so the forward contract cannot
drift from the loop it describes.

### Pacing

Every `commit_send` arms a randomized 180–300 s `next_send_after` gap, inside the
same transaction. Pacing is a hard gate, not advice: a tick that finds the gap
armed does not send. On top of the gap, a **rolling four sends per hour** slot is
enforced per platform (WhatsApp, Instagram; email is not slot-paced) by counting
`message_log.sent_at` over the last 60 minutes. The measured effect on a real day
is a send rate that looks human because it *is* bounded by the same kind of pause
a human takes.

The gap is one global `config` row, not one per channel: the law is "a 3–5 minute
break between any two sends", whichever lanes they belong to.

### The ladder

`followups.py` owns everything after the first touch:

- touch 1 fires, then a day-3 follow-up, then a day-7 follow-up — and that is the
  end. **Two rungs**, not an infinite ladder: `MAX_LADDER_TOUCH = 2` (Ruling B,
  2026-11-05, which retired the 14/30/180-day cadence). Migration 6 rebuilt the
  table so `touch` is any value ≥ 1, and that column bound is what made an earlier
  draft of this document claim the ladder was infinite. The *law* is the cap in
  `followups.py`: `rung_days()` raises past two, `_next_rung_after()` returns
  `None`, and a leftover row from the old cadence is expired rather than sent.
- each rung carries a `channel` stamp, so the day-7 rung can escalate a
  WhatsApp-routed lead to Instagram: one shot, armed after the day-7 WA touch,
  never re-armed, and it ships the opener rather than the follow-up line because
  it is that lead's first Instagram contact.
- a rung more than **72 h** past its due date is **expired**, not sent
  (`expired_reason='expired_overdue'`). A day-3 touch that arrives on day 13 is a
  mass-mailer, not a follow-up.
- a detected reply cancels every pending rung **exactly once** (`cancelled_by_reply`),
- the probe asks the ladder *before* a rung fires: if that channel's reply probe
  has not confirmed its threads within the freshness window, the rung stays
  pending instead of landing on top of a conversation the owner already owns.

---

## 7. Channel internals

### Priority and legality

**Instagram, then email, then WhatsApp through an exception.** That order is the
B29 lane law (owner research, 2026-10-31) and it inverted the earlier
phone-first build for a measured reason: a scraped public number belongs to an
order line staffed by a cashier, so a cold WhatsApp DM is read as spam, ignored,
or reported. `router.route_lead` is a pure function of the row, in this order:

1. a **sanitised Instagram handle** and the account is not inactive
   (`ig_inactive=1`, or a scraped last-post date older than 30 days) → `ig`;
2. otherwise an **email that passes `email_lane_ok`** → `email`. The address must
   sit on the lead's own website domain or carry an explicit verification flag;
   platform inboxes (`restaurantguru`, `linkedin`, `tripadvisor`…) and free
   mailboxes never qualify;
3. otherwise **WhatsApp only through the order-line exception**: the `na`/Canada
   lane *and* the listing itself promotes WhatsApp ordering (`order_line`);
4. anything else → no lawful channel, parked `UNREACHABLE` with
   `park_kind='lane_law'`.

A `DEAD` Instagram link is no link at all; a `SUSPECT` link keeps the lane for
exactly one attempt. Junk social values (tracking pixels, template footers, post
permalinks) are sanitised away before they can become a lane.

### WhatsApp (`wa_sender.py`)

Real WhatsApp Web through a dedicated Chrome profile (ADR-2, ADR-13). The owner
logs in once by hand; automation never touches the owner's personal profile. Sends
go through the UI, and every send leaves a screenshot referenced by
`message_log.screenshot`.

The lane is **last, and small**: only the Canada order-line exception routes here,
and the live box carries `wa_paused` set (owner directive 2026-11-02), so the
claim loop and the ladder both skip it. `wa_paused` is a config row, not a code
change; unsetting it re-arms the lane and nothing else changes.

`WA_DEAD` is a detector, not a guess: a page that says the number is not on
WhatsApp is a verdict, and the lead moves to `WA_DEAD` rather than being retried
forever.

### Instagram (`ig_sender.py`)

Two transports, in order: the `ig.me` shortcut, then the profile page as fallback.
The fallback exists because `net::ERR_*` failures on the shortcut are common and
were previously fatal to the whole send (shipped 2026-11-18, `c1b5496`).

Two ramps exist and they are not the same ramp:

- **channel ramp** (`caps.current_cap`): the IG daily cap is 5 in week 1, 10 in
  week 2, then the configured 15 — and **0 until `ig_warmup_start` is stamped**,
  so the ramp begins with real usage rather than a guess.
- **per-account ramp** (`accounts.RAMP_CAPS`): a newly onboarded account sends
  **no DMs in week 1 at all** (engagement warm-up only), 10/day in week 2, then
  the full 15. A halted account has capacity 0. The fleet total is the sum of its
  accounts' caps; a daily target can never raise a cap.

A login-challenge or ban signal halts that lane and drains its queue to email. The
system **never auto-retries a login**: a wall is an owner action.

### The follower gate (2026-11-11) and its probe (2026-11-14)

`ig_cold_dms_blocked` is armed on the live lane. While it is armed and
`ig_delivery_confirmed` is unset, **no cold first touch to a non-follower is
typed**: the sender returns `follower_gated`, spends no slot, counts nothing, and
the lead stays `ROUTED`. The reason is honest — an account with no delivered
history can be typing into a void the UI never reports.

A deadlock like that cannot be reasoned away, so it is *measured* instead:
`cold_probe` grants **three dish-bearing cold first touches a day**, each one a
real send watched by `_observe_delivery`. The first observed `Delivered` bubble
writes `DELIVERY CONFIRMED (milestone)`, sets `ig_delivery_confirmed`, and the
gate never closes again. A refused slot is not a message and is never counted as
one.

### The fleet

The registry holds five accounts: `ig1` (the owner's live profile, `ACTIVE`),
`wa1` (`ACTIVE`), and `ig2`–`ig4` registered `WARMING` on their own profiles.
Only an `ACTIVE`/`WARMING`/`HEALTHY` account is selectable, capacity and window
stagger decide which one, and `lead_accounts` (primary key on `lead_id`) means
the same lead is never claimed by two lanes. `onboarding.add_account` is the only
way a new one appears: owner confirmation, lane-only profile, and the profile must
already exist on disk before the row lands.

### Email (`email_sender.py`)

Hostinger SMTP via `smtplib` (ADR-5), **lane two** — the fallback for a lead with
no usable Instagram. The default cap is 20/day; the live box runs 0 with
`email_enabled = 0`, so nothing leaves until the owner arms it. The body is
the frozen copy file with no footer appended — footer-free by construction, not
by omission — and the channel refuses at three layers (queue gate, template gate,
SMTP credentials from `.env`).

---

## 8. The AI contract

### The endpoint

`ollama_adapter.py` is the only module allowed to speak to a model, and it speaks
only to loopback. A non-loopback URL is refused *before any I/O*, so a copied
`.env` cannot quietly send lead data to a hosted API.

- `generate()` retries a transient or model failure up to 3× (ADR-4).
- On exhaustion, `ask_with_guard()` flips `queue_status` to `PAUSED` and appends a
  `HALT` record. The dispatcher refuses to dispatch while the queue is paused.
- A **semantically invalid verdict is treated exactly like a dead model**: pause,
  HALT, never partially trust.

This is the fail-closed rule in one sentence: *an AI outage must never result in
unqualified outreach being sent.*

Two smaller rules sit on top of that and are easy to miss when reading the
adapter:

- **The model has a window.** Since 2026-11-15 no request is made before **20:55
  local** (`MODEL_WINDOW_START_MIN`). An 8B model held resident against a 6 GB
  card was competing with the IG browsers all day. A closed window raises
  `ModelWindowClosed`, which is deliberately **not** `OllamaDead`: it is the
  owner's schedule, not a fault, so it never pauses the queue or writes a HALT.
- **The model does not stay resident.** `KEEP_ALIVE_S = 30` lets a burst stay
  warm and lets the footprint go between bursts; the cost is one cold load, which
  is exactly what the gate's prewarm pays on purpose.

### Role #1 — ICP qualification (`icp.py`)

Question: is this an independent, thriving, single-branch restaurant? Reject
chains, multi-location venues, hotels and resorts, ghost kitchens, corporate-run
operations. Rating ≥4.0 and reviews ≥100 are **hints**; the model judges and
returns confidence plus a `reject_reason`.

The output is validated semantically before use. A verdict that does not parse, or
that contradicts its own reason, is a pause — not a default.

### Role #2 — dish extraction (`dishes.py`)

The model proposes a signature dish. **Code decides.** The validation gate requires
the dish to be grounded in this lead's own menu or review evidence, to be a sane
token shape, and not to be a generic non-dish word ("Food", "Menu", "flavors",
"freshness"). No valid dish → the no-dish variant, which is expected to dominate
because menu evidence exists on only a minority of scraper rows.

Junk words that used to reach `extracted_dish` ("full", "such", "flavors") are now
blocked by **folding the inflection**, not by growing a word list (shipped
2026-11-19, `3cc56f0`). A word list loses; a rule wins.

### The third, narrower use: the wall's judge

`proofread.screen` runs before any body reaches a browser. Layer 1 is mechanical
(em dash, banned phrases, rendered length). Layer 2 asks the model for a tone
view — and it is wired **only for generated variant copy** (`V1`–`V9`), never for
the owner-pinned templates. Even there it can veto, never rewrite, and its score
is recorded as advisory telemetry in `proofread_log`.

### What the model never does

No copy. No scheduling. No channel choice. No reply. No cap arithmetic. The
templates are frozen byte-exact and one `{{dish}}` hole is filled; any drift
outside that hole is a test failure, not a style question.

---

## 9. Personalisation: `{{dish}}` and review quotes

`dish_mining.py` mines evidence before the model is asked. It reads menu items and
review text, folds plurals and inflections, filters function words and generic
nouns, and produces candidate phrases with provenance. The model chooses among
candidates; the gate checks the choice is grounded.

The tiers that result (dish → review-quote → moment → none) are `forecast.py`'s
vocabulary for *describing* a lead; they do not drive dispatch order, which sorts
on dish, then evidence strength, then id (§6). `forecast.py` publishes them in
advance so two departments can agree on the order *before* it happens.

**Honest note:** dish annotations are historical, not reproducible. The same lead
re-mined today can yield a different phrase, because evidence is re-read from the
scraper's current rows and the gate's vocabulary has changed since.

---

## 10. The scraper contract

The scraper DB is read-only (ADR-3). Two files pair with the department:

### `SUPPLY_COUNTDOWN.{md,json}` — how much is left

- Fixed paths, next to `outreach.db`. **The path is the contract.**
- Written from **one payload** in one pass, so the Markdown and the JSON can never
  disagree.
- `schema_version: 1`. Renaming a key requires a migration, not a patch.
- Written on the first tick after 00:00 PKT, refreshed at 23:00 PKT.
- Every number is measured from the department's own database. **An input that
  cannot be measured is an omitted key, never an estimate.**
- Below 14 days of supply, both files carry
  `SCRAPER NEEDED: N days of supply at current rate` and the owner gets a toast.

### `DISPATCH_FORECAST.{md,json}` — which leads, in what order

- Same rules: fixed paths, one payload, schema v1.
- Lists the next three days of first-touch claims **in the dispatcher's own
  order**, each with its tier (`dish` / `review-quote` / `moment`), whether the
  lead has reviews, and the top-level enrichment debt.
- A lead claimed at a lower tier than forecast lands in the digest as
  `FORECAST DIVERGENCE` — because that is a counting bug, not luck.

---

## 11. Scheduling, windows and pacing

Windows are `config` rows, in **PKT**, not machine-local time (ADR-9). They are
data so the owner can change hours without a deploy. The seeded bands, both open
every day of the week, are **13:00–23:00 PKT** for `na` and **14:00–20:00 PKT**
for `me`. A window may carry a one-time `until` instant; past it the band is
closed no matter what its hours say, because an external one-shot teardown once
vanished and a catch-up band leaked for four days.

`seconds_to_next_wake` returns:

- `0` when inside an open window with work a gate would actually fire,
- the remainder of the 180–300 s gap or the 4/hour slot when the lane is paced,
  clamped to **300 s** so replies are still polled at least every five minutes,
- seconds until the next window when the region is closed,
- `None` when nothing is queued at all; the loop then sleeps an hour and
  re-checks.

An open window over a fleet that is spent for the day parks at the probe cadence
rather than `None`, because the probes run inside the cycle and a capped day must
not blind reply detection. A lane whose pause flag is set is not "work": counting
it as work pins wake at 0 and burns a core (live, 2026-11-04).

The tick order is fixed and matters:

1. **Replies first.** Email is polled, then the WA and IG in-thread probes. A
   reply registers before the ladder or dispatch can touch the lead, so a rung
   never lands on top of a conversation the owner owns.
2. **Follow-ups second**, through transport-only *resenders*.
3. **Engagement third**, after the ladder, so a lead that just replied is already
   out of the set this stage reads. Optional and fail-soft: a fault is one
   throttled row, never a held send.
4. **Dispatch last**, through end-to-end *senders*, until the window, caps,
   templates or the queue stop it — report, never crash.

Every stage is named twice to the heartbeat (on entry, then with its measured
seconds), and a stage past its budget writes one `STAGE OVERRUN` row. The budgets
are alarms; the refresh pass's own budget is the only hard stop.

Machine clock vs PKT is a real trap: this box has reported PST while window maths
is PKT-based. Anything that reasons about "today" must go through `clock.py` and
`caps.pkt_date`, never `datetime.now().date()`.

---

## 12. The engagement lane

The engagement lane is the part that makes accounts look alive without sending
anything: story replies, comments, warm-up, and ambient scrolling.

### Laws

- **Disarmed means silent.** `config.engage_enabled` seeds `False`. Nothing in
  `engage_stage.py` runs without it. Arming is an owner act, never a default.
- **One act per account per tick, hard.** `engagement.can_engage` decides (account
  status, `per_day` = 30 acts, 90 s spacing) *before* a page is touched, so a
  refusal costs no browser work and cannot half-log an act. The stage gives each
  account one act per tick; the cap is what stops a long tick from batching.
- **Ambient is a range, not a constant.** The owner's words were "80 min/day
  scroll, yes — sometimes 80, sometimes 60, sometimes 120". So
  `ambient_minutes_today` is clamped into `[60, 120]`, defaulting to 80, and a day
  that is spent is spent.
- **Five follows per day across all accounts**, not per account and not per
  session. A follow is the one ambient act the world can see.
- **A session is ~5 minutes**, sized to fit the engagement stage's 300 s budget.

### The pause law (changed 2026-11-19)

A pause used to silence the whole lane, including scrolling. It now means: **no
public act** — no comments, no story replies, no DM warm-up — while **ambient
scrolling may keep running** inside the night window, with follows at 0. The
reason is an industry switch: accounts that go cold for weeks come back looking
like dormant bots.

With `ambient_config.enabled = false`, the old guarantee stands: a paused lane
opens **no browser at all**. Both laws are pinned in the private suite.

### What is not proven

No public act has ever run against Instagram: the stage is **armed but owner-paused**
(`engage_enabled` is true and `engage_paused` was set on 2026-11-19 for the industry
switch), so nothing runs until the owner resumes it. Selectors (the `og:description`
caption, the comment box) are pinned against a fake surface, not observed on a live
post. That observation is the first thing to watch when the owner resumes it.

---

## 13. Replies and human handover

Any reply means the same three things, always:

1. every pending follow-up is cancelled — exactly once,
2. the lead moves to `REPLIED`,
3. an owner alert lands in `digest`.

And one thing never happens: **the system never auto-replies.** The owner takes
every conversation from the moment it exists.

### Detectors

- **Email** — a pure matcher over parsed messages; the IMAP poller feeds real
  messages in, and unreachability is fail-closed.
- **WhatsApp** — in-thread probe, walking active threads.
- **Instagram** — in-thread probe per thread.

### The probe's budget (measured)

The reply probe bounds its state wait to **8 s** and its whole pass to **200 s**. A
real pass over 16 threads takes **55–63 s**, down from **426.5 s** before the
2026-11-17 fix. That is the single largest performance win in the project, and it
came from bounding a wait rather than from parallelising anything.

Undoing a wrong reply is an owner action (see `OPERATIONS.md`, "Replies — how a
reply is decided, and how to undo a wrong one").

---

## 14. Failure taxonomy

Measured over 2026-11-11 → 2026-11-18 UTC, from the department's own `digest` and
`message_log` (re-run 2026-11-21, so the window reproduces):

| Class | Count | Reading |
|---|---|---|
| `IG REPLY PROBE ERROR` | 72 | 57 `net::ERR_*` (one of them `ERR_HTTP_RESPONSE_CODE_FAILURE`), 14 "interrupted by another navigation", 1 `ProfileBusy`. |
| `LintBlocked` | 18 | All on 2026-11-12 and 11-13, ending with the `COPY_HOLD` ruling; the copy lint refusing a render. |
| `IG SEND ERROR` | 0 | Sends themselves did not fail in the window. |

Seventy-two probe errors against **58 Instagram sends** in the same week is the
project's most misleading number: it looks like a broken channel. It is not a send
failure at all — it is the *read* surface failing while the write surface works.

### The shell-page finding (DT-23)

The comment lane was assumed blind because sessions were logged out. Live rows say
otherwise:

```
articles=0  anchors=0  body_len=763–1085  login=False  dead=False
```

on every sampled profile. That is a **shell page** — the HTML frame with no grid —
not an empty account and not a logged-out wall. Any new industry inherits this bug,
because it is about how Instagram serves a profile to automation, not about
restaurants.

### Fallbacks that exist because of a failure

| Failure | Response |
|---|---|
| `net::ERR_*` on the `ig.me` shortcut | retry on the profile page (`c1b5496`) |
| Instagram will not load either DM surface | `TRANSPORT_HOLD` — a park, not a verdict |
| Copy lint refuses the render | per-lead `COPY_HOLD`, not a cycle abort |
| Model unreachable or invalid | queue `PAUSED` + `HALT` record |
| Engine exception | loop parks with a short retry; `ENGINE ERROR` in the digest |

### What the ledger is for

`INCIDENT_LEDGER.md` holds one row per failure *class*, with the trigger that would
mean it moved. A new class, or a class that changed shape, gets a row. Numbers
without a class are noise; a class without a trigger is decoration.

---

## 15. Verification: the gate

`python gate.py check` is the only authority on "done". Exit 0 is green; anything
else is a red you must read.

Neither `gate.py` nor `tests/` ships in this tree: the suite's fixtures are real
scraped rows, so it stays on the owner's machine. What follows is what the gate
enforces there, not a command you can run from this checkout.

| Stage | What it proves |
|---|---|
| `[1]` pytest + coverage ≥ 80 % | The behaviour still holds, and the suite still reaches the parts most likely to break. |
| `[2]` stub scan under `src/` | No `TODO`, `FIXME` or `pass #` hiding in production code. |
| `[7]` secrets scan | No credential shape in the tracked tree, and `.env` must never be git-tracked. |
| `[10]` tree clean | A run leaves the tree as it found it — no stray screenshots in the root. |

Gates **3–9** are pytest tests, so `[1]` runs them. The
suite is wall-bounded at **1800 s** (`PYTEST_WALL_S`): a wedged dependency becomes a
named failure instead of a silent hang, which is worse than a red.

### `--no-prewarm` and `--offline`

Three commands, because there are three questions:

| Command | Touches the model | Live-model tests |
|---|---|---|
| `gate.py check` | prewarms the 8B | all run |
| `gate.py check --no-prewarm` | never | all run (use when the model is already resident) |
| `gate.py check --offline` | never | four deselected **by node id and printed by name** |

The split exists because the first flag used to do both jobs: it promised "don't
load the model" and delivered "don't load the model *and* stop verifying it".
`--offline` implies `--no-prewarm` (loading a model for tests you just removed is
waste); the reverse is a speed flag quietly deleting coverage.

Why prewarm at all: a **cold** 8B load on this box takes ~316 s (3.5 GB CUDA plus
282 MiB CPU), longer than the live tests' own request timeout, so a cold gate
reported RED for no code reason on 2026-11-16. The gate pays the load once, on
purpose, and prints how long it took.

An offline gate is a **weaker** gate and says so: the four live-model paths (b4
generate, b4 liveness contract, b5 judge smoke, b6 extraction smoke) are not
verified. A run must be green with the model up before a release.

A seventh pin exists for a subtle reason: pytest accepts an unknown `--deselect`
node id and simply deselects fewer, while the gate prints `len(LIVE_MODEL_TESTS)`
regardless — so a renamed test would have printed four names and run three.

---

## 16. Owner switches and the pause law

Everything that stops outreach is a `config` row, readable and reversible without
touching code.

| Switch | Effect | Undo |
|---|---|---|
| `queue_status = PAUSED` | `claim_next` refuses; nothing is sent | `python -m outreach.command_center resume` |
| `engage_paused` | no public engagement act; ambient depends on `ambient_config` | `... resume --engage` |
| `engage_enabled` | the engagement stage's master arm, seeds `False` | owner-only |
| `daily_caps` | per-channel caps, `set_cap` validates 0..100 | `set_cap` again |
| `wa_paused` | the WhatsApp lane is skipped by claim and by the ladder | unset the row |
| `ig_halted` | the Instagram lane is skipped the same way (ADR-8) | unset the row |
| `email_enabled` | the email lane sends only when armed (ADR-5) | owner-only |
| `ig_cold_dms_blocked` | cold IG first touches are refused until a `Delivered` bubble is observed | `cold_probe`'s three daily slots, or `ig_delivery_confirmed` |
| template owner-gate | dispatch refuses until the owner confirms the copy | owner-only |
| `ambient_config.enabled` | ambient scrolling on/off inside the lane | owner-only |
| `fleet_caps_enabled` | a channel's ceiling becomes the sum of its accounts' caps | owner-only |

### Three tiers of decision

1. **The system decides** what the laws allow: routing, ordering, claim, commit.
2. **The owner decides** what the laws cannot: who gets messaged (arming lanes),
   what the copy says, money, and any new standing law.
3. **The model decides** two things, both refusable by code.

A change that moves a decision from tier 2 to tier 1 is a standing-law change and
needs an owner ruling, not a commit.

---

## 17. Ops runbook

Every runnable command lives in `OPERATIONS.md`; this is the subset a new reader
needs.

```bash
# status (read-only)
PYTHONPATH=src python -m outreach.command_center status

# the gate runs on the owner's machine only: gate.py and the suite it drives are
# not in this tree (see §15)

# the engine (cwd src) — one instance, the lease refuses a second
pythonw -m outreach.engine_loop
```

Stopping the engine: read the pid from `.engine_lease.json` and stop **that pid**.
Never kill by name — a pattern kill can hit the shell you are typing in.

The engine is supervised by a Windows scheduled task and started by a launcher
generated by `python -m outreach.setup_startup`; `--remove` takes it out. The
watchdog task is `OutreachEngineWatchdog`.

Restarting after a code change: stop, run the gate, restart. A code change that
has not passed the gate is not a code change, it is a hypothesis.

---

## 18. Decision log

The full argument for each ruling is in `DECISIONS.md`; one row per ruling with the
file that enforces it is in `DECISIONS_LOG.md`. The shape of the log is the point:
**a ruling with no test is a wish.**

| Date | Ruling | Enforced by |
|---|---|---|
| 10-26 | One first touch per lead; the ladder owns everything after | `dispatcher`, `followups` |
| 10-27 | Browser channel is Chromium with `--profile-directory`, proven by cookie comparison | `browser.py` |
| 10-31 | Cold WhatsApp is outlawed: Instagram → email, and WhatsApp only by the Canada order-line exception | `router.route_lead` |
| 11-03 | Scraper contamination: quarantine, never guess; the scraper is read-only | `cleaning`, `sync` |
| 11-04 | Junk identity is one rule, asked at intake, judgement and claim | `cleaning.junk_identity_reason` |
| 11-05 | Two follow-ups — day 3 and day 7 — and the probe asks the ladder before a rung fires | `followups.MAX_LADDER_TOUCH`, `replies` |
| 11-09 | A rung more than 72 h past its due date is expired, not sent | `followups.expire_overdue_rungs` |
| 11-11 | `ig1` is follower-gated: no cold first touch until a `Delivered` bubble is observed | `ig_sender.cold_dms_blocked` |
| 11-14 | Three dish-bearing cold touches a day are the measured exit from that gate | `cold_probe` |
| 11-14 | Ambient scrolling is a range (60–120 min/day, default 80) with five follows a day | `ambient` |
| 11-05 | No em dash in lead-facing copy, enforced mechanically (refuse, never rewrite) | `variants.lint` |
| 11-07 | Dish-bearing leads claim first; weak evidence claims later but is never excluded | `dispatcher.claim_priority` |
| 11-13 | Parks, not verdicts: `COPY_HOLD` / `TRANSPORT_HOLD` return only by explicit repair | `machine.ALLOWED` |
| 11-18 | `net::ERR_*` on `ig.me` falls back to the profile page | `ig_sender` |
| 11-19 | A pause stops public acts; ambient may continue inside the night window | pause pin, private suite |
| 11-20 | Skipping the model load is not the same as skipping the model's tests | offline-gate pin, private suite |

---

## 19. Known limitations

Written to be read before trusting the numbers.

1. **Instagram is half-blind.** Sends work; reads return a shell page
   (`articles=0`, `anchors=0`, body 763–1085 chars) with no login wall and no dead
   account. Comment targeting on Instagram is therefore unproven, and the 72 probe
   errors/week are a read-surface symptom, not a send failure.
2. **Nothing public has run on Instagram.** The engagement stage is disarmed by
   default and its selectors are pinned against fakes. First live arming will be
   the real test.
3. **Dish annotations are not reproducible.** Re-mining the same lead later can
   yield a different phrase; the evidence is re-read and the gate's vocabulary has
   changed.
4. **Email is gated.** The channel exists, is capped at 20/day, and stays disabled
   until the owner pastes the template and opens the gate.
5. **The offline gate is weaker than the full one.** Four live-model paths are
   deselected and named; a release needs one green run with the model up.
6. **Windows are PKT.** The machine clock is not always PKT; anything reasoning
   about "today" must go through `clock.py`.
7. **Cold Instagram is gated, not working.** `ig_cold_dms_blocked` is armed and no
   `Delivered` bubble has been observed on the live lane, so ordinary cold first
   touches are refused (`follower_gated`) and only the three daily probe slots can
   send. The 7-day send counts in §14 predate that gate; do not read them as the
   current rate.
8. **The proofread wall has a model layer for generated copy only.** A veto on a
   generated variant is a real refusal; the owner-pinned templates are judged by
   the mechanical layer alone, so a stylistic problem in owner copy will ship.
9. **A daily cap above 20 is not actually usable.** `set_cap` validates 0..100,
   but migration 1's `daily_sends` CHECK stops a stored count at 20, so arming a
   lane at 21+ raises at the 21st commit. No lane runs above 20; raising one needs
   a migration, not a config edit.

---

## 20. Glossary

| Term | Meaning |
|---|---|
| **claim** | The dispatcher preparing a send. Writes nothing. |
| **commit_send** | The atomic landing of a send: cap + log + state, one transaction. |
| **copy wall** | The rule that templates are frozen and only `{{dish}}` fills. |
| **hold** | A park awaiting repair (`COPY_HOLD`, `TRANSPORT_HOLD`, `REGION_UNRESOLVED`). |
| **lane** | One account on one channel. The live registry is `ig1` and `wa1` (active), plus `ig2`–`ig4` warming. |
| **ladder** | The follow-up sequence: touch 1 → day 3 → day 7. |
| **park** | A non-terminal state the lead can leave only by an explicit repair. |
| **rung** | One step of the ladder. |
| **tier** | Personalisation quality: dish → review-quote → moment → none. |
| **wake** | One engine loop pass, triggered by `seconds_to_next_wake`. |
| **PKT** | Pakistan Standard Time, the timezone all windows and caps use. |

---

*Corrections belong in this file, not in a new one. If a claim here stops being
true, the change that made it false is responsible for updating it.*
