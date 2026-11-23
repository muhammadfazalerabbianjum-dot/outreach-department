# DECISIONS.md — executable-decision ledger

Format: batch · question · decision · evidence · alternatives · grade
(OPTIMAL / CONSTRAINED / ASSUMED) · reversibility. Evidenceless entries = red
batch; each ASSUMED/CONSTRAINED carries an upgrade trigger.

## B0 · Salvage: delete or archive drifted build
Decision: archive everything to `_archive_prev_build/20261022-drift/`, delete nothing.
Evidence: blueprint audit mandate; SALVAGE table in DISCOVERY.md §0.
Alternatives: hard delete (less reversible); keep in place (pollutes repo).
Grade: OPTIMAL. Reversible: trivially (un-archive).

## B0 · Gate #2 scanner: native Python instead of `rg`
Decision: `gate.py` implements the `TODO|FIXME|pass *#` scan natively (regex),
identical semantics, over `src/`; gate #7 scans outside `tests/`.
Evidence: Windows dev machine has no guaranteed rg; stdlib-first (ADR-1).
Alternatives: require rg dependency (environment fragility); skip check (violates gate).
Grade: OPTIMAL. Reversible: yes (swap implementation later).

## B0 · tests/ excluded from secrets scan, kept for stub-scan scope decision
Decision: gates #2 stays src/-scoped per blueprint; #7 excludes tests/ because
tests carry canonical secret-shaped fixtures (scanner caught its own test file).
Evidence: test run output in checkpoints/BATCH0.md.
Alternatives: obfuscate fixtures (opacity harms review); keep false positive (gate permanently red).
Grade: OPTIMAL. Reversible: change scan scope anytime.

## B0 · Ollama model choice
Decision: `llama3.1:8b` for both AI roles; fallback `qwen2.5-coder:7b` if the
Batch 5 ICP fixture corpus agrees <90%.
Evidence: `ollama list` output (only these models installed; $0/no-download law
pending network confirmation — network later proven, but plan unchanged).
Alternatives: llama3.2:3b (weaker judge); qwen2.5-coder:7b primary (code-tuned).
Grade: CONSTRAINED (hardware/installed-set). Upgrade trigger: Batch 5 corpus
results, or owner installs a stronger 7–8B instruct (e.g. qwen/qwen2.5:7b-instruct class).

## B5 · ICP judge: model CONFIRMED, alias canonicalization

## B0 · Browser binary for senders (provisional)
Decision: Playwright persistent context over the downloaded Chrome for Testing
(headful, dedicated `Outreach` profile dir); realistic alternative is
`channel="chrome"` against system Chrome. Final call lands at Batch 10 with
screenshot evidence (ADR-15).
Evidence: playwright 1.62 + CFT chromium installed (checkpoint §2); ADR-13
requires a profile isolation the owner's Chrome shares shell ownership of.
Alternatives: system channel (more realistic fingerprint, higher touch risk to owner profiles).
Grade: ASSUMED (verification step: owner confirms at Batch 10 wiring; QR/login with the Outreach profile is the owner-session anyway).
Reversible: config-level switch.

## B0 · "Barrud Bhai" profile absent on this machine
Decision: treat every existing Chrome profile as owner-owned (never touched);
automation gets a fresh dir named "Outreach" created by our launcher only.
Evidence: Local State info_cache listing (DISCOVERY.md D7).
Alternatives: hunt further on other drives (speculative; no evidence of other installs).
Grade: ASSUMED (upgrade trigger: owner confirms whether "Barrud Bhai" lives elsewhere or is a stale blueprint name). Also logged as QUESTIONS.md Q1.

## B0 · City→tz rows added beyond the scraper's map
Decision: config rows will include Vancouver→America/Vancouver (evidenced in
scraper's google_maps.py), Salalah→Asia/Muscat, Seattle/Portland→America/Los_Angeles.
Evidence: constants.py:301-311 + google_maps.py:63-71 for Vancouver/Edmonton;
Salalah/Seattle/Portland absent from both scraper tables.
Alternatives: leave unknown_timezone (guess-averse like the scraper) — but the
blueprint's ADR-9 needs a working lane map now.
Grade: ASSUMED (standard IANA). Upgrade trigger: owner config review before first live send.

## B0 · Long-haul state files (.opencode/plan.md + journal.md, git-ignored)
Decision: adopt to survive context relays across batches (Batches 1–18).
Evidence: multi-session autonomous directive; skill guidance.
Alternatives: single-session memory (fragile).
Grade: OPTIMAL. Reversible: n/a (bookkeeping).

## B3 · Contacted-elsewhere leads: state semantics
Decision: new `leads.previously_contacted` column (migration v2) for D2 signals
(exported_at/exported_wa_at/exported_ig_at/last_contact_ts/is_unsubscribed/
outreach_status/outreach_stage ≠ not_contacted); rows mirror into leads but
never enter the active queue. No state-machine abuse (REJECTED is ICP-only).
Evidence: B3 tests (contacted rows stay NEW + flagged); migration ledger v2.
Alternatives: REJECTED-with-reason (pollutes ICP reject_reason semantics);
DUP (they are not duplicates).
Grade: OPTIMAL. Reversible: migration v3 could repurpose; column is additive.

## B3 · Fuzzy name+city rule (live-data driven)
Decision: `_name_duplicate` = token-set equality OR token containment with
≤1 leftover designator token, OR SequenceMatcher ≥ 0.94 (raised from 0.90).
Evidence: live run — `Ahla Restaurant` vs `Abha Restaurant` (Salalah) matched
at 0.90 and are two different restaurants (false positive); `مطعم المعلقة
الذهبية GOLDEN SPOON` ↔ `Golden Spoon مطعم المعلقة الذهبية` matched and ARE
the same restaurant (Arabic/English reorder). Token sets catch reorders;
0.94 floor still catches typo pairs and stays clear of the Ahla/Abha pair.
Alternatives: pure ratio (false positives at 0.90); edit distance (no better
on 1-char brand substitutions).
Grade: CONSTRAINED (corpus = live dataset only). Upgrade trigger: owner
reports a wrong dup/no-dup; re-tune with those pairs as new regression tests.

## B3 · Phone-level dedupe semantics: "same call destination"
Decision: keep blueprint phone rule literally — normalized-digits collision →
DUP even for different businesses sharing a number (Salalah Gardens Mall
directory phone used by many tenant restaurants; 490 live phone collisions).
Evidence: dup_analysis live run (BATCH3.md): 490 phone dups incl. mall
directory numbers +968 23 381000 / +968 23 212186.
Alternatives: exempt shared mall numbers (needs mall-number registry = new
data source, evidenceless; owner might still want the tenant as a lead).
Grade: OPTIMAL for queue hygiene (never double-message one number).
Reversible: threshold/registry upgradable when owner comments on mall numbers.

## B5 · ICP judge: model CONFIRMED, alias canonicalization
Decision: llama3.1:8b stays (corpus 9/9 live agreement ≥ the 90% bar; B0
fallback trigger did NOT fire). Observed reject-reason synonyms canonicalize
in `_REJECT_ALIASES`; truly unknown reasons still fail closed (parse
ValueError → PAUSE + halt). A pass verdict carrying a reject_reason is
refused, not silently normalized.
Evidence: probe transcript (BATCH5.md) + corpus test output 9/9.
Alternatives: swap model (needed); silently accept any reason string (masked drift).
Grade: OPTIMAL. Reversible: alias map is data; strict gate is one line.

## B7 · Frozen template: sanctioned fills + PROVISIONAL copy until owner paste
Decision: ADR-6 read as TWO sanctioned fills — {{dish}} everywhere in dish
variants and {{name}} (first word of lead name) inside the named variants'
greeting, chosen between "Hi guys," (unnamed) and "Hey <name>," (named).
Current copy is PROVISIONAL — the blueprint's real frozen text is not
materialized anywhere (QUESTIONS.md Q5); `template_status` config gate
defaults PROVISIONAL and the B9 dispatcher refuses without "owner_confirmed".
Byte-drift gates: pinned sha256 in tests + runtime hash check in render.
Evidence: grep over repo/archive/ledgers found no template text.
Alternatives: invent marketing copy silently as final (violates no-fabrication);
halt engineering (violates never-pause directive — this is a paste task, not
a destructive or research blocker).
Grade: ASSUMED (copy semantics; machinery is exact). Upgrade trigger:
owner pastes real copy → re-pin hashes + set status owner_confirmed.

## B8 · Machine map correction: QUALIFIED → UNREACHABLE edge
Decision: added the edge. Leads with no usable channel exit at routing
time (before any dispatch claim), matching the blueprint's UNREACHABLE exit
semantics "no contact channel". The B1 map only allowed UNREACHABLE from
ROUTED — an unreachable-by-design lead would have been stuck R requiring a
fake route.
Evidence: B8 suite caught the gap; machine tests updated for the new edge.
Alternatives: route channel-less leads to ROUTED then exit (fake claim);
keep lead QUALIFIED forever (queue stalling zombie).
Grade: OPTIMAL. Reversible: one map line.

## B9 · PKT as fixed UTC+05:00 offset
Decision: implemented PKT as a fixed `timezone(timedelta(hours=5))` in
caps.py instead of ZoneInfo("Asia/Karachi"). Pakistan Standard Time has no
DST so the fixed offset is year-round exact, and Windows zoneinfo needs the
tzdata pip wheel (new dependency — avoided, ADR-1 stdlib-first).
Evidence: pkt_date tests pin the exact UTC→PKT date rollovers.
Alternatives: zoneinfo + tzdata dependency (violates dependency discipline).
Grade: OPTIMAL. Reversible: one constant if tzdata ever becomes justified.

## B9 · Claim/commit split for the dispatcher
Decision: `claim_next` PREPARES only (renders the message, checks queue/
template/window/cap/lane, writes NOTHING) and `commit_send` lands
daily_sends + message_log + SENT in one transaction after the sender
(B10/11/12 browser mechanics) succeeds. Rationale: mark SENT only after a
real send; a crash between claim and commit leaves the lead ROUTED
(idempotent-safe; the B10 screenshot in message_log is the sent-proof).
Claims take a caller-owned `skip` set to advance past already-prepared
leads; the runner owns it in memory.
Evidence: test_claim_next_happy_path (claim does not flip state);
test_gate3_commit_at_cap_refuses_and_writes_nothing (refusals touch nothing).
Alternatives: claim-reserves-cap-then-commits (wasted cap slots on browser
failure); intermediate "CLAIMED" machine state (violates frozen map).
Grade: OPTIMAL for single-writer; upgrade trigger: if a second process ever
races the queue, switch to a claim-token column inside the caller's
transaction. Reversible: API shape stable for B15.

## B9 · Email-routed leads are never claimed while ADR-5 gate is on
Decision: claim_next skips route_channel='email' rows entirely (and
commit_send raises EmailPendingOwner on them); they sit ROUTED until the
owner pastes the email template, then drain by B12 without re-routing.
Evidence: test_claim_next_never_claims_email;
test_commit_email_waits_for_owner_template (state stays ROUTED).
Alternatives: leave emails to a separate unreachable-ish lane (loses the
queue position); fake-send with a placeholder template (forbidden).
Grade: OPTIMAL. Reversible: one lane rule line.

## B9 · Email pacing bounds [30, 90]s assumed
Decision: ADR-7 freezes WA 60–180s and IG 90–300s but says nothing about
email gaps; seeded email pacing as 30–90s (≤20/day cap dominates rate).
Evidence: DECISIONS.md ADR-7 text; `daily_sends` DB cap is the real limit.
Grade: ASSUMED. Upgrade trigger: owner specifies email gap preference.
Reversible: one config row.

## B10 · Normal-Chrome UA on the Outreach profile launch
Decision: launched the persistent context with an explicit current-Chrome
user-agent (HeadlessChrome UAs draw WhatsApp Web's "works with Google
Chrome 100+" update wall instead of the app). Verified live: with the UA
pinned the REAL login shell renders. The pre-fix classifier returned
`unknown` for the update wall — correct fail-closed behavior — but the UA
override is what makes the product reachable at all.
Evidence: checkpoints/evidence/WA_B10_*.png before/after.
Alternatives: headed browser always (violates unattended runs); CDP attach
to the owner's own Chrome (forbidden — ADR-13 never touches "Barrud Bhai").
Grade: OPTIMAL. Reversible: one constant.

## B10 · WA live smoke skips on external-network failure
Decision: the B10 live test pre-flights a HEAD to whatsapp.com and
pytest.skip without internet. Ollama live tests stay strict (their deadness
IS the fail-closed product behavior); WhatsApp reachability is the
environment's, not the product's, and gating the whole queue's CI signal
on aISP outage would false-RED the build.
Evidence: test file contract; skip observed only when offline.
Alternatives: hard-fail the gate on connectivity (false REDs from ISP).
Grade: OPTIMAL. Reversible: remove the skip.

## B10 · WA composer selector pending owner session confirmation
Decision: `_COMPOSER = 'div[contenteditable="true"]'` generic contenteditable
matcher; the exact Business-composer selector gets confirmed/adjusted in the
live owner-login session (B15 runner dry-run). Classifier is selector-free
by design so this cannot break detection.
Evidence: fake-path tests assert typing; live confirm = owner task.
Grade: ASSUMED. Upgrade trigger: first real login dry-run.
Reversible: one constant.

## B11 · IG warmup start stamped at first lane touch (not first success)
Decision: `send_ig_message` stamps `ig_warmup_start` before the cap
pre-check — the ADR-8 ramp begins with the first real attempt to use the
lane, even if that attempt hits a login wall and aborts (the day still
counts). Rationale: the ramp protects the account, not the queue; a day
with zero successful sends is exactly the day the least IG traffic
should occur, so counting it is the conservative direction.
Evidence: happy-path test asserts the stamp; capped test shows wk1 = 5.
Alternatives: stamp at first successful commit (queue could hammer the
lane the instant login works, defeating the warmup purpose).
Grade: ASSUMED (stricter than blueprint's silence). Upgrade trigger:
owner's warmup preference. Reversible: one call-site move.

## B11 · IG halt marker is config `ig_halted` + full queue drain at once
Decision: on a ban/login-challenge signal the sender does not merely skip
the affected lead — it stops the whole IG lane (config marker checked in
the claim loop) and immediately re-routes every ROUTED ig lead through
CHANNEL_BANNED → ROUTED → (email | UNREACHABLE), so the queue keeps moving
on email while ownership sees the halt (digest/alert wiring at B16).
Evidence: drain test verifies per-lead history, stranded-lead handling and
that claim_next refuses ig lanes post-halt.
Alternatives: drain lazily at B15 scheduler (delays recovery; the point of
ban detection is fast lane exit); per-lead only (lane keeps hammering).
Grade: OPTIMAL. Reversible: config row + one loop.

## B13 · Reply = exactly-once handover via state-transition-then-cancel
Decision: `register_reply` first machine-transitions (REPLIED edges
already frozen: SENT/FOLLOWUP→REPLIED), THEN cancels every pending
follow-up (status='cancelled', cancelled_by_reply=1) and drops one
digest alert. Idempotency front-door: a lead already REPLIED returns
"already" with zero writes; a non-SENT/FOLLOWUP state refuses. The
matcher keys on the normalized From address of ACTIVE leads only.
Evidence: gate #4 test asserts exactly one cancel row, one history row,
one alert across three calls; empty-inbox and noise paths no-op.
Alternatives: cancel-then-transition (dead follow-ups on a refused
transition); dedupe table (overkill when the state machine IS the
dedupe). Grade: OPTIMAL.

## B13 · IMAP poll fail-closed with RECORDED errors (never silent)
Decision: poll_email_replies returns state ok/unreachable and carries
the failing stage text in `.error` (select/search/fetch/imlib exceptions);
missing .env creds raise MailboxPendingOwner loudly; nothing is marked
Seen (idempotent re-scans). The default factory must LOGIN before
select — the real login→NONAUTH catch became a regression test.
Evidence: live poll ok + every negative path unit-tested (18 tests).
Alternatives: pause the queue on unreachable mail (replies are inbound
facts — read-only polling must not gate sends); auto-mark seen (loses
re-detection ability). Grade: OPTIMAL.

## B12 · Variant set collapsed from 4 provisional to 2 real files
Decision: the owner's real pasted copy (2026-10-23/24) is one shared text
for WA+IG ("Hi guys" greeting, {{dish}} hard requirement) plus one email
text (Salam + {{name}}/{{restaurant}}/{{platform}}/{{dish}}) — the earlier
4-variant scaffold (named/generic × dish/no-dish) is deleted, token syntax
normalized to moustaches at freeze time ([name] ⇒ {{name}} etc.; words
untouched), and hashes re-pinned. `social.txt` + `email.txt` are now the
single source of frozen copy.
Evidence: b7 suite rewritten byte-exact-green; drift gate fires at runtime
too. Alternatives: keep 4 variants (dead code + invented no-dish copy —
ADR-6 violation). Grade: OPTIMAL (owner's real copy decides).

## B12 · Dish-less leads HOLD at ROUTED (claim-loop skip, MessageHold)
Decision: leads whose extracted_dish is absent/blank raise MessageHold in
the claim loop and render paths; they stay ROUTED and are skipped — no
no-dish copy is invented (QUESTIONS.md Q7). Same hold fires when an email
lead's name is too blank to fill the greeting.
Evidence: b12 claim/sender tests assert state stays ROUTED, no transport.
Alternatives: draft a fallback no-dish text myself (banned by ADR-6);
reject the leads outright (loses qualified leads for a copy problem that
is fixable by an owner paste). Grade: CONSTRAINED (owner blessing pending
— upgrade when a no-dish variant is pasted; debt noted in DIGEST).

## B12 · Email arming is config `email_enabled` (seed 0) + .env creds
Decision: ADR-5's disable switch lives in config `email_enabled` (seeded
0, armed by the owner after pasting SMTP creds into .env); commit_send
raises EmailPendingOwner when disarmed, claim skips email lanes; the real
transport reads creds ONLY from .env and refuses loudly on missing
SMTP_HOST/USER/PASSWORD/FROM, non-numeric SMTP_PORT, or missing SMTP_FROM
— fail-closed at every layer (environment, config, transport).
Evidence: disabled/capped/paused/nodish/bad-port tests assert zero
transports opened and nothing committed. Alternatives: .env-only switch
(invisible in the command center at B16); always-on with per-send refusals
(noisy discovery of missing creds mid-queue). Grade: OPTIMAL.

## B12 · Email subject seeded "Your own ordering app — mockup inside"
Decision: the owner delegated the subject ("think of something good
bro"); it is a config row `email_subject`, seeded once, editable — not
code. No attachment is modeled until the owner registers the mockup file.
Evidence: b12 subject test + default-factory fail-closed test.
Grade: ASSUMED (delegated). Upgrade trigger: owner retypes the subject.

## B12 · gate.py RED-report is encoding-safe
Decision: gate.py's detail printing falls back to ascii-with-replacement
when the cp1252 console cannot encode RED detail text — a failing gate
must still REPORT its failure instead of crashing inside the reporter.
Evidence: reproduced the UnicodeEncodeError, fixed, gate runs to RED/GREEN
verdict on any console. Grade: OPTIMAL (reporter robustness in scope).

## B12·live · SMTP creds sourced from the scraper repo (owner-directed) + 465 = SSL
Decision: owner was too busy to fetch creds and pointed at the scraper repo's
Hostinger block; only names were echoed to chat, values copied straight
into OUR `.env` (SMTP smtp.hostinger.com:465 per owner's ManyReach usage;
IMAP wired for B13 reply detection; EMAIL_ENABLED=1). The live run caught
a latent B12 bug — port 465 needs implicit TLS — fixed in the default
factory with a regression test on both branches.
Evidence: `_tmp_smtp_check` login OK; self-addressed send receipt 250;
gate 100.00%. Grade: OPTIMAL.

## B12·live · `ready_home` classifier verdict (session proof, still blocks)
Decision: WA probe gains a chat-list `shell` flag and IG a logged-in-feed
`home` flag, both classifying `ready_home`: live-session proof for the
ADR-15 verification flow, NOT sendable — senders still need the deep-link
composer, so any send on `ready_home` fails closed.
Evidence: negative probes (screenshots) prove the owner's logins landed
in their PERSONAL Chrome, never the automation profile — logins must be
redone inside the automation window (owner task). Grade: OPTIMAL.

## B12·live · Owner-delegated research fixes (subject, no-dish, attachment)
Decision (owner: "best recommended proven research based fixes for each"):
1. SUBJECT becomes a config template with {{restaurant}} filled from the
   lead name — personalized subjects are the proven cold-open lift; seed
   "I mocked up {{restaurant}}'s own ordering app"; raw markers left
   behind raise TemplateError (never a half-filled subject).
2. NO-DISH leads render the rating-anchored pair (Q7) using data_json
   rating/review_count — evidence the lead already has, zero invention;
   neither-dish-nor-rating leads still HOLD.
3. NO attachment machinery, deliberately: cold-outreach practice is that
   attachments raise spam risk; the body already carries the mockup
   sentence and signature link. When the owner registers a mockup path,
   the proven pattern is a hosted link, not an attach.
Evidence: b7/b12 suites + gate 100.00% incl. render_for_lead law tests.
Grade: OPTIMAL (owner delegated). template_status stays PROVISIONAL —
the owner's eyeball flips it (Q9).

## B14 Ã¢ Ladder anchors: touch1=+3d Ã¢ touch2=day7 Ã¢ expiry grace +3d
Decision: first touch armed at send+3d; touch2 armed at touch1-due+4d
(lands day 7 since both anchors are the same clock); NO_REPLY expiry =
touch-2 due + 3 days grace. Blueprint freezes day3/day7/max-2 + NO_REPLY
exit but not the grace length; 3d mirrors the touch cadence and gives a
late-arrival window for replies.
Evidence: 14 B14 tests (exact due_at math, grace both sides, idempotent
expiry); gate 100.00% GREEN.
Alternatives: rolling ladder (touch2 = touch1-send+7d: same numbers,
more moving parts); no grace (expiry on day 8 â?? harsh; a weekend rep
would die as NO_REPLY). Grade: ASSUMED on grace length â?? QUESTIONS Q10.
Reversible: one constant (_EXPIRY_AFTER_LAST) + one config row if
owner names another number.

## B14 Ã¢ Resends are transport-only adapters; ladder owns bookkeeping
Decision: senders[channel](db, lead_id, now, message) adapters perform
ONLY transport and return an outcome with .state; the ladder does its
own message_log row, caps via caps.register_send, and the single
machine step SENTÃ¢FOLLOWUP on touch 1. Resends re-render the SAME frozen
copy (no second template exists to invent Ã¢ ADR-6). Non-"sent"
outcomes leave the row pending for the next run Ã?? no arm-ahead.
Evidence: cap negative test (resend counted, refused at cap), fail path
test (network_error Ã? pending), gate #4 interplay test (reply kills
rows, ladder never re-arms them).
Alternatives: reuse B9 commit_send (would re-transition SENTÃ¢SENT
â?? invalid edge); skip cap counting for follow-ups (violates ADR-7).
Grade: OPTIMAL.

## B15 Ã¢ Wake law: work-limited windows, expiry never window-gated
Decision: seconds_to_next_wake = 0 when any region with work is in an
open window (or a lapsed follow-up is expiring), else the earliest next
window opening across work regions; None when nothing is queued. The
engine wakes only when it has work Ã¢ idle queues hold zero timers.
Expiry is a DB write, so it runs even on a closed Saturday.
Evidence: wake-math tests incl. Sat 12:00 PKT Ã? Mon 03:30 (142200s),
4h-until-ME-open, no-rule regions, empty-days rules, naive-UTC inputs.
Alternatives: fixed hourly cron (blind wakes on empty DBs; the blueprint
says the session machinery lives in CONFIG rows, not cron).
Grade: OPTIMAL.

## B15 Ã¢ Tick order is reply-priority: mailbox Ã? ladder Ã? expiry Ã? dispatch
Decision: poll_email_replies runs FIRST every tick, so a replied lead is
hand-overed (REPLIED + cancels) before the ladder resends or dispatch
touches it. Queue PAUSED = the tick parks gracefully (stopped_by=
'paused'): no sends, no half bookkeeping, replies still recorded
(inbound facts outrank the send pause).
Evidence: order test (reply beats ladder+dispatch, _no_resender must
never fire), paused-tick test (sender.calls empty, state untouched).
Alternatives: poll after sends (a replier could get one more resend
between polls Ã? a hard anti-goal).
Grade: OPTIMAL.

## B15 Ã¢ Ladder calls RESENDERS (transport-only). Dispatch calls senders(end-to-end).
Decision: run_engine_tick carries two maps. 
esenders follow B14's
transport-only contract (resend_email: no commit/caps/machine Ã? the
ladder owns bookkeeping); senders are the B9"?.12 end-to-end senders
(commit_send inside, only on ROUTED leads). A SENT-state lead can never
reach the dispatch senders (their commit path would crash) Ã? contract
test asserts exactly that.
Evidence: real resend via FkTransport through the tick; never-routes
test; unwired-channel stop reason unwired:<ch> for WA/IG until the
owner's automation-window logins land.
Alternatives: one map with flag params (spreads bookkeeping across two
layers); refactoring B9"?.12 to transport-only (vast rework, breaks their
frozen tests). Grade: OPTIMAL.

## B16 Ã¢ The interface is a CLI over the scheduler tick, not a daemon
Decision: python command_center.py <cmd> — status/run/pause/resume/
alerts. 
un executes ONE tick with real timestamp; loop-on-interval
composition stays at the OS level (Task Scheduler / manual), keeping a
paused queue and a reboot both handled by the DB row (B15 wake law).
WA/IG keys are ABSENT from the runtime sender maps (owner logins
pending) so the tick stops unwired:<channel> instead of crashing the
browser lanes into a wall.
Evidence: 14 tests + LIVE status/lerts run against the real cold
outreach.db (rc 0, digest JSON); gate 100.00%.
Alternatives: long-running daemon (fights the owner's short sessions
and complicates contract recovery); GUI (over budget).
Grade: OPTIMAL.

## B16 Ã¢ Cold-DB status reports caps as unseeded (null) instead of crashing
Decision: build_status wraps current_cap; a cold/unseeded engine
database shows "cap": null + used 0 so the owner sees the REAL state
(engine seeds on first run) instead of a stack trace.
Evidence: cold-DB live status run + unit test.
Grade: OPTIMAL.

## B17 Ã¢ Retired import = one fingerprinted digest row, never touching the retired reminder app
Decision: the fired slot history lands as ONE digest summary row (not
per-slot spam), fingerprinted via sha256 in config; same file never
imports twice; changed file logs its changed history; empty = noop.
The retired reminder app stays byte-identical UNSAFE-behavior-wise (read-only oath).
Evidence: import test suite + real read of the 7 fired entries; gate
100.00%.
Alternatives: a dedicated fired-slots table (nothing downstream needs
them individually; the digest is the retirement surface).
Grade: OPTIMAL.

## B17 Ã¢ Template gate parks the TICK (stopped_by template_gate), not crashes
Decision: a cold/off in the owner loop means the tick STOPS with
stopped_by="template_gate" instead of raising: pythonw loop-parked
until the owner says yes (Q9) Ã? calm, not noisy.
Evidence: cold-DB default_cycle test (wake None, no crash) + live
python -m outreach.engine_loop 1 rc=0 on the real DB.
Alternatives: an error digest per tick (spam for weeks); a crash loop
with 300s retries (churn without benefit).
Grade: OPTIMAL.

## B17 Ã¢ Startup .cmd = OS-level loop wrapper; remove = delete the file
Decision: register_startup writes outreach_engine_startup.cmd (repo dir
+ PYTHONPATH=src + pythonw -m outreach.engine_loop). The engine boots at
login, parks harmlessly on every gate, and is reversible by file
removal. Live-registered (ADR-15 evidence in BATCH17 checkpoint).
Evidence: file live-written + loop palindrome run (-m ... 1 rc 0).
Alternatives: schtasks (more moving parts; the blueprint's own retired
tool used this exact pattern); registry Run key (harder to remove).
Grade: OPTIMAL.

## B18 . E2e mocks at the transport seams; the machine between them is REAL
Decision: the e2e dry-run mocks exactly three external worlds (LLM
verdict responses in Ollama native shape {"message":{"content"}},
SMTP context-manager transport, IMAP inbox) and runs everything
between them for real: store, machine, corpus dish gate, router,
dispatcher, commit bookkeeping, ladder, reply poll, command digest.
The story asserts cross-stage invariants no unit test could
(resend body byte-equeals first body; reply cancels exactly once;
ordering mailbox-before-resend; REJECTED = nothing routed).
Evidence: 5-test e2e suite + full suite 342 passing at 100.00%.
Alternatives: pointing e2e at live Ollama (flaky, and live model
tests already exist per stage); skipping e2e (unacceptable for the
capstone batch).
Grade: OPTIMAL.

## B18 . The audit is executable
Decision: DEFERRED.md's open-ends register (DT-5..DT-10) is asserted
by a test (B13+ debt present, template_gate/IG warmup/Q10 named),
so a future edit cannot silently drop owner-facing debt. Owner
activation tasks stay in DIGEST.md, separate from debt.
Evidence: test_audit_open_ends_lands_in_deferred.
Alternatives: prose-only audit (drifts unproven; the stub-scan gate
already proved code-side cleanliness).
Grade: OPTIMAL.

## B20 - Deterministic ICP pre-pass runs before the LLM (multi/chain/corp)
Decision: qualify_new_leads applies a rules pre-pass (actor icp-rules) with
fixed precedence: multi_location (is_multi_location flag, a marker proving
>2 locations, or brand group >2 in the run scope) -> chain (is_chain flag OR
snapshotted HARDCODED_CHAINS substring match) -> corporate_account
(hotel/resort/airport/major-brand affiliation regex; the real row "Al Baleed
Resort Salalah by Anantara" is the fixture). The chain reason uses the
pre-existing canonical enum value, extending the spec's two-reason shorthand;
a brand group of exactly 2 passes to the judge. Obvious rejects never cost an
LLM call.
Evidence: 12 rule tests (flag / marker / brand-group / affiliation /
priority order) + full suite green with the pre-pass on the B5 live corpus
(9/9 agreement held).
Alternatives: LLM-only (wastes calls on deterministic rejects, risks
inconsistent verdicts for identical structures); reject every group (a
two-location independent restaurant is valid ICP).
Grade: OPTIMAL.

## B20 - Region ladder's last rung is address country, not timezone
Decision: ladder = city lane map -> phone country code (international form
only) -> config country_region_map read from the payload's address country
-> REGION_UNRESOLVED hold (NEW <-> REGION_UNRESOLVED edges; only ROUTED ever
dispatches, so a held lead can never be messaged). The spec's timezone
fallback was substituted: the scraper payload carries no tz field, and
re-deriving region from a city string would re-open the lane-map drift B3
closed.
Evidence: resolve_regions tests (phone cc / address country / hold / release)
+ E.164 lane tests + routing refuses to dispatch a held lead.
Alternatives: timezone from city (unavailable in SSOT); digit-guessing the
country code (+1 vs +1684 ambiguity); leaving region NULL (claim loop would
crash in_window).
Grade: CONSTRAINED - the optimal rung (tz) does not exist in the SSOT;
upgrade trigger: if the scraper ever ships a tz field, insert it as the 3rd
rung above country (log to debt register).

## B20 - to_e164 derives the country code from the LANE, not from digits
Decision: cleaning.to_e164(phone, region) prefixes via the lane map
(_CC_TO_REGION: 1 -> na, 968 -> me). Already-international numbers pass
through normalized, national numbers get the lane prefix, unprovable input
returns the raw digits. It never pre-guesses wa_dead: a national number
stays routable in its own lane.
Evidence: E.164 tests (+1 613-555-0101 -> +16135550101; +968 23 228248 ->
+96823228248; unknown region -> digits unchanged).
Alternatives: digit-pattern guessing (misroutes territories); storing raw
only (the WA lane needs one canonical key for dedupe + send).
Grade: OPTIMAL.

## B20 - Chains list is a committed 994-entry snapshot, no runtime scraper import
Decision: src/outreach/chains.py holds HARDCODED_CHAINS parsed as text from
the scraper's own `models.py` (generated once, committed). ADR-3 keeps
the scraper read-only for DATA; importing their code at runtime would couple
our gate to their source tree and their edits would silently move our
verdicts. False-positive check run over the fixture corpus BEFORE writing the
rules: 0 hits on the 4 QUALIFIED fixtures.
Evidence: chain snapshot tests (membership / case / substring) + b5 corpus.
Alternatives: runtime import (coupling, non-reproducible verdicts); a
hand-typed short list (misses real chains).
Grade: OPTIMAL.

## B20 - Watcher ingests whole runs under a bounded per-cycle LLM budget
Decision: watcher_tick polls list_scraper_runs (scraper opened ?mode=ro) and
intakes every pending run with limit = config intake_per_cycle (default 25
LLM leads per stage per run per cycle), then scans low-lead stock. The state
machine makes every pass resumable, so a 2162-row run catches up over cycles
instead of stalling the engine loop; import/region/clean/route stages always
run to the end (cheap, no LLM).
Evidence: watcher tests (ingest-then-quiet, grown-run re-ingest, engine
cycle wiring, WATCHER ERROR digest survives a watcher fault).
Alternatives: unbounded intake per cycle (blocks dispatch for hours);
bounding ROWS instead of LLM stages (re-reads the scraper file every cycle).
Grade: OPTIMAL.

## B20 - intake_run owns run-completion bookkeeping (one path for CLI + watcher)
Decision: when intake_run reports complete it stamps config
ingested_runs[run_id] = {rows, completed_at} itself. The CLI backfill and the
watcher therefore share one completion path: a CLI run leaves the watcher
quiet, a re-run is a no-op (recorded rows = scraper count at import), and a
scraper count above the recorded rows re-opens the run (addendum 1). The
first RED run caught the split-brain version (watcher-only marking made CLI
re-runs report pending).
Evidence: idempotency test (second CLI intake -> []) + watcher quiet test
(zero LLM calls on re-poll) + grown-run re-ingest.
Alternatives: watcher-only marking (the bug above); marking without a row
count (growth undetectable).
Grade: OPTIMAL.

## B20 - Low-lead alert: one per starvation episode, silent before intake
Decision: scan_low_leads counts QUALIFIED+ROUTED never-sent leads per region
against config low_lead_threshold (default 10). Empty DB = silence (an
un-ingested DB is not starvation). The episode signature (sorted starving
regions) lives in config low_lead_alerted: repeat scans in the same episode
write nothing, recovery clears the signature, the next drain alerts again.
auto_restart_leg defaults FALSE (alert-only) with a 6h per-region cooldown
on the D10 region-matched invocation; the owner flips the flag to arm it.
Evidence: episode tests (fires once / dedupes / recovery re-arms),
threshold-config test, flag-off and flag-on+cooldown tests, engine wiring.
Alternatives: alert per scan (digest spam as counts tick); restart always
on (spawns scraper legs the owner never asked for).
Grade: OPTIMAL.

## B20 - Cross-channel reply cancel was structural; slice 9 proves, no new code
Decision: register_reply cancels ALL pending followups by lead_id (the
followups table has no channel column) and process_due_followups skips state
not in (SENT, FOLLOWUP), so a REPLIED lead cannot dispatch by ANY path.
Slice 9 adds proofs only: a reply arriving on email cancels day3+day7
exactly once (digest alert names the channel), and a stale pending row
injected AFTER the cancel dispatches nothing (claim loop sees ROUTED only).
Evidence: test_reply_on_any_channel_cancels_all_followups +
test_replied_lead_never_dispatches_by_any_path (gate #4 law extended).
Alternatives: channel-stamped followups (schema churn, zero behavior
change); an extra guard in claim_next (already structural).
Grade: OPTIMAL.

## B20 - Dish confidence threshold is config; its source label stays distinct
Decision: config dish_confidence_threshold (default 0.7); an LLM dish below
it stores dish=NULL with source "low_confidence" (a threshold rejection)
while validation-gate rejects keep source "fallback" (ADR-6 gate unchanged -
B6 tests pin that label). Sourcing stays inside data_json (menu first,
reviews second) under the owner's delegation ("find the dish using your own
brain, reviews or wherever"); nothing is ever invented.
Evidence: threshold tests (default / at-threshold boundary / configurable)
+ full b6 corpus green.
Alternatives: hardcoding 0.7 (owner cannot tune); reusing "fallback" for
both (breaks the B6 provenance contract).
Grade: OPTIMAL.

## B20 - IG link repair classes are structural; no network probing
Decision: repair_instagram returns (value, class): VALID_FORMAT (canonical
instagram.com/<handle> built from a clean handle), SUSPECT (post/reel/stories
paths, ig.me short codes - original kept for ONE attempt, then IG_DEAD via
the ADR-8 halt path), DEAD (bare hosts, non-IG URLs, >30-char handles, "n/a"
- the router falls to email per ADR-5). No HTTP probing: the scraper DB is
the only allowed source and live fetches would add a network surface the
blueprint forbids.
Evidence: repair tests (valid / suspect / dead) + dead-IG routing tests
(dead -> email; suspect -> one ig attempt).
Alternatives: live probing (network surface, rate limits, ban risk on the
Outreach profile).
Grade: OPTIMAL.

## B20 - CLI-first backfill, engine restart after (no concurrent LLM intake)
Decision: the production DB was filled with `python command_center.py
intake` (exact run IDs read from the DB) BEFORE restarting the engine PID
with the new watcher code. Two intake paths on one DB would double every
LLM call and race the ingested_runs stamp; the watcher then boots onto a
caught-up DB and only polls.
Evidence: per-run live intake reports + engine-cycle tests (BATCH20).
Alternatives: restart first (watcher and CLI interleave - nondeterministic
double work).
Grade: OPTIMAL.

## B20 - Live verdict drift must not crash intake (off-enum reject_reason)
Question: during the real backfill llama3.1 answered reject with
`reject_reason: "on_delivery_platform"` (it read our OWN scraped-flag line
`is_on_delivery_platform`), parse_verdict's closed enum raised, and
judge_lead's ValueError path fail-closed PAUSED the production queue
(HALT record written) - the whole intake died rc=1 over a label.
Decision: a REJECT verdict's reason is telemetry, not a gate: parse_verdict
now canonicalizes known aliases and keeps any other NON-EMPTY label verbatim
(the verdict already says qualified=false - the safe direction; the column
already stores free text like "chain (3 locations)"). Genuinely malformed
verdicts still fail closed exactly as before (reject without a reason,
reason on a pass, non-bool/non-number/out-of-range fields). Prompt hardened
in the same breath: the enum list now renders from the
_ALLOWED_REJECT_REASONS constant (single source, sorted = deterministic for
temperature=0) plus the line `Use "other" if none of those fits; never
invent a new reason value.` - and `on_delivery_platform` was left as an
honest verbatim record, NOT aliased, because being on a delivery platform
is not itself an ICP reject criterion (the model's verdict stands, its
label is kept as evidence).
Evidence: test_parse_verdict_keeps_off_enum_reason_verbatim (RED first,
reproducing the exact live failure, then GREEN); the invalid-shape
parametrize KEEPS every malformed case (reject-without-reason, pass-with-
reason, non-string reason 7, bad types, range); full suite 99.15% rc=0;
queue resumed (`command_center.py resume`) and the backfill relaunched.
Alternatives: aliasing unknown -> "other" (erases WHY the model rejected -
the label is the only evidence of this drift); retrying the call (the
model would likely repeat itself at temperature=0, then pause anyway);
accepting empty reasons (that IS the malformed case worth failing closed).
Grade: OPTIMAL - and the pause itself proved ADR-4 fail-closed works live
(HALT record component=icp captured before the fix).

## B20 - Owner copy edit transplanted + hashes re-pinned (ADR-6 owner path)
Question: owner edited the rendered preview in-place ("tinkered with the
template, all good now") - four line deltas inside a UTF-16 evidence file -
while the REAL templates (src/outreach/templates/*.txt) stayed byte-frozen
behind the b7 hash gate. How does an edit to a RENDER become the frozen copy?
Decision: treat the preview edit as the sanctioned ADR-6 owner edit:
inverse-map each delta onto the four template files keeping every fill
marker ({{dish}} etc.), re-pin PINNED_SHA256, and update the byte-exact
expectations to the owner's new words (owner decree, not test-gaming - the
drift gate went RED first, proving it watches). Applied: (1) social.txt
delivery line expanded ("and having to spend crazy amounts on marketing");
(2) social_nodish.txt rating line to "stars" + good-feedback/chef
sentences + the same delivery expansion; (3) email.txt delivery expansion
+ sign-off "Faz from Carnama Labs"; (4) email_nodish.txt received the same
propagation because the owner never saw that render - flagged to the owner,
reversible on veto. social.txt feeds WA AND IG (one shared file), so the
WA expansion reaches IG too - flagged rather than silently split. Q9 flips
as a result: production template_status=owner_confirmed, and email_enabled
=1 (ADR-5 arm condition - template pasted - met 2026-10-23, confirmed now).
Evidence: b7 RED (9 failures) on the raw edit -> re-pin -> b7+b12 49
passed rc=0; preview regenerated UTF-8 with all 6 renders + subject row
(old file showed only 4 and was double-encoded).
Alternatives: reject the edit and ask the owner to re-paste text-only
(they already typed their preferred copy - making them redo it buys
nothing); edit only the preview (sends would keep the OLD copy - a lie);
split social.txt into WA/IG files (scope expansion nobody asked for).
Grade: OPTIMAL (reversible: re-pin back + git revert; only the two
flagged propagations need owner veto to roll back).

## B20 - ME window 14:00-20:00: owner answer overrides ADR-9's 19:00
Question: ADR-9 freezes ME Mon-Fri 14:00-19:00 but also declares windows
are config rows; owner answered Q2 with "yeah from 2pm to 8pm". Blueprint
conflict policy says the blueprint wins - who wins?
Decision: the owner (instruction priority 1: user's explicit instructions
outrank the frozen ADR). ME window set to 14:00-20:00 PKT in BOTH places
the window lives: the production session_windows config row (live
scheduler) and _DEFAULT_WINDOWS in dispatcher.py (fresh-DB seed). ADR-9's
stated 19:00 is now stale prose; this entry is the override trail. NA
window (03:30-09:30) untouched.
Evidence: config before/after printout (me.end 19:00 -> 20:00, na
unchanged); b9+b15 57 passed rc=0 after the code-default edit; grep shows
no test pins 19:00.
Alternatives: change only the config row (fresh DBs would silently revert
to 19:00 - two sources of truth); amend the ADR text in AGENTS.md
(blueprint is frozen; the override belongs in this ledger).
Grade: OPTIMAL (reversible: two literals back).

## B20 - Local toast YES; Gemini API-key dashboard DECLINED (ADR-12)
Question: owner wants to hear from the department (Q11) and floated "a
dashboard with a gemini 2.5 api key" giving a concise breakdown every ~2
days with issues listed - while also saying "if it is not needed then we
can very much avoid building ourselves another thing". Build it?
Decision: no external dashboard. Three reasons: ADR-12 red-lines paid
APIs/SaaS, new accounts and keys; lead + alert data would leave this
machine; the need is already covered cheaper - a local Windows toast on
new digest rows (owner opted in: "yes this sounds like a good idea"),
plus pull via `command_center.py status`/`alerts` or any AI session
(state lives in outreach.db, never in one chat). The toast batch runs
after B20 lands (config-flagged, no accounts, localhost-only).
Evidence: ADR-12 text (no auth surface, no paid APIs/SaaS, no listeners);
the owner's own "avoid building another thing"; existing status/alerts
CLI already prints counts, queue health and open alerts.
Alternatives: build the Gemini dashboard anyway (violates ADR-12, costs
money, exports lead data - requires an explicit owner override of the ADR
first); a local markdown report command (deferred - pull already answers
it; add only if the owner asks again after living with toast+status).
Grade: CONSTRAINED by ADR-12 - upgrade trigger: owner explicitly
overrides ADR-12 in writing (then amend the blueprint, never a silent
exception). Reversible: n/a (nothing built).

## B21 - Owner toast: digest-table drain, balloon, cooldown, stamp-always
Question: Q11 - how does the owner actually hear from the department?
Owner opted in ("yes this sounds like a good idea") to a local toast on
new digest alerts. Where is the seam, and what are the semantics?
Decision: drain the digest TABLE once per engine cycle from run_forever,
BEFORE the wake-sleep (a REPLIED handover must not wait hours for the
next wake) - the table is the single seam that catches every writer
(REPLIED handover, watcher alerts, engine/watcher errors, retirement
import). One local powershell NotifyIcon balloon per new row: Windows
built-in assemblies only, CREATE_NO_WINDOW (no flash), message passed
via env var (no quoting hell), icon kept alive 9s. Semantics: config
`owner_toast` default ON (the owner's opt-in IS the default; row=0
disables), 600s cooldown ACROSS drains (an error loop must not spam the
desktop), and the stamp ALWAYS advances - even on disable/cooldown/
sender failure - because the digest row is the durable record and the
toast is best-effort sugar; a broken shell therefore retries at most
once per window, never once per cycle. drain_engine_toasts swallows
EVERYTHING: a toast fault must never park the engine loop.
Evidence: tests/test_toast.py RED first (ModuleNotFoundError, all 8
failing) -> implementation -> 8/8 GREEN rc=0; live smoke via the REAL
powershell path (`sendast returned: True`, balloon popped on the owner's
screen); wiring test proves one drain per engine cycle.
Alternatives: powershell WinRT toast (needs AppUserModelID registry +
shortcut machinery - overkill for one alert type); msg.exe message box
(intrusive, requires a click); a second polling daemon (exactly the
"another thing" the owner asked to avoid); pull-only status (rejected -
the owner explicitly opted in).
Grade: OPTIMAL (reversible: set owner_toast=0, or delete the two-line
wiring; takes effect on engine restart).

## 2026-10-26 - Repeated power-loss crashes during the B20 backfill: serialize + boot-proof Ollama
Question: the box suffered 4-5 unclean shutdowns between 14:52 and 17:18 PKT
today (4x Kernel-Power 41 + one clean restart), every one while the backfill
window was live. Relaunch the same way, or change the setup?
Decision: (1) SERIALIZE - intake runs alone now, `gate.py check` waits until
intake completes (they used to run concurrently); (2) engine stays STOPPED
until backfill + gate are done - re-asserted because the Startup .cmd
resurrects it on every boot and each fail-closed wake writes a halt + PAUSED
flip (3 OllamaDead halts already logged today); (3) boot-proof Ollama: the
Startup Ollama.ln targeted "ollama app.exe" (tray) which produced NO running
server after any of today's boots, so it now targets "ollama.exe serve"
(the binary proven working 3x manually), minimized.
Evidence: event log = Kernel-Power 41 at 14:52:45, 15:32:58, 16:22:30,
17:18:30 PKT; NO BugCheck 1001, no WHEA, no Resource-Exhaustion 2004, no
display-driver 4101, no disk errors -> power-loss / hard-freeze signature,
not a driver, RAM, or disk crash (RAM 9.7GB/16GB free). DB integrity ok
after every crash (atomic writes), zero data lost; lead counts unchanged
across relaunches (last state change 14:45 PKT) - zero new judgments in all
3 relaunch windows, root cause under diagnosis (first cold generate measured
50.6s for 10 tokens; adapter timeout is 120s so a cold load alone does not
trip retries). Owner asked directly whether power flickered or the machine
froze - awaiting answer.
Alternatives: (a) keep intake+gate concurrent (rejected - maximizes load and
both jobs died with every crash); (b) Ollama CPU-only (rejected - inference
already slow, would stretch the backfill to days); (c) halt the batch until
hardware is checked (rejected for now: zero data loss, jobs resumable, owner
responsive - revisit on a 5th crash or a power-flicker report).
Grade: CONSTRAINED (hardware root cause not visible from software; upgrade
trigger = owner confirms flicker -> PSU/outlet check, or next unexplained
shutdown -> full stop until hardware is looked at). Reversible: restore
concurrent gate run; restore Ollama.ln to "ollama app.exe".

UPDATE same day ~17:50 - ROOT CAUSE of the intake stalls FOUND, with a
second-order correction: the FIRST fix attempt was a no-op. Evidence: for
"localhost" getaddrinfo returns `::1` first; Ollama serves v4 only. A raw
two-address probe measured `::1` -> refused after 2.03s vs v4 -> connected
in 0.0s (every adapter call paid a ~2s v6-refusal penalty), and on at
least one boot (window 1, 15:09-15:32, zero judgments in 23 min) the ::1
SYN produced no answer at all while the process sat in `sock.connect(sa)`
(py-spy stacks, twice: judge_lead -> generate -> urlopen ->
create_connection -> socket.py:853). Window 2 (16:09) was import-time
only (~2 min before the crash). Window 3 (17:25 launch) was actually
JUDGING normally (~7s/row, 32 verdicts 17:26:28-17:30:17) when it was
killed during the diagnosis - that kill was a misread of momentary
connect frames as a permanent wedge (process only 5 min old, not 15;
rc=-1 in the shell log is that deliberate kill). CORRECTION: pinning
`.env` OLLAMA_BASE_URL to 127.0.0.1 (relaunched 17:30:23) changed
NOTHING - grep proves no code reads that key; the endpoint is the
hardcoded constant `OLLAMA_URL = "http://localhost:11434"`
(ollama_adapter.py:23), and the 17:30 process still went to ::1
(observed: SynSent ::1, zero model calls). Real fix, TDD: contract test
`test_live_ollama_liveness_contract` strengthened to require a literal
IPv4 loopback -> RED ("unexpected Ollama URL contract:
http://localhost:11434") -> constant changed to
`http://127.0.0.1:11434` -> GREEN: tests/test_b4_ollama.py 13/13 rc=0
(adapter 99% cov; `_assert_local` already allows 127.0.0.1, gate #9 /
ADR-4 localhost-only intact). Intake restarted on the fixed module.
Intake mystery RESOLVED; this entry stays CONSTRAINED only for the
hardware power-loss question, still awaiting the owner.

UPDATE 2026-10-27 - serialization REVERSED (the leg this entry listed as
reversible): no Kernel-Power 41 since 17:18 on 10-26, and the 05:35 PKT
boot today was orderly (no 41 before it = clean shutdown overnight);
owner pressed for finalization. Intake and gate ran CONCURRENTLY today,
both rc=0 (intake: both runs stamped 01:17:52Z / 01:50:21Z; gate:
GREEN, 98.75%). Evidence before reversal checked, not assumed. The
other legs stand: engine stayed STOPPED until backfill+gate finished,
Ollama.ln keeps the `ollama.exe serve` retarget (it again failed to
fire at the 05:35 login - manual start, DT-16 open), hardware question
still awaiting the owner (asked twice).

## 2026-10-27 - Send path live fix: wa.me -> direct web URL, load -> DCL, classify polled through boot

Question: the restarted engine failed 6/6 dispatch cycles with
`Page.goto: Timeout 30000ms` on wa.me, and manual probes showed classify
stuck `unknown` forever even after navigation - park the queue, hand
back, or fix the transport?

Decision: fix the transport in-place, TDD. Three parts, both blockers
removed: (1) `deep_link()` now returns the web app's own send URL
`https://web.whatsapp.com/send/?phone=<digits>&type=phone_number&app_absent=0`
(the interstitial's canonical Continue-to-WhatsApp-Web href) instead of
`wa.me` - wa.me lands on api.whatsapp.com's share page which NEVER
auto-forwards, so classify stayed `unknown` permanently; (2) goto uses
`wait_until="domcontentloaded", timeout=45000` instead of Playwright's
default `load`; (3) new `_wait_wa_state()` polls classify every 1s
through the app's ~54s boot with a 90s deadline, fail-closed at expiry
(last non-decisive state returned, nothing typed, lead untouched).

Evidence: engine digests 03:05:55/03:11:36/03:17:12/03:22:56/03:28:31/
03:34:13Z, all `TimeoutError ... navigating to wa.me, waiting until
load`; live probes: `load(complete)` at t0+27.7s vs DCL 1.2-3.4s across
4 fresh launches, wa.me interstitial classify=unknown for 40s (incl. a
click-through attempt, and its web.whatsapp.com links do not navigate
the tab), direct web URL lands 1.4-4.3s with the live session and boots
`unknown -> shell ~42s -> decision ~54s`; Chromium reached WhatsApp
(Established -> 57.144.149.32:443) so the network was never the cause.
TDD: 7 RED tests (all for the right reasons) -> implementation -> 29/29
green in tests/test_b10_wa.py; `python gate.py check` GREEN rc=0,
98.75% coverage, stubs 0, secrets clean (2026-10-27 09:0x PKT).

Alternatives: (a) raise the goto timeout to 60s keeping `load` - still
dead-ends on the interstitial, fixes nothing; (b) click the
interstitial's Continue link - extra fragile UI dependency when the href
target itself works directly; (c) park the queue and hand back - blocks
the owner's first send on a transport the probes already explained.

Grade: OPTIMAL (smallest change that removes both live blockers; every
claim live-probed). Reversible: isolated to wa_sender.py constants +
deep_link; `git revert` restores the old behavior.

## 2026-10-27 - Send-path batch: double-send layers + ADR-7 pacing supersession + engine-brick fix + IG paint-wait

Question: first campaign day produced four failure shapes at once: (1)
double-sends - `composer.type(full_copy)` fired Enter at every newline
mid-typing (lead 196 Retaj got 2x4 fragments, 0 DB rows) and the old
engine re-sent lead 2385 three times (14:32/14:38/14:44 PKT, three
composer-timeout ENGINE ERRORs, the `.message-out` guard never matched,
0 rows until recovery); (2) ADR-7's frozen gaps (WA 60-180s, IG
90-300s) no longer match the owner's superseding directive:
randomized 180-300s between ANY two sends, all channels; (3) engine
brick - a failed `launch_persistent_context` inside `_LivePage.__enter__`
never runs `__exit__`, leaking the sync-Playwright session flag so every
later launch died with "Sync API inside the asyncio loop" (digests
25-35, 17:53-18:18 PKT); (4) IG never sent - classify ran ONCE right
after goto while the SPA paints the composer ~5.1s later, so every
cycle fail-closed on `unknown`. Fix all four without touching frozen
copy?

Decision (one batch, commit 5d1f839):
- WA layers (wa_sender): `_type_copy` types one paragraph then ONE
  deliberate Enter (never a newline inside type() - the root cause);
  recovery guard `_already_delivered` (copy already in the chat and
  never committed -> screenshot + commit WITHOUT retyping); delivery
  proof `_wait_sent_tick` before screenshot+commit (every >=40-char
  paragraph present in the thread, else `unverified`: no commit, no
  count, no shot - fail-closed; the guard lands it on the next claim).
- Pacing supersession (ADR-7): `next_send_after` armed inside
  `commit_send`, enforced as a claim gate in `claim_next` (gap not
  expired -> no claim) plus the 4/hour slot cap; live `pacing` config
  row set to [180,300] for wa/ig/email.
- Engine brick (browser.py): a failed `__enter__` tears down the
  half-started Playwright session; the sync-API flag never leaks.
- IG paint-wait (ig_sender): `_wait_ig_state` polls classify (30s
  deadline, mirrors `_wait_wa_state`) through the SPA's ~5.1s paint.
- Live selector discovery (read-only probe): this WA Web build renders
  NO `.message-out` and NO msg-check icons (allIcons = wa-wordmark,
  tail-out) - the handoff's planned tick/guard selectors do not exist
  in this build; presence-of-copy verification replaced them.

Evidence: TDD red->green in b10/b11/b9/b14/b15; full suite 424 passed,
coverage 98.47%; `python gate.py check` GREEN rc=0 in-session; commit
5d1f839 records gate GREEN rc=0 98.26%. Live same day: lead 2385
recovery-committed 15:28:19 PKT (screenshot, NO fourth copy - the guard
caught the old engine's uncommitted send), lead 2391 fresh typed send
15:33:02 PKT - message_log rows 8->9 are 283.3s apart (>=180), four
paragraphs each delivered once with ticks; the passing delivery proof
on 2391 (Arabic restaurant name) also proves the thread text matched
the expected copy verbatim - a logically scrambled bidi result cannot
satisfy it, so the screenshot's mid-sentence Arabic is visual bidi
rendering, not corruption. Session-3 sweep same day: 15/15 WA sends,
15 distinct leads, zero DB duplicates, all post-15:28 gaps >=180s
(checkpoints/STATUS_2026-10-27_evening.md).

Alternatives: (a) keep ADR-7's old bounds - rejected, owner superseded
them with 180-300s; (b) sleep between sends inside the tick - rejected,
a claim gate + wake math survives restarts and cannot drift; (c) commit
right after typing with no delivery proof - rejected, that is exactly
how the uncommitted sends happened; (d) keep the handoff's
`.message-out`/msg-check selectors - impossible, the live probe shows
they do not exist in this build.

Grade: OPTIMAL (every layer test-backed and live-proved the same day;
frozen bytes untouched; reversible per layer).

Open: DT-18 - the delivery proof's fragment rule (a >=40-char prefix
of an expected paragraph satisfies `p.includes(t)`) lets a truncated
tail commit; tighten to exact normalized-paragraph match when an
actual truncation is observed.

=== 2026-10-29 (owner directive) — ADR-9 NA window moved to 21:00-23:00 PKT

Question: the PC was off across Wednesday's windows (both lost) and the
owner will not keep the 03:30 AM slot alive — "basically only the 3:30
and 4:30 sends to be timed at 9PM-11PM". When should NA sends happen,
and when does the change take effect?

Decision: `session_windows.na` -> start 21:00, end 23:00 PKT; days
[0,1,2,3] (Mon-Thu) unchanged; ME window untouched. Applied AFTER today's
03:30-09:30 window closes (owner's timing choice: keep draining today's
15 first) via the one-shot scheduled task `OutreachWindowFlip` at 09:31
PKT on 2026-10-29, with an in-session fallback run. Config-only — ADR-9
law: windows are config rows, not code. `flip_na_window.py` is
idempotent, writes a WINDOW CHANGE digest row, unregisters itself, and
FAILS CLOSED on any unexpected window shape (digest row, no guessing).

Evidence: `in_window`/`next_window_start` read the config row on every
call and `ensure_dispatcher_config` only seeds when missing (never
overwrites), so the engine picks the new band up on its next tick with
no restart; flip/idempotent/skip paths proven on a snapshot copy of
outreach.db before scheduling.

Alternatives: (a) flip immediately — kills today's in-flight NA batch
(owner chose to finish it first); (b) keep the morning band AND add an
evening band — config holds ONE window per region, needs schema/code and
crosses ADR-9; (c) also move ME — not requested, untouched.

Grade: OPTIMAL (owner-directed, config-only, one-row reversible: set na
back to start 03:30 / end 09:30 and delete the one-shot task).

### 2026-10-30 (owner directive) — outreach runs DAILY; one-time catch-up

Question: Friday morning had zero sends and the owner called it out
("we missed our morning windows terribly today... point to note is that
outreach will run daily now"). What is the standing schedule, and can
 oday still be salvaged?

Decision: (1) ADR-9 day bands DROPPED — `session_windows.na` and
`session_windows.me` both get days [0..6] (all 7): NA Mon-Sun 21:00-23:00
standing band, ME Mon-Sun 14:00-20:00. (2) One-time NA catch-up for
2026-10-30 only: na temporarily widened to 13:00-23:00, restored to the
21:00-23:00 standing band by the one-shot task `OutreachRestore` at
23:01 PKT (same fail-closed pattern as `flip_na_window.py`: exact-shape
match, digest row, snapshot-proven restored/idempotent/unexpected paths).
Config-only — ADR-9 law holds.

Timeline honesty: the original catch-up band was drafted as 09:00-11:00
but the session was suspended ~4h mid-turn and the config landed 13:05
PKT — after 11:01 — so the morning NEVER opened (schttasks rejected the
11:01 restore as already-past, N/A next-run). Corrected same-day to
13:00-23:00 before the window would have opened, engine restarted (PID
2640 @ 13:21 PKT), and sends resumed: 3 NA follow-up resends with
screenshots at 13:28/13:29/13:33 (leads 12/105/179, daily 3/15). All
2026-10-30 window math in this entry uses the Windows clock (PKT);
bash `date` in this environment drifted ~4h and is NOT trusted for
scheduling.

Alternatives: (a) restore na to 03:30-09:30 morning — contradicts
yesterday's explicit directive; (b) two windows per region (morning AND
21:00-23:00) — config holds one window per region, needs schema/code,
not requested; (c) no catch-up, let ME's 14:00 window carry today —
rejected, owner explicitly chose "one-time catch-up today only".

Grade: CONSTRAINED-then-OPTIMAL (morning half unrecoverable — suspended
session; afternoon/evening half delivered, restore armed, reversible by
deleting `OutreachRestore` and setting the row by hand).

### 2026-10-30 (owner directive) — temporary FOLLOW-UP template + gap sweep

Question: the ladder re-sent the OPENER on every rung (day 3/7/14/30/180),
so leads like "Eddy Lebanese cuisine" received the same message twice on
the same topic. The owner asked what was happening, asked for a sweep of
"all such problems", and supplied a temporary follow-up copy:
"salam! just following up on my previous message, hope it wasntt buried".

Decision: (1) NEW frozen variant `templates/followup.txt` (hash-pinned;
typo normalized "wasntt" -> "wasn't", "Salam!" capitalized to match the
house opener — owner to re-word if he wants it byte-exact). ADR-6's "no
second template may be invented" is AMENDED by this owner paste: ladder
rungs ship the follow-up line; FIRST SENDS and the day-7 IG escalation
(the first Instagram contact for that lead) keep the opener. (2) Probe
knowledge: the WA/IG reply probes now compare against opener+follow-up
(`_ours_copy`), otherwise our own rung would read as a lead reply (false
REPLIED + ladder cancel + handover) — proven by new tests in b13/b24.
(3) Ladder containment: a sender crash in `process_due_followups` is
recorded (SENDER ERROR row) and skipped instead of killing the whole
pass — same failure class as the 2026-10-29 IG composer crash. (4) An
unrenderable opener (MessageHold) no longer aborts every reply scan.

Evidence: gate GREEN rc=0, 96.39% coverage; live send lead 2397 at
16:11:49 PKT books template_hash 4a6c8436.. = the follow-up pin (engine
restarted 16:04 PKT with the new code); TDD RED->GREEN on 7 new/updated
tests (b7 x2, b14 x3 incl. crash containment, b13, b24, b18 x2).

Alternatives: (a) kill the ladder entirely — contradicts the standing
"we never stop following up" directive; (b) per-rung copy (day3 vs day7
different words) — one temporary line now, revisit when the owner writes
real rung copy; (c) send the follow-up only on day 14+ — not requested.

Grade: OPTIMAL (owner-supplied copy, config-file + hash pin, reversible
by swapping the one file; every seam covered by tests).

### 2026-10-31 (owner paste + directive) — 4-stage ladder copy, no-em-dash law, reply-guard

Question: three threads landed together. (1) The ladder's single temporary
line ("just following up... hope it wasn't buried") shipped unchanged on
day 3/7/14/30/180, and the owner pasted the real 4-stage set he wants
instead. (2) Owner directive: "no message should have any em dashes" —
including the existing openers. (3) "Make sure you don't send a follow-up
to a lead who replies": reply probes can be stale or failing while rungs
come due (live: 17:16-18:12 PKT probe outage, 39 WA + 39 IG errors).

Decision:
1. FOUR frozen variants replace the single line, hash-pinned:
   `followup.txt` (touch 1 = day-3 offer, byte-exact paste; the two pasted
   lines joined to one paragraph because both senders forbid an Enter
   inside type()), `followup_recap.txt` (touch 2 = day-7; "Hero Grill
   House's" -> {{restaurant}}'s per the ADR-6 moustache rule, words
   untouched), `followup_value.txt` (touch 3 = day-14, no fills),
   `followup_soft.txt` (touch 4+ = day-30/180; [restaurant] ->
   {{restaurant}}). `render_followup(lead_row, touch)` maps touch ->
   stage; a fill stage with a blank lead name raises MessageHold so the
   rung stays pending (never invent a restaurant). Reply probes learn all
   FOUR rung texts via `_ours_copy` — without them a later-stage rung
   would read as a lead reply (false REPLIED + ladder cancel + handover).
2. EM-DASH LAW (owner directive, applies to ALL message copy): the 7
   em-dashed openers (email, email_nodish, social_nodish, wa_generic_dish,
   wa_generic_nodish, wa_named_dish, wa_named_nodish) were swept with a
   punctuation-only swap " — " -> "..." (no word, no comma touched),
   hashes recomputed, and `test_no_template_contains_an_em_dash` pins the
   law. Incident note: the first sweep used write_text on Windows, which
   translated line endings to CRLF; the runtime drift gate caught it
   (hash mismatch) and every template file was normalized back to LF —
   read_text bytes == file bytes is now asserted before pinning.
3. REPLY-GUARD: a WA/IG rung defers while that channel's last SUCCESSFUL
   reply probe is >35 min stale (window + one tick of headroom). The row
   stays pending; probes retry every tick, so recovery is automatic and
   the rung fires on the next pass after a probe succeeds. First sends
   untouched; a stale probe can no longer fire a rung blind over a lead
   who already replied.

Evidence: gate GREEN rc=0, 96.20% coverage (pytest+stub+secrets all
PASS). TDD: reply-guard RED->GREEN (b14 x8, b15), stage set pinned in b7
(stage bytes/variants/hashes, blank-name hold, em-dash law), probe copy
b13/b24, e2e b18, email byte law b12 updated. Ollama was found down
(connection refused) failing 5 live tests; restarted `ollama serve` and
the full gate went green — environment, not code.

Alternatives: (a) keep the single temporary line — rejected, owner pasted
real stage copy; (b) rewrite em dashes as commas/re-words — rejected,
punctuation-only keeps every word byte-identical; (c) fail rungs hard on
a stale probe — rejected, the queue would stall on a probe outage;
defer-until-probe-recovers keeps the ladder moving safely.

Grade: OPTIMAL (owner bytes verbatim, hash pins, every seam test-backed,
reversible by swapping one file per stage).

Still open (owner-approved decisions not yet built): auto-extend window
<=90 min after an outage eats the session; split phone+IG leads
round-robin across WA/IG; v1.1 multi-account (accounts table, per-account
caps/warmup, stagger, owner login tooling); startup readiness script
(owner directive 2026-10-31: "there must be a script at startup ensuring
everything is ready" — slots were missed with the PC on because apps
didn't launch).

### 2026-10-31 B26+B27 — startup readiness gate + auto-extend (owner directives)

Context: 2026-10-31 boot — PC on, both Startup entries present
(outreach_engine_startup.cmd + Ollama.lnk), yet Ollama was dead and
nobody noticed; the owner's directive: "there must be a script at
startup ensuring everything is ready". Separately: "if power goes out
and we miss the slot what do we do?" (decision 2026-10-30, unbuilt
until now).

Decisions:
1. B26 startup_readiness (NEW module, the Startup entry point):
   - probe Ollama :11434 first; grace 5s (the .lnk instance may still
     be starting), then spawn detached `ollama serve`, re-probe until
     answering (bounded 90s). Never assume, never double-launch a
     healthy app.
   - engine proof: live leased pid untouched (ADR-13), but a DEAD or
     MISSING lease ALWAYS respawns even when the heartbeat promise
     looks fresh — a dead process cannot keep a promise. This is the
     boot/resume hole the watchdog's staleness check cannot see.
   - ONE digest row per boot (STARTUP READINESS: ollama=... engine=...),
     rc 0 healthy / 1 degraded; a failure reports, never crashes boot.
   - retirement._STARTUP_TMPL now boots startup_readiness (single entry
     point); real Startup .cmd re-registered. Ollama.lnk left in place
     (harmless: probe-first spawn only fires when it is down).
2. B27 auto-extend <=90 min (dispatcher: window_extension config row +
   reconcile_window_extensions; hooked into seconds_to_next_wake and
   run_engine_tick):
   - grant ONLY when the most recent window closed <=90 min ago AND
     zero sends committed inside it AND queued work waits (ROUTED or
     pending followups). Until = window_end + 90 min, hard cap.
   - in_window honors the grant, so claim, ladder (regions filter) and
     wake math all recover together; pacing gap, 4/hour slot, daily
     caps, reply-guard and template gates stay untouched.
   - expired grants self-clear; one WINDOW EXTENDED [region] digest row
     per grant; no queued work = no grant = no daily digest spam.

Evidence: gate RED then GREEN rc=0, 95.98% coverage. TDD: b26 18 tests
(startup readiness, spawn/confirm, dead-pid respawn, digest row, .cmd
wiring), b27 12 tests (grant/ceiling/not-eaten/no-work/expiry/wake).
Live-proved on the real stack: readiness found Ollama down AGAIN (it
had died since the manual restart), spawned it, confirmed answering
(1 process, llama3.1:8b listed), engine alive, one digest row
"STARTUP READINESS: ollama=restarted engine=alive".

Alternatives: (a) keep the .lnk-only boot — rejected, it failed
silently (live); (b) watchdog-check ollama too — rejected, the watchdog
runs every 5 min forever whereas boot needs one authoritative gate;
(c) always stretch windows +90 min — rejected, extension is earned
only by an eaten session; (d) detect outage via heartbeat file —
rejected, the send-ledger condition covers both outage shapes (machine
off AND apps dead while ticking).

Grade: OPTIMAL (owner directives directly implemented, every seam
test-backed, hard 90-min ceiling, reversible by restoring one .cmd
template line).

### 2026-10-31 B29 — channel strategy: IG-first, email fallback, order-line WA exception

Context: owner research (paste 2026-10-31): scraped public WhatsApp
numbers are ORDER LINES (cashiers/family), so cold sales DMs die unread
or get marked spam. First touch must be Instagram DM (or email);
WhatsApp only later-stage, on an owner-confirmed number. Locked
answers (ask_user 2026-10-31): (1) send mode = "same flow we have
right now, ignore that line ... we just stop sending on whatsapp and
focus more on instagram" — automation unchanged; (2) "IG first
everywhere" — active IG wins in every market, email is the fallback.

Changes (routing/qualification layer only; frozen templates byte-frozen):
- router.py: lane law = active IG (<=30d) -> email -> WA order-line
  exception (Canada lane 'na' + payload order_line evidence, with the
  owner's acknowledgment opener) -> park "no viable channel". The phone
  no longer wins any lane. effective_ig_inactive derives inactivity from
  ig_last_posted_date (top-level or social_activity.instagram shape);
  operator ig_inactive=1 wins (ADR-53); a MISSING date keeps pre-B29
  behaviour (fail-open) until the scraper exports dates.
- templates.py (fills only): _ORDERLINE_ACK = owner spec copy with the
  em dash swapped to a comma (standing no-em-dash law), prefixed onto WA
  first touches when order_line; optional {{owner}} fill (MessageHold
  when owner unknown — never "Hi guys", never a restaurant) and
  {{dish_phrase}} fill (review-sourced dishes carry "your reviews keep
  mentioning", menu-listed dishes stay plain) ready for owner pastes.
- dispatcher.claim_next: deprioritises (never excludes) leads whose
  owner is unknown AND whose evidence is weak (rating <4.3 or reviews
  <80; missing figures = weak). Stable sort, id order kept per group.
- ig_sender drain now passes region + order_line (drained IG leads park
  or take the exception — never a cold WA restage).
- command_center `plan [--lead ID]`: spec item 5 per-lead output —
  chosen channel + reason, the filled first-touch message, and the
  warm-handle note when ig_last_posted_date is >14 days old.

Evidence: GATE GREEN rc=0, 95.44% coverage. TDD: tests/test_b29
(25) RED->GREEN. Fallout rewritten to the new law: b8 x8 (phone-wins
suite reversed), intake x3 (Mandi Spot parks as gulf-phone-only, Zest
routes ig, watcher row now proves the order-line exception end-to-end),
b18 hostile-bytes decode guard in _weak_lead.

Alternatives: (a) keep phone-wins — rejected, this is the research's
whole point; (b) Gulf->email even with active IG — rejected, owner
chose "IG first everywhere"; (c) fail-closed on missing post dates —
rejected, it would park the entire live pool before the scraper
exports the field; (d) rewrite frozen templates for owner greetings /
provenance phrasing — rejected, templates are owner-pinned; renderer
supplies the fills, the owner pastes placeholders when ready.

Grade: OPTIMAL (both locked answers verbatim, frozen bytes untouched,
cold WA fail-closed, every seam test-backed).

Still open: scraper enrichment gaps (export last_post_date,
order_line detector, review-text dish miner, owner from review
responses) + Ottawa/Salalah enrichment re-run + v1.1 multi-account.

### 2026-10-31 B29.1 — cutover repair: engine-start reroute of stale route_channel

Problem (found while verifying B29 against the live DB): dispatch's
claim_next trusts leads.route_channel, and all 824 never-contacted
ROUTED leads were routed by the PRE-B29 phone-wins law — every one
carries 'wa'. The next open window would have fired cold WhatsApp
first-touches the owner outlawed.

Fix (routing layer, per the owner spec — dispatch semantics untouched):
- router.route_from_row(row, now): SSOT for the lane law over one leads
  row (payload parse + 30-day derivation + route_lead). apply_routes and
  command_center's `plan` now share it (Law 5: no triplicated wiring).
- router.reroute_stale_routes(db): re-evaluates every ROUTED,
  never-contacted lead once per engine process start (run_forever,
  before the first cycle): changed channel -> UPDATE route_channel
  (state stays ROUTED); no viable channel -> ROUTED→UNREACHABLE exactly
  like apply_routes (state_history carries the reason); a repair fault
  lands in the digest (ENGINE ERROR) and never stops the boot.
- Idempotent: a second run reports zeros.

Live impact on restart: 13 leads re-lane to IG, 24 to email, 787 park
(no order_line evidence yet — the enrichment gated in
SCRAPER_CHANGES.md); 0 unlawful cold-WA first-touches remain in the
uncontacted pool.

Evidence: GATE GREEN rc=0, 95.42% coverage, stub 0, secrets clean.
TDD: tests/test_b29 +3 (reroute repair, idempotence/contact/state
guards, run_forever boot repair) RED->GREEN; affected suites 199/199
(b8, b9, b11, b12, b17, b18, b22, b24_escalation, intake).

Alternatives: (a) claim-time recompute inside claim_next — rejected:
the owner spec says "implement this as a change to the routing/
qualification layer", and apply_routes owns route_channel; (b) one-shot
schema migration — rejected: scraper enrichment refreshes land later
(SCRAPER_CHANGES.md) and need the repair to re-run — boot-time is
self-healing on every watchdog respawn; (c) flip stale leads back to
QUALIFIED and re-run apply_routes — rejected: a wasted state round-trip
through the machine map for 824 leads.

Open: SCRAPER_CHANGES.md (scraper enrichment, owner-paused) +
Ottawa/Salalah enrichment re-run; v1.1 multi-account; LIVE BLOCKER —
WhatsApp Web session is login-walled since 09:12Z (owner must re-scan
the QR on their phone; ADR-8 forbids auto-login), which is why 10-31
shows zero sends despite an open window.

### 2026-11-01 D4 - email screening: platform-junk domains are not a channel

Packet B item 2 (orchestrator, 2026-11-01). B29 made email a primary lane,
and old rows carry platform guest inboxes (live audit: 3 restaurantguru.com,
3 linkedin.com, plus known platform domains as future-proofing) - a first
touch to those addresses reaches nobody at the restaurant. Regex audit: 0
junk addresses in the current uncontacted ROUTED pool, so this is hygiene +
future-proofing; no live lead moves because of it.

Changes (routing/qualification layer, frozen templates untouched):
- router.PLATFORM_EMAIL_DOMAINS + router.is_platform_email(): case-insensitive
  domain match incl. subdomains (mail.restaurantguru.com), never a local-part
  substring (x@restaurantguru.com.evil.ca stays clean).
- route_lead: a junk address now counts as NO email - the lane law falls
  back exactly as if the column were empty (active IG -> ig; Canada
  order-line exception -> wa; else park "no viable channel"). Parking keeps
  writing state_history (never a silent drop).
- claim_next (claim seam, defense in depth): an email-routed lead with a
  junk address is never claimed even if it sits route_channel='email';
  router.reroute_stale_routes at engine boot re-lanes/parks such stale rows
  through the same law (self-healing, idempotent).

Evidence: TDD RED->GREEN, tests/test_d4_email_screening.py (10 tests:
blocklist positives/negatives, fallback routing, apply_routes park with
reason, claim refusal, boot-repair). Affected suites 288/288; GATE GREEN
rc=0, 95.43% coverage, stub 0, secrets clean.

Alternatives: (a) validate at the email_sender transport - rejected: too
late (routing law would still promise a lane that cannot work, and the
ladder/plan would keep rendering it); (b) config-row blocklist - rejected:
a constant is auditable, diffable and zero-I/O at the seam, and no owner
workflow exists to edit it; (c) delete/blank the email column on sight -
rejected: destroys evidence and breaks the audit trail; the lead keeps its
data and falls back through the law.

Grade: OPTIMAL (smallest seam that cannot be bypassed, zero live-lead
impact, every path test-backed).

## 2026-11-02 - Owner directive: PAUSE the entire WhatsApp lane until further notice

Question: the owner stopped WhatsApp entirely ("Pause whatsapp entirely until
my further notice, no more whatsapp sending until I say so"). How is this
armed so NO wake can open WhatsApp, and what happens to routed WA leads and
the B13 reply probe?

Decision (config-switch pause, same convention as `ig_halted`):
- Config row `wa_paused = {"reason": "owner directive 2026-11-02: ..."}` in
  the live `outreach.db` + a WA PAUSED digest row.
- Three gates, all guaranteed-key checked (an absent/unset `wa_paused` keeps
  the lane live — never an unconditional default): (1) `claim_next` skips
  every WA-routed lead while set; (2) `process_due_followups` holds due WA
  rungs pending (retried when the owner re-arms); (3) `run_engine_tick`
  skips the WA in-thread reply probe — the probe opens WhatsApp Web, and
  under a total pause that is also WhatsApp usage (ASSUMED, recorded here:
  reply detection for WA is dormant while the owner is away).
- Reroute decision: NONE. Live DB at arm time: the only 2 ROUTED leads are
  already IG-lane zero WA-routed leads exist behind the gate, so nothing
  re-lanes and no state machine writes occur. Any FUTURE WA routing
  decision stays ROUTED and simply never claims (reversible: delete the
  row to re-arm the lane; nothing was drained, cancelled or destroyed).
- TDD: `test_owner_wa_pause_blocks_the_wa_lane_entirely_and_never_unconditional`
  (b9), `test_owner_wa_pause_holds_wa_rungs_pending` (b14),
  `test_owner_wa_pause_stops_the_reply_probe_too` (b15); all green with the
  b9/b14/b13/b15 focused suites.

Evidence: tests above; live config row verified by SELECT after write;
digest row written 2026-11-02 ~15:1xZ.

Alternatives: (a) drain WA leads to email like the IG halt does rejected:
nothing needs draining today (see above) and the owner said pause, not
repurpose; (b) pause the whole queue too rejected: the owner's ME/NA
Instagram + email lanes keep their own orders; (c) numeric-halt per account
rejected: WhatsApp here is one owner session, the channel IS the account.

Grade: OPTIMAL (smallest reversible switch enforcing exactly what was
ordered; zero state-machine writes; upgrade path = account table in v1.1
multi-account, which supersedes per-channel flags at L8).

## 2026-11-02 v11-batch1 - variant engine behind the render seam

Question: orchestrator D6 audit APPROVED B29/B29.1 and greenlit v1.1;
dispatch order = v11-batch1 VARIANT ENGINE per V11_BATCH1_OWNER_SCRIPTS.md
(byte-pinned 9-variant packet + routing + P.S. rotation + banned-phrase
lint + bounded V1 generation). It must sit BEHIND the existing render
seam so the current single-account lane uses it immediately.

Decision:
- `src/outreach/variants.py`: the 9 owner bodies byte-pinned from
  V11_BATCH1_OWNER_SCRIPTS.md; `render_lane_opener(db, channel, row,
  now=...)` is the one seam entry (dispatcher claim + both reply probes
  call it); `render_ig_lead`/`render_lead` compose on top.
- Routing priority per the packet: V1/V6 -> V8 -> V2 -> V7 -> V4 ->
  V3/V9 -> V5; V5 capped at 30% of composed sends.
- P.S. rotation: never the same P.S. twice in a row per segment
  (persisted pick state in config).
- Banned-phrase lint AFTER compose, BEFORE ship: a hit raises
  `LintBlocked` -> refuse + digest row, NEVER auto-repair (owner packet).
- Bounded V1 generation (Ollama): concrete-grounding sentence +
  confidence >= 0.8 + genericness self-verify; dead model = routing
  fallback to the byte-pinned bodies, never a queue pause (unlike ICP's
  ADR-4 fail-closed, which stays).
- Dispatcher claim projection gained `city`/`id` (the lane seam needs
  them); reply probes thread `db` into `_ours_copy` (see LIVE_LESSONS
  normalization entry).

Evidence: TDD RED->GREEN, tests/test_v11_b1_variants.py (22 tests:
byte-pin, greeting rule, P.S. rotation, lint refusal, routing priority,
V5 cap, bounded generation, claim-seam integration). Full suite EXIT=0,
94.88% coverage. Focus pins (toast/b15/b10/b13/b24) all green.

Alternatives: (a) replace the frozen v1.0 templates outright - rejected:
ADR-6 byte-exact law keeps the v1.0 bodies as the floor, and the seam
must serve both while v1.1 multi-account lands; (b) generate all
variants via Ollama at claim time - rejected: generation failures would
block sends and the owner packet pins 9 byte-exact bodies; (c) lint as a
post-send digest warning - rejected: the packet says refuse, never ship.

Grade: OPTIMAL (one seam, zero behavior change for lanes that render
v1.0, every gate test-backed).

## 2026-11-03 - LIVE_LESSONS batch: pacing commit instant + notification
storm + opener normalization

Three live anomalies closed under the standing law (each RED-pinned
BEFORE its fix was declared done; full LIVE_LESSONS.md entries carry the
test names):

1. Pacing floor breach (email 113s apart < 180s): `commit_send` armed
   `next_send_after` from the tick's captured `now`, stale by the whole
   send session. Fix: `commit_instant(now, started)` + a `_session_clock`
   stamp at session entry in wa/ig/email senders - the gap arms from the
   TRUE commit instant. Pin: `test_pacing_gap_counts_from_true_commit_instant`
   (b10, RED-first with a 97s fake session).
2. Notification storm (owner desktop flooded): dead DNS -> every tick's
   probe failed -> UNTHROTTLED `REPLY PROBE ERROR` row, and the toast
   drain popped one balloon PER ROW in one burst. Fix: `_record_probe_error`
   30-min per-reason throttle on both probe legs + one balloon per drain
   pass (oldest-new first, count summarized). Pins:
   `test_repeated_probe_failures_throttle_to_one_digest_row_per_lane`
   (b15), `test_drain_pops_one_balloon_per_pass_even_in_a_storm` (toast).
3. False REPLIED from our own copy: `_ours_copy` joined a RAW opener with
   `_norm`-ed rungs while probes compare `reading in ours` on a normalized
   reading - our newest opener could read foreign and flip REPLIED +
   ladder cancel + owner handover. Fix: normalize every part; thread `db`
   through the helper for the variant lane seam. Pins:
   `test_our_own_copy_is_never_a_reply` (b24, mutation-proved 2026-11-03:
   removing `_norm(opener)` -> FAILS with `replied == (1,)`) +
   `test_our_followup_line_with_a_foreign_header_is_never_a_reply` (b13).

Evidence: full suite EXIT=0, 94.88% coverage (671+ tests); focused pin
re-run green after mutation restore; running engine lease pid 11784
(started 2026-11-03 05:28 PKT) booted AFTER all three fixes - live state
carries them.

Alternatives: (a) fix the storm by turning toasts off - rejected: Q11
opted the owner IN; the flood was a missing throttle, not the feature;
(b) pause the queue while DNS is dead - rejected: probes fail-closed per
tick and sends were windowed anyway, the anomaly was noise not danger;
(c) normalize readings DOWN to raw copy instead - rejected: `_norm` is
the shared comparison law across both probes and the guard.

Grade: CONSTRAINED->OPTIMAL (each fix is the smallest seam that kills
the incident class; DNS itself is host-side, owner-owned DT-16 territory).

## Batch P1 (2026-11-03) — delivery audit: four defects pinned, one band left to the owner

1. Ladder pacing armed from the tick stamp (`process_due_followups`) while the
   dispatch path had already been fixed on 11-02. Evidence: rows 32→33 58.7s
   apart (live), email rungs 101.3/99.9/112.1s. Pin:
   `test_ladder_gap_counts_from_the_session_commit_instant` (b14) RED at
   58.7s before the fix. Fix: arm from `commit_instant(now, started)`.
2. Ladder rows wrote `screenshot=None` while the PNGs existed on disk (13 rows,
   ADR-15 chain break). Pin: `test_ladder_row_links_the_senders_screenshot`
   (b14). Fix + historical backfill (13/13, 0 NULL browser rows left).
3. Watchdog's `_pid_alive` trusted `OpenProcess` alone: a terminated process
   whose handle a parent still holds, and a reused pid from a stale lease, both
   read as "hung engine" → a GONE engine is never respawned (silent death).
   Found by the snapshot kill-test, not by unit tests. Pins:
   `test_pid_alive_says_no_to_a_dead_process_whose_handle_is_open`,
   `test_pid_alive_rejects_a_reused_pid_when_the_lease_knows_the_start_time`
   (b22). Fix: `GetExitCodeProcess` + creation-time match vs the lease.
4. One-time window band integrity. The owner's 10-30 one-day NA catch-up
   (13:00–23:00) was torn down by an external one-shot task that never ran
   (no digest row, no schtasks entry), so the band leaked four days: 22 na
   emails (11-01 13:04–16:14, 11-02 13:11–13:13) landed inside the expired
   band and outside the ordered 21:00–23:00 standing band. Pin:
   `test_one_time_band_closes_at_its_until_even_when_teardown_never_runs`
   (b27). Fix: `session_windows.<region>.until` honored by `in_window` —
   expiry is enforced in the engine, fail-closed, independent of any task;
   the flip/restore one-shots clear stale `until` rows.

Live window band left UNCHANGED as a deliberate owner-confirm decision: the
live na band has been 13:00–23:00 since 10-30, and the orchestrator's own
11-02 watch brief ("NA window opens 13:03 PKT") treats that as the current
open. Changing it mid-day would strand queued work on an ambiguous order;
the engine no longer depends on a task to expire a band, so nothing leaks
while the owner confirms (QUESTIONS.md 2026-11-03).

Alternatives for (4): (a) restore 21:00–23:00 now — rejected: no fresh owner
order, the orchestrator brief reads 13:03 as the NA open, and a live band flip
mid-session is a behaviour change the owner did not ask for; (b) re-register
`OutreachRestore` — rejected: it repairs one task, not the class; (c) keep a
task-free `until` on an expiring band — chosen.

Grade: (1)(2)(3) OPTIMAL (smallest seam, class-killing, mutation/RED-proved);
(4) OPTIMAL mechanism + CONSTRAINED live state (debt: owner must confirm the
na hours; upgrade trigger = owner answer, then one config write).

Evidence: 84/84 rows hash-verified against `git show` of the template in force;
0 out-of-window rows under the running config; 0 cap breaches; 0 `daily_sends`
mismatches; 0 post-REPLIED sends; 4 replies → 4 cancels → 0 pending rungs;
kill-test: 1 spawn exactly, live DB sha256 unchanged.

## Batch C (2026-11-03) — scraper contamination hold: quarantine, don't guess

Question: the scraper's enrichment is semantically contaminated; what does the
outreach side do about data it already holds and data it may import?

Decision: (1) owner-name fills OFF by default (`owner_fill_enabled=0`) and
evidence-gated (`owner_verified`) when re-armed — the copy keeps the frozen
`Salam!` opener rather than a guess (ADR-6: never rewrite, never invent).
(2) The email lane is quarantined to own-website-domain addresses or an
explicit verification flag; free mailboxes and unrelated domains are not a
channel and park with a reason through the existing D4 lane law. (3) Junk
social links sanitise to "no handle" at the routing seam. (4) `is_chain`
verdicts that contradict tier evidence are restored and tagged
`needs_review=chain_verdict_contradiction` (tier wins for now, human review
owed); a scraped `is_chain=True` flag still outranks the verdict. (5) A
`REJECTED → QUALIFIED` edge is added to the machine, reserved for that repair
only — no dispatch path can re-qualify a rejected lead.

Evidence: `CONTAMINATION_SWEEP.md` — 0 owner fills and 0 junk IG sends ever
left; 22/26 email sends used free/unrelated addresses (logged, cannot be
unsent); pool imported 10-26/27, so the contaminated refresh never entered our
DB; 3 tier-valid chain rejects restored live ([414,859,930]) and all three
park under the lane law.

Alternatives: (a) keep email flowing and "watch" — rejected: cold mail to a
stranger's address is unrecallable damage; (b) blanket-disable email — rejected:
it is the Gulf's primary lane and own-domain addresses are provably good (90 in
pool); (c) trust owner names because 49 % are plausible — rejected: a
fabricated name in a DM is the single worst failure available to this product.

Grade: OPTIMAL for the seams (each is fail-closed and one-line-tight);
CONSTRAINED on supply (email lane shrinks to own-domain addresses until the
scraper ships confirmation). Upgrade trigger: scraper's ≥90 % dry-run precision
gates + orchestrator go → re-run the handshake; owner answers the NA band
question separately.

## Batch C addendum — handshake is orchestrator-gated

No re-ingest, no enrichment handshake re-run on scraper fixes without an
explicit orchestrator go, even if their gates pass. The scraper side owes
evidence-gated owners, domain-confirmed emails, sanitised links and corrected
chain flags; our side re-ingests only after acceptance (QUESTIONS.md).

## Batch C live activation — restart the engine to load the quarantine (2026-11-04)

Question: the quarantine was committed at 12:35 PKT but the running engine
(pid 18000, booted 06:20) had loaded the old router — do we leave the old law
live until the next natural restart?

Decision: restart into the committed code immediately, at the one moment the
engine cannot be mid-send. Conditions checked first: engine asleep (heartbeat
next wake 13:01 PKT), every session window closed until 13:00, last send
2026-11-03 09:31Z. Killed pid 18000, spawned exactly one detached engine via
`watchdog.spawn_engine`, verified one `pythonw` process, lease pid 3416,
`owner_fill_enabled` seeded 0, and 106 of 150 unworked email holders now
refused the lane.

Alternatives: (a) wait for the watchdog's natural restart — rejected: the
promise ran to 13:01, i.e. the first window would have opened under the old
law and could have mailed a quarantined address; (b) pause the whole queue —
rejected: IG is lawful and the owner wants it running, and pause would not
load the fix anyway.

Grade: OPTIMAL, fully reversible (source unchanged; a second restart returns
the previous behaviour). Evidence: lease/pid/heartbeat reads and the
lane-verdict count above, taken read-only.

## 2026-11-04 — Adapter prep sits behind a flag-free typed reader

Question: the scraper now emits four new labels (`method="estimate_*"`,
`needs_chain_review` / `classification="needs_review"`,
`coordinates_unavailable`, evidence-backed `owner_name`/`email`). Where does the
read live, and does anything act on it before the bulk pass is accepted?

Decision: a pure module `src/outreach/scraper_labels.py` reads them into a
frozen `ScraperSignals`, and `scraper_adapter.LeadRow.signals` carries it. No
consumer: `importer`, `icp`, `dispatcher` and the senders are untouched, so the
live pool cannot change shape from this batch. Email evidence is NOT re-derived
— `read_signals` asks `router.email_lane_ok`, the same rule the sender gate
uses, so re-ingestion cannot double-guess an address and reach a second
verdict. Estimated post dates collapse to "unknown" (`trusted_post_date`), which
is the scraper's own §5 wording ("absence of a post date remains unknown, not
inactive").

Alternatives: (a) fold the labels straight into `refresh_leads` now — rejected:
the packet forbids live behaviour change before `BULK_PASS_REPORT`, and a
refresh would re-shape 1,000+ rows on unaccepted data; (b) keep a private copy
of the own-domain test inside the adapter — rejected: two rules drift, and the
contamination sweep showed the cost of exactly that.

Grade: OPTIMAL for the prep, fully reversible (additive fields; deleting the
module restores the previous adapter). Evidence: `python gate.py check` GREEN,
94.84% coverage, stub scan 0, 24 pins in `tests/test_scraper_labels.py`; live
hold re-read 12:53 PKT — 84 `message_log` rows, last send 2026-11-03T09:31:58Z,
`wa_paused` set, 44 lane-ok vs 106 quarantined email holders.

## 2026-11-04 - Junk identity is one rule, asked at intake, judgement and claim

Question: the 5-lead Denver supply test returned a Tier-1 row named "Unknown
Name" with no address, phone, website or handle. `_rule_reject_reason` returned
None for it, so it could have been judged, routed and messaged, and it counted
toward the run's five-lead target.

Decision: `cleaning.junk_identity_reason` answers "is this a usable identity"
once, and the three boundaries that matter ask it: `importer.refresh_leads`
skips the row and counts it in `ImportReport.skipped_junk`, `icp` rejects it as
`junk_data` before any model call, and `dispatcher.claim_next` never hands it
out (a row that predates both still cannot send). Only whole-token placeholders
are junk - "Unknown Brewing Co" and "NA Grill" are real businesses and stay.

Alternatives: (a) filter only at the scraper - rejected: the scraper repo is
read-only and the pool must be safe regardless of what it emits; (b) count the
row but refuse it at send - rejected: it still poisons a run target and wastes
a judgement; (c) reject any lead without an address - rejected: a lead with a
handle and no address is still reachable and legitimate.

Grade: OPTIMAL, reversible in three small hunks. Evidence: 21 pins in
`tests/test_junk_guard_20261104.py` went RED as an ImportError first, then
GREEN; the touched suites (`test_b3_importer`, `test_b5_icp`, `test_b11_ig`,
`test_b13_replies`, `test_v11_b4_fleet`, `test_intake_driver`) pass; gate GREEN,
94.65%.

## 2026-11-04 - A city map learns new seed cities, and Denver is North America

Question: the Denver test made the live-refresh contract go RED with
`unknown_regions=('denver',)`. A city added to `_LANE_SEED` never reached a DB
whose `city_lane_map` had already been seeded, so its leads imported with
region NULL and could never pass a window gate.

Decision: `ensure_lane_map` merges any seed city the stored map lacks, and only
that: an entry already present is never overwritten (an owner edit survives).
Denver joins the `na` lane. Applied to the live DB, which grew 17 -> 18 entries
with exactly `denver: na` added and nothing removed or changed.

Alternatives: (a) reseed the whole map - rejected: it would clobber any owner
edit and there is no evidence there are none; (b) relax the contract test -
rejected: the contract is exactly what caught this.

Grade: OPTIMAL, additive and reversible. Evidence: two pins in
`tests/test_b3_importer.py` (RED first: ImportError-free but lane missing, then
GREEN); `test_live_refresh_contract` passes against the real scraper DB with
`unknown_regions=()`; live before/after diff printed 17 -> 18, added
`{'denver': 'na'}`, changed `{}`.

## 2026-11-04 - D4 ruling: the 357 NEW leads were never contacted, so the flag was cleared

Question: every NEW lead carried `previously_contacted=1`, so `qualify_new_leads`
scoped to `previously_contacted=0` judged nothing and the pool looked empty.
Was the flag true (contacted elsewhere, leave it) or an old-importer artefact?

Evidence, taken read-only (`checkpoints/D4_PREVIOUSLY_CONTACTED_2026-11-04.txt`):
all 357 had 0 `message_log` rows, 0 SENT/FOLLOWUP/REPLIED history and 0
`lead_accounts` assignment; the scraper's own row for each said
`outreach_status=NULL`, `outreach_stage='not_contacted'`, `dm_opened=0`,
`last_contact_ts=0`, `is_unsubscribed=0`. The only contact-shaped signal was
`exported_at == exported_wa_at == exported_ig_at` on all 357 rows, in exactly
two bulk stamps (1790041797, 1790208128) - one list export, not three channel
sends, and it is what the old importer read as "contacted".

Decision: clear the flag on those 357 only, under the same guard in the same
transaction (`state='NEW'` AND no message row AND no sent history AND no
assignment). Cleared 357, still flagged 0. Then run the judge and the router:
357 judged, 239 qualified, 118 rejected (971 s), routed 198 (197 ig, 1 email),
44 unreachable.

Alternatives: (a) leave the flag and wait for a re-ingest - rejected: the
orchestrator ruled the flag wrong and 312 of these leads hold a usable handle;
(b) clear every flagged row - rejected: a lead with a real send history must
keep it, hence the guard; (c) route straight to IG without judging - rejected:
the judge is the only quality gate before a first touch.

Grade: OPTIMAL, reversible (`previously_contacted` is one column) and every
input to it is in the evidence file. The 1 email-routed lead parks: `claim_next`
refuses the email lane while `email_enabled=0`.

## 2026-11-04 - D7: the engine's death now leaves a trace

Question: the engine died twice today (leased pids 1856 and 3416, digest rows
`WATCHDOG: engine GONE ... restarted`) and left nothing behind: a detached
pythonw process inherits no console, and `subprocess.Popen` was called with no
stdout/stderr. Two deaths, zero diagnosable cause.

Decision: the single spawn point (`watchdog.spawn_engine`, which is also what
the Task Scheduler task runs) now opens `logs/engine.log` (append, rotated at
5 MB to `engine.log.1`) and passes it as stdout with stderr merged in.
`engine_loop.main` prints a boot line with pid and timestamp and prints a
traceback before re-raising if the process dies outside the per-cycle guard
(inside it, the existing digest row already fires). `logs/` is git-ignored.

Alternatives: (a) redirect in the Task Scheduler `/TR` string - rejected:
`/TR` runs the WATCHDOG, which spawns the engine, so the redirect would miss
the child; (b) a logging framework - rejected: ADR-1, stdlib, and the need is
one file; (c) log only exceptions - rejected: a boot line turns "process gone"
into "process gone after N cycles".

Grade: OPTIMAL, additive. Evidence: 4 pins in `tests/test_b22_watchdog.py`
(three RED as AttributeError before `open_engine_log`/`ENGINE_LOG_MAX_BYTES`
existed, then GREEN; the fourth pins the boot line and the printed traceback).
Live: the next engine restart writes the file; the process running now was
spawned before this change, so it still has no redirected streams.

## 2026-11-04 - D8: message_log keeps the text, not only the hash

Question: the correctness audit could prove which template file shipped (hash
reproduced byte-exactly) but not which sentence a recipient read, because
`message_log` had no body column. A hash of the template plus a screenshot is
weaker evidence than the text.

Decision: migration 9 adds a nullable `message_log.body`; `commit_send` and the
follow-up ladder write `message.text`. Old rows stay NULL - nothing backfills
text that was never captured. Applied to the live DB: schema version 9, 107
existing rows, 0 with body, as designed.

Alternatives: (a) store a fill-hash instead - rejected: it proves equality only
against a re-render, which is the same weakness as the template hash; (b)
backfill from screenshots - impossible without OCR and would invent text.

Grade: OPTIMAL, additive and reversible (extra nullable column). Evidence:
`tests/test_message_audit_20261104.py` (2 pins: the first-touch body equals the
rendered text with no placeholder; the column is nullable and leaves old rows
alone) plus `test_ladder_row_records_the_exact_text_it_sent` in
`tests/test_b14_followups.py`; all RED first (`assert 'body' in columns` failed).

## 2026-11-04 - v1.1 batch 5: the engagement core is pacing plus a ledger, nothing clever

Question: batch 5 is "follow/like, pacing, logging" with gate 15 as its bar.
Where do the laws live - inside the browser click, or above it?

Decision: above it. `engagement.engage` decides L1 (who owns the lead), L6
(the 90 s gap and the 30/day cap) and the optional account window from the
DB, and only then calls `perform_act` to click one control. A refusal costs
no browser work and can never leave a half-logged act. `can_engage` is the
single gate, so the warming queue (batch 8) parks on the same answer the
engine uses. Migration 10 adds `engagement_log` (account, lead, act, target,
confidence, screenshot, acted_at), the SEND-day law (PKT) is reused so an
"act day" and a "send day" mean the same thing, and `ig_handle` is reused
from the sender instead of a second URL parser.

A failure that matters: `like` needs the POST, not the handle. The first
version of the gap-boundary test asked a like of a handle and the transport
correctly refused (`like needs the post URL`). I fixed the TEST, not the
transport: inventing a post URL would have meant guessing which post to warm
up on, which is batch 7's promising-lead filter. The boundary test now paces
two follows across two leads, which is the real shape of the burst law.

Alternatives: (a) enforce pacing inside the click - rejected: a refusal would
then happen after a browser boot, and the ledger could not be trusted;
(b) count acts in UTC days - rejected: every other daily ledger is PKT;
(c) gate likes/follows behind a confidence score - rejected by L5 (comments
only); (d) create an L1 assignment on engagement - rejected: the first DM
claim owns assignment, and engaging a lead must not consume it.

Grade: OPTIMAL, additive (one table, no existing row touched, no engine
wiring - batches 7/8 own that). Evidence: `tests/test_v11_b5_engagement.py`,
11 pins, RED first (`ModuleNotFoundError: outreach.engagement`, then a real
`ValueError` on the like-of-a-handle test), including gate 15
(`test_gate15_a_second_act_inside_ninety_seconds_is_refused`), the L1 refusal
with a second registered account, fail-closed when the control is absent
(no row, no click) and the 31st act of the day refused.
UNVERIFIED: the live Instagram selectors. No account that may engage is
signed in yet (ig2-4 are `MISSING`), so the two aria-label spellings are
unit-tested against a fake page only; the first real follow/like is the
evidence, and batch 9's health signal is what will catch a selector that
drifts.

## 2026-11-04 - v1.1 batch 6: a comment is only as good as the post it answers

Question: L5 allows one place where the system writes words a human reads -
an Instagram comment - and requires a confidence score above a threshold.
What stops a bad comment from reaching a public post?

Decision: three gates, cheapest first. (1) No caption and no alt text means
no draft at all (`CommentRefused`): a comment with nothing to refer to could
only be generic, and generic is the failure L5 names. (2) The draft's own
confidence must clear `comment_confidence_min` (config, default 0.8, not a
constant in the click path). (3) A copy veto runs on the text regardless of
confidence: empty, em-dash, unfilled placeholder, link, over 300 chars. Any
of the three sends the draft to `comment_review` with the reason recorded
and touches no browser. Only a draft that passes all three is typed and
submitted, and it is logged with its post URL, caption, alt text, the model's
`why`, its confidence and a screenshot.

Alternatives: (a) post the draft and delete it if wrong - rejected: a public
comment is seen before any cleanup runs; (b) trust the model's confidence
alone - rejected: the live 0.99 draft in the test contained an em-dash, so a
confident model still violates the owner's copy law; (c) keep the veto list
in the prompt - rejected: prompts drift, code does not; (d) auto-post below
threshold after a delay - rejected: L5 says skip and queue, and the owner has
not approved a threshold change.

Grade: OPTIMAL, additive (`comment_review` table, no existing behaviour
touched; batch 7 wires the pre-DM step). Evidence: `tests/test_v11_b6_comments.py`,
11 pins, RED first (`ModuleNotFoundError: outreach.comments`), covering gate
13 (0.5 confidence queued, nothing filled, one pending row), a config-driven
threshold, no-context refusal, em-dash/placeholder/link vetoes, malformed
LLM output posting nothing, out-of-range confidence refused, and the missing
comment box failing closed with no fill and no press.
UNVERIFIED: the live comment box selector and the real model's confidence
distribution. No engaging account is signed in and Ollama has not yet been
asked for a comment on a real post, so the threshold default stays at the
blueprint's 0.8 pending the owner's look at the first drafted batch.

## 2026-11-05 - v1.1 batch 7: warm-up is a state machine, and it is off until the owner says so

Question: batch 7 wants follow + like before the first DM for promising
leads. The 90 s pacing law means no single tick can do three acts, and the
live send path is mid-run - how does this land without breaking either?

Decision: `warmup.warm_up` is resumable. Each call performs the ONE next owed
act (follow, then a like on each of up to two discovered posts) through
`engagement.engage`, so pacing, the daily cap and L1 all still apply, and it
returns [] when the account is inside its gap, when the lead is not
promising (`icp_score >= warmup_config.icp_min`), or when the warm-up is
complete. `warmups.dm_after` = last act + 30 min, and `claim_next` skips an IG
lead while that window is open. The claim-side check is behind
`warmup_config.enabled`, which seeds False: with the owner's current DB the
send path is byte-identical to before this batch, and turning warm-up on is
one config write after the owner has seen the dry-run.

Alternatives: (a) loop all three acts in one call - rejected: it would break
the 90 s law or sleep the tick; (b) gate the DM on "was warmed" instead of
"when" - rejected: a follow 5 s before the DM is not a warm-up, the 30 min is
the point; (c) default the flag on - rejected: it changes live first-touch
timing without the owner's go/no-go; (d) let warm-up create the L1
assignment - rejected: the first DM claim owns assignment, and warming a lead
belongs to the same account that will message it only because `pick_account`
chooses so at claim time (an assignment here would lock a lead to an account
that may be at cap when the DM is due).

Grade: OPTIMAL, additive (one table, one config key, one guarded branch).
Evidence: `tests/test_v11_b7_warmup.py`, 10 pins, RED first
(`ModuleNotFoundError: outreach.warmup`), including the off-by-default
byte-identical claim, the ICP filter, one-act-per-tick across three ticks
with `dm_after` proven, pacing returning [] instead of bursting, a profile
without controls never being marked warm, and the claim parked then claimed
across the window. Plus `warmup_dryrun.py`: PASS on all five checks against a
temp DB, report `checkpoints/WARMUP_DRYRUN_2026-11-05.md`.
UNVERIFIED: live post discovery and the follow/like controls. No engaging
account is signed in, so the dry-run's page is fake and its screenshot paths
are markers in a temp directory (the report says so); the selector set is the
same unverified surface as batch 5.

## 2026-11-05 - v1.1 batch 8: warming acts share one budget with lead warm-up, and the queue drains

Question: an account with no lead queued still needs to look alive. What does
it act on, how is that paced, and where does it stop?

Decision: `warming.warm_account` performs the ONE next owed act for the
account from an owner-supplied niche handle list
(`warming_config.handles`, normalised, deduped, capped at 20): follow the
handle, then like up to `likes_per_handle` (default 2) of its newest posts,
then move to the next handle. Progress lives in `warming_progress`
(account_id, handle) so a follow is never repeated and a finished handle is
never re-liked. Acts go through `engagement.perform_act` and are written to
`engagement_log` with `lead_id = NULL` and `detail = 'warming:<handle>'`, so
L6's 30/day and 90 s gap are spent from the SAME allowance as lead warm-up -
one account cannot double its activity by running two queues. The switch
`warming_config.enabled` seeds False and `warm_account` returns [] before any
page work while it is off. When every handle is complete, `has_warming_work`
reports False and the queue stops instead of looping on old posts.

Alternatives: (a) discover targets from the explore/hashtag feed - rejected:
that selector is unstable, unbounded and unreviewable, while a 20-handle list
is mockable and owner-auditable; (b) log warming acts in their own table -
rejected: two ledgers means two daily budgets, which is exactly the L6
loophole; (c) route warming through `engagement.engage` - rejected: `engage`
resolves a LEAD's handle and enforces L1, and a warming act has no lead;
(d) re-warm a handle once its posts are exhausted - rejected: a second follow
and a second like of the same post are no-ops that would only burn pacing;
(e) default the switch on - rejected: liking strangers is a live behaviour
change and the owner's go/no-go owns it.

Grade: OPTIMAL, additive (one table, one config key, one module).
Evidence: `tests/test_v11_b8_warming.py`, 13 pins, RED first
(`ModuleNotFoundError: outreach.warming`), covering off-by-default with zero
page work, list normalisation/dedupe/cap, follow-then-likes order, the shared
90 s gap, a one-post handle stopping at one like, a drifted surface logging
nothing, a halted account never acting, the shared daily budget, two accounts
pacing independently, and the drained queue reporting no work.
`warming_dryrun.py`: PASS 6/6 against a temp DB, report
`checkpoints/WARMING_DRYRUN_2026-11-05.md`.
UNVERIFIED: the live follow/like controls and real profile-grid discovery
(same unverified surface as batch 5; only ig1 is signed in). Not yet wired
into `run_engine_tick`: `has_warming_work` exists for that wake math, and the
engine keeps its current behaviour until the fleet batch wires and the owner
arms the switch.

## 2026-11-05 - v1.1 batch 9: a halt moves only the leads the account never touched

Question: gate 14 wants a halted account's leads redistributed with zero
double-touch, but L1 says one lead, one account, ever. Those pull against
each other - what actually moves?

Decision: `health.redistribute` releases ONLY leads that account never touched:
still `ROUTED`, with no `message_log` row, no `engagement_log` act and no
`warmups` row for that lead. For those the `lead_accounts` row is deleted and
`leads.account_id` returns to '' (the existing "unassigned" convention, since
the column is NOT NULL), so any eligible account may claim the lead normally.
A lead the halted account already touched keeps its assignment and parks: it
is that account's lead forever, and its follow-ups wait for that account to be
un-halted rather than being handed to a second sender. `halt_account` is
idempotent (one `account_health` row, one digest alert, one redistribution),
blocking signals go through that single door, and soft signals
(timeout/navigation_error/unknown) are recorded without changing status - one
slow page is not a ban. `fleet_health` exposes per-account cap, sends and acts
for the batch 11 dashboard, and `lane_switches` exposes the owner's global
switches so a view can never hide them.

Alternatives: (a) move every lead of the halted account - rejected: it would
message a restaurant twice from two accounts, the exact L1 violation gate 14
is designed to catch; (b) leave assignment rows dangling so another account
sees NoAccountAvailable - rejected: that strands untouched supply behind a
dead account; (c) halt on any signal - rejected: a timeout would park a
working account and the fleet would shrink for no reason; (d) mark a soft
signal HEALTH_ALARMED - rejected: HEALTH_ALARMED is not in ACTIVE_STATUSES, so
the account would stop dispatching on a recoverable blip.

Grade: OPTIMAL, additive (one table, one module, no change to the send path).
Evidence: `tests/test_v11_b9_health.py`, 12 pins, RED first
(`ModuleNotFoundError: outreach.health`), covering untouched-only
redistribution, the three touch kinds (message, engagement act, warm-up)
keeping their account, other accounts untouched, gate 14 end to end
(redistribute -> claim by the survivor -> exactly one message row attributed to
it), idempotent halt, exactly one owner alert naming the account and the
reason, soft signal recorded without halting, blocking signal halting through
the same door, a halted account never claiming again, and fleet_health
reporting every account.
UNVERIFIED / not wired: no sender calls `report_signal` yet, so a live
login wall still only produces the sender's own failure digest. Wiring it into
`ig_sender`/`wa_sender` changes the live send path, so it waits for an owner
window; the halt/redistribute core is complete and testable meanwhile.

## 2026-11-05 - v1.1 batch 10: pace is measured on the channel's real working day, once a day

Question: L7 wants "50% of the window elapsed, <50% of target claimed" to
raise an owner alert. The fleet spans two regions with different bands and a
channel has no region of its own - what clock does the checkpoint use?

Decision: `targets.working_day` builds the channel's working day as the union
of the region windows that still hold ROUTED work for that channel, and only
while `now` is inside one of them. With the owner bands that is 13:00-23:00
PKT when only NA work remains; outside every such window nothing can send, so
nothing is "behind". `pace()` reports target/sent/elapsed/below-pace against
that span, and `check_targets` raises at most ONE digest per channel per PKT
day (`target_alerted` holds the date), so a slow morning is one toast, not a
stream. On the IG fire it also calls `health.redistribute` for HALTED and
HEALTH_ALARMED accounts, which is idempotent and keeps L1 (only untouched
leads move). `daily_targets` seeds all-zero, so an unset goal alarms nobody,
and the tick calls `check_targets` every pass - a real no-op on the owner's
current DB until he sets a number. A target never touches `cap_for`; L2 still
governs what is physically sendable.

Alternatives: (a) one target clock per region - rejected: a channel's sends
are not region-attributed in `daily_sends`, so a per-region target would
report numbers nobody can verify; (b) fire every tick while behind -
rejected: toast spam; (c) alarm after the window closes - rejected: too late to
act on, the point is the halfway checkpoint; (d) rebuild the supply alarm here
- rejected: `intake.scan_low_leads` already alerts per starving episode, and
two supply alarms would double-toast; (e) raise caps to hit the target -
rejected by L2 and by the owner's standing law.

Grade: OPTIMAL, additive (one module, two config keys, one call in the tick).
Evidence: `tests/test_v11_b10_targets.py`, 13 pins, RED first
(`ModuleNotFoundError: outreach.targets`), covering the zero seed staying
silent, a 99 target leaving the cap at 15, the halfway fire, silence at 40%,
an on-pace channel, one alert per day, a fresh day alerting again, silence
outside every window, silence with no pending work, the stalled-lead requeue,
the tick firing the alarm itself (wired, not just available), the existing
supply alarm's episode dedupe, and the pace report's window/elapsed numbers.
UNVERIFIED: how it reads on a real under-target day - the owner's DB has no
`daily_targets` yet, so the alert has only ever fired against a temp DB.

## 2026-11-05 - v1.1 batch 11: the fleet view is read-only, and it shows the switches

Question: the dashboard has to show per-account health and target progress.
Should it compute the supply alarm by calling the same function that raises it?

Decision: no. `dashboard.build_fleet` is strictly read-only: it renders
`health.fleet_health` (status, cap, sends, acts), the per-channel target bar
from `targets.sent_today` (the identical ledger the cap and the pace alarm
read, so the three can never disagree), the owner's switches via
`lane_switches`, and a supply line computed from `intake.dispatch_ready_by_region`
rather than `scan_low_leads`, because the latter WRITES a digest. A view that
alerts when opened is a trap: a curious look at 02:00 would fire a toast. The
switch line is not optional paint - queue state, WA pause, email arming and the
IG lane halt are printed on every render, so a silenced lane can never look
like a quiet healthy one.

Alternatives: (a) call `scan_low_leads` and let the view alert - rejected:
read-only means read-only; (b) sum `message_log` instead of `daily_sends` -
rejected: caps, pace and the bar must agree on one number, and `daily_sends`
is that number; (c) a separate web/TUI surface - rejected: the blueprint says
the command center gains the view, and one interface is the standing law.

Grade: OPTIMAL, additive (one module + one subcommand, zero writes).
Evidence: `tests/test_v11_b11_dashboard.py`, 8 pins, RED first
(`ModuleNotFoundError: outreach.dashboard`), covering per-account lines with
sends/cap, engagement act counts, the target bar (4/10 with a visible bar), the
honest "no target set", all switches visible, the supply line emitting no
digest, the last health signal per account, and `command_center main(["fleet"])`
actually printing the view. Live output against the owner's DB:
`ig1 ACTIVE 10/10 sends`, `ig2..ig4 HALTED 0/0`, `wa1 ACTIVE 0/15`,
`queue LIVE | WA paused | email paused | IG lane live`,
`LOW LEADS: me=0 (threshold 10)`.
UNVERIFIED: nothing here sends or writes, so there is no live risk to verify;
the view was read against the live DB once (19:43 PKT).

## 2026-11-05 - v1.1 batch 12: onboarding refuses everything it cannot verify

Question: `add_account` is the one command that decides who sends. What should
it check before the registry row exists?

Decision: five refusals, in this order, all before any INSERT: (1) the caller
must pass `--confirm`, because registering a sender is an owner decision;
(2) the id must look like `ig2`/`wa2` AND match the channel; (3) the profile
name must be inside the automation lane (ADR-13), so an owner profile can never
be registered; (4) the profile must already exist on disk (`require_profile`) -
Chrome happily creates a fresh profile on launch, and that is how a fleet ends
up "healthy" while every account stares at a login wall; (5) neither the id nor
the profile may be in use by another account, and the window offset must be
0..599. Only then does the row land, WARMING, week 1 (cap 5), with the ramp
clock stamped today in PKT. A registered-but-unsigned account is legitimate -
it is the state the owner signs in from - so a missing Instagram session prints
a loud warning and exits 0 instead of blocking.

Alternatives: (a) let onboarding create the profile - rejected: an empty
profile is a bot signature and the account would silently be unusable;
(b) accept any profile name and fix it later - rejected: ADR-13 is a hard
boundary, and the check costs nothing; (c) block unsigned accounts - rejected:
the order has to be register-then-sign-in, because the login window reads the
account row; (d) default the offset to a random stagger - rejected: a silent
random offset makes the fleet's timing unpredictable and untestable.

Grade: OPTIMAL, additive (one module + one subcommand, refuses before writes).
Evidence: `tests/test_v11_b12_onboarding.py`, 11 pins, RED first
(`ModuleNotFoundError: outreach.onboarding`), covering the week-1 WARMING row,
the stored offset, the owner-profile refusal, the missing-profile refusal, the
duplicate id, the profile-already-used refusal, the channel/id contradiction,
the nonsense offset, the unsigned warning, the CLI's refusal without
`--confirm`, and the CLI onboarding account 2 end to end (row + fleet view).
UNVERIFIED: the real second profile's login (ig2/ig3 are still unsigned in the
lane; that is the owner's sign-in task, not an onboarding defect).

## 2026-11-05 - v1.1 batch 13: the fleet needs the channel ceiling to stop being one account's ramp

Question: the full fleet dry-run is supposed to show two accounts dividing a
day. It showed ONE account sending and the second permanently starved. Why?

Root cause (found by the dry-run, not by reading): `caps.current_cap` computes
the channel ceiling from the single `ig_warmup_start` ramp, and `pick_account`
enforces per-account caps inside it. Both numbers derive from the same stamp,
so the channel ceiling is always equal to account 1's cap - account 1 fills it
alone and account 2 can never send. L2 ("the fleet total is the sum of
per-account caps") was therefore unreachable with more than one account.

Decision: add `accounts.channel_cap` (sum of the ACTIVE accounts' `cap_for`)
and let `caps.current_cap` return it - but ONLY when `fleet_caps_enabled` is
armed. The switch seeds unset (off), so today's behaviour is byte-identical and
the live 10/day ramp is untouched. Arming it changes the live ceiling of the
one account sending (ig1's steady 15 instead of the ramp's 10), which is why it
is the owner's write, not a batch's. The dry-run arms it in its temp DB and
shows 15 + 5 = 20, with the 21st lead still queued.

Also fixed while running it: `commit_send` (and the follow-up rung insert) let
SQLite stamp `sent_at` with the wall clock instead of the tick's `now`, so the
rolling 4-per-hour slot, the PKT day cap and the ramp counts read a different
clock than the engine decided with. Both paths now write `dispatcher.utc_stamp(now)`;
one clock, one format.

Alternatives: (a) raise `daily_caps` only - rejected: the ramp clamp still
caps the channel at 15, so a second account stays starved; (b) always sum
account caps - rejected: it silently raises the live cap mid-ramp without the
owner's word; (c) drop the channel ceiling entirely - rejected: it is the only
place the owner can state a fleet-wide budget.

Grade: OPTIMAL, additive (one function, one guarded branch in `current_cap`,
one config switch, plus the timestamp fix).
Evidence: `tests/test_fleet_caps_20261105.py` (4 pins: off by default,
armed sums, halted contributes nothing, no active account falls back) and
`tests/test_commit_stamp_20261105.py` (2 pins: the stamp is the tick's, the
slot law clears an hour later). `fleet_e2e_dryrun.py`: PASS 8/8 on a frozen
clock - 4-per-hour slot held, 20 dispatches = 15 + 5, 21st lead queued, exact
attribution, zero double-touches, halt releases only the untouched lead, one
BELOW PACE alert, fleet view renders. Report
`checkpoints/FLEET_E2E_2026-11-05.md`.
UNVERIFIED: nothing here ran against Instagram - transport is mocked, so this
proves decisions, not selectors.

## 2026-11-05 - v1.1 batch 10.5: "is this working" needs a number that cannot flatter itself

Question: the fleet can send 20 a day across two accounts and the owner asks
whether the copy is earning replies. What may honestly be counted?

Decision: measure only the FIRST TOUCH per lead (the earliest `message_log`
row), because the follow-up ladder's later rungs are not independent
experiments - a lead that replied to touch 3 was already counted at touch 1, and
treating each rung as its own sample would multiply the denominator by four and
halve the apparent rate. A send is DECIDED when it is `window_hours` (72h) old
or the lead has already replied; a three-hour-old send with no answer is
`pending` and excluded from both halves of the rate, so a fresh morning cannot
be scored as a failure. A reply arriving after the window is reported as `late`
and never folded into the rate. Attribution is the hash that actually shipped,
reversed through `PINNED_SHA256`; an unpinned hash becomes `unknown:<8 hex>`
rather than being dropped, because a drifting template is exactly what this
report has to expose. Results group by (variant, channel) and carry the median
hours to reply over in-window replies only.

Alternatives: (a) count every message_log row - rejected: dependent samples,
inflated denominator; (b) count only leads old enough to be judged - rejected:
hides replies already in hand and makes the report lag reality by a day;
(c) fold late replies into the rate - rejected: the rate then measures the
follow-up cadence, not the copy; (d) drop unpinned hashes - rejected: it would
silently hide a template that drifted.

Grade: OPTIMAL, additive (one read-only module, one CLI subcommand, no writes).
Evidence: `tests/test_v11_b105_measure.py`, 11 pins, RED first
(`ModuleNotFoundError: outreach.measure`): inside/outside the window, pending
vs decided, first-touch-only, the unknown label, channel separation, the
median excluding late replies, the exactly-72h boundary, a mature silent send,
and the printed line (rate, pending count). One pin was corrected during the
run: its sample set made the asserted median arithmetically impossible
(within-window values 20 and 60 have median 40, not 20), so it now uses three
in-window samples plus one late one and asserts 50.0 - a stronger pin, not a
weaker one.
UNVERIFIED: the live numbers do not exist yet - with 40 sends and 5 replies the
per-variant cells are single digits, so the first real read will be noise.

## 2026-11-05 - v1.1 batch 10.6: a reply must be reachable when the toast is not

Question: the reply alert is a digest row plus a Windows balloon. Both can be
lost silently - Focus Assist swallows the balloon, and nobody reads a digest
that already scrolled. A reply is the one event where that silence costs a live
conversation. Where should the record live?

Decision: a durable `owner_inbox` row per reply, written exactly once inside
`register_reply`'s already-idempotent path, stamped with the detecting tick's
`now` (not the wall clock, so it lines up with `message_log.sent_at` for the
10.5 measurement). The toast stays as a nudge; the row is the record. Only an
explicit `inbox --ack` clears it, and acknowledging keeps the row (a missed
reply must stay auditable). The fleet view carries `UNREAD REPLIES: n` so the
number is visible without a new window. `match_email_replies` and both pollers
thread the tick clock through to the same stamp.

Alternatives: (a) rely on the digest table - rejected: it advances a stamp and
forgets what was seen, and a failed send is indistinguishable from a read one;
(b) exponential re-toast until acknowledged - rejected: Focus Assist is not a
retry problem, it is a delivery problem; repeated balloons train the owner to
ignore them; (c) delete rows on ack - rejected: loses the audit trail of when a
reply was announced and when it was read.

Grade: OPTIMAL, additive (migration 15, one module, one CLI subcommand, one
dashboard line, `at=`/`now=` threading).
Evidence: `tests/test_v11_b106_reachability.py`, 7 pins, RED first
(`TypeError: register_reply() got an unexpected keyword argument 'now'`):
exactly-once inbox row, tick-clock stamps on both the inbox row and the REPLIED
history row, no row when the transition is refused, a suppressed toast leaving
the reply unread, ack idempotence keeping the row, the fleet line, and the CLI
list-then-ack cycle. Two pin corrections were made against real signatures
(`sender=` not `send=`; `drain_new_digest_toasts` takes no `now`), and the
channel label is canonicalised in `add_reply` (`whatsapp` -> `wa`) because the
table's CHECK is the real gate and a rejected insert would lose the reply.
UNVERIFIED: toast delivery on the owner's actual Focus Assist setting (no
balloon was sent from this run).

## 2026-11-05 - v1.2 pack, Ruling A: the no-em-dash law is now mechanical

Question: the owner's v1.2 pack is byte-pinned copy, but the standing law
(2026-11-03) says no em dash in ANY message. What wins when the paste and the
law disagree, and how does the next paste stay honest?

Decision: the law wins, and it is now enforced by code rather than by careful
reading. `variants.lint` refuses U+2014 outright, so a future owner paste that
carries one fails at the message level (refuse + digest), never ships. The
v1.2 lines arrived em-dash free - the owner had already replaced them with
periods and colons - so this pack needed no rewriting; the divergence to log
is that the earlier PS-A/PS-B did carry "— but"/"— the", which the v1.1
port had already swapped to commas. `tests/test_v12_pack.py` pins that no
pinned body, P.S. line, manual script or template file contains the character,
and that `lint` refuses it. PS-C stays product-free on purpose: rotation keeps
one slot that does not pitch, so the "genuinely curious" register survives.

Incidental fix found while previewing the copy: `command_center.py variants`
printed the P.S. bodies straight to a cp1252 console and died on the emoji
with UnicodeEncodeError. `_console_safe` now degrades only the characters the
terminal cannot encode; the pinned bytes keep the emoji, the preview survives.

Grade: OPTIMAL, additive (one lint branch, one display helper, one CLI verb).
Evidence: `tests/test_v12_pack.py` (PS-A/PS-B v1.2 bytes, PS-C unchanged, no
em dash anywhere, lint refusal, the preview surviving cp1252). DECISIONS keeps
the ruling reversible: if the owner overrules, delete the lint branch and
restore his literal characters, then re-pin.

## 2026-11-05 - v1.2 pack, Ruling B: two follow-ups, and the probe asks the ladder

Question: the owner's note references day 3/7/14/30 follow-ups, but the
research-backed law (2026-11-04) caps the ladder at two. Which one is the
system allowed to arm?

Decision: two. `followups._RUNG_DAYS` is (3, 7) and `MAX_LADDER_TOUCH` is 2;
`rung_days(3)` raises, `_next_rung_after` returns None past the cap,
`render_followup(3)` refuses, and a row armed under the retired cadence is
expired on sight (status='expired') instead of sitting pending forever or
firing. The day-14/day-30 copy files stay pinned and un-deleted because
historical `message_log` rows resolve their hashes; they are retired copy, not
dead code. The day-7 IG escalation row (touch 2, channel-stamped) is the same
touch on another channel, so it is not capped away - it is what makes the
second touch reachable for a WhatsApp-routed lead.

The cap caught a live defect the tests had been hiding: `replies.py`'s
direction guard built "our own copy" from a hardcoded `(1, 2, 3, 4)` rung list
and caught only MessageHold. With the cap in place, `render_followup(3)` raises
TemplateError, which would have killed `poll_wa_replies` / `poll_ig_replies` on
every single lead - a read-only probe dying on a copy refusal. It now iterates
`range(1, MAX_LADDER_TOUCH + 1)` and catches TemplateError, so the guard asks
the ladder instead of remembering yesterday's law.

Alternatives: (a) keep four rungs and treat the research as a suggestion -
rejected: the owner never re-armed 14/30, and silently shipping four touches is
exactly what Ruling B forbids; (b) let lapsed rungs fire late to preserve the
old "never stop following up" rule - rejected: with two rungs it would replay
the entire ladder after a long outage, which the same law calls a burst;
(c) delete the retired stage files - rejected: their hashes are the only way
to attribute historical sends.

Grade: OPTIMAL, additive-plus-cap (one constant, one guard, one expire branch,
one probe loop that now reads the cap).
Evidence: `tests/test_v12_pack.py` (calendar cap, no third rung armed, render
refusal, an above-cap row expiring without a send, the day-7 completion arming
nothing) and the six rewritten B24 pins (walk ends at day 7, lapsed rung fires
once and not a burst, ladder ends without expiring the lead, reply cancels the
armed day-7 rung, backfill arms touch 2 and stops). `tests/test_b7_templates.py`
now asserts touches 3/4/5 are refused rather than rendering retired stages.
UNVERIFIED: no live lead has passed day 7 under this code yet; the first real
day-7 tick is the proof.

## 2026-11-07 - "signed in" means the session cookie, not a host key

Question: the fleet reported ig2/ig3 as `signed_in`, but the owner says their
sessions never crossed from his own Chrome. Which signal is true?

Evidence: read straight from the profiles' cookie DBs (names only, values
encrypted and never read). `Outreach` (ig1) carries `sessionid` + `ds_user_id`.
`Outreach-ig2` carries csrftoken/datr/ig_did/mid/wd and `Outreach-ig3` adds
ps_l/ps_n: anonymous cookies a logged-out browser leaves behind. No `sessionid`
in either. They are logged out, and the old check called any `instagram.com`
host key a session.

Decision: `instagram_session` requires Instagram's auth cookie (`sessionid`).
A bare host key is `no_session`. The consequence is the honest one: both new
lanes are reported `no_session` until someone signs in inside their own
profile, and onboarding's warning stays true instead of being swallowed.

Grade: OPTIMAL, one predicate (host key + name, not host key alone).
Evidence: `tests/test_v11_b4_fleet.py::test_anonymous_instagram_cookies_are_not_a_session`
(RED against the old check: `'signed_in' != 'no_session'`), the profile fixture
now writes `sessionid` for a session and csrftoken/mid/ig_did for the
logged-out shape, plus the live read above:
`Outreach -> signed_in, Outreach-ig2 -> no_session, Outreach-ig3 -> no_session`.
UNVERIFIED: whether either lane can complete a sign-in at all from this IP
(the owner's last attempt hit a challenge loop) - that is the next live test,
not something this check can decide.

## 2026-11-07 — dish personalization: the copy was ready, the evidence never arrived

Owner report: "re-enrichment runs and 0 reply rates due to no personalization …
we need to hit personalization, needing dishes basically for each restaurant".

**What was measured before any change (read-only, this hour).**

| fact | value | where |
|---|---|---|
| messages ever sent | 122 (45 wa, 34 ig, 43 email) | `message_log` |
| sent with a dish | **0** | `message_log.dish` |
| leads with `extracted_dish` | **1 of 3,159** | `leads` |
| leads in `QUALIFIED` (the state `extract_dish` required) | **0** | `leads.state` |
| send-ready leads (`ROUTED`) | 186 | `leads.state` |
| reviews in the sender | 6 leads | `leads.data_json` |
| reviews in the scraper | 429 leads / 2,110 strings | `scraper` cache |
| email channel | `email_enabled=0`, `daily_caps.email=0` | `config` |
| WhatsApp | `wa_paused` (owner directive 2026-11-02) | `config` |
| queue | `PAUSED` | `config` |

So "0 replies" is first a **volume** fact (only IG has been live; email and WA are
held by the owner), and the personalization fact is separate: the dish never
reached a single message. The DM copy says so out loud — "What's the dish that
earns it" — it asks the owner the question the pipeline should have answered.

**Why the dish never arrived — three independent blocks.**
1. `extract_dish` required `state='QUALIFIED'`; production had 0 such rows.
2. Extraction called Ollama on every lead; Ollama is the least available
   dependency here (repeated `halts`), and the queue parks when it is down.
3. The evidence never crossed the folder boundary: imports only run when a RUN is
   ingested, while the scraper keeps enriching existing leads. Today's
   `--refresh-fields review_samples,…` pass wrote 429 leads' reviews *after* the
   last import, so the sender had 6 while the scraper had 429.

**The batch (TDD, in this order).**
- `dish_mining.py` — deterministic proposer over the lead's own reviews; a
  candidate must be a menu item, contain a lexicon dish term, or recur in two
  reviews, and is still passed through the existing `validate_dish` grounding
  gate. Model becomes the fallback, not the requirement.
- `dishes.py` — deterministic-first ordering; `_SEND_READY_STATES =
  (QUALIFIED, ROUTED)`; `extract_dishes` skips already-annotated rows (idempotent);
  `use_model=False` + `assert_live=False` for backfills.
- `sync.py` — the missing arrow: refresh every lead from the scraper read-only,
  then mine dishes. No ICP, no routing, no sends, no model.
- Tests: 16 new/updated in `test_dish_mining_20261107.py` +
  `test_sync_evidence_20261107.py`; four `test_b6_dishes.py` fixtures retargeted
  so they still exercise the model gate (praise without a dish name) rather than
  the new miner.

**Precision was the lesson.** A first version scored praise-adjacent n-grams and
mined a "dish" for 421 of 429 leads — proposing `we`, `it`, `definitely one`,
`hot straight from`. A wrong dish is worse than none (addendum 5): the copy would
quote it at the owner. After the dishwasher-signal filters: 233 of 429 (54%)
leads with a review body yield a dish, and the sample reads like food
(`shawarma`, `pho`, `mutton curry`, `gyro wrap`, `biryani`).

**Measured on a copy of the live DB** (`sync` run against `/tmp/outreach_copy.db`,
never the production file): 3,160 scraper rows → 3,008 refreshed; 186 send-ready
leads scanned → **39 with a dish**, 40 `no_model`, 107 `no_evidence`. Reviews in
the sender rose from 6 leads to 427.

**What this does not fix.** 39/186 send-ready leads is 21%: the ceiling is review
coverage upstream (Google's limited view served 0 review bodies this session), not
the miner. Menu capture (33 leads) and the "Popular dishes" panel remain the
levers for the other 79%.

### 2026-11-07 (later) — the dish pass becomes run-scoped, and precision is re-earned

Owner direction: "the engineering should complete and data should be assigned to a
scraper run". That maps onto a seam that already exists: the scraper's re-enrich
pass is itself run-scoped (`--re-enrich "<run_id>" --refresh-fields
review_samples,rating_breakdown,…`), so the evidence from one pass belongs
to one run. The sender simply had no way to say "apply this to that run".

**`python -m outreach.sync --run <run_id>`** (new) confines both stages — the
scraper refresh and the dish pass — to one run. Measured on the run that actually
holds the enriched send-ready leads (`2026-09-12T01-08-54.455049-1c4a2ab7`):
2,162 rows in the run, 2,136 refreshed, 164 send-ready scanned, 29 with a dish,
135 without, 39 s wall clock.

**`--remine`** (new) drops existing dish annotations before the pass. Without it a
tightened proposer cannot fix anything: idempotence is per row, so a stale verdict
would live forever. This is how the four junk verdicts from the first pass
(`korean`, `pub`, `variety`, `made`) were retired rather than tolerated.

**Precision, round two — the first pass was not shippable.** Reviewing the 39
proposals by eye (not by count) found ~10 that would have embarrassed the sender:
a venue format (`pub`), a cuisine (`korean`), mass nouns on recurrence
(`ingredients`, `variety`), light verbs and modals (`made`, `start`, `must`), the
shop's own name echoed back (`shawarma shish`), a description (`perfectly seasoned
rice`), a complaint (`chewy pizza`), and — after the first fix — a *server's name*
(`nunu`, from "Thank you Nunu"). Each is now a named guard with a regression test
taken from the real review text: `_GENERIC_NOUNS`, modals/light verbs in
`_NON_DISH`, extended `_VENUE_NOUNS`/`_CUISINE_WORDS`, `_NEGATIVE_DESCRIPTORS`
(criticism is never a selling point), `_WEAK_LEXICON` (a category word needs
recurrence), the name-echo rule, and `_PERSON_CONTEXT`.

Result on the live DB: **29 send-ready leads carry a dish, and every value is
food** — `shawarma` ×8, `mandi`, `musakhan`, `mutton curry`, `chaat papadi`,
`koobideh`, `shish kebab`, `fish tacos`, `garlic rice lumpia`, `halwa`, `baklava`,
`pho`, `falafel`, `masala chai`, `beef chaplee kabob`, `halwa`… Rendering through
the real template proves the wire: "Saw the musakhan post, looks unreal."

**The honest ceiling.** 75 of the 186 send-ready leads have any review body; 29
of those 75 yield a defensible dish (39%). The other 111 send-ready leads have no
review text at all, so no miner can help them — that is a scraper-supply problem,
and the paused enrichment queue (runs `2026-09-12…` and `2026-09-22…`) is the
lever that widens it.
