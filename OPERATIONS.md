# OPERATIONS — running the outreach engine

Everything here is a command you can run and a result you can check. No step depends on memory.

Working directory for all engine commands: the repo's `src` directory
Python: `PYTHONPATH=src` from the repo root for tools; the engine boot uses the package directly.

---

## 1. Start / stop / verify the engine

```bash
# who should be running?
cat ../.engine_lease.json      # {"pid": N, "started_at": "..."}
cat ../.heartbeat.json         # stamped_at, next_wake_at, stage

# start (single instance only - the lease refuses a second engine)
cd <repo>/src && pythonw -m outreach.engine_loop

# stop (ONLY the leased pid; never kill by name - the agent's own command line
# contains the same words and a pattern kill can take out the cleanup shell)
taskkill //PID <pid> //F
```

**Healthy looks like:** lease pid alive, `next_wake_at` in the future, `stage` advancing every cycle, and no
`ENGINE ERROR` rows in `digest` for the current window. A heartbeat whose promise expired by more than 15 min
is `heartbeat_stale` — the watchdog restarts it.

`.heartbeat.json` also carries `stages` — the current cycle's stage trace with measured durations, e.g.
`[{"stage": "boot", "seconds": 0.0}, {"stage": "replies", "seconds": 1.2}, …, {"stage": "dispatch", "seconds": 0.4}]`.
Read it to answer "where is the cycle right now and what is slow", without waiting for the cycle to end.

**Windows:** `config.session_windows` — `na` 13:00–23:00 PKT, `me` 14:00–20:00 PKT. Silence outside the
window is normal; do not treat it as death.

## 2. What is live right now

```bash
cd <repo> && python - <<'PY'
import sqlite3, json
db = sqlite3.connect("file:outreach.db?mode=ro", uri=True); db.row_factory = sqlite3.Row
print(dict(db.execute("SELECT state, COUNT(*) FROM leads GROUP BY state")))
print("sends today:", db.execute(
  "SELECT COUNT(*) FROM message_log WHERE substr(sent_at,1,10)=date('now')").fetchone()[0])
print("cap/used:", db.execute("SELECT value FROM config WHERE key='daily_caps'").fetchone()[0],
      db.execute("SELECT count FROM daily_sends WHERE date=date('now')").fetchall())
PY
```

The two contract files are the daily health gauge:
`SUPPLY_COUNTDOWN.md` (queue, lanes, `sent_today`, days of supply) and
`DISPATCH_FORECAST.md` (next claims by tier, `cap_total`, `today_capacity_remaining`).

## 3. Change a daily cap (the only safe external knob besides pausing)

Never edit `config.daily_caps` JSON by hand — use the validated setter, which refuses unknown channels,
bools, negatives and anything above 100:

```bash
PYTHONPATH=src python -c "
from outreach.store import Database
from outreach.caps import set_cap
with Database('outreach.db') as db: print(set_cap(db, 'ig', 15))"
```

No restart needed: the engine reads the cap every tick.
**Owner-only in effect:** changing a cap changes how much outreach happens. Treat 0 as "stop the channel".

## 4. Pause / resume

| switch | key | effect | restart? |
|---|---|---|---|
| whole queue | `config.queue_status` = `"PAUSED"` | fail-closed: no claims, watcher stops | no |
| WhatsApp | `config.wa_paused` | lane parked (owner directive) | no |
| email | `config.email_enabled` = 0 | email never claimed (ADR-5) | no |
| cold DMs | `config.ig_cold_dms_blocked` | cold first touches blocked | no |
| IG lane | `config.ig_halted` | drains IG leads to `CHANNEL_BANNED` | no |
| engagement lane (comments + scrolling) | `config.engage_paused` | lane refuses, digest says why | no |
| engagement window | `config.engage_window` | the hours the lane may act at all | no |

Pausing is always safe. **Un-pausing is not**: `ig_cold_dms_blocked` and `ig_halted` are owner-only.

## 5. Quality gate (before every commit)

```bash
# gate.py is not published with this tree; this runs on the owner's machine
PYTHONPATH=src python -u gate.py check 2>&1 | tail -20   # GATE: GREEN / RED + GATE_EXIT
PYTHONPATH=src python -m pytest tests -q --no-cov          # focused runs while iterating
```

The gate is the only authority on "done": pytest+coverage (≥80 %), stub scan, secrets scan. A gate that
passes for the wrong reason is a defect, not a win.

## 6. Reading failures

```bash
# last day of digest, newest first
cd <repo> && python -c "
import sqlite3; db=sqlite3.connect('file:outreach.db?mode=ro',uri=True)
[print(r[0][:19], r[1][:120].replace(chr(10),' ')) for r in
 db.execute('SELECT created_at, body FROM digest WHERE created_at >= datetime(\"now\",\"-1 day\") ORDER BY id DESC LIMIT 30')]"
```

Prefixes that mean something specific:

| prefix | meaning | first action |
|---|---|---|
| `SENDER ERROR [ig]` | the send path raised | read the repr; check IL-1 |
| `SEND SKIPPED [profile_no_message] lead N` | that profile offered no Message affordance; the lead was skipped for this pass and stays `ROUTED` | check whether our Message selectors are stale (R9); other leads kept sending |
| `SEND BLOCKED [thread_slow]` | the thread never painted inside the deadline | a slow session, not a dead lead; it retries |
| `SEND BLOCKED [profile_no_message]` | pre-R9 shape only: a lane-level stop on the same fact | should not appear; if it does, the skip branch is not wired |
| `SEND BLOCKED [|<reason>|]` | fail-closed stop before typing | the reason names the gate |
| `ENGINE ERROR` | the cycle itself threw | must not happen; file an IL row |
| `PROOFREAD ALERT / VETO` | the wall refused copy | telemetry; owner copy is never edited by it |
| `HOLD RELEASED` | a wall hold cleared | informational |
| `DELIVERY CONFIRMED` | the milestone | surface immediately to the owner |

## 7. Holds and parked leads

- `COPY_HOLD` — the message lint refused the render. Reason in `state_history`. Fix the evidence, then
  transition back to `ROUTED`. Never weaken the lint.
- `TRANSPORT_HOLD` — Instagram would not load either DM surface. Not a dead lead; park, retry later, one
  repair edge back to `ROUTED`.

## 7a. The wall's open holds (R7, 2026-11-13)

A vetoed render holds its lead until the owner clears it. Nothing clears automatically, ever.

```bash
PYTHONPATH=src python -m outreach.command_center holds          # inventory, read-only
PYTHONPATH=src python -m outreach.command_center holds --log    # + one HOLDS INVENTORY digest row
PYTHONPATH=src python -m outreach.command_center holds --clear 481   # owner judgement: one explicit lead
```

- Each hold is classified by the layer that spoke: `copy_lint` (deterministic layer 1 — a real copy bug) or
  `model_veto` (layer 2 — the dish could not be defended).
- `--clear` without a matching open hold exits 2 rather than pretending to have cleared something.
- Live 2026-11-13: 9 holds, all `model_veto` (dish grounding), zero copy bugs.

## 7b. Arming the engagement stage (O1, 2026-11-13)

The stage that comments, warms up and warms accounts ships **OFF**. Turning it on is an owner decision: a
comment is public and attributable, so it changes what the world sees.

```bash
# is it armed?
PYTHONPATH=src python -c "
from outreach.store import Database, get_config
with Database('outreach.db') as db: print(bool(get_config(db, 'engage_enabled')))"

# arm / disarm (owner only)
PYTHONPATH=src python -c "
from outreach.store import Database, set_config
with Database('outreach.db') as db: set_config(db, 'engage_enabled', True)"
```

What arming starts, per IG account per tick (at most one act each):

1. a comment on a `ROUTED` lead's latest post (LLM draft, confidence ≥ 0.8, else queued in `comment_review`);
2. per-lead warm-up, only if `warmup_config.enabled`;
3. standalone warming, only if `warming_config.enabled`.

**The timing law that switches on with it:** a DM follows its comment only 12-48 h later. `config.comment_window`
(`{"gap_h": 12, "expire_h": 48}`) moves both ends; a stale comment (past 48 h) parks the lead as
`PARKED [comment_stale]` and the engagement stage re-comments it, so the DM never lands on a post nobody
remembers. A lead commented *just now* parks as `PARKED [comment_waiting]`.

**The law that switches on with it:** from the moment `engage_enabled` is true, `dispatcher.claim_next` refuses
any IG lead whose own post has no logged comment, and `variants.render_lane` raises `UnwarmedLeadError` for one.
Each refused lead lands one `PARKED [not_warmed_up] lead N` row per 30 min. So arming = "no cold DMs until the
comments land" by design, not a bug. Disarming restores the previous behaviour exactly.

Guards that do not depend on the switch: 30 acts/day/account, 90 s spacing, the lead's ADR-9 window, the
account's own profile session, and one logged comment per lead. Failures land as `ENGAGE ERROR` (30 min
throttle) and never stop dispatch. Watch the first armed run's digest for `ENGAGE [comment]` or
`ENGAGE SKIPPED [no_context]` — the caption selector is pinned against a fake surface, not yet observed live.

## 7g. Signing a lane in by hand (owner action, 2026-11-15)

The engine and the login tool share ONE Chromium user-data dir (`.chrome`), and Chromium refuses a second
instance on it. So, always the same three steps:

```bash
# 1. stop the engine by its own pid (never by name pattern)
#    (PowerShell one-liner in RESUME.md; it prints the pid it stopped)

# 2. open the sanctioned login window for ONE lane and log in yourself
PYTHONPATH=src python interactive_login.py --account ig3

# 3. start the engine again with the Startup entry, then believe only the disk:
PYTHONPATH=src python -m outreach.command_center fleet
```

- The tool waits 10 minutes per login, screenshots ADR-15 evidence, and exits 0 only on a real `ready` verdict.
- It now says **"the engine (or another window) is holding .chrome"** instead of a raw `TargetClosedError`.
- A window left open on `.chrome` blocks the engine's browser for **every** lane, not just that one: the whole
  fleet waits while it is open. That is the price of one cookie jar per lane directory.
- `fleet` prints `(signed in / no IG session / no profile)` per account. That line, not the registry, is the truth.

### Which lane is which account (measured 2026-11-15)

Read from each profile's live session (the page the account itself sees, no cookie decryption) and cross-checked
against Chrome's saved logins:

| lane | profile | Instagram account | evidence |
|---|---|---|---|
| `ig1` | `Outreach` | **fazbuildsthis** | live `/fazbuildsthis/` link; no saved login in that profile |
| `ig2` | `Outreach-ig2` | **fazbuilds** | live link + saved login match |
| `ig3` | `Outreach-ig3` | **faz_codesthis** (signed OUT) | saved login only; `/accounts/current_user/` returns 401 |
| `ig4` | `Outreach-ig4` | **fazcodes** | live link + saved login match |

`fazbuildsthis` and `fazbuilds` are two distinct accounts with almost the same name — never infer the lane from the
handle. `profile_health` reads the jar; this table reads the session.

## 7f. Is the queue timed on the venue's own hours? (O5, built 2026-11-14)

```bash
PYTHONPATH=src python -c "
from datetime import datetime, timezone
from outreach.store import Database
from outreach.clock import status_line
with Database('outreach.db') as db:
    print(status_line(db, datetime.now(timezone.utc)))"
```

`unknown` is not a failure: it means the lead has no `hours` for today, and the lane sends exactly as before. A
`CLOCK PARK lead N: venue closed (opens HH:MM local)` digest row is a lead the engine decided not to message into a
dark kitchen; it stays ROUTED and comes back when the venue opens. Off with `send_clock.enabled = false`.

## Replies — how a "reply" is decided, and how to undo a wrong one

`replies.classify_newest` is the one direction law for an Instagram thread: the newest bubble is `ours` (our own
copy — every frozen render and every pinned variant the lead could have received), `auto` (a canned
acknowledgement: recorded as `AUTO REPLY` in the digest, no advance, ladder continues), `reply` (a human), or
`None` (too short to judge — never invented).

If a lead was advanced on something that was not a reply:

```
PYTHONPATH=src python -m outreach.command_center inbox --repair
```

It reverts the lead to its last real pre-send state, keeps the inbox row as the audit record and acknowledges it
(nothing for you to answer), and writes one `FALSE REPLY REPAIR:` digest row per lead. Live 2026-11-15 it undid
three: two of our own DMs read back, one Instagram autoresponder.

## The local model — the owner's window, the footprint, and what it is for

`outreach.ollama_adapter` is the department's only AI endpoint (ADR-4, loopback only). The engine asks it small
things: proofreading a message before it goes out, extracting a dish from a lead, drafting a comment, judging ICP
fit, and assembling copy variants. Nothing about sending, scrolling or replies goes through it.

**The owner's window (2026-11-15): the model may only run after 20:55 local, every day.** Before that no request
is made at all — `generate()` raises `ModelWindowClosed` before touching the network, and `_real_transport`
refuses again at the wire itself. Each caller degrades the way it already knows how:
`comments` refuses (`CommentRefused`), `dishes` falls back to deterministic menu/review mining (`source="no_model"`),
ICP judging does not run (leads stay `NEW` for the night), the proofread second opinion fails open on layer 1, and
an uncomposable variant line falls back to another variant.

A closed window is the owner's schedule, **never** an `OllamaDead`: it does not pause the queue and writes no HALT.

**The footprint.** `llama3.1:8b` costs 4.92 GB and the card is 6 GB (`nvidia-smi`: 6144 MiB), so a resident model
leaves ~1.2 GB for the IG Chromium lanes. `ollama_adapter.KEEP_ALIVE_S = 30` asks Ollama to release the model
after 30 idle seconds, so between bursts the VRAM goes back. Measured live 2026-11-15: one request → loaded,
gone 40 s later, GPU back under 1 GB.

Checks:

```
curl -s http://127.0.0.1:11434/api/ps          # {"models": []} = nothing resident
PYTHONPATH=src python -c "from datetime import datetime; from outreach.ollama_adapter import model_window_open; print(model_window_open(now=datetime.now()))"
```

## Alerts — how you hear about a P0/P1 without reading the digest

`ALERT P0|P1|P2 [CODE]: detail` rows are written by `outreach.alerts.raise_alert`, and **P0/P1 also pop a local
balloon** (one per code per 30 min; a throttled alert still writes its row). `outreach.alerts.sweep` runs once
per engine cycle and carries four codes:

| code | severity | means |
|---|---|---|
| `HEARTBEAT_STALE` | P0 | no heartbeat for 2× the engine's own promise, inside an open window |
| `PROBE_STALE` | P1 | no reply probe on a **live** channel for two open-window hours (a paused lane is skipped) |
| `TRANSPORT_HOLD` | P1 | a lead parked today because a DM surface would not load |
| `CAP_EARLY` | P2 | every lane at cap before the band's midpoint (row only, no popup) |

Silence by design: outside every window the engine is supposed to be asleep, so heartbeat and probe alerts stay
quiet. `ALERT SWEEP ERROR: ...` means the sweep itself failed (it never parks a send).

## 4a. The engagement window and the owner pause (2026-11-15)

**Owner order.** *"the entire comment thing can happen after 8:55pm … and then in the morning it stops
before like 10am … with a pause mechanism as well … so I can pause as per need when PC is needed and resume
when nothing is an issue."*

The whole engagement lane — comments, story replies, warm-ups and ambient scrolling — runs **20:55 → 10:00
local** and is silent the rest of the day. Outside the band `run_engage_stage` returns before any browser work,
so no Chromium is launched during the owner's working day; the digest gets **one** line per day
(`ENGAGE WINDOW: closed …`), never one per tick. Sending is untouched: the window gates engagement only.

```bash
PYTHONPATH=src python -m outreach.command_center status            # .engagement = armed / window / window_open / paused
PYTHONPATH=src python -m outreach.command_center pause  --engage --reason "PC needed"
PYTHONPATH=src python -m outreach.command_center resume --engage   # window still applies
```

The pause is its own switch: `pause` alone still stops the **send queue**, `pause --engage` stops only the
engagement lane, and one never moves the other. A malformed `engage_window` row refuses (fail-closed) rather
than turning the lane on. Move the band with:

```bash
PYTHONPATH=src python -c "
from outreach.store import Database, set_config
with Database('outreach.db') as db:
    set_config(db, 'engage_window', {'start': '20:55', 'end': '10:00'})"
```

## 7e. The three owner switches, and how to move them (2026-11-14)

```bash
# R1 cold probe: how much of today's three is spent, and is the gate open?
PYTHONPATH=src python -c "
from datetime import datetime, timezone
from outreach.store import Database
from outreach.cold_probe import status_line
from outreach.ambient import status_line as ambient_line
with Database('outreach.db') as db:
    now = datetime.now(timezone.utc)
    print(status_line(db, now)); print(ambient_line(db, now))"

# O8 ambient: today's minutes (60..120, default 80)
PYTHONPATH=src python -c "
from datetime import datetime, timezone
from outreach.store import Database
from outreach.ambient import set_minutes_today
with Database('outreach.db') as db:
    print(set_minutes_today(db, 60, datetime.now(timezone.utc)))"   # or 80 / 120
```

- **R1:** three dish-bearing cold first touches a day; the first `Delivered` bubble opens the gate for good.
  Off with `cold_probe_config.enabled = false`.
- **O8:** five follows a day across every account; the day's minutes are **split across the lanes unevenly and
  re-planned daily** (`AMBIENT PLAN <date>: ig1 34 / ig2 30 / ig4 16`), one four-minute slice per cycle.
  Off with `ambient_config.enabled = false`. Ambient needs an open session window; outside one it does nothing
  and writes nothing.
- **O6:** armed 2026-11-14 (chain + ghost kitchen). `targeting_policy` is in §7c.

## 7d. Is the overhaul working? (O10, 2026-11-14)

Two places, same numbers:

```bash
# one dimension at a time, live from the DB
PYTHONPATH=src python -m outreach.command_center replies --by commented_before_dm
PYTHONPATH=src python -m outreach.command_center replies --by send_hour
PYTHONPATH=src python -m outreach.command_center replies --by target_class
PYTHONPATH=src python -m outreach.command_center replies --by touch_kind
```

- `DISPATCH_FORECAST.json` → `reply_rate_by`, and one table in `DISPATCH_FORECAST.md`.
- Laws are `measure`'s: first touch only, decided at 72 h or on reply, late replies reported as late.
- `commented` vs `cold` is the O2/O3 law's own scoreboard; `story-first` stays empty until O4 logs story acts.
- An empty dimension reads “no decided first touches yet” — 0 % out of nothing is never printed.

## 7c. Targeting: fewer, better sends (O6/O7, 2026-11-14)

Ships **off**. Arming it changes who is messaged, and the numbers are yours.

```bash
# see the current policy
PYTHONPATH=src python -c "
from outreach.store import Database
from outreach.targeting import policy
with Database('outreach.db') as db: print(policy(db))"

# arm it with your lines (chain / ghost kitchen / brand / dine-in-first list)
PYTHONPATH=src python -c "
from outreach.store import Database, set_config
with Database('outreach.db') as db: set_config(db, 'targeting_policy', {
    'enabled': True, 'max_followers': 5000,
    'dine_in_first_categories': ['cafe', 'bakery'],
    'park_chain': True, 'park_ghost_kitchen': True, 'park_delivery_only': False})"
```

- A refused lead moves to `DEPRIORITIZED` (not terminal) and lands one `PARKED [targeting:<rule>] lead N` row.
- `repair()` is the one edge back to `ROUTED`; nothing else re-queues a parked lead.
- The follower count is read during the comment act (`data_json.ig_followers`); `unknown` never parks anything.
- `is_on_delivery_platform` alone is NOT delivery-only — that rule also needs `has_dine_in == False` (off by default).

## 8. Restart after a code change

The engine imports its modules at boot, so **code changes require a restart** to take effect. Copy changes
(templates) also need one, because the hash pin is asserted at boot. Config keys do not.

```bash
taskkill //PID $(python -c "import json;print(json.load(open('.engine_lease.json'))['pid'])") //F
cd src && pythonw -m outreach.engine_loop
# verify: lease pid changed, heartbeat fresh, stage present
```

## 8a. The boot launcher (R4, 2026-11-13)

The Startup `.cmd` is a **rendered copy** of `outreach/retirement.py`, never edited by hand. One command surface:

```bash
PYTHONPATH=src python -m outreach.setup_startup            # install / re-render (idempotent)
PYTHONPATH=src python -m outreach.setup_startup --verify    # installed AND byte-current? rc 1 if not
PYTHONPATH=src python -m outreach.setup_startup --remove    # stop booting at login
```

- Exit code 0 means the requested state holds; `--verify` writes nothing (safe from cron).
- A launcher that differs from the definition is reported as `installed but DIFFERS` and clears
  `config.startup_registered` — existence is not verification.
- The launcher calls **this repo's absolute `python.exe`** (not a bare `pythonw` on PATH) and appends to
  `logs/startup.log`, so a boot failure leaves a traceback. It boots `outreach.startup_readiness` (the B26
  gate), not the engine directly.
- It does not stop a running engine; only the next login is affected.
- If `--verify` returns 1, run the install form and re-verify before the next reboot.

## 9. Commit discipline

Explicit paths only, never `git add -A`. `OWNER_REVIEW.md` is the owner's live queue and is never staged:
the engine rewrites it every tick, so staging it commits a snapshot, not a decision. `AGENTS.md` is tracked
here (this repo is the reference copy); stage it only when the change *is* the manual, named in the commit.
Commit message says why, not what. Reports and contracts are owner-facing artefacts and belong in the repo.

## 11. The refresh schedule (R2, 2026-11-13)

The scraper refresh/intake pass (`intake.watcher_tick` → `refresh_leads`, ~15 min over ~2,500 rows) no longer
runs inside a send tick. It is a scheduled stage:

```bash
# is it due right now? (read-only)
PYTHONPATH=src python -c "
from datetime import datetime, timezone
from outreach.store import Database
from outreach.intake import refresh_due
with Database('outreach.db') as db: print(refresh_due(db, datetime.now(timezone.utc)))"
```

- **When:** off-window only (no ADR-9 window open), at/after `enrichment_hour_pkt` (default **04:00 PKT**),
  once per PKT day. Change the hour with `set_config(db, 'enrichment_hour_pkt', 5)`; the day stamp is
  `config.enrichment_day`.
- **Order:** dispatch always runs first in the cycle; the refresh can never sit in front of a send.
- **Budget:** 600 s per pass, checked *between* runs. `REFRESH BUDGET: stopped after N run(s) … next pass
  resumes` means it ran out of time, not that it failed; the next off-window cycle continues.
- **Paused queue:** a paused queue holds the whole pass (the import would otherwise repeat every cycle).
- **Alarms:** `STAGE OVERRUN [stage] Ns > Ns budget` in the digest means one tick stage passed its bound
  (`scheduler._STAGE_BUDGET_S`); it is a measurement, not a cutoff. The refresh pass is the only hard stop.
- **Consequence of a PC that sleeps through the night:** the refresh waits for the next off-window night.

## 12. Machine paths (the `OUTREACH_*` settings)

Five settings point outside this repository. Their values belong to the machine, not to the source: the
published tree names no drive letter, no user directory and no absolute default.

| Setting | What it points at | Read by |
|---|---|---|
| `OUTREACH_SCRAPER_ROOT` | the read-only scraper checkout (its `cache/scraper_cache.db` is the SSOT) | `scraper_adapter`, `intake` |
| `OUTREACH_LEG_PYTHON` | the interpreter a spawned scraper leg runs under | `intake` |
| `OUTREACH_LEG_LOG_DIR` | where a spawned leg writes `leg_<ts>.log` | `intake` |
| `OUTREACH_RETIREMENT_STATE` | the retired reminder's `state.json` (read-only archive) | `retirement` |
| `OUTREACH_LINKEDIN_MOUNT` | the mounted poster sibling — presence check only, never imported | `retirement` |

`outreach.machine_paths` resolves each name in this order, first hit wins:

1. the environment variable — set it before the engine boots, or in whatever Task/service entry point
   starts the loop;
2. `src/outreach/machine_paths_local.py` — **git-ignored**, this machine only, holding the same names, so a
   working box keeps working with no environment set at all;
3. nothing: it raises `KeyError` naming the variable. A missing root is a configuration error, never a guess.

**Never put a path in a tracked file.** A literal in source is inherited by every copy of the tree, which is
how one machine's drive letters end up in a public repository. `OUTREACH_LEG_PYTHON` and
`OUTREACH_LEG_LOG_DIR` default to paths *inside* `OUTREACH_SCRAPER_ROOT`, so overriding the root moves the
whole leg invocation with it.

## 10. When something is unknown

Say unknown. Measure with a read-only query. Never estimate into a report, never write a number you did not
read from a file or a table, and never claim a check you did not run.
