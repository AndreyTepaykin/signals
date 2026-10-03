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
| M4 Frozen rules | done | see `git log` (commit after `048eb77`) |
| M5–M10 | pending | — |

Working tree clean. **951 tests pass.** DB `signals` at Alembic head `a41f7c05d2e9`. `RULE_VERSION = 'phase1-v3'`, `rule_hash = eb32a9d4140c5c55691d74d8c278adcb83860e3d68cf42d07ad8b3a7a3923dff`.

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

## Open

Per-coin options OI floor + “large expiry” (M5); independent sample = group first row vs mean (M8). M3.1 Ambiguity 1: the state is measured against the last confirmed swing, so a new swing that forms after the break can end it early. Revisit if replay shows it matters.

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

1. Run **M5 — options check** (Sonnet 5.5 high subagent, Bugbot review): Deribit ingest with `available_at` lag, the options verdict on `watch` + `pending_options_check` candidates, `stop_price` / `target_cap_price`, headwind cancel. Decide the per-coin OI floor and "large expiry", and how cancelled shadow rows with NULL options are handled. Read the M4 entry for the candidate price layers and status codes.
2. Then M6 labeler → M7 replay → M8 stats → M9 API/UI → M10 gate.

Local run (from `repo/api`):

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q
& '.\.venv\Scripts\python.exe' -m app.worker.backfill --coins BTCUSDT --timeframes 15m --days 1
& '.\.venv\Scripts\python.exe' -m app.worker   # live ingest
```
