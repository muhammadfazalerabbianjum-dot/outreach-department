# Outreach Department

**A deterministic outreach engine for independent restaurants.** One owner, one
queue, three channels. Scraped leads enter a SQLite state machine and leave as
personalised first touches on **Instagram → Email → WhatsApp**, in that order,
inside the owner's working windows, under hard daily caps.

It is not a mass mailer with a spreadsheet attached. It is a queue with laws:
every send is claimed, checked, committed atomically, capped, paced, logged and
screenshot-backed, and anything the engine cannot lawfully send it parks with a
reason instead of guessing.

> **This is the short README.** It names every feature so you know what is in the
> box. The full engineering document — architecture, data model, state machine,
> failure taxonomy, decision log, known limitations — is
> [`docs/DEEP-DIVE.md`](docs/DEEP-DIVE.md). That relative link is the only one
> written down, so it renders on GitHub and works locally, and a fork keeps
> working without an edit.

- 65 Python modules, ~21.7k lines under `src/outreach/`
- stdlib-first: SQLite, Playwright, `smtplib`/`imaplib`, `urllib`. No framework.
- **Engine code only.** The test suite, `gate.py` and the live databases are not in
  this tree: the fixtures are real scraped rows, so they stay on the owner's machine.

---

## How it works

```
scraper (read-only)
     │  python -m outreach.sync
     ▼
  intake ──► cleaning ──► ICP qualification ──► routing ──► dispatcher
                             (AI #1)                          │
                       ┌──────────────────────────────────────┼──────────────┐
                       ▼                        ▼                        ▼
                 Instagram sender          email sender             WhatsApp sender
                       │                        │                        │
                       └──────────── commit_send (one transaction) ───────┘
                                        │
                                        ▼
                    outreach.db — leads, state_history, daily_sends,
                    message_log, followups, digest, config, accounts
                                        │
              ┌─────────────────────────┼──────────────────────────┐
              ▼                         ▼                          ▼
        reply probes              digest / alerts        SUPPLY_COUNTDOWN.json
        (handover)                (owner surfaces)       DISPATCH_FORECAST.json
```

Nothing lives in memory between ticks. Wake time, caps, windows, pending
follow-ups and every switch are rows in the database, so killing the process at
any instant loses nothing: the next process turns the same crank.

---

## Every feature

### Queue and state machine

- **A frozen transition map.** Seventeen states, one `ALLOWED` dict (sixteen keys
  — `NO_REPLY` is a terminal nothing writes any more, kept so old rows stay
  legal), and an `InvalidTransition` for every edge that does not exist. Only
  `ROUTED` is claimable, which is why no lead can be messaged twice by accident.
- **Append-only history, enforced by the database.** `BEFORE UPDATE` and
  `BEFORE DELETE` triggers abort on `state_history`. A transition and its history
  row commit together, so the audit trail cannot disagree with the row.
- **Parks, not verdicts.** `COPY_HOLD`, `TRANSPORT_HOLD`, `DEPRIORITIZED` and
  `UNREACHABLE` are places a lead waits for an explicit repair, never a quiet
  death. `park_kind` stores the park's class (`lane_law` vs `verdict`) so a repair
  never parses prose.
- **A revive path.** `router.reroute_stranded` re-decides parked rows under the
  same pure lane law, so a phone-only park is not a one-way door — 554 lane-law
  rows were still eligible on 2026-11-21, 536 of them phone-bearing.
- **Reason discipline.** `REJECTED` and `DUP` refuse to fire without a reason. For
  `REJECTED` (and the three holds) the reason also lands on the row itself, so
  `holds` is not the only place to learn why; `DUP` keeps its reason in history.

### Intake and cleaning

- **One read-only arrow from the scraper.** `sync` and `scraper_adapter` open the
  scraper database with `mode=ro`; the department never writes to it.
- **Three-stage dedupe**: `place_id` → normalised phone → fuzzy name+city.
- **Junk identity is one rule**, asked at intake, at judgement and again at the
  send boundary, so a row that predates the rule still cannot send.
- **Contacted-elsewhere signal** imported and kept out of the queue.
- **Contamination quarantine for email**: an address must sit on the lead's own
  website domain or carry an explicit verification flag. Platform inboxes and
  free mailboxes never become a lane.
- **Targeting policy** parks delivery-only, chain and brand accounts before a send,
  with a follower-count read (and `unknown` rather than a guess) feeding it.
- **Their clock, not ours**: venue hours and busy times park a closed kitchen
  instead of messaging into it. Unknown hours change nothing.

### The two AI jobs

- **ICP qualification** — is this an independent, thriving, single-branch
  restaurant? Rating and review count are hints; the model judges and must return
  a reason for a rejection.
- **Dish extraction** — propose the dish a restaurant is known for. **Code
  decides**: the dish must be grounded in that lead's own menu or review evidence,
  must be a sane token, and must not be a generic non-dish word. Inflections are
  folded rather than blocked with a growing word list.
- Both live behind `ollama_adapter`, the only module allowed to speak to a model:
  loopback only, three retries, and on exhaustion the queue **pauses** with a HALT
  record. A semantically invalid verdict is treated exactly like a dead model.
- The model has a **window** (nothing before 20:55 local) and a short keepalive, so
  an 8B model does not hold the whole GPU all day.

### Copy and the wall

- **Frozen templates, byte-exact.** SHA-256 pinned per file; the only sanctioned
  fill is `{{dish}}` (plus the owner's name/restaurant/platform tokens). Any drift
  is a test failure, not a style question.
- **A variant engine, off by default.** `variant_engine` switches Instagram first
  touches to the owner's nine-variant packet: byte-pinned scripts, P.S. rotation
  that never repeats inside a segment, and `V5` capped at 30% of the day's IG cap.
  `V1` is the only composed copy — bounded generation gated on confidence ≥ 0.8,
  grounding in the post text and a genericness self-check — and a dead model
  degrades to the frozen fallback instead of pausing the queue.
- **A copy lint that refuses, never rewrites**: no em dash, banned phrases,
  rendered-length checks. A refused render parks that one lead in `COPY_HOLD`
  instead of aborting the cycle.
- **The proofread wall** sits between a render and a browser: mechanical layer for
  every body, an advisory model tone layer for generated copy only, and every
  verdict recorded in `proofread_log`.

### Routing and dispatch

- **One lane law, pure and shared**: active Instagram (last post ≤ 30 days) → an
  evidence-backed email → WhatsApp only through the Canada order-line exception.
  No lawful channel parks the lead.
- **`claim_next` writes nothing. `commit_send` writes everything.** Cap increment,
  `message_log` row (including the exact body the recipient read), the state edge,
  the account health stamp and the next send gap land in **one transaction**. A
  failed send is indistinguishable from a send that never happened.
- **Dish-bearing leads claim first**, then owner-known/strong-evidence leads, then
  lead id. Weak evidence is claimed later, never excluded.
- **Hard pacing**: a randomized 180–300 s break between any two sends, plus a
  rolling four-sends-per-hour slot per platform.
- **Daily caps, validated**: 15 WhatsApp, 15 Instagram, 20 email by default;
  `set_cap` refuses anything outside 0–100, so a typo cannot silently stop a lane
  or blow the day's risk budget.
- **Warm-up ramps**: 5/10/15 per channel week, and a newly onboarded account sends
  **no DMs at all** in week 1 (engagement only), 10 in week 2, 15 after.
- **The follower gate**: while a lane has no observed `Delivered` bubble, cold
  first touches are refused and three dish-bearing probe slots a day measure the
  way out. The first delivered bubble opens the gate permanently.

### The follow-up ladder

- **Day 3, then day 7.** Two rungs, anchored on the first send; a stale row from a
  retired cadence is expired rather than replayed as a burst.
- **A 72-hour grace window.** A day-3 touch arriving on day 13 is a mass mailer,
  so it expires with a durable reason instead of sending.
- **One-shot Instagram escalation** for a WhatsApp-routed lead that missed day 7,
  riding the same caps, ramps, gap, slot and window laws.
- **A reply kills the ladder exactly once**, and a rung never fires on a channel
  whose reply probe has gone stale.

### Replies and handover

- **Three detectors**: a pure email matcher over parsed IMAP messages, a WhatsApp
  in-thread probe, and an Instagram in-thread probe.
- **The system never auto-replies.** Any reply cancels pending rungs, moves the
  lead to `REPLIED`, and puts a real conversation row in the owner inbox.
- **A measured read path**: the IG probe bounds each state wait to 8 s and the
  whole pass to 200 s — 16 threads in 55–63 s, down from 426.5 s before the fix.
- Replies are throttled and deduplicated so a probe outage cannot flood the digest
  or the desktop.

### The engagement lane

- **Story replies, comments, follows, likes and warm-up** — the acts that make an
  account look alive without sending a DM.
- **One act per account per tick**, 30 acts/day per account, 90 s spacing, decided
  before a page is touched so a refusal costs no browser work.
- **A confidence gate and a copy veto** for comments: a refused comment is kept for
  the owner in `comment_review` instead of posted.
- **Ambient scrolling is a range, not a constant**: 60–120 minutes a day, default
  80, split unevenly across lanes with a plan that is written to the digest, and
  **five follows a day for the whole fleet**.
- **A pause stops public acts.** Scrolling may continue inside the night window at
  zero follows; with ambient disabled, a paused lane opens no browser at all.

### The fleet

- **An account registry** with per-account profile dirs, status, ramp week and
  staggered windows.
- **An assignment ledger with a primary key on the lead**, so the same lead is
  never claimed by two accounts.
- **Onboarding that fails closed**: owner confirmation, automation lane only, the
  profile must already exist on disk, then a `WARMING` row.
- **Per-lane session checks** read Chrome's cookie store read-only instead of
  guessing whether a login survived.

### Contracts for the next department

- **`SUPPLY_COUNTDOWN.{md,json}`** — how many days of supply are left at the
  measured rate, written from one payload so the two files cannot disagree, schema
  v1, alerting below 14 days.
- **`DISPATCH_FORECAST.{md,json}`** — which leads the next three days will claim,
  in the dispatcher's own order, with each lead's measured tier.
- **A forward-contract check**: a lead claimed below its forecast tier lands in the
  digest as a divergence, because that is a counting bug, not luck.

### Operations

- **`command_center`** — operator CLI: `status`, `run`, `pause`, `resume`,
  `alerts`, `intake`, `plan`, `fleet`, `replies`, `inbox`, `holds`,
  `resolve-region`, `variants`, `add-account`.
- **`engine_loop` + `watchdog`** — a reboot-safe loop with a heartbeat file, a
  lease file, and a Task Scheduler check that re-spawns a gone engine and refuses
  to double-spawn a hung one.
- **`startup_readiness`** — pre-flight checks before the loop starts.
- **`heartbeat` / `health` / `alerts` / `toast`** — stage-level liveness, lane
  health, P0/P1 escalation, and an optional local balloon on new digest rows.
- **`agent_api`** — a read-only JSON snapshot for a supervising agent, opened
  `mode=ro`, with no path to the dispatcher.
- **`dashboard`** and the `digest` table — every owner-facing event in one place.

### The gate

- **`python gate.py check`** is the only authority on "done" — and it is **not in
  this tree**. The suite it runs is private, because every fixture is a live scraper
  row. What it proves:
  - `[1]` full pytest suite with a coverage floor of 80%
  - `[2]` a stub scan under `src/` (no TODO/FIXME/pass-comment in production code)
  - `[7]` a secrets scan (no credential shape in the tracked tree, `.env` never tracked)
  - `[10]` a tree check (a run leaves no stray screenshot in the repository root)
  - gates 3–9 are pytest tests, so `[1]` runs them
- **Three commands, three questions**:
  `check` (prewarms the 8B and runs everything), `check --no-prewarm` (no model
  contact, every test still runs), `check --offline` (never contacts the model,
  deselects the four live-model tests **by node id and prints them by name**).
- **A wall bound on the suite** (1800 s), so a wedged dependency becomes a named
  failure instead of a silent hang.
- **A stale-node-id guard**: pytest accepts an unknown `--deselect` target and
  silently deselects fewer, so a seventh pin asserts every named live test still
  exists.

---

## What it refuses to be

- **Not a scraper.** Scraping lives in a separate project; this system treats it as
  read-only and has exactly one arrow out of it.
- **Not a CRM.** No pipeline board, no deal stages. A reply is a handover trigger,
  not a "conversation".
- **Not a LinkedIn bridge.** A previous build drifted into one and was archived;
  any new LinkedIn code is a red batch with no exceptions.
- **Not a copywriter.** The model writes no send copy: every body is a frozen
  template with one hole punched in it, and the single bounded exception (variant
  `V1`) is off by default and gated before it can ship.

---

## Safety model in one table

| Risk | What stops it |
|---|---|
| Messaging the same lead twice | only `ROUTED` is claimable; the first commit moves it to `SENT` |
| A send half-counted after a crash | cap + log + state land in one transaction |
| Too many messages, too fast | daily caps, a 180–300 s gap, a rolling 4/hour slot |
| Sending to the wrong people | ICP verdict, junk-identity rule, email quarantine, targeting parks |
| An AI outage sending junk | fail-closed: retries, then PAUSE + HALT, and the dispatcher refuses |
| A bad body reaching a browser | the copy lint and the proofread wall, both before the transport |
| A lead lost to a bad verdict | parks with reasons, and an explicit repair edge for each |
| An account burned | warm-up ramps, the follower gate, halts on a login wall, no auto-login |
| An owner surprised | every stop writes one throttled digest row with a reason |

---

## Measured on the live box

Numbers are from the department's own database and logs, not from a benchmark:

- **Reply probe**: 16 threads in **55–63 s**, from **426.5 s** before the 2026-11-17
  bound (an 8 s state wait per thread, a 200 s pass budget).
- **Failure split, 2026-11-11 → 11-18 UTC**: 67 `IG REPLY PROBE ERROR` against
  **58 Instagram sends** — a *read*-surface failure, not a send failure.
- **The shell page**: Instagram serves `articles=0 anchors=0 body_len=763–1085
  login=False dead=False` to an automation profile. That is a frame with no grid,
  not an empty account and not a login wall.
- **Copy lint**: 18 `LintBlocked` renders, all before the per-lead `COPY_HOLD` fix,
  which is why one poison body no longer aborts a cycle.

---

## Run it locally

```bash
# install (Windows, Python 3.10+)
pip install -e .
python -m playwright install chromium

# the scraper boundary and the queue
PYTHONPATH=src python -m outreach.sync
PYTHONPATH=src python -m outreach.command_center status

# the gate and its test suite are not published (see above), so there is no
# gated test command to run from this tree

# the engine (one instance; the lease refuses a second)
cd src && pythonw -m outreach.engine_loop
```

Stopping the engine: read the pid from `.engine_lease.json` and stop **that pid**.
Never kill by name — a pattern kill can hit the shell you are typing in.

---

## Honest limitations

Read these before trusting a number.

1. **Cold Instagram is gated, not running.** The live lane has no observed
   `Delivered` bubble yet, so ordinary cold first touches are refused and only the
   three daily probe slots can send.
2. **Instagram reads are half-blind.** Sends work; profile reads return the shell
   page. Comment targeting against a live post is therefore unproven, and the weekly
   probe errors are a read symptom.
3. **Nothing public has run on the engagement lane.** The stage ships disarmed, and
   its selectors are pinned against fakes, not observed on a live post.
4. **Dish annotations are historical, not reproducible.** Re-mining the same lead
   later can yield a different phrase; the evidence is re-read and the gate's
   vocabulary has changed.
5. **Email is built but disarmed.** The channel exists, capped at 20/day by default,
   and the live box runs it at 0 until the owner arms it.
6. **The offline gate is weaker than the full one.** Four live-model paths are
   deselected and named by the gate; a release needs one green run with the model up.
7. **Windows are PKT.** The machine clock is not always PKT; anything reasoning
   about "today" must go through `clock.py`.
8. **A daily cap above 20 does not actually work.** `set_cap` accepts up to 100,
   but the send ledger's `CHECK` stops a stored count at 20, so a lane armed at
   21+ raises on its 21st send. No lane runs above 20 today.

---

## Go deeper

The short version stops here. The full document covers, in order: what the project
refuses to be, the shape of the machine, the module map, the data model and every
migration, the state machine and each non-obvious edge, dispatch order and pacing,
channel internals, the AI contract, personalisation, the scraper contract,
scheduling, the engagement lane and the pause law, replies and handover, the failure
taxonomy, the gate, owner switches, the ops runbook, the decision log, known
limitations and a glossary.

**Read it on GitHub:** [`docs/DEEP-DIVE.md`](docs/DEEP-DIVE.md) — GitHub renders
it as the full document, and the same relative link opens the file locally.

---

*Corrections belong in the deep dive, not in a new file. If a claim there stops
being true, the change that made it false is responsible for updating it.*
