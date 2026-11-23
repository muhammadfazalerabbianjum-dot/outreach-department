# ROADMAP — outreach department

Owner: engineering lane (orchestrator role transferred 2026-11-13).
Every unfinished batch lives here. Done means **proven**, with the evidence named.
Status values: `OPEN`, `IN PROGRESS`, `BLOCKED-OWNER`, `DONE`.

---

## Standing constraints that shape every batch

- Owner copy is byte-pinned (`PINNED_SHA256`); the wall vetoes, never edits.
- WA paused by owner directive; email disarmed (ADR-5). Only `ig1` may send.
- ig2–ig4 are `WARMING`: week 1 = engagement only, zero DMs.
- The scraper DB is read-only (ADR-3). The only legal arrow is `python -m outreach.sync`.
- Explicit `git add` only, never `-A`. `OWNER_REVIEW.md` is never staged; `AGENTS.md` only when the change
  is the manual.
- One engine, leased; heartbeat + watchdog.

---

## R1 — Cold-DM gate exit · DONE (2026-11-14, owner: "probe")

**Correction (2026-11-13, measured).** This was first written as "the detector does not exist". It does:
`ig_sender._observe_delivery` reads the thread for a `Delivered` bubble after every send, writes the
`DELIVERY CONFIRMED (milestone)` digest row once, stamps `config.ig_delivery_confirmed`, and opens
`ig_cold_dms_blocked` permanently. The gate's exit path is built and fail-closed (an unreadable page proves
nothing). What is missing is **the situation that can produce an observation**: cold first touches are gated,
and only cold threads can show a fresh `Delivered`.

**Goal.** Choose and implement the exit: either a measured cold probe (3/day, dish-bearing leads only, each
watched by the existing observer) or an explicit owner acceptance that throughput stays at the ladder's
natural rate.

**Acceptance evidence.** Either one `DELIVERY CONFIRMED` row under a real send, or a written owner ruling in
`DECISIONS_LOG.md` that supersedes the gate condition.

**What shipped (owner ruling 2026-11-14: "probe").** `cold_probe.py`: **three cold first touches per day**,
dish-bearing leads only, one per lead per day. The slot is taken where the gate would otherwise refuse, so the
send that follows is the measurement — `ig_sender._observe_delivery` already watches every thread for a
`Delivered` bubble and already opens the gate permanently on the first sighting. Each slot is announced before
the browser opens (`COLD PROBE 1/3 lead N (dish: X)`), a refused slot sends nothing and is never counted, and a
lead without a dish is never probed. Switched off (`cold_probe_config.enabled`) the gate behaves exactly as
before.

**Evidence.** `tests/test_cold_probe_20261114.py` (10 pins: default three slots, a slot and its row, dishless
refused, the fourth refused, same lead never twice, a new PKT day resets, off restores the gate, the status
line, a confirmed delivery opens the gate for good, an unreadable page leaves it shut) plus `test_b11_ig`
(68 passed with `test_b13_replies`) — including the three pins that had to follow the new one-paragraph copy.

**What happens next, without any further decision:** the first `Delivered` bubble writes `DELIVERY CONFIRMED`
and the gate opens for all cold sends; if none appears in three days, the digest shows 3/3 spent per day and
the gate stays shut — the probe never becomes a silent exception.

**Dependencies.** Owner approval — received.

## R2 — Split enrichment out of the send tick · DONE (2026-11-13)

**Goal.** `refresh_leads` (~15 min over ~2,500 rows) can no longer delay or block a dispatch tick; every
stage has a deadline and names itself in the heartbeat.

**What shipped.**

- **Order:** the cycle is dispatch first, refresh second (`engine_loop._default_cycle`). A slow refresh can no
  longer sit in front of a send.
- **Schedule:** `intake.refresh_due` — off-window only (no ADR-9 window open), at/after `enrichment_hour_pkt`
  (default 04:00 PKT), once per PKT day (`enrichment_day`). `intake.mark_refresh_done` stamps the day, and
  only for a pass that finished inside its budget, so a truncated pass resumes instead of waiting a day.
- **Hard budget:** `engine_loop._REFRESH_BUDGET_S = 600 s`, enforced by `intake.watcher_tick(budget_s=)`
  *between* runs — one run always lands, then `REFRESH BUDGET: … next pass resumes`.
- **Stage clocks:** `run_engine_tick(stage_cb=)` stamps `boot → replies → ladder → dispatch → review` on entry
  and again with the measured duration; the heartbeat now carries `stages: [{stage, seconds}]` beside `stage`.
  Per-stage alarm budgets in `scheduler._STAGE_BUDGET_S`; an overrun lands one throttled
  `STAGE OVERRUN [stage] Ns > Ns budget` row.
- **Pause law:** a paused queue gates the refresh in `_refresh_if_due`. Without it, `intake_run` imports first
  and raises `QueuePaused` at qualification, so every off-window cycle would repeat the full import and never
  stamp the day (found in self-audit, pinned by `test_a_paused_queue_holds_the_refresh`).
- **Menu sync** is untouched and still runs after refresh inside `sync.sync_evidence` (refresh rewrites
  `data_json`; the 2026-11-12 ruling stands).

**Evidence.** `tests/test_stage_split_20261113.py` (10 pins: schedule law, once-per-day, stage trace, slow
refresh cannot starve dispatch, off-schedule skip, pause gate, truncation row, overrun row). Live, engine
restarted at `12:42:57Z`, first heartbeat at `12:42:59Z`: `stages = boot 0.0s / replies 1.2s / ladder 0.0s /
dispatch 0.4s / review 0.0s`, `stage="cycle 1 parked 300s"`. Gate GREEN, coverage 91.11 %.

**Remaining evidence (honest gap).** The `refresh` stage has not yet run in production: it is due 04:00 PKT
(23:00Z) and today's 04:00 had already passed when this shipped. Its first live stamp is tomorrow's off-window
pass; the schedule is pinned by test only.

**Dependencies.** None. Reversible.


---

## R3 — Alerting channel for P0/P1 conditions · DONE (2026-11-15, toast + digest)

**Goal.** The owner learns about a stalled engine, a dead probe or a repeated transport failure without
reading the digest by hand.

**Built.** `outreach.alerts.raise_alert(db, severity, code, detail)` (R3's `alerts.raise`; `raise` is a
keyword) is the one seam: it **always** writes a `ALERT P0|P1|P2 [CODE]: detail` digest row, pops the local
balloon only for P0/P1, and throttles that popup **per code per 30 minutes** — a condition that repeats every
cycle cannot repaint the desktop for an hour, and a throttled alert still writes its row. A *failed* popup does
not stamp the throttle (no success, no window burned), so a broken shell retries next cycle. The popup is the
existing `outreach.toast` NotifyIcon seam (ADR-12: local powershell, nothing leaves the machine); `raise_alert`
advances the generic drain's cursor past its own row so one alert is never offered twice.

**`alerts.sweep(db, now)`** runs in the engine cycle after the tick (`engine_loop._sweep_alerts`, fail-soft: a
sweep fault is a digest row, never a parked send) and carries the four conditions:

| code | severity | condition |
|---|---|---|
| `HEARTBEAT_STALE` | P0 | no heartbeat for 2× the engine's own promise, inside an open window |
| `PROBE_STALE` | P1 | no successful reply probe on a **live** channel for 2 open-window hours |
| `TRANSPORT_HOLD` | P1 | a lead parked in `TRANSPORT_HOLD` today (a DM surface would not load) |
| `CAP_EARLY` | P2 | every lane at cap before the open band's midpoint (information, no popup) |

A closed window means the engine *should* be asleep, so the heartbeat and probe conditions are silent there.

**Live lesson (2026-11-15).** The first real sweep alarmed on `[wa] last probe 305h 12m ago` — WhatsApp has
been paused since 2026-11-02, so its probe is silent **by design**. A lane the owner switched off is not a
fault: `_channel_live` now skips paused channels, pinned by
`tests/test_alerts_20261115.py::test_a_paused_lane_is_never_called_stale`.

**Acceptance evidence.** `tests/test_alerts_20261115.py` — 17 pins: P0/P1 toast, P2 row-only, per-code 30 min
throttle (rows keep coming), throttle expiry, independent codes, failed-toast retry, raising sender swallowed,
stale heartbeat P0 in-window / silent out-of-window, probe P1, transport-hold P1, cap-early P2 (no popup),
healthy engine silent, paused lane silent, engine sweep wire, broken sweep fail-soft. **One live balloon
delivered** on 2026-11-15 (`sendast(...) -> True`) and the live sweep ran clean after the fix.

---

## R4 — Startup defined in source control · DONE (2026-11-13)

**Goal.** PC-off recovery stops depending on an untracked Windows Startup `.cmd`.

**Design.** `setup_startup.py` that writes the `.cmd`, verifies it, and can remove it; content matches what
the engine actually needs (CWD `src`, `pythonw -m outreach.engine_loop`, log redirect). Status echoed and
recorded in `config.startup_registered`.

**What shipped.**

- The definition already lived in `retirement._STARTUP_TMPL`; it is now **verifiable and removable**, not just
  written: `retirement.startup_status()` compares the installed file's **bytes** with the definition (a leftover
  launcher from an older template reads as "installed" to any existence check), `register_startup()` re-renders
  and then verifies what landed (a truncated write fails there, not at 02:00), and `unregister_startup()` removes
  it idempotently without touching a neighbour's file.
- **Two real defects fixed in the launcher itself:** it called a bare `pythonw`, so a login shell's PATH decided
  whether the lane started (a reboot at 02:00 was one PATH change away from silence); and `pythonw` discards
  stderr, so the two blind deaths of 2026-11-04 left no traceback anywhere. It now uses an absolute `python.exe`
  and redirects into `logs/startup.log`.
- **Operator surface:** `python -m outreach.setup_startup` installs, `--verify` reports and writes nothing,
  `--remove` uninstalls; exit codes are the contract (0 = the requested state holds).

**Evidence.** `tests/test_startup_20261113.py` (9 pins: install content + `config.startup_registered` stamp,
byte-identical re-install, verify/current, stale-definition detection, missing launcher, idempotent remove,
neighbour untouched, stable render, CLI exit codes). `tests/test_b17_retirement.py` still 16 passed. The exact
`start "…" /min cmd /c ""{python}" -m … >> "{log}" 2>&1"` quoting shape was executed for real through
`cmd.exe` with a harmless `-c print(...)`: `rc 0`, `logs/startup.log` created with the expected line.

**Remaining evidence (honest gap).** The launcher has not been exercised by an actual reboot: that needs the
machine to restart (owner). What is proven is the rendered bytes, the verify/remove contract, and that the
quoted command line executes and logs.

**Dependencies.** None. Reversible.

---

## R5 — Copy rewrite (owner approval) · DONE (2026-11-14)

**Goal.** Remove the pitch-from-first-line, the repeated tics ("Salam" on 8 of 9 variants, "Genuinely
curious" on 3), the assumed "brother", the invented social proof in V9, and every ellipsis the lint bans.

**Design.** Seven exact replacements are drafted in `ORCHESTRATOR_REPORT_2026-11-13.md` §7. Applying them
requires re-pinning `PINNED_SHA256` for `social`, `followup`, `email` and the variant bodies, and updating
the measure constants.

**Acceptance evidence.** Owner approves specific items; hashes re-pinned; the four render pins plus the wall
pass; gate green.

**What shipped (owner ruling 2026-11-14: "apply all").** All seven tabled replacements applied verbatim:
`V4`, `V6`, `V7`, `V9`, `templates/social.txt` (one line, question only, transparency P.S. moved to touch 2),
`templates/followup.txt`, `templates/email.txt` (first two lines; the offer and signature unchanged).
`PINNED_SHA256` re-pinned for `social`, `followup`, `email` — `social_nodish` untouched, because the table did
not name it and owner copy moves only by owner words.

**Found while applying.** A trailing newline in the rewritten files made the wall veto every render
(`leading/trailing whitespace`) — the frozen files carry no final newline, and that is now respected. Pins
updated to the new copy: the exact social render, the follow-up ladder's day-3 rung, the email file, and
`measure`'s hash constants (now read from `PINNED_SHA256` instead of a literal, so the next owner edit cannot
leave a stale hash behind). Evidence: 137 passed across `b7_templates`, `b9_dispatch`, `b14_followups`,
`v11_b105_measure`, `targeting`, `attribution`.

**Dependencies.** Owner approval — received.

---

## R6 — Inbox triage and acknowledgement · DONE (2026-11-23, acknowledged)

**Goal.** 14 owner-inbox rows, all unacknowledged; only one (id 14, 2026-11-13T09:43Z) carries a reply body.

**Design.** Rows without captured text are audit artefacts, not replies (already the contract rule). The one
real reply needs an owner read and an acknowledgement so the pipeline can mark the lead conversational.

**Acceptance evidence.** `replies_pending_ack` in `SUPPLY_COUNTDOWN.json` reaching 0 with the reply answered.

**Closed 2026-11-23 (measured).** `owner_inbox` holds **18** rows and every one carries
`acknowledged_at`; `SUPPLY_COUNTDOWN.json` reports `replies_pending_ack: 0`. Acknowledgement
is not an answer — the owner still owns every conversation — but the queue no longer advertises
replies that nobody has seen.

**Dependencies.** Owner time. Nothing in code blocks it.

---

## R7 — Proofread hold inventory · DONE (2026-11-13)

**Goal.** `proofread_log` holds vetoed renders (`vetoed=1`, `cleared_at IS NULL`); `SUPPLY_COUNTDOWN.json`
reports `held_by_wall: 9`. Those leads can never send until cleared.

**What shipped.**

- `proofread.open_holds(db)` returns `OpenHold` rows (variant, dish, reason, layers, lead name/state), and
  `proofread.classify_veto(reason)` names the layer that spoke: `copy_lint` (deterministic layer 1) or
  `model_veto` (layer 2). `format_holds` prints one block per hold plus the split and the owner clear path.
- **Command surface:** `python -m outreach.command_center holds [--log] [--clear LEAD_ID]`. `--log` writes one
  `HOLDS INVENTORY` digest row (the results of record); `--clear` needs an explicit lead id and exits 2 when
  there is no open hold for it. Nothing ever clears in bulk.
- **Found and fixed while running it:** `outreach.command_center` had **no `__main__` guard**, so the documented
  `python -m outreach.command_center …` interface exited 0 in silence. Pinned.

**Live results (2026-11-13T17:52Z, digest row 1071).** 9 open holds, **0 copy-lint / 9 model (layer 2)** — every
one is a *dish grounding* refusal (`no dish mentioned`, `flavors is not a dish`, `saffron poutine is a
non-standard combination`, `Poké` read as the restaurant's name). **No real copy bug is on hold**: the wall
withheld bodies whose dish was not defensible, which is the wall doing its job. Clearing is owner copy
judgement, one lead at a time — engineering does not decide what goes out.

**Evidence.** `tests/test_holds_20261113.py` (8 pins: classification, open-hold query, cleared/accepted rows
absent, the split + owner path in the output, explicit-empty message, `--log` digest row, explicit clear with
refusal, module entry point). Neighbouring suites (`proofread_wall`, `b16_command_center`, `ops_hardening`,
`b15_scheduler`) 103 passed together.

**Dependencies.** None to list; clearing is owner copy judgement.

---

## R8 — Tests must not write into the repository root · DONE (2026-11-13)

**Fresh evidence (2026-11-13, this session).** Another full gate run dropped two new 8-byte `ig_1_*.png` files
into the root at `17:29Z` and `17:34Z`; both were archived to `_archive/2026-11-13/test-debris/` (now five such
files there today). The class reproduces on every gate run, exactly as written below.

**Goal.** A gate or suite run leaves the working tree exactly as it found it.

**Why it exists.** Twenty-five test call sites pass `screenshot_dir="."`, so a full run drops 8-byte `ig_1_*.png`
files into the repo root (three were found after today's gate runs and archived to
`_archive/2026-11-13/test-debris/`). Evidence files in production are correct — the real ones in
`checkpoints/evidence/` are 128–146 KB and match their `message_log.screenshot` paths.

**What shipped.**

- All **25** call sites (9 in `tests/test_b10_wa.py`, 16 in `tests/test_b11_ig.py`) now pass
  `screenshot_dir=str(db.path.parent)` — the test's own temporary directory, derived from the fixture that is
  already in scope. No signature changed, and no test can name the repo root again by accident.
- **Gate #10 `tree clean`**: `gate.scan_root_debris` refuses any `*.png/.jpg/.jpeg/.webp` **file** directly in the
  repository root. Production evidence lives in `checkpoints/evidence/` at 128–146 KB, so a root image is debris
  by definition — no exception to carve out. A directory named like an image is not debris.
- Pins: `tests/test_tree_clean_20261113.py` (clean root with evidence in `checkpoints/`, every suffix named,
  a directory is not a file).

**Evidence.** `grep -rn 'screenshot_dir="\."' tests/` → **0**. The two sender suites + the new pins: **82 passed**
(2 live probes deferred as `ProfileBusy`), and `ls *.png` in the root afterwards → **0 files**.

**Residual (named, not hidden).** Playwright's async teardown still prints `Task was destroyed but it is
pending!` after a run that opened a browser. It is upstream chatter, not a failure and not debris; recorded as
R13 rather than silently accepted.

**Dependencies.** None. Reversible.

## R9 — The profile fallback can land on "unknown" · DONE (2026-11-13)

**Goal.** After the `ig.me` fallback lands on `instagram.com/<handle>/`, the send must either reach the
composer or fail with a *named* state.

**Evidence it existed.** `digest` 2026-11-13T11:46:47Z and 12:33:55Z: `SEND BLOCKED [unknown]: dispatch
stopped before typing`.

**What shipped.**

- `_open_dm_thread` now reports the surface it actually used: `ig.me`, `profile` (Message found **and**
  clicked), or `profile_no_message` (profile loaded, no affordance matched). The third case used to return
  `profile` and leave the caller with a nameless frame.
- `_resolve_send_surface` turns the pair into `(surface, state)`: `ready`, `login_wall`, `ban_signal`,
  `ig_dead`, `undeliverable` as before, plus the two new names — and `unknown` (a *classification*, not a
  verdict) is reported as `thread_slow`. Both send paths use it, so no send can return a nameless state.
- `profile_no_message` is a **per-lead** fact, so the dispatch loop skips that lead for the pass instead of
  breaking the lane (the pre-R9 behaviour starved every send behind it: the IL-2 shape). The lead stays
  `ROUTED` — a stale selector is not proof the account cannot receive a DM — and `SEND SKIPPED
  [profile_no_message] lead N` names it once per lead per 30 min.

**Evidence.** `tests/test_named_states_20261113.py` (9 pins: each named state, the resolver, the sender
leaves the lead untouched, one blocked lead does not starve the queue, the skip row throttles);
`tests/test_b11_ig.py` updated where it pinned the old nameless verdict.

**Remaining evidence (honest gap).** No live send exercised it today: the IG cap (15) was reached at
`13:02:43Z` before the new code booted at `13:16:47Z`, so the first live named state lands tomorrow after the
cap resets. Also still open from the original design: **the two extra Message selectors** need one live
observation of a real `profile_no_message` page — the first such digest row is the trigger.

**Dependencies.** None.

## R10 — A gate test takes the LIVE probe lock · DONE (2026-11-13, cause corrected)

**Goal.** Running the quality gate must not corrupt the production engine's profile or be blocked by it.

**Evidence it existed.** `digest` 2026-11-13T13:16:51Z: `IG REPLY PROBE ERROR: ProfileBusy("profile held by
probe 'gate-live'")` while `gate.py check` ran; `tests/test_b10_wa.py` and `tests/test_b11_ig.py` acquire the
shared probe lock (`"gate-live"`, ttl 90 s).

**Correction — the first design was wrong.** "Give the tests a `tmp_path` lock" would have made two Chromium
sessions open the same persistent profile with NO mutual exclusion, which is the ADR-13 hazard itself. The
measured cause of the 15:25Z red was the opposite: the lock granted too readily. `browser._engine_state`
judged "asleep" from the heartbeat promise alone, and R2's mid-cycle stamp deliberately promises **900 s** so
the watchdog tolerates a slow stage — so a cycle *inside a stage* looked like a long sleep. Two live-browser
tests took the profile mid-cycle and died in `launch_persistent_context` (proved by re-running them with the
engine stopped: 2 passed).

**What shipped.** `_engine_state` is now stage-aware: the parked shape (`"cycle N parked Ns"`) is a sleep, and
any other stage name means a cycle is running **now** — `running`, so the lock refuses. A heartbeat without a
`stage` key (pre-R2) keeps the promise-only behaviour. Mutual exclusion now holds in both directions: the
engine's own `launch_outreach_profile` already refuses while a probe lock is live.

**Evidence.** Three pins in `tests/test_b25_contention.py` (mid-cycle promise → `running` + `ProfileBusy`;
parked promise → granted; legacy stage-less heartbeat unchanged). 80 tests green across the lock, wake,
scheduler, readiness and watcher suites. **Live:** with the engine running (`cycle 1 parked 300s`), both
live-browser tests pass; stopped-engine control also passes.

**Residual risk.** A probe that holds the lock longer than the engine's park still makes the engine's *own*
launch refuse (recorded as `IG REPLY PROBE ERROR`), which is correct fail-closed behaviour but reads like a
fault in the digest; R3's alerting is where that noise should be classified.

**Dependencies.** None. Reversible.

## R12 — The gate can hang forever on a wedged local model · DONE (2026-11-13)

**Goal.** `gate.py check` either finishes or fails loudly inside a bounded time. Today it can hang
indefinitely, and a hang is worse than a red: it reports nothing.

**Evidence (2026-11-13, measured).** `ollama.exe` (pid 2084) answered `/api/tags` in 0.19 s and listed
`llama3.1:8b`, but every generation request stalled with **zero bytes**: `curl /api/chat` → `HTTP=000` after
20 s and after 295 s; `/api/ps` showed no loaded model and no `ollama_llama_server.exe` ever appeared. Five
live-model tests then hung the suite at the same point on four consecutive runs (38 % of collection):
`test_b4_ollama.py::test_live_ollama_{generate_real_transport,liveness_contract}`,
`test_b5_icp.py::test_live_judge_smoke_real_transport`, `test_b5_icp.py::test_corpus_agreement_live`,
`test_b6_dishes.py::test_live_extraction_smoke`. Both ollama files are byte-identical to `HEAD`, so this is an
environment condition, not a regression (`git diff HEAD -- src/outreach/ollama_adapter.py tests/test_b4_ollama.py`
is empty). The same full gate was GREEN at 12:59Z today, with all five of those tests passing.

**Design.** (a) bind each live-model test to an explicit short deadline (`REQUEST_TIMEOUT_S` override +
`retries=1`) so a wedged service fails the test in under a minute with `OllamaDead`; (b) make the test
process itself bounded, so a future wedged dependency cannot stall the gate silently. Never skip: a live
dependency that is missing must fail loudly (ADR-4's "no silent skip" ruling stands).

**What shipped.**

- **A deadline that reaches the socket.** `ollama_adapter.generate`/`ask_with_guard` take `timeout_s` and pass it
  to the real transport (via `functools.partial`); production keeps 120 s. The deadline is threaded through
  `icp.judge_lead`, `icp.qualify_new_leads`, `dishes.extract_dish`/`extract_dishes`, together with `retries`,
  so a caller can bound a call without touching the production default.
- **The five live-model tests** take `tests/_model_limits.LIVE_MODEL_TIMEOUT_S` (60 s) and
  `LIVE_MODEL_RETRIES` (1): one bounded attempt instead of three 120 s ones.
- **The gate's pytest step is wall-bounded.** `gate.PYTEST_WALL_S = 1800` and
  `run_pytest(..., wall_s=)` return a named failure ("pytest exceeded the …s wall bound and was killed") instead
  of stalling forever. `run_pytest` also accepts `pytest_args` so the bound itself is testable.
- `pyproject.toml` `pythonpath` gains `tests` so one helper constant serves all three live-test files.

**Measured — and why the first bound was wrong.** At `timeout_s=15, retries=3` the run failed at 45 s with
three `TimeoutError`s, and the *next* run on the same service passed in 1.3 s: that was a cold `llama3.1:8b`
load, not a wedge. A short deadline would have made the gate flaky-red on the first call after idle. Hence 60 s
(a cold load clears it) with **one** attempt (a wedge still fails in ~60 s, not 6 minutes).

**Evidence.** `tests/test_gate_wall_20261113.py` (6 pins): a silent server on loopback makes `generate` raise
`OllamaDead` in < 5 s at `timeout_s=0.4`; `judge_lead`, `qualify_new_leads` and `extract_dish` each pass the
bound to the model call; a paused queue still refuses before any call; `run_pytest(wall_s=1.5)` over a sleeping
test returns `False` with "wall bound" in the tail and returns in seconds. Full local-model set: **80 passed in
42 s**, all five live tests green (generate 0.85 s, judge 4.7 s, corpus 32.8 s).

**Remaining evidence (honest gap).** The wedged case was observed with the real service (45 s of `TimeoutError`,
named `OllamaDead`) but the *wedged* run of the five tests was before the 60 s retune; the pin covers the
mechanism with a silent loopback server rather than the wedged Ollama process itself.

**Dependencies.** None. Reversible.

## R11 — A lane at its cap sleeps an hour, and reply probes with it · DONE (2026-11-13)

**Goal.** Reaching the daily send cap must not stretch reply detection from 5 minutes to 60.

**Evidence (measured 2026-11-13T13:17:09Z).** IG hit 15/15 at `13:02:43Z`. The next cycle's heartbeat read
`cycle 1 parked 3600s`: `seconds_to_next_wake` drops a region whose accounts are all at cap, so `work` yields
no candidate and the function returns `None` → the loop's 1-hour idle fallback. Reply polls (mailbox, WA, IG)
run inside the cycle, so a reply arriving at 13:20 is not seen until ~14:17.

**What shipped.** `seconds_to_next_wake` now separates the three "no claim" states that used to collapse into
one `None`:

- **empty queue** → `None` (the one-hour idle sleep, unchanged);
- **queued work, window OPEN, fleet spent** → `_MAX_PAUSE_S` (300 s), so the probes keep their cadence;
- **no cap to give today at all** (warming-only / halted accounts, via `_fleet_cap_left`) → not work, no probe.
- **window CLOSED** still sleeps to its own open, so a capped night costs no browser probes; and the answer
  is never `0.0`, so the 2026-11-04 hot-spin class cannot return.

**Evidence.** `tests/test_cap_wake_20261113.py` (4 pins: the uncapped control still returns `0.0`; capped +
open window ≤ 300 s; capped + closed window still waits the full 4 h; capped + a due rung does not hot-spin).
Full wake/scheduler/accounts/contention/extension regression: 81 tests green. **Live:** engine restarted
`15:22:46Z` (pid `7768`), first post-boot heartbeat `cycle 1 parked 300s`, `next_wake_at +300 s` — the same
capped state that read `parked 3600s` at 14:18Z.

**Residual risk.** The probe cadence is not free: one capped cycle measured `replies 94.8 s`, so a capped
afternoon now spends roughly a third of a cycle inside a browser. That is the designed law (probe at least
every 5 min) and it is visible as a stage duration, but O1/O8 must budget for it before adding ambient
activity to the same window.

**Dependencies.** None. Reversible.

## O-batches — the owner's 2026-11-13 overhaul brief · all delivered; O5 built but data-blocked

Full design, laws, acceptance criteria and build order: **`OVERHAUL_PLAN_2026-11-13.md`**. Scraper half
(popular times, attributes, follower counts, hours, timezone): **`SCRAPER_PROMPT_2026-11-13.md`** — that one is
to be pasted into the scraper repo, not built here.

Measured starting point (2026-11-13T14:4xZ): `engagement.py`, `warming.py`, `warmup.py` and `comments.py`
are built and tested but **have no caller in `src/`**, and `config.warmup_config` / `warming_config` /
`engagement_caps` are all NULL — the machinery is inert, which is why the overhaul is mostly wiring.

| batch | what | blocked on |
|---|---|---|
| O1 | wire the engagement stage into the tick (`engage` between `ladder` and `dispatch`) | **DONE 2026-11-13** |
| O2 | `warmed_up(lead)` derived from the ledger + **no comment, no send** refusal at claim AND render | **DONE 2026-11-14** |
| O3 | comment → DM interval (12 h min, 48 h expiry, re-comment when stale) | **DONE 2026-11-14** |
| O4 | story reply as the alternate first touch | **DONE 2026-11-14** (owner: counts as warm-up) |
| O5 | send in their clock (popular times → hours → Tue–Thu 14–16 fallback; never Fri dinner) | **BUILT 2026-11-14, inert** — cache has 0 rows with `hours` (scraper hold); fallback applies |
| O6 | targeting policy: dine-in-first / delivery-only / brand → `DEPRIORITIZED` park | **DONE 2026-11-14** |
| O7 | follower-count read (brand filter input, `unknown` when unreadable) | **DONE 2026-11-14** |
| O8 | ambient activity: 60-120 min reels scroll a day, ≤5 follows, shared act budget, **split unevenly across lanes and re-planned daily (O8b)**, jittered scroll pace | **DONE 2026-11-15** |
| O9 | PC-off: startup + ≤90 min recovery exist; stale-touch replan does not | **DONE 2026-11-14** |
| O10 | reply attribution (commented-before-DM, hour bucket, target class) | **DONE 2026-11-14** |

### IL-15, 2026-11-15: the fleet now reads the disk, not the registry

Four IG rows, one signed-in login. `profile_health` answers from the profiles themselves, `pick_account` refuses a
lane that cannot send and names it, the engage stage skips it without opening a browser, and `command_center fleet`
prints `(signed in / no IG session / no profile)` per account. Details in `INCIDENT_LEDGER.md` IL-15.

### R5 live follow-up, 2026-11-15: the copy rewrite needed the owner-paste reseed

The rewrite changed the pinned bodies, and the live `variants` table still held the retired text, so the drift
guard returned the correct verdict — `TemplateError('variant drift: V4 body differs from the pinned owner copy')`
— and the engine loop cycled on it instead of sending. Fixed the sanctioned way, not by loosening the guard:
`command_center variants --reseed` → `VARIANT COPY reseeded from the owner paste: V4, V6, V7, V9`.

### O8b — shipped 2026-11-15: the day's minutes are split across the lanes, unevenly

Owner request: *"we can have a split for all of them… unequally everyday so no one thinks its a bot."* Before this,
`ig1` took the whole pool — measured today as 10 sessions, all `ig1`, 40/80 minutes, nothing for the two lanes that
had just been signed in.

- **A per-day plan** (`ambient_split`, written to the digest once as `AMBIENT PLAN <date>: ig1 34 / ig2 30 /
  ig4 16`) splits the owner's daily number across the lanes that can act. It sums exactly to that number, is
  cached for the day so the plan shown is the plan that runs, and is reseeded from the date — so tomorrow is a
  different split, and two lanes never get the identical number by construction.
- **Weights are drawn per lane** (0.55–2.0 → up to ~3.6× spread), with a 6-minute floor so no lane sits at zero
  while another scrolls for an hour.
- **The share is a ceiling, not a suggestion**: a lane that has spent its minutes stops even when the global pool
  still has some for its neighbours.
- **The scroll is no longer a metronome**: the session length is still exact (budget honesty), but each wait is
  drawn from 0.5–1.45 × 8 s and one reel in eight is watched through — ~30 reels in four minutes, not a press
  every 8.000 s.

`AMBIENT [ig2]: 4 min reels, 0 follow(s) (4 of 30 min for this lane, 12/80 min today)` is the new digest shape;
`ambient.status_line` shows the day's plan with what each lane has used.

**Evidence.** `tests/test_ambient_split_20261115.py` — 12 pins: the plan sums to the budget, floors and ceilings
hold, it is unequal, it is stable within the day and different across days, it follows the owner's changed daily
number, a lane cannot outspend its share while its neighbour keeps its own, an unknown lane gets zero, it scrolls
~30 reels, and the pace varies. Ambient + engage + fleet suites: **67 passed**.

### O8 live fix, 2026-11-14: the first ambient sessions were real, and too long

The owner's scroll went live and announced itself three times —
`AMBIENT [ig1]: 20 min reels`, `[ig2]: 5 min`, `[ig3]: 5 min` — which is the first
proof the ambient path works against a real browser. It also exposed two real faults:

- **`STAGE OVERRUN [engage] 1861.5s > 300.0s`** — three accounts each opened a
  fresh session inside one cycle, so a 300 s stage ran 31 minutes. The stage now
  hands out **one four-minute slice per cycle** across the whole fleet; the other
  accounts wait for the next tick, and the day still accrues 60-120 minutes.
- **A 5-minute floor silently rounded that slice up to 304 s** (`MIN_USEFUL_SESSION_MIN`),
  i.e. the "4-minute" slice was slightly over the budget it was sized against. The
  floor is now 3 minutes, named in the code with the reason.

Pinned by `test_one_ambient_session_per_cycle_keeps_the_stage_inside_its_budget`
(one `AMBIENT` row per cycle, ≤ 240 s of sleeps) in the ambient suite (13 pins).

### O5 — built 2026-11-14: send in their clock (inert until the data lands)

Built against the contract I asked the scraper for (`SCRAPER_PROMPT_2026-11-13.md` §1/§2/§5), so the day the hour
maps land nothing needs writing: `clock.read_clock` answers `open` / `closed` / `unknown` from the lead's own
`timezone` + `hours` + `popular_times`, and the claim loop parks a provably shut venue with one `CLOCK PARK` row
instead of messaging a dark kitchen.

Three refusals, deliberate:

* **Unknown is not closed.** No `timezone`, no `hours`, an explicit `[]`, or a short busy curve all answer
  `unknown` and the lane sends exactly as it did before. Verified against today's corpus: 3,160 scraper rows carry
  **zero** `hours`, so every live lead is `unknown` and behaviour is unchanged.
* **No smoothing, no invention.** The busy curve is read at the current local hour and only when that day has a
  full 24-value list; a 3-value curve yields no opinion.
* **Empty day list ≠ open all day.** A day with `[]` is "unread", never a fabricated 00:00–23:59 window.

The forecast now carries `send_clock` (JSON) and renders it as a heading in `DISPATCH_FORECAST.md`:
`SEND CLOCK: N open / M closed / K unknown of T routed (hours known for X%)`.

**Evidence.** `tests/test_send_clock_20261114.py` — 11 pins: venue-local conversion (and a missing/absurd zone
refusing), open with its busy reading, closed naming the next opening, `unknown` for no hours / no zone / `[]`,
a flat or short curve answering no-opinion, the coverage line, and a claim-loop pin where a shut Calgary kitchen is
parked while an unread one still claims.

### O4 — shipped 2026-11-14: a story reply is a warm touch

Owner ruling: *"story reply counts as warmup yes."* So the no-comment-no-send law now names **one vocabulary** —
`warmup.WARM_ACTS = ("comment", "story_reply")` — and everything downstream follows it: `warmed_up`, the render
gate, the 12-48 h window (`comment_state`), and O10's `commented_before_dm` bucket. A lead warmed by a story is
claimable exactly like one warmed by a comment.

- **Schema:** migration 19 rebuilds `engagement_log` to allow `story_reply` (SQLite cannot ALTER a CHECK). One
  transaction, rows copied, nothing dropped — verified on a copy of the live DB: version 19, same columns, same
  act vocabulary plus one.
- **The act** (`story.py`): opens `instagram.com/stories/<handle>/`, requires a live ring or readable text, drafts
  with the SAME comment drafter (confidence floor + copy vet + owner queue), then fills the story reply box and
  sends. No readable text means **no reply** — the lane falls through to a comment on a real post, which is why
  this can never invent a line about a story it could not read.
- **Order:** comment first, then story, then warm-up, then standalone warming — one act per account per tick.

**Evidence.** `tests/test_story_warmup_20261114.py` (9 pins: the schema accepts the act, a story reply makes a lead
warm, the render gate accepts it, the 12-48 h window reads it as the warm touch at 1 h / 20 h / 50 h, attribution
counts a story-first DM as `commented`, no text → `no_text`, no story → `no_story`, a real post → posted + logged,
a low-confidence draft → `draft_refused`) with the engagement/interval/attribution/dispatch set: **82 passed**.

**Deviation, named:** the story surface is pinned against a fake page. The ring and reply-box selectors have not
been driven against a live story; the first live attempt will land either `ENGAGE [story_reply:lead N]` or one
`STORY SKIPPED [reason]` row.

### O8 — shipped 2026-11-14: ambient time out of a budget, not out of habit

Owner ruling: *"80 min/day scroll, yes — sometimes 80, sometimes 60, sometimes 120, get me?"* So the budget is a
**range with one number per day**, never a constant: `ambient_minutes_today` sets today's minutes, clamped into
`[60, 120]`, default 80. `ambient.set_minutes_today(db, 60|80|120)` is the one call that moves it.

- **A session** is 5 minutes of reels scrolling (one Down every ~8 s; sized so it fits the engage stage's
  300 s budget, with the day's 80 minutes accruing over several sessions) plus follows — and the five follows are
  **per day across all accounts**, never per session: a follow is the one ambient act the world can see.
- **Budget arithmetic** is pinned: a session never exceeds the minutes left (10 left → a 10-minute session), and
  below 5 minutes nothing runs at all.
- **Its own switch, on purpose.** The owner said yes to scrolling while the message lane stays disarmed, so the
  stage spends ambient time even when `engage_enabled` is off — and it still never comments, likes or DMs in that
  pass (`engagement_log` stays empty, pinned).
- **Fail-soft and honest:** a dead surface records nothing (a failure is not activity), a spent day stays spent,
  and each session lands one `AMBIENT [ig1]: 20 min reels, 2 follow(s) (37/80 min today)` row.
- **Live:** arming applied 2026-11-14 — `AMBIENT: 80 min budget today (80 left), 5/5 follows left`.

**Evidence.** `tests/test_ambient_20261114.py` (11 pins) with `test_engage_stage`, `test_unwarmed_gate`,
`test_comment_interval`: **43 passed**.

**Found live, 2026-11-14T15:15Z.** The first ambient tick after restarting the engine died as
`ENGAGE ERROR: ValueError('engage stage needs a page or a page_factory')`: the disarmed path returned before
opening a browser, so an ambient-only pass had no page to scroll in. The engine now supplies its account-scoped
opener whenever ambient is on, pinned by `test_the_engine_opens_a_session_for_ambient_even_with_the_lane_shut`.
The default session also moved from 20 to 5 minutes — a 20-minute dwell would have outlived its own stage
budget, and a test that calls the driver without an injected sleep hung on exactly that.

**Deviation, named:** the scroll driver is pinned against a fake page. The reels surface itself has never been
driven live, so the first real session is the first live evidence — it will land an `AMBIENT` row or an
`AMBIENT SKIPPED` row, never silence.

### O10 — shipped 2026-11-14: prove it, or disprove it

`attribution.py` groups the same decided first touches `measure` already counts by one dimension at a time,
and the forecast contract now carries the answer: `DISPATCH_FORECAST.json` gains `reply_rate_by`, the Markdown
gains one table, and `command_center replies --by <dimension>` prints just that table.

Four dimensions, each from stored facts, never a guess:

| dimension | buckets | source |
|---|---|---|
| `commented_before_dm` | `commented` / `cold` | `engagement_log.act='comment'` before `message_log.sent_at` |
| `send_hour` | 3 h blocks in PKT | `message_log.sent_at` (UTC+5, no DST) |
| `target_class` | `chain` / `ghost_kitchen` / `delivery_only` / `independent` | lead scraper flags (O6's classes) |
| `touch_kind` | `story-first` / `dm-only` | an `engagement_log` row against `target IN ('story','reel')` before the send |

Laws kept identical to `measure` so the numbers can be read side by side: first touch only, decided at 72 h or
on reply, a late reply reported as late and never folded into the rate, and an empty dimension reporting
“no decided first touches yet” instead of a 0 % that would look like failure.

**Evidence.** `tests/test_attribution_20261114.py` (15 pins: the comment-before-DM split both ways, a comment
after the DM does not retroactively warm it, replies land in the bucket that earned them, young send is pending,
late reply counted as late, first-touch only, PKT hour blocks, class from flags, story-first, empty-dimension
wording, unknown dimension refused, the contract shape, and the forecast JSON + Markdown carrying it). With
`test_v11_b105_measure`, `test_b16_command_center`, `test_dispatch_forecast`: **60 passed**.

### O9 — shipped 2026-11-14: the touch the machine slept through

Three layers answer "the PC was off": **restart** (watchdog + R4 launcher), **missed window**
(`reconcile_window_extensions`, one ≤90 min band, B27), and **missed touch** — the real gap, now closed by
O3's window plus this visibility pass:

- `replan.py` names every ROUTED lead whose comment expired past 48 h, once per lead per 6 h, as
  `REPLAN lead N: its comment expired past the 12-48 h window — the engagement stage re-comments before any
  DM`. `engine_loop._default_cycle` calls it **before** `run_engine_tick`, i.e. before any claim, and a fault
  there lands one `REPLAN ERROR` row instead of stopping a cycle.
- It changes no state: the claim refuses (O3) and the stage re-comments (O3) — this is only the record that
  lets a 14-hour outage read as N replanned leads instead of silence.

**Evidence.** `tests/test_replan_20261114.py` (5 pins: a stale touch is named once, a restart loop does not
repeat it, a fresh/waiting touch is not a replan, after a long outage no stale DM can be claimed while
`PARKED [comment_stale]` names it, and a 14 h outage *inside* the window still sends — the recovery does not
overshoot).

### O6 + O7 — shipped 2026-11-14: fewer, better sends

`targeting.py` (policy) and `followers.py` (the read that feeds it), both **off by default**: parking a lead
changes who is messaged, and the category list and the follower line are the owner's numbers.

- **Rules** (`config.targeting_policy`): `dine_in_first_category` (owner list, empty = inert), `brand_account`
  (`data_json.ig_followers > max_followers`, default 5000), `chain`, `ghost_kitchen`, and `delivery_only` —
  which needs `has_dine_in == False` as well, because `is_on_delivery_platform` alone is true for plenty of
  dine-in restaurants, and it is off by default.
- **Park:** a new `DEPRIORITIZED` state (ROUTED → it, and exactly one repair edge back). Nothing but ROUTED is
  claimable, so a parked lead cannot be dispatched; each park lands one `PARKED [targeting:<rule>] lead N`.
- **Follower read (O7):** while the comment act already has the profile open, `read_followers` parses
  `1,234` / `1.2K` / `1.2M` from the header and stamps `data_json.ig_followers` + `_at`. Unreadable is
  `unknown` — a wrong “4,900” would let a brand through and a wrong “51,000” would drop a real restaurant.
- **Unknown refuses nothing:** an `unknown` count never parks a lead.

**Evidence.** `tests/test_targeting_20261114.py` (22 pins: nine count formats, header parse, unknown header,
stamp, unknown stamp, disarmed = nothing parked, chain/ghost/category/brand rules, unknown refuses nothing,
delivery-only needs the flag, the claim parks and keeps walking, repair edge). With `b9_dispatch`,
`b15_scheduler`, `unwarmed_gate`: **108 passed**.

**Deviation, named:** the plan wanted the park at *intake* as well as at claim. Claim-side only ships here,
because intake-side parking rewrites a much larger path (`intake_run`, tier/countdown contracts) and the
claim gate already guarantees no parked lead is messaged. Intake-side parking is backlog, not done.

### O3 — shipped 2026-11-14: the 12-48 h window, one function

`warmup.comment_state(db, lead_id, now)` returns **`none` | `waiting` | `ready` | `stale`** from the latest
logged comment, with `comment_window(db)` reading `config.comment_window` (`gap_h` 12, `expire_h` 48; a
nonsense pair falls back whole rather than silently disabling the law).

- **Claim:** when armed, only `ready` claims; `none`/`waiting`/`stale` park the lead with one
  `PARKED [comment_waiting|comment_stale|not_warmed_up] lead N` per lead per 30 min.
- **Stage:** `_comment_candidates` now offers `none` **and `stale`** leads, so a lead whose comment expired is
  refreshed (a second logged act) instead of being sent a stale DM. A `waiting` or `ready` lead is never
  re-commented.
- **Boundaries** are pinned exactly: 11 h 59 m waits, 12 h is ready, 48 h is still ready, 48 h + 1 s is stale.

**Evidence.** `tests/test_comment_interval_20261113.py` (12 pins: the machine, exact boundaries, latest act
wins, config + nonsense fallback, parse, claim for each state, disarmed ignores the window, throttled parks,
stale re-comment, waiting never re-commented). With O1/O2 and `b9_dispatch`: **66 passed**. The O2 pin was
updated where O3 legitimately changed it (a comment logged *now* is `waiting`, not claimable).

### O2 — shipped 2026-11-14: "no comment, no send", armed with the comment lane

- **`warmup.warmed_up(db, lead_id)`** is derived, not stored: true only when `engagement_log` holds a
  `comment` act for THAT lead. No column, no second writer, nothing to drift (the IL-7 lesson). A follow or
  a like does not count; a comment on another lead does not count.
- **Two enforcement points.** `dispatcher.claim_next` skips an unwarmed IG lead and records one
  `PARKED [not_warmed_up] lead N` per lead per 30 min (a per-lead fact parks the lead, never the lane — the
  R9 shape). `variants.render_lane` — THE render seam — calls `assert_warmed`, so a manual run or a future
  caller cannot produce cold copy either; it raises `UnwarmedLeadError`.
- **The gate arms with the means to satisfy it** (`config.engage_enabled`, the O1 switch). Disarmed = today's
  behaviour, byte for byte; armed = the law. A gate that refused every DM while commenting was switched off
  would have bricked the live lane the moment it shipped, which is not a law, it is an outage.

**Owner-facing consequence, stated plainly:** arming the engagement stage will stop COLD DMs until each lead
has a logged comment. That is the intended law, and it is why the arming moment is the owner's call.

**Evidence.** `tests/test_unwarmed_gate_20261113.py` (9 pins: derived fact, per-lead isolation, follow/like do
not count, disarmed = old behaviour with no digest noise, armed refuses + keeps walking, armed with an all-cold
queue claims nothing, park row throttled, render raises, render works once the comment is logged). Regression set
(`unwarmed`, `b9_dispatch`, `b16_command_center`, `v11_b7_warmup`, `v12_pack`, `dispatch_forecast`,
`v11_b1_variants`): **125 passed**.

### O1 — shipped 2026-11-13: `engage_stage.py`, the caller the machinery never had

`src/outreach/engage_stage.py` is now the only caller of `comments.post_comment`, `warmup.warm_up` and
`warming.warm_account`. `scheduler.run_engine_tick` gained an `engage` stage (`boot → replies → ladder →
engage → dispatch → review`) that fails soft: a fault lands one `ENGAGE ERROR` row (30 min throttle) and
dispatch still runs.

- **The switch:** `config.engage_enabled` seeds **False**. Nothing opens a browser, comments, follows or
  likes until the owner arms it. This is the arming decision the plan said could not be inferred.
- **One act per account per tick:** `engagement.can_engage` decides (status, 30/day, 90 s spacing) before a
  page is touched; the account is then skipped for the pass.
- **Window-gated:** a candidate lead must be inside its ADR-9 window, and the stage needs at least one open
  region (`open_session_regions`).
- **Priority:** comment on a ROUTED IG lead with no logged comment → per-lead warm-up (if armed) → standalone
  warming (if armed) → nothing.
- **Login isolation:** `page_factory(account_id)` opens THAT account's own profile. The first wiring handed
  every account one page, which is the ADR-13 hazard; the pin catches it (`opened == ["ig1", "ig2"]`).
- **Evidence:** each act writes its `engagement_log` row + ADR-15 screenshot through the called module; the
  stage never writes an act row itself, and never touches `message_log`.

**Evidence.** `tests/test_engage_stage_20261113.py` (11 pins: disarmed silence, one comment + evidence, no send
rows, pacing gap, window gate, no-caption skip, low-confidence queue, nothing-owed fallback, tick wiring + stage
name, broken stage fails soft before dispatch, per-account sessions). With the neighbouring suites
(`stage_split`, `b15_scheduler`, `cap_wake`, `v11_b5..b8`, `named_states`): **121 passed**.

**Not proven live:** no act has run against Instagram — the stage is disarmed and arming is owner-only. The
selectors (`og:description` caption, comment box) are pinned against a fake surface, not observed on a live
post. That observation is the first thing to watch when the owner arms it.

**Owner-only (never inferred):** arming the warm-up/warming/ambient flags, the comment→DM gap, whether a story
reply counts as warm-up, the category list, the 5,000-follower line, 80 min/day of browser time across four
accounts, and the public/attributable nature of comments.

## DONE (for the record, with evidence)

| batch | evidence |
|---|---|
| R9 named post-fallback states + per-lead skip | commit named in `RESUME.md` §0; `tests/test_named_states_20261113.py` (9 pins), gate GREEN |
| R2 refresh as a scheduled stage | commit `cc0bf63`; `tests/test_stage_split_20261113.py` (10 pins), live stage trace |
| ig.me → profile transport fallback, `TRANSPORT_HOLD` | commit `5b5cc45`, `tests/test_transport_fallback.py`, gate GREEN |
| lint refusal is per-lead `COPY_HOLD`, not a cycle abort | same commit, machine edges pinned |
| WAL + busy_timeout, `last_ok_at`, heartbeat `stage`, validated `set_cap` | commit `27825cd`, `tests/test_ops_hardening.py`, gate GREEN 91.08 % |
| archive of screenshots/superseded reports/one-offs | commit `f785ddf` |
| contract pair states cap and remaining room unambiguously | `cap_total` / `sent_today` / `today_capacity_remaining` + pin |
| menu evidence arrow (website menus → dish pipeline) | `cee8af3`, 43 leads with menu dish |
| V4/V7 name the menu when the dish came from a menu | `bd97fbc`, four pinned renders |
| measure attributes every body a send can carry | `1082a88`, attribution pins |
| gate split: `--offline` deselects and names the live-model tests, `--no-prewarm` only skips the load | `tests/test_gate_offline_20261119.py` (7 pins, incl. a stale-node-id catcher); gate GREEN offline |
| deep-dive fact-check + showcase README, duplicate `12:` migration removed | `docs/DEEP-DIVE.md` corrected against source (lane order B29, two-rung ladder, claim order, follower gate, fleet, model window); `docs/SHOWCASE-README.md` names every feature; `tests/test_store_migrations_20261121.py` (4 pins); second-pass review 2026-11-21 fixed the stale park count, the §9 tier contradiction and the DUP reason claim, and documented the cap-20 ceiling; gate GREEN offline |
