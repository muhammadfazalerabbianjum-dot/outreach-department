# DEFERRED.md — deferred work tracking (blueprint law 17)

| ID | Item | Why deferred | Optimal path | Upgrade trigger | Batch/When |
|---|---|---|---|---|---|
| DT-1 | Ollama model upgrade beyond 8B instruct | $0 law + installed model set | owner installs stronger 7–13B instruct | Batch 5 corpus <90% agreement | Batch 5 |
| DT-2 | Browser binary choice — **RESOLVED**: `channel="chromium"` pinned 2026-10-26 (chrome-headless-shell silently ignored `--profile-directory=Outreach`, hiding the owner's logins from every cold run; proven by cookie-jar comparison) | was provisional until sender wiring | done | done | Batch 10 / fix 2026-10-26 |
| DT-3 | City→tz ASSUMED rows (Salalah/Seattle/Portland) | absent from scraper SSOT; standard IANA used as fill | owner config review | before first live send | Batch 3 import |
| DT-4 | ~~None~~ — superseded by B13–B18 open ends below | — | — | — | — |
| DT-5 | Template gate + owner eyeball (Q9) - **RESOLVED 2026-10-26**: owner edited the copy, `template_status=owner_confirmed`, `email_enabled=1`, 4 files re-pinned; the `template_gate` park-guard stays in code (scheduler.py) as the fail-safe if the owner ever un-verifies | was provisional | - | - | B12/B20 |
| DT-6 | WA/IG live-send verification + in-thread reply probes + manual owner mark — **WA LIVE SEND DONE 2026-10-27 09:11:22 PKT**: lead 12 Bereket Kebab House, screenshot `checkpoints/evidence/wa_12_20261027T091122.png` (4 template paragraphs delivered, ✓ ticks, daily 1/15), preceded by 6 correct WA_DEAD walkouts; sessions verified 2026-10-26 (`verify_sessions.py` → ready_home, rc=0). **Remaining: IG first send + in-thread reply probes (owner reply needed)** | send-path fix landed (DECISIONS 2026-10-27) | IG ramp start → reply probes | IG send + owner reply | B10/B11 |
| DT-7 | IG warmup ramp anchored on first real IG use (`ig_warmup_start` stamp) | no IG profile session yet | stamp auto at first dispatch; ramp 5/10/15 (ADR-8) | first IG send | B11 |
| DT-8 | NO_REPLY expiry grace = +3d after day-7 touch - **RESOLVED 2026-10-26: Q10 yes**, code already matches (`_EXPIRY_AFTER_LAST`) | was owner-delegated judgment | - | - | B14 |
| DT-9 | Email attachment/link for mockup images dropped (deliverability) | cold SPAM law; no hosted asset yet | revisit when owner supplies a mockup URL | owner provides asset | B12 decision |
| DT-10 | WA/IG sends + resends unwired in the runtime maps — **RESOLVED 2026-10-26 (B19)**: `real_senders`/`real_resenders` now carry `wa`/`ig`/`email`; WA/IG entries are profile-owning adapters (`send_*_via_profile` / `resend_*_via_profile`) with the template gate asserted before any browser boot, transport-only resend cores for the follow-up ladder, and ADR-8 safety halts preserved | wiring batch done | — | — | B19 |
| DT-11 | Browser opened and closed per dispatch (WA cold chat sync 30–90s on a slow link is paid once per message, up to 15×/day) | correct-but-not-fast first wiring; stateless sender maps make per-send the natural step | keep one profile open per tick window and reuse the page across the dispatch loop | live pacing review after first real sends / if a tick exceeds its window | B19 |
| DT-12 | Owner alerts were digest-only - **RESOLVED 2026-10-26 (B21)**: local powershell NotifyIcon toast drains the digest table each engine cycle (owner opted in, Q11), config `owner_toast`, tests 8/8 GREEN | was silent pythonw engine | - | - | B19/B21 |
| DT-13 | Scraper leg auto-restart is armed-but-off: `auto_restart_leg` default FALSE (LOW LEADS digest alert only) + 6h per-region cooldown on the D10 region-matched invocation. **2026-11-16: the start is now DETACHED** (`intake._spawn_leg`, own process group, output to `scraper/logs/leg_<ts>.log`, named in the digest) — the first wiring used blocking `subprocess.run`, so arming it would have parked a whole engine cycle on an hours-long scrape and tripped STAGE OVERRUN. A failed start is named once a day and does NOT stamp the cooldown, so a fixed venv retries on the next pass | spawning legs from the engine is a consequential external action the owner has not authorized; blueprint says alert | owner arms it with `set_config(db, "auto_restart_leg", True)` (no CLI yet — one-line python); then a starving region with stock below `low_lead_threshold` starts its leg at most once per 6h | recurring LOW LEADS alerts the owner keeps ignoring | B20 addendum 2 |
| DT-14 | `REGION_UNRESOLVED` hold has no owner-facing release tool — **RESOLVED 2026-11-16**: `command_center resolve-region LEAD --region na\|me` sets the region and releases the lead to NEW; `--reject` closes a junk row instead (machine now allows REGION_UNRESOLVED -> REJECTED). Refuses a row that is not held, an unknown id, and a call with neither flag. Lead 3149 closed as junk identity with this command; region holds now 0. Placeholder names are also rejected at intake by `cleaning.resolve_regions`, so junk never becomes a region question again | was shipped as an empty command | done | — | B20 addendum 3 / 2026-11-16 |

| DT-15 | 1 legacy scraper row with `run_id IS NULL` is invisible to the watcher/CLI intake (`run_id IS NOT NULL` filter) — its lead can only arrive via a manual full import | legacy artifact of run 1; scoping by NULL run_id is ambiguous (it would sweep every run) | backfill the NULL run_id in the SCRAPER DB — **forbidden (ADR-3 write)**, so either leave it or map it once via a read-side rule if the owner cares | owner asks where that one lead went | B20 watcher |

| DT-18 | `tests/test_v11_b1_variants.py::test_claim_next_serves_the_variant_lane_when_enabled` is INTERMITTENT under the full gate (RED once 2026-11-16 ~03:20Z, GREEN alone, GREEN in its own file, GREEN in the next full gate 30 min later) — no seed, no xdist, no random plugin, so the contaminant is cross-file state or wall-clock, not my change | not reproducible yet; the gate log keeps only the summary, not the traceback | reproduce with a full `pytest tests` run capturing `--tb=long` before touching anything | a second unexplained RED | 2026-11-16 |

| DT-19 | All 18 `COPY_HOLD` leads were held by ONE cause: the scraper truncates long reviews with an ellipsis (`review_samples`: "…which simply wasn't …"), the quote path copied that truncated sentence into V2/V5 copy, and the message lint bans ellipsis. **FIXED 2026-11-16**: `judge_quote` refuses such a sentence as `truncated_source` (the wall keeps its strictness), the three-way exit is real (`holds --release-copy` returns a lead to ROUTED once no veto is open — the machine edge had no caller, so the rows were permanently parked), and all 18 are back at ROUTED with their reason on the row. **Open:** their next render is unverified — it needs a model, and a different ellipsis source would legitimately re-hold them | root cause fixed, re-render unverified | first send window with a model up | second `copy lint` hold on the same body | 2026-11-16 |

| DT-20 | **Lead 638 (Lotus of Siam) holds the only open veto**: `V3`, dish "flavors", `model_veto` — "Flavors is a noun phrase … does not name a specific dish (score 0.50)", logged 2026-11-09, layers 1+2. Layer 2 is a model judgement, so it cannot be re-judged while Ollama is off; the lead is ROUTED and simply not claimable while the hold stands | needs a model (re-judge) or owner judgement | with a model up: `holds --clear 638` after a re-render, or re-mine the dish off "flavors" first | owner wants that lead in the queue | 2026-11-16 |

| DT-21 | **Parked rows now carry a CLASS, not prose**: `leads.park_kind` (migration 20) is written by the code that parks — `lane_law` (routing found no lawful channel) / `verdict` (the platform refused). `reroute_stranded` filters on the column, so a wording change can never silently disable the revive again; backfill mapped the 555 parked rows from the router's own actor field. Pin: a lane-law row whose reason mentions no channel word is revived, and a verdict row whose reason DOES contain the old markers is not | closed 2026-11-16 | done | — | 2026-11-16 |

| DT-22 | **Replies pass had no clock and no timing** — live 2026-11-17 02:55Z: `REPLIES PASS: 16 thread(s) in 426.5s (avg 26.7s) | slowest lead 499: 38.4s (nav 8.4 / state 30.0 / read 0.0, unknown)`, i.e. the read-only probe was paying the SEND path's 30 s state deadline on threads that never became decisive. **Fixed 2026-11-17**: probe state wait bounded to 8 s, pass bounded to 200 s with an explicit 'stopped early, N thread(s) left for the next pass' row, and one timing summary per pass. Also fixed: any `net::ERR_` on the dm shortcut now falls back (an ERR_HTTP_RESPONSE_CODE_FAILURE escaped the hint list and aborted a whole pass) | **post-fix (2026-11-17 08:10Z / 08:40Z)**: `16 thread(s) in 62.9s (avg 3.9s)` and `16 thread(s) in 55.7s (avg 3.5s)`, slowest 5.0-7.8 s (nav 1.7-2.0 / state 3.1-6.0), all 16 surfaces `ig.me`, no overrun | closed by measurement | - | - | 2026-11-17 |
| DT-23 | **Six real profiles read `[no_post]` twice** (leads 82/90/92/189/211/212, all `VALID_FORMAT`, icp 0.9) at 15:55Z and again at 19:00Z — the second time with the 12 s grid wait live, 12-14 s apart, so paint timing was NOT the cause. The lane now logs what the browser actually had (url, articles, anchors, body_len, login/dead/ban) and names a wall `ENGAGE SKIPPED [session]` instead of a postless verdict. **Open:** the first diagnostic row is due at the next engage window (20:55 PKT): if articles>0 the selector `article a[href]` is fine and the grid was genuinely empty; if articles=0 with anchors>0 the selector drifted | instrumentation shipped, cause unknown | next engage cycle with a fresh candidate | another silently postless week | 2026-11-17 |
| DT-16 | Host hardware unstable: 4-5 unclean shutdowns 2026-10-26 14:52-17:18 PKT (4x Kernel-Power 41, zero BugCheck/WHEA/RAM/disk events = power-loss or hard-freeze signature) while the backfill ran; boot-time Ollama.ln never left a running server | not diagnosable from software | owner confirms power flicker -> check outlet/PSU; mitigations live: Ollama.ln retargeted to `ollama.exe serve`, intake+gate serialized, engine held stopped until backfill+gate done | next unexplained shutdown or owner reports flicker/freeze | B20 recovery |
| DT-17 | **PARTLY RESOLVED 2026-11-07** — `{{dish}}` was dormant because the model-only extractor required `QUALIFIED` (0 rows) and the sender held 6 leads with reviews while the scraper held 429. Now: `dish_mining.py` mines reviews deterministically (menu item / lexicon term / 2-review recurrence, still through `validate_dish`), extraction covers QUALIFIED+ROUTED, and `python -m outreach.sync` refreshes evidence without Ollama. Final measured state: 29 of 186 send-ready leads hold a dish (39% of the 75 that have any review text), reviews 6 -> 427 leads; a run-scoped pass (`sync --run <id> --remine`) applies an enrichment pass to exactly that run's leads. **Remaining:** review coverage upstream caps the rest — 111 of the 186 send-ready leads have no review body, so the paused re-enrich queue (runs 2026-09-12 / 2026-09-22) is the lever that widens it; menus on 33 leads. Old note: 1 of 1010 qualified leads had extractable evidence. | was an upstream data gap plus a state scope bug | capture reviews/menus more broadly (Popular dishes panel, website menus), then re-run `python -m outreach.sync` | owner wants >21% dish coverage on sendable leads | 2026-11-07 dish personalization batch |

| DT-18 | WA/IG delivery proof accepts fragments: a >=40-char PREFIX of an expected paragraph satisfies the rule (`t.length>=40 && p.includes(t)`), so a message truncated mid-paragraph could still commit (full-copy match is exact; only tails are exposed) | every live send matched a full paragraph - no truncation observed; rule kept for whitespace tolerance | tighten `_TICK_JS` to exact normalized-paragraph equality once a truncated delivery is actually seen | owner reports a cut-off message, or an ADR-15 screenshot shows a short bubble | Send-path batch 2026-10-27 |

Owner activation tasks (not debt; see DIGEST.md): WA QR scan, IG login,
email template paste → .env + template registration.

**Fleet launch prep — owner, logged 2026-11-05, remind when the fleet is ready
to message (not before):**

- New profile picture for **outreach 1** and **outreach 2** (the three accounts
  must not look like a batch of clones).
- **One reel uploaded on each** of outreach 1, outreach 2 and outreach 3, so a
  fresh account has a post at all before its first DM.
- Account state 2026-11-05: all three are signed in inside the owner's own
  Chrome (profiles `Outreach One`, `outreach 2`, `outreach 3`), so the PFPs and
  reels can be uploaded there directly — no engine involvement needed.
- **ig3 lane session is still missing.** The engine can only drive its own
  Chromium profiles: `.chrome/Outreach` (ig1) is signed in and live, but
  `.chrome/Outreach-ig3` holds anonymous cookies only, so ig3 cannot send yet
  even though the account itself is logged in. Unblocked by ONE sign-in inside
  `interactive_login.py --account ig3` (captcha loop on 2026-11-05, parked by
  the owner) or by moving the lane to real Chrome (`channel="chrome"`, a
  supervised change that requires re-establishing ig1's session).

## 2026-11-15 — the evening plan (owner: "make a plan for whatever is left")

State it starts from, measured: 123 ready leads (8.2 days at 15/day), 23 with a
dish, 13 open holds, 3,160 places imported (241 usable = 7.6 %), ig3 without a
session, WA paused by owner directive. Code fixes landed today: model window
20:55-midnight, engagement window 20:55-10:00 + owner pause, one-sentence V2
quotes, no-dish vetoes downgraded to advisory.

| ID | Step | Who | Acceptance |
|---|---|---|---|
| P1 | Release the 8 holds the wall now accepts (284/464/481/557/82/94/157/186) | owner one command | 8 leads back at ROUTED, re-rendered, zero new holds |
| P2 | Release the 4 copy_lint holds (893/994/1921/153) and confirm the re-render is <= 8 sentences with a one-sentence quote | owner + me watching | 4 sends with no `copy_lint` on the new body |
| P3 | Dish coverage: 100 of 123 ready leads still carry no dish | engine, tonight 20:55-10:00 | ready-with-dish 23 -> >= 60; lead 638 re-mined off "flavors" |
| P4 | First real evening cycle under both windows (comments armed, ambient 80 min, model only after 20:55) | engine | digest shows acts, no `ENGAGE SKIPPED [model_window]`, `/api/ps` empty between bursts |
| P5 | WhatsApp as a second channel (owner's idea, design first) | owner decision | 20 paced WA sends, no new WA_DEAD, reply rate vs IG baseline |
| P6 | Supply: import is complete, yield is the wall (7.6 %) - pick a city with Ottawa-like contact density or review the 1,201 rejections by reason | owner decision | >= 15 days of ready supply |
| P7 | ig3 session | owner login (in progress) | `fleet` shows ig3 signed in, 0 `no_session` skips |
| P8 | Push the local commits | owner decision | remote `main` matches local |

WA pool measured for P5: 255 usable leads carry a phone, 216 carry an IG handle,
so WhatsApp is a second touch for most of the list rather than new volume.
