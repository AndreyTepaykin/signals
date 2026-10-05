# Handoff — Phase 1 signals loop

Written 2026-10-03, updated 2026-10-05 after M10. Spec: [`self-learning-crypto-signals.md`](../../self-learning-crypto-signals.md). Plan: `C:\Users\User\.cursor\plans\phase_1_signals_loop_cdbc8e73.plan.md`. Detailed contracts: [`BUILD-LOG.md`](BUILD-LOG.md).

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

**1572 tests pass** (plus 9 Angular tests). DB `signals` at Alembic head `d8b2f6a31e05`: replay 2026-09-04 .. 2026-10-05 07:45 UTC, 15 coins, 862 candidates, 843 labels (19 awaiting bars); live candidates since bar 2026-10-05 07:45 UTC. `RULE_VERSION = 'phase1-v7'`, `rule_hash = 443d1f49564fb16fe3cfbf7e52d638d1c1c746faab2aaf3d00a8613f2389680d`. Live worker restarted 2026-10-05 07:53 UTC on `f124e80` (runs ingest, notify and `signal_pipeline`). Old v6 replay rows backed up in `backups/m10-v6-replay/`.

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

## Open

M3.1 Ambiguity 1: the state is measured against the last confirmed swing, so a new swing that forms after the break can end it early. Revisit if replay shows it matters. M6: a symbol Binance has moved to 1h or 4h funding is held to the 8h hole tolerance, so a single missed print there goes undetected; if M8 shows funding matters, read `GET /fapi/v1/fundingInfo` for per-symbol intervals. M10: the report's `would_abstain` check uses 0 R because the funding-band edge is not stored on the candidate.

## Key files

| Path | Role |
|------|------|
| `api/app/core/constants.py` | Frozen thresholds → `rule_hash` |
| `api/app/core/features/` | Pure feature math + `params.py` (also → `rule_hash`) |
| `api/app/core/hashing.py` | `feature_hash`, `rule_hash` (constants + engine params), `code_version` |
| `api/app/models/as_of.py` | Available-at read gate |
| `api/app/models/*.py` | 13 tables; no order/position tables |
| `api/app/core/rules/abstain.py` | Pure abstain rule (M10) |
| `api/app/worker/` | Ingest, backfill, `feature_job`, `rules_job`, `options_job`, `label_job`, `pipeline` (live signal job), `replay`, `stats_job`, `notify`, `stand_down` |
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

1. **M7 loose ends** (see the M7 entry in `BUILD-LOG.md`; harness, marker and the real replay run are done):
   - **Tardis fixtures.** Tardis hit its free data-transfer cap on 2026-10-04 at about 16:00 UTC (HTTP 429, code 277, retry after about 24h), so the next run is due after about 16:00 UTC on 2026-10-05 (not run during M9 or M10). After it resets, from `repo/api`: `& '.\.venv\Scripts\python.exe' scripts\tardis_options_fixtures.py --timeout 1200`, then `... --report`. Months already built are skipped, so rerun daily until all 12 exist (2025-10 and 2025-11 done, `652d711`; the cap allows about two months per day per IP). Do not probe the endpoint by hand: every probe counts against the cap.
   - **Threshold decision (owner).** From the report: do the OI floors (BTC 100k / ETH 400k) and the 5% large-expiry share behave sensibly on real books? 2 of 12 months so far show no reason to change (floors pass 3-5.6x; large expiry never trips because the expiry nearest the hold end is usually a daily, and first-of-month books rarely sit near a monthly expiry). Any change moves `rule_hash`, and the replay then has to be rerun (about 2 h): the labeler skips rows on an old hash.
   - **Replay rerun done 2026-10-05 on `phase1-v7`** (counts in the M10 entry). To label the 19 rows still awaiting bars, rerun the same range with a later `--label-at` (existing rows are skipped, about 2 h because every feature row is rechecked). After any hash change, back up and add `--purge-stale`, then run `stats_job --enrich-estimates`.
   - Options history policy stays as decided: no paid Tardis history; BTC/ETH replay rows without a book carry `options_ignored:no_options_history`, and M8 must never pool them with live rows that went through the options layer.
   - **Keep the live worker running** (`python -m app.worker`, or the Worker row in `signals-panel.ps1`). It now also decides every live bar; a bar missed while it is down is never decided, and every day down loses Deribit OI for good.
2. **Phase 1 exit (M10 verdict): not passed, not failed; waiting on live labels.** Labels and hashes: met. Reliability diagram: built, but empty until a pool reaches 30 (best 18). Expectancy: replay full-lookback alts alerts -0.052 R [-0.151, +0.045], n=54, interval includes flat and the naive baseline (-0.125 [-0.177, -0.073], n=134), so not beaten; thin-lookback +0.141 [+0.032, +0.247], n=70, not conclusive. Kill rule: 0 live labels of 300. Recheck with `python -m app.worker.stats_job --source live` (and `--source replay`) as live labels accumulate; the first live pools reach 30 after 30 independent live outcomes per edge-line cell, and the kill rule binds at 300.
3. **Owner:** set `NTFY_URL=https://ntfy.sh/<long random topic>` in `repo/api/.env` and subscribe in the ntfy app, then reload the worker (panel). Until then live alerts only log.
4. **Watch the live pipeline** for a day: `job_run` rows with `job_name='signal_pipeline'` should show one `ok` per 15-minute bar (hourly bars may take two ticks), `errors` empty, and live labels appearing once horizons resolve.

Local run (from `repo/api`):

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q
& '.\.venv\Scripts\python.exe' -m app.worker.backfill --coins BTCUSDT --timeframes 15m --days 1
& '.\.venv\Scripts\python.exe' -m app.worker   # live ingest
cd ..\web; npm start                             # dashboard on http://localhost:4200 (API on :8000)
```
