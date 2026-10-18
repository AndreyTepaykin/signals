# Handoff - Research: Elder Triple Screen (position)

Written 2026-10-17, before any code. Plan: `C:\Users\User\.cursor\plans\elder_triple_screen_research_aa55e9a1.plan.md`. Source idea: [`торговая_система_элдера.txt`](../../../../торговая_система_элдера.txt). Detailed contracts: [`BUILD-LOG.md`](BUILD-LOG.md). The Phase 1 signals loop has its own handoff in [`../../HANDOFF.md`](../../HANDOFF.md); this research does not change it.

## Goal

Test one new hypothesis offline: Elder's Triple Screen as a single **position** system on Binance USDT-M perps, 10 coins, no options. Tune parameters with an LLM on 2026-01-01 .. 2026-06-30 only, freeze them, then run once on 2026-07-01 .. the last closed bar. Paper only, no order route. Done when the holdout report exists next to the train report, with both baselines, and the result is written up here.

## Status

| Milestone | State | Commit |
|-----------|-------|--------|
| E0 Plan and owner decisions | done (docs only) | - |
| E1 History backfill (perp 4h, 2024-10-01 .. 2025-10-01) | done (21,900 rows, no gaps, 64 full weeks before train) | see git log |
| E2 Indicators and resampling (1h -> 6H, 4h -> 1W) | done (60 tests) | see git log |
| E3 Signals (tide 1W, wave 6H, ripple 15m, stop, min-distance filter) | done (109 tests, Bugbot clean) | see git log |
| E4 Simulator (T1 partial, breakeven, trailing, 10R cap, tide exit, costs, funding, censoring) | done (98 tests, Bugbot clean) | see git log |
| E5 Runner (read-only load, report, baselines, `trials.jsonl`, holdout lock) | done (smoke train: E[R] +0.33 at 6.3 trades/week, infeasible; random tide +0.28) | see git log |
| V2a Timeframes 30m / 4H / 1D in resample, signals, simulator | done (Bugbot clean) | see git log |
| V2b Runner: pyramiding up to 5 per coin, compounding 1% risk, ROI objective | done (smoke train, defaults: ROI -48%, max DD 75%, infeasible; random tide -31%) | see git log |
| E6 LLM tuning on Jan-Jun (budget 20 trials), freeze `frozen.json` | done (stopped after 5 trials; frozen trial 2: ROI +3.0%, lower bound -7.1%, DD 3.4%, 27 trades; no better than random tide) | see git log |
| E7 Holdout Jul-Oct, once; comparison report | pending | - |

## Rule change v2 (owner, 2026-10-18)

These replace the matching 2026-10-17 decisions below; everything not named here stays. The change was made after the E5 smoke train run and before any tuning trial or holdout run, so the holdout is still unseen.

- **Timeframes: ripple 30m, wave 4H, tide 1D.** 30m is built from stored perp 15m on :00 / :30 (2 bars); 4H is the stored perp 4h; 1D is built from 4h at 00:00 UTC (6 bars). An incomplete bar is not built. 1h and 6H and 1W are no longer used.
- **Resolution on 30m bars**, the ripple timeframe, so signals and the simulator share one bar series. The stop, the stop floor and the trailing stop use 4H bars and 4H ATR(14); a tide exit fires on a 1D reversal.
- **Up to 5 open trades per coin**, all in one direction, whose risk zones do not overlap: a new long is taken only if its initial stop is at or above the highest entry of the open longs on that coin (shorts mirrored). A fill against the open direction, a sixth trade or an overlapping one is skipped and counted.
- **Sizing: 1% of current equity at risk per trade, compounding.** Equity starts at 1.0 and moves only on realized part exits; a trade's risk is 1% of the equity at its fill. A part's P&L is risk x share x (price R - costs + funding). Open shares of censored trades are not in equity.
- **Success is ROI.** ROI = final realized equity - 1 over the period. Objective: the lower bound of the 90% interval of ROI over Jan-Jun (bootstrap by week), feasible only when the realized max drawdown is no worse than 20%. The 1-3 trades/week limit is dropped. E[R], P(T1) and P(10R) are still reported.
- **Parameter units follow the new bars:** `ripple_expiry_bars` counts 30m bars, `stop_lookback_bars` counts 4H bars, `trail_atr` and `min_stop_atr` use 4H ATR.

## Decisions locked (owner, 2026-10-17)

- **Isolation.** Code lives in `repo/api/app/research/elder/`, outside `app/core`, so it never enters `rule_hash`. It reads `candle` and `funding` only. The live worker, `candidate`, `label` and the Phase 1 stats stay untouched. Outputs go to files in `repo/api/research_out/elder/`.
- **The only DB write** is E1: historical perp 4h candles via the existing `app.worker.backfill` (`ON CONFLICT DO NOTHING`). Phase 1 feature windows never reach back past 2025-10-03, so nothing in Phase 1 moves.
- **Coins:** the first 10 of `DEFAULT_WATCHLIST`: BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK, TRX (USDT perps).
- **One system: position.** Tide 1W, wave 6H, ripple 15m. Swing was dropped by the owner.
- **Bars.** 1W built from 4h on Monday 00:00 UTC (42 bars, Binance convention); 6H built from 1h on 00/06/12/18 UTC; 15m used as stored. No daily bars are built. An incomplete bar (a hole inside it) is not built. A cold indicator is NULL, never zero.
- **Resolution on 15m bars**, the same timeframe as the Phase 1 labeler.
- **No options, no OI or funding votes.** Funding enters only as a cost (realized prints).
- **Exits: two targets, no time limit.** Part `t1_share` closes at T1 = `t1_r` x R (1 to 2 R); optional move to breakeven; the rest trails on 6H ATR(14), updated at each 6H close, with a hard cap at T2 = 10R; everything closes on a tide reversal. This replaces the original wish "TP > 10 x SL with P > 70%", which the owner accepted is not achievable together (a driftless walk reaches +10R before -1R about 1 time in 11).
- **Conventions copied from the Phase 1 labeler:** same-bar stop and target resolves to the stop; a gap through the stop fills at the open; 12 bps round trip charged on every closed part; one open position per coin.
- **Stop distance floor.** The stop sits beyond the extreme of the last K 6H bars plus `stop_atr_buffer` x 6H ATR(14). A signal is dropped when 1R is below `min_stop_atr` x 6H ATR or the costs exceed 0.1R, i.e. a stop under about 1.2% of price (the Phase 1 loop showed stops as close as 0.15 ATR, where fees ate 0.4R).
- **Tuning protocol.** An LLM subagent proposes `params.json` from the train report and the trial history; budget 20 trials; every trial is appended to `trials.jsonl` and never edited. Objective: the lower bound of the 90% interval of blended E[R], at 1 to 3 trades per week across all 10 coins. P(T1) >= 70% is a target to report, not a hard constraint.
- **Holdout runs once.** It needs `frozen.json` recorded in `trials.jsonl`; the first run writes a lock; a second run refuses without `--force` and a logged reason.

## Open

- Weekly tide flips only a few times in six months: the train result depends heavily on the regime. The "random entry with the weekly tide" baseline is there to show whether the wave and the ripple add anything.
- The screens are further apart than Elder's ~5x: 1W/6H is 28x, 6H/15m is 24x. A 6H wave fires often, so getting down to 1-3 trades a week falls on the oscillator threshold and the stop-distance floor.
- Sample size: about 26-78 trades in train and 15-45 in holdout. Intervals on P(T1) are about +/-12-15 points; conclusions are provisional.
- Trades have no time limit, so some holdout trades will be censored at the end of the data. They are reported separately and kept out of E[R].

## Key files (planned)

| Path | Role |
|------|------|
| `repo/api/app/research/elder/indicators.py` | EMA, MACD histogram, Force Index, Stochastic, Elder-ray, Impulse, ATR (pure) |
| `repo/api/app/research/elder/resample.py` | 1h -> 6H and 4h -> 1W, UTC boundaries, incomplete bars skipped |
| `repo/api/app/research/elder/signals.py` | Three screens, stop, min-distance filter |
| `repo/api/app/research/elder/simulate.py` | One trade on 15m bars: parts, trailing, cap, tide exit, costs, funding |
| `repo/api/app/research/elder/report.py`, `run.py` | Read-only load, report, baselines, trial log, holdout lock |
| `repo/api/app/research/elder/params_space.json` | Parameter bounds for the tuner |
| `repo/api/research_out/elder/` | `trials.jsonl`, `frozen.json`, holdout lock, reports |
| `repo/api/tests/test_elder_*.py` | Tests for every module above |

## Model plan

Main chat runs on **Opus 5.5 medium** as orchestrator: it briefs one subagent per milestone, reviews the diff, runs the full `pytest -q`, appends the milestone entry to `BUILD-LOG.md`, updates this file, and commits. It does not write milestone code itself. If Opus quota runs out, Sonnet 5.5 high takes over as orchestrator with the same table.

| Milestone | Implementer | Review | Why |
|-----------|-------------|--------|-----|
| E1 History backfill | none (Opus runs the existing CLI) | dry run first, then row counts per coin | No new code; one command and a coverage check |
| E2 Indicators and resampling | Grok 4.7 Higfh Fast | Opus diff review + golden-series tests | Textbook formulas; the risk is the 6H (00/06/12/18 UTC) and Monday bar boundaries and incomplete bars, which tests pin down |
| E3 Signals | Sonnet 5.5 high | Bugbot | Lookahead risk: a 15m bar may see only the last closed 6H and 1W bar; stop and min-distance logic feed every trade |
| E4 Simulator | Sonnet 5.5 high | Bugbot | The riskiest code: partial exits, breakeven, trailing update timing, 10R cap, tide exit, gaps, same-bar ambiguity, costs per part, funding, censoring. A mistake here silently inflates R |
| E5 Runner | Grok 4.7 Higfh Fast | Opus diff review | Plumbing over E2-E4 plus `bootstrap_interval`; the append-only `trials.jsonl` and the holdout lock get explicit tests |
| E6 LLM tuning | Fable 5.1 thinking high (tuner) | Opus checks the protocol after every trial | Needs slow reasoning about trade-offs, not code. A different model family from the implementers, so it does not "know" the code's weak spots. Fallback: GPT-5.6 Sol medium |
| E7 Holdout | none (Opus does it) | - | One run and a judgment call on train vs holdout; little code |

Codebase lookups during any milestone: `explore` subagent on Grok 4.7 Higfh Fast.

Subagents do not see the chat. Each brief names the milestone, points to this file, `BUILD-LOG.md` and the plan, lists the files in scope, and requires tests, a green full `pytest -q` and no commits (the orchestrator commits). Ask for the model by name, e.g. "Run E4 in a subagent using Sonnet 5.5 high."

**E6 tuner rules.** The tuner reads only the train reports, `params_space.json` and `trials.jsonl`; it never sees holdout output or the holdout code path. Each turn it returns one `params.json` with a short rationale; the orchestrator runs it, appends the trial and passes back the report. Stop at 20 trials or earlier if three trials in a row fail to raise the objective; the orchestrator picks `frozen.json` by the objective, not by the tuner's preference.

## Next step

E6 is done (see `BUILD-LOG.md`): frozen trial 2 has a negative ROI lower bound and does not beat `random_tide`. E7 waits for the owner: run the holdout once as planned, or stop here. Points 0-2 below are history.

0. V2a and V2b are done. Under v2 the defaults lose 48% with a 75% drawdown at about 20 trades a week (E[R] -0.07). The tuner's first job is to get the drawdown under 20%: fewer, better trades through the wave threshold, the stop floor and the tide mode. Point 2 below (1-3 trades/week) no longer applies.
1. E6: tuning. Trial 1 is `params_default.json` (from `repo/api`: `python -m app.research.elder.run --period train --params app/research/elder/params_default.json`). Then the tuner subagent (Fable 5.1 thinking high) proposes one `params.json` per turn from the train reports, `params_space.json` and `trials.jsonl` only. The orchestrator runs each one, checks the protocol, and stops at 20 trials or after three in a row without a better objective. It then freezes the best trial by objective with `--freeze --trial N`.
2. The defaults trade 6.3 times a week; the first job is getting to 1-3 a week through the wave threshold and the stop floor without losing the E[R] lower bound.
3. E7 holdout once, per the plan. Keep the live worker running meanwhile; this research does not need it stopped.
