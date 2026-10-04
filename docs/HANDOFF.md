# Handoff — Phase 1 signals loop

Written 2026-10-03, updated 2026-10-04 after M7 code. Spec: [`self-learning-crypto-signals.md`](../../self-learning-crypto-signals.md). Plan: `phase_1_signals_loop_cdbc8e73.plan.md`. Detailed contracts: [`BUILD-LOG.md`](BUILD-LOG.md).

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
| M8–M10 | pending | — |

**1351 tests pass.** DB `signals` at Alembic head `d8b2f6a31e05`: replay 2026-09-04 .. 2026-10-04, 15 coins, 129,375 feature rows, 833 candidates, 817 labels (16 awaiting bars); no live candidates yet. `RULE_VERSION = 'phase1-v6'`, `rule_hash = ee25b0d73c1469bef61b85822264d47347adb43a9a090018eb8e151a70605c90`. Live worker restarted 2026-10-04 14:35 UTC after a gap from 2026-10-02 (it had not been running; Deribit snapshots start then).

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

## Open

Independent sample = group first row vs mean (M8). M3.1 Ambiguity 1: the state is measured against the last confirmed swing, so a new swing that forms after the break can end it early. Revisit if replay shows it matters. M6: a symbol Binance has moved to 1h or 4h funding is held to the 8h hole tolerance, so a single missed print there goes undetected; if M8 shows funding matters, read `GET /fapi/v1/fundingInfo` for per-symbol intervals. Labeler and rules jobs are not scheduled yet (M10).

## Key files

| Path | Role |
|------|------|
| `api/app/core/constants.py` | Frozen thresholds → `rule_hash` |
| `api/app/core/features/` | Pure feature math + `params.py` (also → `rule_hash`) |
| `api/app/core/hashing.py` | `feature_hash`, `rule_hash` (constants + engine params), `code_version` |
| `api/app/models/as_of.py` | Available-at read gate |
| `api/app/models/*.py` | 13 tables; no order/position tables |
| `api/app/worker/` | Ingest, backfill, `feature_job`, `rules_job`, `options_job`, `label_job`, `replay` |
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
   - **Tardis fixtures.** Tardis hit its free data-transfer cap on 2026-10-04 (HTTP 429, code 277, retry after about 24h). After it resets, from `repo/api`: `& '.\.venv\Scripts\python.exe' scripts\tardis_options_fixtures.py --timeout 1200`, then `... --report`. Months already built are skipped, so rerun daily until all 12 exist (2025-10 and 2025-11 done, `652d711`; the cap allows about two months per day per IP). Do not probe the endpoint by hand: every probe counts against the cap.
   - **Threshold decision (owner).** From the report: do the OI floors (BTC 100k / ETH 400k) and the 5% large-expiry share behave sensibly on real books? 2 of 12 months so far show no reason to change (floors pass 3-5.6x; large expiry never trips because the expiry nearest the hold end is usually a daily, and first-of-month books rarely sit near a monthly expiry). Any change moves `rule_hash`, and the replay then has to be rerun (about 2 h): the labeler skips rows on an old hash.
   - **Replay run done 2026-10-04** over the 30-day backfill (counts in the M7 entry). To label the 16 rows still awaiting bars, or to extend the range, rerun `python -m app.worker.replay --start ... --end ... --label-at ...` (existing rows are skipped).
   - Options history policy stays as decided: no paid Tardis history; BTC/ETH replay rows without a book carry `options_ignored:no_options_history`, and M8 must never pool them with live rows that went through the options layer.
   - **Keep the live worker running** (`python -m app.worker`). Its 5-minute Deribit poll builds options history for free, and every day it is down loses OI for good.
2. Then M8 stats (carries the M6 requirements: exclude `no_entry`, report ambiguous and `funding_partial` shares; and the M7 ones: keep `no_options_history` rows apart, and weigh the thin-lookback first ~20 days of replay; first look: 61% of replay labels time out) → M9 API/UI → M10 gate.

Local run (from `repo/api`):

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q
& '.\.venv\Scripts\python.exe' -m app.worker.backfill --coins BTCUSDT --timeframes 15m --days 1
& '.\.venv\Scripts\python.exe' -m app.worker   # live ingest
```
