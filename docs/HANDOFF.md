# Handoff — Phase 1 signals loop

Written 2026-10-03, updated 2026-10-14 after P2.0 (Phase 1 loose ends, Phase 2 prep). Spec: [`self-learning-crypto-signals.md`](../../self-learning-crypto-signals.md). Plan: `C:\Users\User\.cursor\plans\phase_1_signals_loop_cdbc8e73.plan.md`. Detailed contracts: [`BUILD-LOG.md`](BUILD-LOG.md).

## Goal

Private paper-only crypto signals loop on the Win10 laptop: ingest → features → frozen rules → options check → barrier labeler → dashboard. No order route in any phase. Phase 1 done when labels carry the three hashes, the reliability diagram hides bins under 20, and expectancy beats always-flat and the 4h swing-break baseline (or the kill rule has been followed).

## Status

| Milestone | State | Commit |
|-----------|--------|--------|
| M0 Foundations | done | `25aec17` |
| M1 Schema + `as_of` | done | `25aec17` |
| M2 Binance ingest | done | `25aec17` |
| M3 Feature engine | done | `dfc9ba9` |
| M3.1 Pre-M4 decisions | done | see `git log` (commit after `dfc9ba9`) |
| M4 Frozen rules | done | `3ba592a` |
| M5 Options check | done | `2994b6f` |
| M6 Barrier labeler | done | see `git log` (commit after `2994b6f`) |
| M7 Replay | done (Tardis threshold check 2 of 12 months) | `8b48f32` .. `fe226ed` + docs |
| M8 Stats | done | `840e8eb` |
| M8.1 Stats follow-ups | done | see `git log` (commit after `840e8eb`) |
| M9 API/UI | done | see `git log` (commit after `9a8cd14`) |
| M10 Gate | code done; Phase 1 exit waits on live labels | `f124e80` + docs |
| Tardis 12 months, Vision import, 2026 replay | done | `a7b6642` .. `c079942` |
| P2.0 Loose ends + Phase 2 prep (no model) | done | see `git log` (commit after `c079942`) |

**1670 tests pass** (plus 9 Angular tests). DB `signals` at Alembic head `e3f9a1c47b20`: replay 2026-01-01 .. 2026-10-12 14:30 UTC, 15 coins, 8,720 candidates, 8,705 labels (12 at the tail await bars); live candidates since bar 2026-10-05 07:45 UTC (156 on 2026-10-14; 59 independent alts alerts and 15 options-layer alerts of the 300 the kill rule needs). `RULE_VERSION = 'phase1-v7'`, `rule_hash = 443d1f49564fb16fe3cfbf7e52d638d1c1c746faab2aaf3d00a8613f2389680d`. Live worker runs ingest, notify and `signal_pipeline` (0 errors over 9 days). Old v6 replay rows backed up in `backups/m10-v6-replay/`.

## Decisions locked

- Worker lives in `repo/api` (`app/core` + `app/worker`); one venv.
- Backfill-first for early labels; replay rows use `source='replay'`.
- ATR(14); OI-price quadrant = six states (keep price side in the dead zone).
- Model plan: see **Model plan** below (Opus 5.5 medium orchestrates, subagents implement, Bugbot on M5/M6/M8).
- `available_at` includes measured lag (OI +300s, funding +30s, kline settle 30s).
- UTC forced at DB connect time.
- Trend family is **stateless** (most recent decisive reading), so live and replay agree.
- **Engine params are in `rule_hash`.** `params.py` stays its own file. `hashing.frozen_engine_params()` lists its public UPPERCASE values explicitly under a separate `engine_params` key (`FUNDING_MAX_AGE` → `FUNDING_MAX_AGE_SECONDS` int, `PRODUCTION_POLICY` → dict of its fields). A guard test fails if `params.py` gains a name that isn't listed. `config.py` values (lags, settle, `WATCHLIST`) are NOT hashed.
- **Swing break = event + state, both stateless.** `swing_break_event`: the latest completed 4h close is beyond swing S, the previous 4h close was not, and this 15m bar's close is that 4h close. No grace window. `swing_break_state` (direction permission for the 15m triggers): the 4h close is beyond S and `chandelier_stop_distance_atr > 0`. It ends when a 4h close moves back through S or the distance is <= 0. `swing_break_direction` gives the side for both. Old column `swing_break` was renamed to `swing_break_state`.
- **M4 rules.** `swing_break` trigger fires on the event; the state is only the direction permission. Setup families frozen (CHECK): `breakout`, `pullback`, `squeeze`, `reversion`, 1:1 from the trigger. Direction vote `MIN_NET = 0` (the break direction stands unless the opposing votes outnumber the confirming ones; owner-confirmed). Candidates are created `watch` + `pending_options_check`; `stop_price` holds the pre-options stop until M5 overwrites it. Locked book = sha256 of `{coin, bar_close}` mod 5. Full list in the M4 entry of `BUILD-LOG.md`.
- **M5 options.** OI floors owner-set: BTC 100,000, ETH 400,000 (coin units, about 30% of the 2026-10-03 books); confidence full at 2x the floor. With a stale, missing or thin book, BTC/ETH get `ignore` and stay `watch`; alts (no Deribit book) proceed. Deribit staleness does NOT stand the whole book down (owner-confirmed). Large expiry = nearest expiry >= 5% of total OI; near-dated = 72h. `expiry_pin` -> `delayed`; headwind with < 0.5R room -> `cancelled`. Full list in the M5 entry of `BUILD-LOG.md`.
- **M6 labeler.** Fill = open of the next perp 15m bar; `entry_price`, `one_r`, barriers and `vertical_deadline` are written once (guarded UPDATE + DB trigger `candidate_freeze_fill_values`). Every status except `pending_options_check` is labeled, live and replay, shadow included. **Labeler semantics are hashed** (`LABEL_*` in `constants.py`, guard test), and the job only labels candidates whose `rule_hash` equals the current one; others count as `stale_rule_hash`. Owner-level calls (made by the orchestrator, 2026-10-04):
  - Next open at/through the stop or through `target_cap_price` -> `first_barrier='no_entry'` label with every R NULL. M8 excludes these from expectancy and reports their rate.
  - Stop and target touched in one bar -> stop, `barrier_ambiguous=true`. M8 reports the ambiguous share; above 5%, revisit with on-demand Binance 1m klines for those bars only.
  - Gap through stop exits at the open (R below -1); gap through target exits at the target. Timeout exits at the deadline bar close.
  - Funding waits for complete prints (hole tolerance 8h + 5 min, the Binance default max); after a 24h stall the label is written with `funding_partial=true`.
  - Label-only context columns (`cmi_*`, `lead_venue*`, etc.) stay NULL; M8 fills them in a NULL -> value-once enrichment step if its stats need them.
  - Full list in the M6 entry of `BUILD-LOG.md`.

- **M8 stats.** Independent sample = first row of each `sample_group_id`. Every statistic runs inside one cohort `(source, options_path, lookback)`; mixing raises `MixedCohortError`, so `no_options_history` rows never pool with options-layer rows, and signal bars before 2026-09-24 form a separate `thin` lookback cohort. `no_entry` is out of every R statistic; ambiguous and `funding_partial` shares are reported. Every cell carries raw and independent counts and an `insufficient_sample` flag below 30 independent. Report: `python -m app.worker.stats_job [--source] [--json] [--out]`, read-only. Full list in the M8 entry of `BUILD-LOG.md`.
- **M8.1 (orchestrator-decided at the owner's request).** P(upper before stop) and the other estimates = the spec's pooled estimate: edge-line walk-up, >= 30 independent outcomes in 90 days or NULL; never pooled across horizon, source or options path; thin lookback and `no_entry` not pooled; all statuses pooled; point in time. A timeout is a miss for calibration. Estimates are written NULL -> value once by `rules_job` (alts), `options_job` (BTC/ETH) and `stats_job --enrich-estimates` (run it after any replay). Swing-break baseline = every 4h break event traded naively on the same stop, costs, barriers and horizons, through the labeler's shared pure core. Kill rule binds on live only; replay gets an advisory preview. Abstain rule -> M10 (needs a RULE_VERSION bump); it will test the lower bound of net expected R against the funding-band adverse edge, not the fees again. Full list in the M8.1 entry of `BUILD-LOG.md`.
- **M9 (orchestrator-decided at the owner's request).** API is GET-only plus `WS /ws`, guarded by `route_policy.audit_app_routes` (no order-like path, no request body, hidden routes included). Routers use sync READ ONLY sessions and reuse the M8 loaders. Stats cached per source until the label/estimate fingerprint changes (cold build about 20 s). Card hides every estimate below a 30-outcome pool (`edge_hidden`); the options line has no probability. Notification = ntfy from the worker on stand-down start/change/end and new live alerts; empty `NTFY_URL` logs only. Full list in the M9 entry of `BUILD-LOG.md`.
- **M10 (orchestrator).** Abstain: an `alert` with a pool of at least 30 abstains when the interval's lower bound of net expected R is below the funding-band adverse edge in R (0 when unknown; fees not charged again). Status `abstained`, reason `abstain:edge_below_cost`. The options job is the single live estimate path: estimate, abstain and verdict are written together, and an estimator failure defers the check. One scheduled job `signal_pipeline` (60 s) runs labels, features, rules and options for the latest closed bar; missed bars are not back-filled. Replay labels each bar before deciding it and refuses stale-hash replay rows unless run with `--purge-stale`. Full list in the M10 entry of `BUILD-LOG.md`.

- **P2.0 (orchestrator).** Options thresholds stay (12 Tardis months: floors pass 2.7-5.7x, large expiry trips only in Jan 2026). `would_abstain` re-derives the rule's funding edge from the stored feature row. `fundingInfo` is not needed (every watchlist symbol settled every 8 h all year; funding is about 0.004 R per trade). Phase 2 prep without a model: walk-forward harness (`app/core/learning`), shadow promotion gate (`app/core/promotion`) with the append-only `promotion_decision` log, and the calendar importer with unlock coverage. Full list in the P2.0 entry of `BUILD-LOG.md`.

## Open

- M3.1 Ambiguity 1: the state is measured against the last confirmed swing, so a new swing that forms after the break can end it early. Revisit if replay shows it matters.
- **Thin-lookback cutoff (owner).** `ESTIMATE_THIN_LOOKBACK_CUTOFF_UTC = 2026-09-24` (hashed) still marks every 2026 replay bar before it as `thin`, though the Vision import gave those bars a full feature window. 1,220 independent replay alts alerts therefore never feed pools, the kill-rule preview or a `full` fold set. Moving it changes `rule_hash` and needs the 36 h replay again (`--purge-stale` after a backup).
- **"Calendar loaded" (owner).** Any `event_calendar` row, an unlock included, flips `event_flag` from NULL to FALSE for every coin. Decide whether only stand-down kinds should count before loading unlocks; the importer refuses the first load without `--allow-event-flag-flip`.
- M1's `promotion_log` is superseded by `promotion_decision` and unused.

## Key files

| Path | Role |
|------|------|
| `api/app/core/constants.py` | Frozen thresholds → `rule_hash` |
| `api/app/core/features/` | Pure feature math + `params.py` (also → `rule_hash`) |
| `api/app/core/hashing.py` | `feature_hash`, `rule_hash` (constants + engine params), `code_version` |
| `api/app/models/as_of.py` | Available-at read gate |
| `api/app/models/*.py` | 13 tables; no order/position tables |
| `api/app/core/rules/abstain.py` | Pure abstain rule (M10) |
| `api/app/worker/` | Ingest, backfill, `vision_import`, `feature_job`, `rules_job`, `options_job`, `label_job`, `pipeline` (live signal job), `replay`, `stats_job`, `notify`, `stand_down`, `walk_forward_job`, `promotion_job`, `calendar_import` |
| `api/app/core/learning/` | Purged walk-forward harness and the 300-label fit guard (no model) |
| `api/app/core/promotion/` | Shadow promotion gate and its layers; log table `promotion_decision` |
| `api/data/calendar/` | Calendar CSV template and README (owner fills unlock, CPI, FOMC dates) |
| `signals-panel.ps1` (workspace root, not in the repo) | Start, stop and reload the API, web and worker |
| `api/app/routers/` | Read-only API (cards, tracker, stats, feed health, stand-down, `/ws`) + `route_policy` |
| `web/src/app/` | Angular dashboard |
| `api/scripts/tardis_options_fixtures.py` | Dev tool: historical Deribit books from Tardis free files |
| `api/alembic/` | Schema migrations |
| `docs/BUILD-LOG.md` | Per-milestone contracts |

## Model plan

Main chat runs on **Opus 5.5 medium** as orchestrator: it briefs one subagent per milestone, reviews the diff, runs the full test suite, updates `BUILD-LOG.md` and this file, and commits. It does not write the milestone code itself. If Opus quota runs out, Sonnet 5.5 high takes over as orchestrator with the same table.

| Milestone | Implementer subagent | Review | Why |
|-----------|----------------------|--------|-----|
| M4 Frozen rules | Sonnet 5.5 high | Opus diff review | Touches `rule_hash` and the swing-break contracts; mistakes freeze into every label |
| M5 Options check | Sonnet 5.5 high | Bugbot | New external data + `available_at` lag; open questions on OI floor / large expiry |
| M6 Barrier labeler | Sonnet 5.5 high | Bugbot | Lookahead risk; labels carry the three hashes |
| M7 Replay | Composer 2.5 fast | Opus diff review + live/replay parity tests | Mostly plumbing over existing worker and `as_of` gate |
| M8 Stats | Sonnet 5.5 high | Bugbot | Reliability bins, expectancy vs baselines, independent-sample choice |
| M9 API/UI | Composer 2.5 fast | Opus diff review | Routine endpoints and dashboard |
| M10 Gate | none (Opus does it) | — | Judgment call on the Phase 1 exit criteria; little code |

Codebase lookups during any milestone: `explore` subagent on Composer 2.5 fast.

Subagents don't see the chat. Each brief must name the milestone, point to this file, `BUILD-LOG.md` (and the M3.1 entry for M4+), list the files in scope, and require: tests added, full `pytest -q` green, no commits (the orchestrator commits). Ask for the model by name, e.g. "Run M4 in a subagent using Sonnet 5.5 high."

## Next step

1. **Keep the live worker running** (`python -m app.worker`, or the Worker row in `signals-panel.ps1`). It decides every live bar; a bar missed while it is down is never decided, and every day down loses Deribit OI for good. Phase 2 cannot start before the live cohorts reach 300 independent labels (about 6 weeks for alts at the current rate, months for BTC/ETH).
2. **Phase 1 exit: not passed, not failed; waiting on live labels.** Labels and hashes: met. Reliability diagram: built, empty until a pool reaches 30. Expectancy: the 2026 replay did not beat the naive break (numbers in the P2.0 entry). Kill rule: live 59 of 300 (alts), 15 of 300 (options layer). Recheck with `python -m app.worker.stats_job --source live`.
3. **Owner decisions** (see Open): the thin-lookback cutoff, what "calendar loaded" means, and confirm the options thresholds stay (12-month Tardis check found no reason to move them).
4. **Owner data:** fill `repo/api/data/calendar/events_template.csv` with unlock dates for the 13 alts (and CPI/FOMC when ready) from a trusted source, then `python -m app.worker.calendar_import --file ... --dry-run`, then the real run with the flags it asks for. Check with `--coverage`. Unlocks are required before alts leave the book in Phase 2.
5. **Owner:** set `NTFY_URL=https://ntfy.sh/<long random topic>` in `repo/api/.env` and subscribe in the ntfy app, then reload the worker (panel). Until then live alerts only log.
6. **Phase 2 tooling, read-only until the floor:** `python -m app.worker.walk_forward_job --source live` (fold sets and `fit allowed`), `python -m app.worker.promotion_job --all --source live` (shadow gate; add `--record` to log verdicts to `promotion_decision`). No model is fitted anywhere; LightGBM is not installed.
7. Replay tail: 12 replay candidates at 2026-10-12 await bars; a rerun with a later `--label-at` labels them (not worth the hours on its own). After any hash change, back up, rerun with `--purge-stale`, then `stats_job --enrich-estimates`.

Local run (from `repo/api`):

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q
& '.\.venv\Scripts\python.exe' -m app.worker.backfill --coins BTCUSDT --timeframes 15m --days 1
& '.\.venv\Scripts\python.exe' -m app.worker   # live ingest
cd ..\web; npm start                             # dashboard on http://localhost:4200 (API on :8000)
```
