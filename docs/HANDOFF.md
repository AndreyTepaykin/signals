# Handoff — Phase 1 signals loop

Written 2026-10-03. Spec: [`self-learning-crypto-signals.md`](../../self-learning-crypto-signals.md). Plan: `phase_1_signals_loop_cdbc8e73.plan.md`. Detailed contracts: [`BUILD-LOG.md`](BUILD-LOG.md).

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
| M4–M10 | pending | — |

Working tree clean. **800 tests pass.** DB `signals` at Alembic head `1c329301cc23`. `RULE_VERSION = 'phase1-v2'`, `rule_hash = 26ba07a110202114f8117f1be5a1551ded290c300dd2712325f3920a53256915`.

## Decisions locked

- Worker lives in `repo/api` (`app/core` + `app/worker`); one venv.
- Backfill-first for early labels; replay rows use `source='replay'`.
- ATR(14); OI-price quadrant = six states (keep price side in the dead zone).
- Models after Opus 5.5 quota: Sonnet 5.5 high + Bugbot on M5/M6/M8.
- `available_at` includes measured lag (OI +300s, funding +30s, kline settle 30s).
- UTC forced at DB connect time.
- Trend family is **stateless** (most recent decisive reading), so live and replay agree.
- **Engine params are in `rule_hash`.** `params.py` stays its own file. `hashing.frozen_engine_params()` lists its public UPPERCASE values explicitly under a separate `engine_params` key (`FUNDING_MAX_AGE` → `FUNDING_MAX_AGE_SECONDS` int, `PRODUCTION_POLICY` → dict of its fields). A guard test fails if `params.py` gains a name that isn't listed. `config.py` values (lags, settle, `WATCHLIST`) are NOT hashed.
- **Swing break = event + state, both stateless.** `swing_break_event`: the latest completed 4h close is beyond swing S, the previous 4h close was not, and this 15m bar's close is that 4h close. No grace window. `swing_break_state` (direction permission for the 15m triggers): the 4h close is beyond S and `chandelier_stop_distance_atr > 0`. It ends when a 4h close moves back through S or the distance is <= 0. `swing_break_direction` gives the side for both. Old column `swing_break` was renamed to `swing_break_state`.

## Open

Setup-family vocabulary and whether the `swing_break` trigger fires on the event or the state (M4); per-coin options OI floor + “large expiry” (M5); independent sample = group first row vs mean (M8). M3.1 Ambiguity 1: the state is measured against the last confirmed swing, so a new swing that forms after the break can end it early. Revisit if replay shows it matters.

## Key files

| Path | Role |
|------|------|
| `api/app/core/constants.py` | Frozen thresholds → `rule_hash` |
| `api/app/core/features/` | Pure feature math + `params.py` (also → `rule_hash`) |
| `api/app/core/hashing.py` | `feature_hash`, `rule_hash` (constants + engine params), `code_version` |
| `api/app/models/as_of.py` | Available-at read gate |
| `api/app/models/*.py` | 13 tables; no order/position tables |
| `api/app/worker/` | Ingest, backfill, `feature_job` |
| `api/alembic/` | Schema migrations |
| `docs/BUILD-LOG.md` | Per-milestone contracts |

## Next step

1. Run **M4 — frozen rules** (Sonnet 5.5 high): direction, efficiency gate, triggers, anti-noise, chandelier as 1R, 1-in-5 locked book. Freeze setup-family vocabulary. Read the M3.1 entry for the swing-break and `rule_hash` contracts.
2. Then M5 options check → M6 labeler → M7 replay → M8 stats → M9 API/UI → M10 gate.

Local run (from `repo/api`):

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q
& '.\.venv\Scripts\python.exe' -m app.worker.backfill --coins BTCUSDT --timeframes 15m --days 1
& '.\.venv\Scripts\python.exe' -m app.worker   # live ingest
```
