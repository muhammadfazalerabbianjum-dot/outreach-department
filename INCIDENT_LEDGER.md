# INCIDENT LEDGER — failure classes and their four layers

One row per **class** of failure, not per occurrence. Occurrences belong in `digest`; this file exists so the
same class cannot come back unannounced. Four layers are mandatory: **prevention**, **detection**,
**recovery**, **owner control** (what a human can do while it is happening).

Companion: `LIVE_LESSONS.md` (narrative history), `DECISIONS.md` (the rulings that came out of each class).

---

## IL-1 · Navigation timeout on the deep-link surface (2026-11-13)

**Class.** Instagram's `ig.me/m/<handle>` shortcut fails to load while `instagram.com/<handle>` works.
24 of 24 IG navigation failures in 7 days were `ERR_CONNECTION_TIMED_OUT` on `ig.me`; three on the same lead.

- **Prevention:** one deep-link attempt with an explicit 20 s timeout, then the profile URL and a Message
  click (`ig_sender._open_dm_thread`). Bounded: two surfaces, no loop.
- **Detection:** `SENDER ERROR [ig]` digest rows now carry the handle; `SEND BLOCKED [transport_hold]` when
  both surfaces fail. New `TRANSPORT_HOLD` state count is the daily gauge.
- **Recovery:** the lead sits in `TRANSPORT_HOLD`, not `IG_DEAD`; one repair edge returns it to `ROUTED`.
  Nothing is lost and no cap slot is spent.
- **Owner control:** `config.dispatch_backoff_until` parks dispatch; the digest names the exact handle.
- **Residual risk:** a network-wide Instagram outage produces a growing `TRANSPORT_HOLD` population that
  nobody is alerted about yet (R3).

---

## IL-2 · One poisoned render aborting the whole cycle (2026-11-12 → 11-13)

**Class.** `variants.lint` raised `LintBlocked`, the engine treated it as fatal, and because claim order is
stable by id the same lead was re-selected every tick. 17 `ENGINE ERROR: loop cycled through LintBlocked`
rows in two days; a single lead with `...` in its evidence could starve the entire queue.

- **Prevention:** the dispatcher catches `LintBlocked` per lead and parks it in `COPY_HOLD`; the queue keeps
  moving. Lint itself stays strict — it is not weakened.
- **Detection:** one `state_history` row per park (`actor=dispatcher`, reason carries the lint problem), and
  the `COPY_HOLD` count is visible in the state tally.
- **Recovery:** fix the evidence or the copy, then transition `COPY_HOLD → ROUTED`.
- **Owner control:** the reason text names the offending rule (ellipsis, banned phrase, missing question).
- **Residual risk:** pinned `followup*.txt` and `email.txt` still contain `...`; they pass because the file
  lint is separate from `variants.lint`. Any future move of a ladder rung onto the variant engine will hit
  the same wall. Worth one ruling (R5 / D6).

---

## IL-3 · Silent death and invisible slowness (2026-10-26 → 2026-11-13)

**Class.** The engine stops or crawls and nothing says so. Historic: dead 14:06–15:47 UTC with no trace.
2026-11-12→13: 14 h 15 m of silence across a PC-off period, indistinguishable from a hang until the digest
timestamps were read.

- **Prevention:** heartbeat promise (`next_wake_at`) + lease + watchdog that restarts a gone engine; the tick
  now stamps `stage`, so a slow cycle names itself.
- **Detection:** `heartbeat_stale` (promise expired by more than the grace window); `accounts.last_ok_at` is
  finally written by `commit_send`, so an account that has not sent is visible.
- **Recovery:** watchdog spawns the engine; startup `.cmd` restarts it at logon.
- **Owner control:** `.heartbeat.json` and `.engine_lease.json` are readable without the engine.
- **Residual risk:** startup lives outside source control (R4); no alert yet when the heartbeat is late but
  the process is alive (R3). Silence during a *closed* window is normal and must not be alarmed on. A slow
  *stage* is no longer indistinguishable from a stall — see IL-8 / R2.

---

## IL-4 · Two documents disagreeing about one fact (2026-11-13)

**Class.** `SUPPLY_COUNTDOWN.json` said `daily_send_rate_effective: 15` while `DISPATCH_FORECAST.json` said
`daily_rate: 7`, both generated two seconds apart. Neither was wrong: 15 was the cap, 7 was the remainder
after 8 sends. A reader (me, on takeover) treated it as a contradiction and spent a review pass on it.

- **Prevention:** the pair now publishes `cap_total`, `sent_today` and `today_capacity_remaining`, so the
  two numbers carry distinct names.
- **Detection:** `test_contract_pair_states_the_same_cap_two_ways` fails if the pair drifts again.
- **Recovery:** none needed; the numbers were always right.
- **Owner control:** read `cap_total` for the ceiling, `today_capacity_remaining` for the rest of today.
- **Residual risk:** any third document quoting "rate" without saying which rate re-opens the class.

---

## IL-5 · A reply that was not a reply (2026-11-11)

**Class.** `owner_inbox` rows were created from our own outgoing bubble read as an incoming reply; 13 rows
showed `REPLIED` with no captured text, and `REPLIED` is terminal.

- **Prevention:** a row counts only with captured reply text; the contract's `replies_pending_ack` filters on
  `TRIM(reply_text) != ''`.
- **Detection:** `inbox-audit` compares `state_history` against `message_log` and the bubble text.
- **Recovery:** documented repair path `inbox.revert_unverified` (`REPLIED → SENT`, actor `inbox-audit`),
  which is the only legal revert and is recorded in history.
- **Owner control:** the audit row names every reverted lead and why.
- **Residual risk:** the probe that would have confirmed the thread is itself blocked when `ig.me` times out
  (IL-1), so silent inbox drift is possible while transport is degraded.

---

## IL-6 · A gate with no exit (open, 2026-11-12 →)

**Class.** `ig_cold_dms_blocked` waits for a `Delivered` bubble; a bubble can only come from a cold send;
cold sends are blocked. Throughput collapsed from 15/day to 3–8/day and 144 leads are parked.

- **Prevention:** the gate is explicit, config-armed and fail-closed — the right shape for a safety rule.
- **Detection:** `SEND BLOCKED [follower_gated]` in the digest; `dispatch_ready` staying flat.
- **Recovery:** none exists by design; the exit condition is an owner decision.
- **Owner control:** `config.ig_cold_dms_blocked` is owner-only. Nothing in code may flip it.
- **Residual risk:** the longer it stays armed, the more the program looks idle while claiming to be live.
  This is the top item in `ROADMAP.md` (R1).


---

## IL-7 · A second writer that skips the shared bookkeeping (2026-11-13)

**Class.** Two code paths write the same table. `dispatcher.commit_send` lands the dispatch row and the
account health stamp; `followups` wrote its own `message_log` row directly and never touched
`accounts.last_ok_at`. The result looked like a broken feature ("the stamp is never written") when the truth
was "one of the two writers does not know about it". Found by measuring the live DB one minute after a send,
not by reading code: the unit test for `commit_send` passed the whole time.

- **Prevention:** one helper, `dispatcher.stamp_account_health`, called from both paths; the ladder row now
  also carries its `account_id`, so attribution and the health clock share one fact.
- **Detection:** a send with `message_log.account_id` set and `accounts.last_ok_at` NULL is the smell; the
  ops check in `OPERATIONS.md` §2 surfaces both columns.
- **Recovery:** nothing to repair — the missing stamp is informational, not a lost send.
- **Verified live 2026-11-13T12:02:31Z:** the first send after the fix wrote `last_ok_at` through the
  shared helper, so this class is closed by measurement, not by test alone.
- **Owner control:** the accounts table is readable at any time; a NULL `last_ok_at` next to a fresh send is
  the signal.
- **Residual risk:** any future writer of `message_log` (a new channel, a migration) can repeat this. Rule
  going forward: a table with N writers gets N tests, or it gets one writer.

---

## IL-8 · A promise that covered a stage it could not see (2026-11-13)

**Class.** The heartbeat promised "next cycle at T" for the WHOLE cycle, so one long stage inside it was
indistinguishable from a stall, and a ~15 min refresh sat exactly on the watchdog's 900 s grace. Observed: the
heartbeat stamped 64 s past its own promise with `stage="cycle 2"` — no way to tell which step was slow.

- **Prevention:** dispatch runs first, and the refresh is a separate off-window, once-per-day, hard-budgeted
  stage (`engine_loop._default_cycle`, `engine_loop._refresh_if_due`, `intake.refresh_due`,
  `_REFRESH_BUDGET_S = 600 s`). A paused queue holds the pass — an ungated attempt re-imports every cycle
  (self-audit find, pinned by `test_a_paused_queue_holds_the_refresh`).
- **Detection:** every tick stage stamps the heartbeat on entry and again with its measured duration; the
  cycle's trace is `heartbeat.stages`, and a stage past its alarm budget writes one
  `STAGE OVERRUN [stage] Ns > Ns budget` row. A truncated refresh writes `REFRESH BUDGET: … next pass
  resumes`.
- **Recovery:** a truncated pass resumes on the next off-window cycle (the day is not stamped); dispatch is
  untouched by any of it.
- **Owner control:** `.heartbeat.json` is readable with the engine down, and `stages` names where the cycle is
  and how long each step took (OPERATIONS §1, §11).
- **Residual risk:** the tick stages' budgets are alarms, not preemptive cutoffs — a genuinely hung browser is
  bounded by the probe's own timeouts, not by the tick. The refresh stage's first live pass is still pending
  (due 04:00 PKT; R2 records it as remaining evidence).

---

## IL-9 · One lead's silence stopping the whole lane (2026-11-13)

**Class.** The send path returned `unknown` for a frame the classifier could not name, and the dispatch loop
treats any non-`sent` outcome as a LANE-level stop (`break`). Claim order is stable by id, so the same lead
sat at the head of every tick: `SEND BLOCKED [unknown]` at 11:46:47Z and 12:33:55Z, each one a lost send for
no nameable reason. Same shape as IL-2 (one poisoned render starving the queue), different cause.

- **Prevention:** a send never reports a nameless verdict — `_resolve_send_surface` returns `ready` or a named
  blocker; and a blocker that is a fact about ONE lead (`profile_no_message`) skips that lead for the pass
  instead of breaking the lane. The lead stays `ROUTED`, because a stale selector is not proof that an
  account cannot receive a DM.
- **Detection:** `SEND SKIPPED [profile_no_message] lead N` once per lead per 30 min, so the count and the
  identity are both readable; `thread_slow` stays a lane-level `SEND BLOCKED` because a slow thread really is
  a slow session.
- **Recovery:** the skipped lead is retried on a later pass with no state change and no cap slot spent; a
  `thread_slow` lead is retried when the lane wakes.
- **Owner control:** both rows name the reason in the digest; the lead's state shows it was never touched.
- **Residual risk:** `profile_no_message` may mean our three selectors are stale rather than the profile
  having no Message button — the difference needs one live observation, which R9 records as remaining
  evidence. Until then no lead is drained on this signal (by design: a false guess here spends a real lead).

---

## IL-10 · A test holding the production probe lock (2026-11-13)

**Class.** `gate.py check` ran while the engine was live, and `digest` 2026-11-13T13:16:51Z recorded
`IG REPLY PROBE ERROR: ProfileBusy("profile held by probe 'gate-live' until 13:18:17Z")`. Two tests
(`tests/test_b10_wa.py:569`, `tests/test_b11_ig.py:500`) take `acquire_probe_lock("gate-live", ttl_s=90)` on
the SHARED lock path, so verifying the code blocks the machine's only sending lane for 90 s.

- **Prevention:** CLOSED 2026-11-13 (ROADMAP R10, cause corrected). The lock was granting too readily, not
  too rarely: `browser._engine_state` read "asleep" from the promise alone, and a mid-cycle stamp promises
  900 s on purpose (R2), so a *running* cycle looked like a long sleep. It is now stage-aware — the parked
  shape (`"cycle N parked Ns"`) sleeps, any other stage name means `running` and the lock refuses. Three pins
  in `tests/test_b25_contention.py`. Giving the tests a private `tmp_path` lock (the first design) would have
  permitted two Chromium sessions on one profile, i.e. it would have removed the protection.
- **Detection:** the `IG REPLY PROBE ERROR … ProfileBusy` row, whose lock owner (`gate-live`) names the
  culprit; and, before the fix, a live-browser test dying in `launch_persistent_context` while the heartbeat
  showed a future `next_wake_at` — that pair is the signature of "busy engine read as asleep".
- **Recovery:** the lock expires after 90 s and the next tick's probe succeeds; nothing is sent or lost in
  the meantime, but no reply can be detected either.
- **Owner control:** the digest row is enough to tell "a probe collision happened" from "Instagram is down".
- **Residual risk:** any other fixed-path resource a test takes (R8's PNGs are the same family) can perturb
  the live lane until the gate is provably side-effect free.

---

## IL-11 · A capped lane that stops listening (2026-11-13)

**Class.** Reaching the daily send cap turned reply detection off for an hour. Measured 2026-11-13: IG hit
15/15 at `13:02:43Z`; the next heartbeat read `cycle 1 parked 3600s`. `seconds_to_next_wake` drops a region
whose accounts are all at cap (correct — no claim can succeed), the work set empties, the function returns
`None`, and the loop falls back to its 1-hour idle sleep. The reply probes run *inside* the cycle, so a reply
arriving at 13:20 is not seen until ~14:17. Same shape as the 2026-11-04 pause spin (a gate state mistaken for
an empty queue), inverted: now an empty *claim* set is mistaken for no work at all.

- **Prevention:** CLOSED 2026-11-13 (ROADMAP R11). `seconds_to_next_wake` now separates an empty queue
  (`None`) from a SPENT fleet inside an open window (`_MAX_PAUSE_S`, 300 s) and from a lane with no cap left to
  give today (not work), never returns `0.0`, and still sleeps a CLOSED window to its own open so a capped
  night costs no browser probes. `_fleet_cap_left` is the one predicate behind the distinction; four pins in
  `tests/test_cap_wake_20261113.py`, including the uncapped control and the closed-window guard.
- **Detection:** the heartbeat's `parked 3600s` next to a non-empty `ROUTED` count is the signature; both are
  readable without the engine. After the fix the same state reads `parked 300s` (live 15:24:22Z), so a
  regression is visible in one field.
- **Recovery:** the hour expires, or a restart re-turns the crank (`OPERATIONS.md` §8). Nothing is lost.
- **Owner control:** a capped day is visible in `SUPPLY_COUNTDOWN.md` (`today_capacity_remaining`), so a late
  reply can be anticipated from the contract files alone.
- **Residual risk:** every non-claimable-but-not-empty state (cap, all lanes asleep, everything on hold) can
  re-open this class; the wake math needs one rule: "probes keep their cadence while leads exist".

---

## IL-12 · A gate that hangs instead of failing (2026-11-13)

**Class.** The quality gate's authority rests on it producing a verdict. A wedged local model made five
live-model tests block forever instead: `/api/tags` answered in 0.19 s while `/api/chat` returned zero bytes
for 295 s, no runner process ever spawned, and `pytest` sat at 38 % across four separate runs (two of them
full `gate.py check` invocations, killed at ~20 min). A hang reports nothing — worse than a red, because red
names a test.

- **Prevention:** CLOSED 2026-11-13 (ROADMAP R12). Every live-model call now carries an explicit `timeout_s`
  that reaches the socket (`ollama_adapter._real_transport`), the five live tests use one bounded attempt
  (60 s, `tests/_model_limits.py`) and the gate's pytest step is wall-bounded (`gate.PYTEST_WALL_S`, 1800 s) so
  a stall becomes a named failure instead of silence. Measured while fixing it: a cold `llama3.1:8b` load can
  exceed 45 s, so the deadline clears a cold load and the retry budget is what was cut — a 15 s deadline would
  have turned a slow first call into a false red.
- **Detection:** `pytest` idle at one collection point with no output; `curl` to `/api/chat` returning
  `HTTP=000` while `/api/tags` is healthy; `/api/ps` empty. All three were used to name this class today.
- **Recovery:** restart the local Ollama service (owner action — the process is machine-wide and was not
  started by this lane), or deselect the five named tests and report the reduced run as reduced.
- **Owner control:** the digest/ops never depend on the model for sends, so a wedged model costs judgement
  quality, not the lane; `startup_readiness` already probes the endpoint at boot.
- **Residual risk:** every other live dependency (scraper DB, browser profile, Instagram) could hang a gate the
  same way until R12 lands.

## IL-13 — our own DM registered as the lead's reply (found 2026-11-14)

**What the owner would have seen.** `R6 — inbox triage`: 14 unacknowledged rows, described as "one real reply".
Reading it: 13 carry "no reply text captured" (audit artefacts) and row 14's `reply_text` was **our own
outbound DM**, truncated to 280 characters.

**Cause (measured).** 107 of 195 `message_log` rows carry no `body` (sends before body logging, including lead
3151's 2026-11-04 send). The IG probe guarded against our own copy with exact membership in `_ours_copy`, and
`register_reply` stores at most 280 characters — so when the newest text in a thread was our DM, what the probe
read was a *prefix* of our copy and membership failed.

**Fix.** `replies._looks_like_our_copy`: our copy is recognised as ours when the reading is an exact member, a
prefix of any body we ship, or shares the first 60 characters of one. Pinned by
`tests/test_reply_direction_20261114.py` (7 pins, the live shape among them). Both the IG and WA direction
guards now use it.

**Consequence.** No reply has been lost by this: the register can only be *more* conservative. What it removes
is a false REPLIED, its ladder cancel and its owner handover — and it makes O10's `dm-only`/`commented` rates
honest, because a self-reply was counting as a reply.

## IL-14 — one unlogged browser profile stopped the whole engagement stage (found 2026-11-15)

**What the owner would have seen.** `ENGAGE ERROR: ProfileMissing("profile 'Outreach-ig4' is not on disk at
A:\outreach-department\...")` every cycle from 08:05Z, and no comment, follow or warm-up from any account for
that time — including `ig1`, whose profile is fine and which is the only account with work to do.

**Cause (measured).** `ig4` is registered in `accounts` (WARMING, seeded 2026-11-04) but has never been logged
into, so its profile directory does not exist. `engine_loop._open` correctly refuses to auto-create a logged-out
profile (`require_profile`), and the stage's factory loop let that exception out of `run_engage_stage`, aborting
every remaining account. The fault is one account's, the blast radius was the fleet — the same shape as F2 (one
poisoned render aborting the whole cycle) one level up.

**Fix.** `engage_stage.run_engage_stage` catches `ProfileMissing` per account, records
`<account>:profile_missing`, writes one deduplicated digest line
`ENGAGE SKIPPED [no browser profile] <account>: <path>` and continues with the rest of the fleet. Any other
exception still propagates: this is a named, narrow case, not a blanket swallow.

**Live evidence.** `ENGAGE SKIPPED [no browser profile] ig4: profile 'Outreach-ig4' is not on disk` at 08:28:19Z,
with `AMBIENT [ig1]: 4 min reels` continuing at 08:27:55Z and no `ENGAGE ERROR` after the fix. Pinned by
`tests/test_engage_profile_skip_20261115.py`.

**Also in the same window (not an incident, a scheduled consequence).** `ENGINE ERROR: loop cycled through
TemplateError('variant drift: V4 body differs from the pinned owner copy')` — the R5 copy rewrite left the live
`variants` rows holding the retired text, and the drift guard (correctly) refuses to overwrite them silently. The
owner-approved paste path resolved it: `command_center variants --reseed` →
`VARIANT COPY reseeded from the owner paste: V4, V6, V7, V9`. The guard stays as it is: copy changes only through
that explicit act.

**Owner control.** `ig4` will keep being skipped, once per cycle in the digest at most, until the profile exists.
To bring it in: log into Instagram once in that profile, or remove the account from the fleet until it is needed.

## IL-15 — four sender rows, one working login (found 2026-11-15, owner-visible)

**What the screen said.** `accounts` carried four IG lanes (`ig1`–`ig4`, all ACTIVE/WARMING). The disk said:
`ig1` = `.chrome/Outreach`, **signed in**; `ig2`/`ig3` = profiles that exist but carry **no `sessionid`** (logged
out); `ig4` = **no profile directory at all**. Nothing in account selection read either fact, so engagement opened
browsers for lanes that could only meet a login wall, and a claim could have been stamped to a lane that cannot
send.

**Cause.** The registry was trusted as the truth. The rows for `ig2`–`ig4` were hand-seeded on 2026-11-04 (three
inserts 100 ms apart, outside `onboarding.add_account`, which refuses exactly this), and no code path ever
re-checked the disk afterwards.

**Fix (`accounts.profile_health` / `usable_account` / `health_reason`).** The disk is now read at the decision
point: `signed_in` / `no_session` / `unknown` / `missing`, never raising. `pick_account` refuses a lane with a
missing profile or a proven logged-out one and names it in the error; the engage stage skips it with one
`ENGAGE SKIPPED [no_session|profile_missing]` row and opens no browser; `command_center fleet` prints the verdict
per account. **`unknown` deliberately stays usable** — the one account that really sends reads `unknown` while its
browser holds the cookie DB, and excluding it would stop the fleet.

**Live evidence.** `fleet` now reads:
`ig1 … (signed in)`, `ig2 … (no IG session)`, `ig3 … (no IG session)`, `ig4 … (no profile)`, with `ig1` still
claiming 3/15 sends. Pinned by `tests/test_fleet_profile_health_20261115.py` (6 pins, including the claim refusal
and the digest line).

**Owner decision (2026-11-15).** Sign the lanes in **inside the automation lane** rather than pointing automation
at the owner's own Chrome profiles (`Profile 9 "Outreach One"`, `Profile 10 "outreach 2"`, `Profile 12 "outreach
3"`, all signed in). ADR-13 forbids automation opening the owner's profiles, their Chrome is running (its cookie
DBs are locked), and two processes sharing one cookie jar churn the session and blend the owner's browsing identity
into the outreach accounts. Three one-time logins in `.chrome/Outreach-ig2|3|4` is the whole cost.

**Follow-up, same day.** Sign-in windows were opened for `ig2`, `ig3`, `ig4`. Measured afterwards from the cookie
jars: `Outreach-ig2` and `Outreach-ig4` = **signed_in**; `Outreach-ig3` still open, waiting on an email code.
Also measured: while ANY window holds `.chrome`, the engine's own browser cannot launch at all
(`TargetClosedError`, one ProcessSingleton per user-data dir) — so the engine is paused for the duration, and
`interactive_login.py` now explains that instead of surfacing a raw launch error. Procedure in `OPERATIONS.md` §7g.

**Identities, measured 2026-11-15 from the live sessions** (page read only, no cookie decryption):
`ig1` = `fazbuildsthis`, `ig2` = `fazbuilds`, `ig3` = `faz_codesthis` (**signed out**, 401), `ig4` = `fazcodes`.
The saved-login rows agreed for ig2/ig4; ig1 has no saved login at all, so only the live read could name it, and
`fazbuildsthis` vs `fazbuilds` is exactly the confusion a registry-only fleet invites. Lane→handle map lives in
`OPERATIONS.md` §7g.

## IL-16 — 2026-11-15 · An alert that fires on a lane the owner switched off is noise

**Found.** The first real `alerts.sweep` run against the live DB (R3, 2026-11-15T10:56Z) raised
`ALERT P1 [PROBE_STALE]: [wa] last probe 305h 12m ago`. Nothing was wrong: WhatsApp has been paused by the
owner since 2026-11-02, so its reply probe is silent **by design**. The condition R3 named ("no successful
inbox probe for 2 open-window hours") had no notion of a lane being switched off.

**Fix.** `alerts._channel_live` skips a paused channel (`wa_paused`) before measuring staleness —
`tests/test_alerts_20261115.py::test_a_paused_lane_is_never_called_stale`. The pre-release row (id 1223) was
removed; the release self-test balloon is the only residual.

**Law.** An alarm measures the system against *its own current configuration*. A switch the owner turned off is
configuration, not a fault — before adding a monitor, ask what "healthy" means while each lane is paused,
disarmed or halted.

## IL-17 — 2026-11-15 · Our own DM read back as a restaurant reply

**Found.** `owner_inbox` 16 and 17 (New Spice Grill, Chatkhara By Krishna) held OUR copy — *"Salam! New Spice
Grill's 4.5 stars from 795 reviews caught my eye…"* — and 15 (Eat & Dip) held Instagram's canned
*"Hi, thanks for contacting us…"*. All three had flipped the lead to `REPLIED`, cancelled its ladder and handed
the thread over as if a human had answered.

**Cause, two layers.** The direction guard compared the newest bubble against (a) the opener rendered *today* and
(b) `message_log`. Both failed for 86/109: their threads carried copy from the **frozen template family**
(`templates/social_nodish.txt`, rendered before dish extraction existed) while the guard rendered the **variant
lane** that is on today, and the log row held a third copy again. One sample of a rotating, config-dependent copy
family cannot prove whose text is in a thread.

**Fix.** `templates.frozen_family` + `variants.all_possible_openers` now enumerate **every** render this lead could
have been shipped — both frozen variants, every pinned variant body (paired with all three P.S. bindings) — and
`replies.classify_newest` is the one direction law: `ours`, `auto`, `reply`, or `None` (too short to judge). A
fuzzy fallback (≥80 % of the candidate's content words in one of our known copies) catches an edited paste.
Canned acknowledgements are `auto`: a digest row, **no** advance, ladder keeps running.

**Repair.** `inbox.revert_false_replies` (also `command_center inbox --repair`) undoes such a row: the lead returns
to its last real pre-reply state, the inbox row stays as audit and is acknowledged, and one digest row names what
was undone. Ran live 2026-11-15: leads 86/109 → `SENT`, 164 → `FOLLOWUP`, zero unread replies left.

**Evidence.** `tests/test_false_replies_20261115.py` — 16 pins (family coverage, our unlogged frozen copy is
`ours`, a human answer is never ours, four auto-responder shapes, three human shapes, short bubbles unjudged,
and the three repair paths).

**Law.** Direction is decided on the *whole* copy family the lead could have received, never on one render; and a
canned acknowledgement is not a person reading the message.

## IL-18 — 2026-11-15 · The model runs on the owner's clock, not the engine's convenience

**Order.** *"Pause the llama thing so it can happen after 8:55pm everyday and not before that."*

**Why.** A 4.92 GB model on a 6 GB card competes with the Instagram browsers all day: inference stalls, the
live-model tests time out, and the machine fights itself. Nothing on the send path uses the model, so a closed
window costs no send.

**Law.** `ollama_adapter.model_window_open` (20:55 → midnight, local) is enforced twice: in `generate()` before any
I/O, and again in `_real_transport` on the real clock so an env seam cannot open the wire early. A closed window
raises `ModelWindowClosed`, deliberately NOT `OllamaDead` — the owner's schedule must never pause the queue.
Callers degrade one by one: comments refuse, dishes fall back to deterministic mining, ICP skips the pass,
proofread fails open, variant copy falls back. Live tests skip instead of failing a daytime gate.

## IL-19 — 2026-11-15 · The engagement lane keeps the owner's hours

**Order.** The whole comment thing after 20:55, stopped before 10:00, plus a switch for "PC is needed".

**Why.** Engagement is the only stage that opens Chromium for its own sake. Run in the day, it competes with the
owner's work on a machine that already hosts four Instagram lanes; run at night, it costs nobody anything.

**Law.** `engage_stage.engagement_window_open` (config `engage_window`, default 20:55-10:00 local, band may cross
midnight) is checked before any browser work, alongside `engage_paused` (config `engage_paused`, set by
`command_center pause --engage`, cleared by `resume --engage`). Both refuse the lane and nothing else: the send
queue has its own switch and is never touched. A malformed band refuses rather than opening.
