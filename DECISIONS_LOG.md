# DECISIONS LOG — index of standing rulings

`DECISIONS.md` holds the full argument for each ruling (2,300+ lines, read the section before changing
behaviour it covers). This file is the **index**: one line per ruling, newest first, so a new session can find
the law without reading the whole history.

Format: `date · ruling · where it is enforced (file/test) · status`.
Status: `STANDING` (must not be broken), `SUPERSEDED`, `RESOLVED` (debt closed).

---

## 2026-11-13

| date | ruling | enforced at | status |
|---|---|---|---|
| 11-14 | **Owner ruling: "story reply counts as warmup yes"** — a logged story reply satisfies the no-comment-no-send law exactly like a comment, and is the alternate first touch; the 12-48 h window applies to whichever warm touch is newest | `warmup.WARM_ACTS`; `story.py`; migration 19; `engage_stage._story_one`; `tests/test_story_warmup_20261114.py` | STANDING |
| 11-14 | **Owner ruling: "80 min/day scroll yes — sometimes 80, sometimes 60, sometimes 120"** — ambient budget is a per-day number inside a 60-120 range, 20 min a session, and **five follows a day across all accounts**; ambient has its own switch and never acts when the message lane is shut | `ambient.py`; `engage_stage._run_account`; `tests/test_ambient_20261114.py` | STANDING |
| 11-14 | **Owner ruling: "arm chain parking"** — the targeting policy is armed live with chain + ghost-kitchen parking on, delivery-only off (needs `dine_in == False`), 5,000-follower line, no category list yet; a parked lead lands in `DEPRIORITIZED`, never terminal | `config.targeting_policy` (armed 2026-11-14); `targeting.disqualify_reason` | STANDING |
| 11-14 | **Owner ruling: "probe"** — the cold-DM gate exits by measurement, not by acceptance: at most 3 cold first touches a day, dish-bearing leads only, one per lead per day, each announced and watched for `Delivered`; the first sighting opens the gate permanently | `cold_probe.take_slot`; `ig_sender.send_ig_message`; `ig_sender._observe_delivery`; `tests/test_cold_probe_20261114.py` | STANDING |
| 11-14 | **Owner ruling: "apply all"** — the seven copy replacements in `ORCHESTRATOR_REPORT_2026-11-13.md` §7 are the copy of record: no pitch in the first line, no ellipsis, no assumed "brother", no invented social proof in V9, transparency moved to touch 2 | `templates/social.txt`, `followup.txt`, `email.txt`; `variants.V4/V6/V7/V9`; `PINNED_SHA256`; `tests/test_b7_templates.py` | STANDING |
| 11-14 | The reply question is answered per **attribution dimension** (`commented_before_dm`, `send_hour`, `target_class`, `touch_kind`) on the same first-touch / decided / late laws as `measure`, and the forecast contract carries it as `reply_rate_by`; an empty dimension says “no decided first touches yet” and never 0 % | `attribution.py`; `forecast.build` / `render_markdown`; `command_center replies --by`; `tests/test_attribution_20261114.py` | STANDING |
| 11-14 | Delivery-only is read from `service_options.dine_in`/`delivery` when the scraper filled it, with **null = unknown and never “no dine-in”** (measured: the cache carries no `has_dine_in`, and nulls are the majority) | `targeting._delivery_only`; `tests/test_targeting_20261114.py` | STANDING |
| 11-14 | **R12 amended:** a live-model test charges the cold load to a separate generous warm-up (`warm_model`), and the measured call keeps 60 s + one attempt. Measured: the first gate after an idle machine failed all three live tests with `TimeoutError` at 60 s; with the warm-up they pass. A wedge still dies in 60 s | `tests/_model_limits.py` `warm_model`; the three live tests | STANDING |
| 11-14 | Targeting parks a lead in `DEPRIORITIZED` (never terminal, one repair edge back) when it is a chain, a ghost kitchen, on the owner's dine-in-first list, or over the follower line; the policy and the follower line ship disarmed, `is_on_delivery_platform` alone never means delivery-only, and an unreadable follower count refuses nothing | `targeting.disqualify_reason` / `park`; `dispatcher.claim_next`; `followers.read_followers`; `tests/test_targeting_20261114.py` | STANDING |
| 11-14 | A DM follows its comment only inside a **12-48 h window** (`config.comment_window`): earlier is a bot, later is a stranger, and a stale comment is refreshed by a new logged act instead of being followed by a cold DM. One function (`warmup.comment_state`) is the single truth for claim and stage | `warmup.comment_state` / `comment_window`; `dispatcher.claim_next`; `engage_stage._comment_candidates`; `tests/test_comment_interval_20261113.py` | STANDING |
| 11-14 | **No comment, no send.** A lead is `warmed_up` only when its OWN post carries a logged `comment` act; unwarmed leads are never claimed and never rendered, and the gate arms with the comment lane (`engage_enabled`) so the law can always be satisfied by the time it applies | `warmup.warmed_up` / `assert_warmed`; `dispatcher.claim_next`; `variants.render_lane`; `tests/test_unwarmed_gate_20261113.py` | STANDING |
| 11-13 | Engagement (comments, warm-up, warming) is one stage in the tick, DISARMED by default (`config.engage_enabled` seeds False); it acts at most once per account per tick, only inside the lead's window, always in that account's own profile session, and a fault in it never blocks a send | `engage_stage.run_engage_stage`; `scheduler.run_engine_tick(engage_stage=)`; `tests/test_engage_stage_20261113.py` | STANDING |
| 11-13 | **Working agreement.** The engineering lane runs continuously for the whole session: it finishes a batch, commits it, and immediately takes the next one — it does not stop between batches to ask whether to continue, and it reports only when the owner interrupts with a status question, or when a genuine owner-only decision or blocker is reached (who gets messaged, copy, money, new standing law, a wedged service only the owner can restart). "Industry grade or above" is the bar: every change is re-read, pinned, gated and free of loose ends before the next batch starts | `RESUME.md` §0 (working agreement); this row | STANDING |
| 11-13 | Every live-model call carries a deadline that reaches the socket, and the gate as a whole is wall-bounded; a wedged dependency must fail loudly inside a bounded time, never hang (a hang names nothing) | `ollama_adapter.generate(timeout_s=)` → `_real_transport`; `gate.run_pytest(wall_s=)`; `tests/_model_limits.py` | STANDING |
| 11-13 | A probe lock is refused while a cycle is RUNNING, and the heartbeat `stage` decides that: the parked shape (`"cycle N parked Ns"`) is a sleep, any other stage name means running — a mid-cycle promise (900 s) is not evidence of sleep | `browser._engine_state`; `tests/test_b25_contention.py` | STANDING |
| 11-13 | The wake math tells three "no claim" states apart: an empty queue sleeps, a SPENT fleet inside an open window parks at the 300 s probe cadence, and a lane with no cap to give today is not work at all; the answer is never `0.0`, and a closed window still sleeps to its own open | `scheduler.seconds_to_next_wake` (`_fleet_cap_left`); `tests/test_cap_wake_20261113.py` | STANDING |
| 11-13 | A send never fails as `unknown`: `unknown` is a *classification*, a **verdict** is one of `ready` / `profile_no_message` / `thread_slow` / `login_wall` / `ig_dead` / `undeliverable` / `ban_signal` | `ig_sender._resolve_send_surface`; `tests/test_named_states_20261113.py` | STANDING |
| 11-13 | A blocker that is a fact about ONE lead (`profile_no_message`) skips that lead for the pass; it never breaks the lane and never becomes an account verdict — the lead stays `ROUTED` | `scheduler.run_engine_tick` (`_record_send_skip`); same test file | STANDING |
| 11-13 | The enrichment refresh is a **scheduled stage**, not a send-tick tax: dispatch runs first, the refresh runs only off-window, at/after 04:00 PKT, once per PKT day, inside a 600 s budget; a truncated pass resumes and does not stamp the day; a paused queue holds the whole pass | `intake.refresh_due` / `mark_refresh_done` / `watcher_tick(budget_s=)`, `engine_loop._refresh_if_due`, `scheduler` stage clocks; `tests/test_stage_split_20261113.py` | STANDING |
| 11-13 | Every tick stage names itself on the heartbeat when it starts **and** with its measured duration when it ends; `heartbeat.stages` is the cycle's trace, and a stage past its budget lands one `STAGE OVERRUN` row | `scheduler.run_engine_tick(stage_cb=)`, `heartbeat.write_heartbeat(stages=)`, `engine_loop.run_forever` | STANDING |
| 11-13 | A transport failure is a network fact, never an account verdict: `ig.me` gets one bounded attempt, then `instagram.com/<handle>/` + Message click; both failing parks the lead in `TRANSPORT_HOLD` (never `IG_DEAD`), one repair edge back to `ROUTED` | `ig_sender._open_dm_thread`, `scheduler` (`IgTransportError`), `machine.ALLOWED`; `tests/test_transport_fallback.py` | STANDING |
| 11-13 | A lint refusal is a fact about one lead: `COPY_HOLD`, never a cycle abort, and the lint itself is never relaxed | `dispatcher.claim_next` (`LintBlocked`), `machine.ALLOWED`; same test file | STANDING |
| 11-13 | The database runs WAL with `busy_timeout=5000`, so a read-only dashboard cannot stall a send cycle | `store.Database.__enter__`; `test_ops_hardening.py` | STANDING |
| 11-13 | Account health is written on the send that proves it: `accounts.last_ok_at` inside the send transaction | `dispatcher.commit_send` | STANDING |
| 11-13 | Caps are changed only through `caps.set_cap` (int, 0..100, known channel); raw JSON edits are a defect | `caps.set_cap`; 8 refusal pins | STANDING |
| 11-13 | The contract pair states one fact under distinct names: `cap_total`, `sent_today`, `today_capacity_remaining` — "rate" alone is ambiguous and must not be reintroduced | `forecast.build`, `countdown.build`; `test_contract_pair_states_the_same_cap_two_ways` | STANDING |
| 11-13 | The heartbeat names its stage, so slow and stalled are distinguishable | `heartbeat.write_heartbeat(stage=)`, `engine_loop` | STANDING |
| 11-13 | Root hygiene: screenshots and superseded reports live in `_archive/`, never in the working root; nothing is deleted without the owner | `_archive/2026-11-13/`, commit `f785ddf` | STANDING |

## 2026-11-12

| date | ruling | enforced at | status |
|---|---|---|---|
| 11-12 | `measure` attributes every body a send can carry — pinned templates **and** engine variant bodies, including menu-sourced V4/V7; an unclaimed hash stays `unknown:` | `measure._NAME_BY_HASH`; `tests/test_menu_sync.py` | STANDING |
| 11-12 | When a dish comes from the restaurant's own menu, the opener says so ("on your menu") in V4 and V7; the recorded `template_hash` is the body actually used | `variants.MENU_DISH_BODIES`; four render pins | STANDING |
| 11-12 | Website-menu items are candidate evidence only; `validate_dish` judges last; nav junk ("Home", "Order Online") is refused in two places | `menu_sync`, `dish_mining.propose_dish_from_menu` | STANDING |
| 11-12 | Menu sync runs **after** `refresh_leads` (refresh rewrites `data_json` and would drop menu items) | `sync.import_menu_items` ordering | STANDING |
| 11-12 | ig2–ig4 begin week-1 warm-up (engagement only, 0 DMs); ig1 remains follower-gated | `config.ig_cold_dms_blocked`, `accounts.status=WARMING` | STANDING |

## 2026-11-11

| date | ruling | enforced at | status |
|---|---|---|---|
| 11-11 | ig1 is follower-gated until a `Delivered` bubble is **observed**; the first sighting is a milestone and must be surfaced immediately | `config.ig_cold_dms_blocked`, `ig_sender` (`follower_gated` outcome) | STANDING (exit condition open — ROADMAP R1) |
| 11-11 | A row is a reply only with captured reply text; `REPLIED` is terminal, so an unverified revert uses the documented `inbox.revert_unverified` path | `countdown.replies_pending_ack`, `inbox.py`, actor `inbox-audit` | STANDING |
| 11-11 | Wall layer 2 is coherence telemetry only; it never vetoes owner copy, and never edits a byte | `proofread.screen`, `dispatcher` (`judge=None` for pinned templates) | STANDING |
| 11-11 | The opener's verb must fit the evidence source (menu vs post vs review) | `variants` (V4/V7 menu bodies) | STANDING |

## Earlier standing laws (still binding)

| date | ruling | where |
|---|---|---|
| 11-07 | Owner order: dish-bearing leads claim first; weak evidence claims later but is never excluded | `dispatcher.claim_priority` |
| 11-05 | No em dash in lead-facing copy, enforced mechanically (refuse, never rewrite) | `variants.lint` |
| 11-05 | Two follow-ups, and the probe asks the ladder before a rung fires | `followups`, `replies.probe_fresh` |
| 11-04 | Junk identity is one rule, asked at intake, judgement and claim | `cleaning.junk_identity_reason` |
| 11-03 | Scraper contamination hold: quarantine, never guess; `A:\scraper` is read-only (ADR-3) | `cleanings`, `sync` |
| 10-27 | Browser channel is `chromium` with `--profile-directory` (proven by cookie comparison) | `browser.py` |
| 10-26 | One first touch per lead; the ladder owns everything after | `dispatcher`, `followups` |

---

## How to add a ruling

1. Append the argument to `DECISIONS.md` under a dated `##` heading (why, alternatives rejected, evidence).
2. Add one row here with the date, the ruling in a sentence, and the file/test that enforces it.
3. If it changes behaviour, it needs a pin. A ruling with no test is a wish.
