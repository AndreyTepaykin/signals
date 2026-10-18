# BUILD-LOG - Research: Elder Triple Screen

Handoff log for the Elder Triple Screen research. Each milestone agent **appends exactly one entry on completion**. Once E1 has started, earlier entries are never edited; corrections go in your own entry's Deviations. Before that (design stage), E0 is edited in place. Every agent **reads this file, [`HANDOFF.md`](HANDOFF.md) and the approved plan (`C:\Users\User\.cursor\plans\elder_triple_screen_research_aa55e9a1.plan.md`) before starting**, and treats the "Contracts frozen" sections as verbatim API.

This research is separate from the Phase 1 signals loop ([`../../BUILD-LOG.md`](../../BUILD-LOG.md)). It never imports into `app/core`, never changes `constants.py`, `params.py` or `rule_hash`, and never writes `candidate` or `label`. Paper only. There is no order endpoint.

Train window: 2026-01-01 .. 2026-06-30 UTC. Holdout window: 2026-07-01 .. the last closed bar. No agent reads holdout output before E7.

## Entry format

Copy this template to the end of the file and fill every heading (write "None" rather than deleting one).

```
## E<id> - <name>

- Date: <YYYY-MM-DD>
- Model: <model used>

### Files created or modified
- `path` - one-line purpose

### Contracts frozen
Exact names later milestones must use verbatim: module paths, function/class
signatures, parameter names in params.json, file names in research_out/elder,
CLI invocations, string values.

### Results
Numbers produced in this milestone (train only before E7), or "None".

### Deferred to a later milestone
- <item> -> E<n>

### Deviations from the plan
- <what changed from the plan or the brief, and why> (or "None")
```

Optional, when relevant: **Ambiguities** - places where Elder's description or the plan was unclear, quoted, with the choice made.

---

## E0 - Plan and owner decisions

- Date: 2026-10-17
- Model: Claude Opus 5.5 (orchestrator, docs only)

### Files created or modified
- `repo/docs/research/elder/HANDOFF.md` - goal, status, locked decisions, next step.
- `repo/docs/research/elder/BUILD-LOG.md` - this log and the E0 entry.

### Contracts frozen
- Package: `repo/api/app/research/elder/` (modules `indicators`, `resample`, `signals`, `simulate`, `report`, `run`). Outputs: `repo/api/research_out/elder/`.
- CLI: `python -m app.research.elder.run --period train --params <file>` and `python -m app.research.elder.run --period holdout --params frozen.json [--force --reason "<text>"]`.
- Files in `research_out/elder/`: `trials.jsonl` (append-only), `frozen.json`, `holdout.lock`.
- Coins: `BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT, DOGEUSDT, ADAUSDT, AVAXUSDT, LINKUSDT, TRXUSDT`.
- Timeframes: tide `1W`, wave `6H`, ripple and resolution `15m`.
- Bars: 1W from perp 4h on Monday 00:00 UTC (42 bars); 6H from perp 1h on 00/06/12/18 UTC (6 bars); 15m as stored. No 1D bars. A bar with a hole inside is not built.
- No-lookahead rule: a decision on 15m bar t sees only closed 15m bars, the last closed 6H bar and the last closed 1W bar.
- Stop: beyond the extreme of the last `stop_lookback_bars` 6H bars plus `stop_atr_buffer` x ATR(14) on 6H. Floor: 1R >= `min_stop_atr` x 6H ATR and costs <= 0.1R.
- Trailing: `trail_atr` x ATR(14) on 6H, updated at each 6H close. Tide exit fills at the open of the next 15m bar.
- Parameter names (bounds go in `params_space.json` in E5): `tide_mode` (`ema` | `macd` | `both`), `wave_osc` (`force2` | `stoch` | `elder_ray`), `wave_threshold`, `wave_block_impulse` (bool, Impulse on 6H), `ripple_expiry_bars` (15m bars), `stop_lookback_bars` (6H bars), `stop_atr_buffer`, `min_stop_atr`, `t1_r` (1.0 .. 2.0), `t1_share`, `be_after_t1` (bool), `trail_atr`. Fixed, not tuned: T2 = 10R, EMA 13, MACD 12/26/9, ATR 14 on 6H, costs 12 bps round trip, max costs 0.1R.
- Labeler conventions copied from Phase 1: same-bar stop and target -> stop; gap through the stop -> fill at the open; costs on every closed part; realized funding; one open position per coin.

### Results
None (no code yet).

Data checked on 2026-10-17: `candle` holds perp and spot 15m/1h/4h for 15 coins from 2025-10-01 03:00+03 to now; `funding` from 2025-10-03 for 15 coins. That gives about 13 weekly bars before 2026-01-01, short of the 35 a weekly MACD(12,26,9) needs, hence E1. The 6H wave needs no backfill: 1h from 2025-10-01 gives about 360 6H bars before 2026-01-01.

### Deferred to a later milestone
- Perp 4h backfill 2024-10-01 .. 2025-10-01 for the 10 coins -> E1.
- Everything else per the plan -> E2 .. E7.

### Deviations from the plan
None.

**Ambiguities**
- Owner wish "TP > 10 x SL, probability > 70%": not achievable together (a driftless walk hits +10R before -1R about 1 time in 11). Owner chose two targets (partial at T1 = 1-2R, the rest trailing to a 10R cap) and no time limit. P(T1) >= 70% stays a reported target, not a constraint.
- Elder's screens are "an order of magnitude" apart; owner set 1W / 6H / 15m. That is 28x (1W/6H) and 24x (6H/15m), against Elder's ~5x; kept as set, and the train report shows how many raw wave signals the threshold and the stop floor remove. Earlier drafts (swing 1D/4h/1h, position 3D/1D/4h, then 1W/1D/1h) were dropped.
- With costs capped at 0.1R, no stop under about 1.2% of price survives. On 6H stops this filter bites more often than on daily ones; its drop rate is in the report.
- "Pick the weights with an LLM": read as an LLM subagent proposing whole `params.json` files within `params_space.json`, scored on train only, budget 20, all trials logged.

## E1 - History backfill

- Date: 2026-10-17
- Model: Claude Opus 5.5 (orchestrator ran the existing CLI; no code)

### Files created or modified
- `repo/docs/research/elder/BUILD-LOG.md` - this entry.
- `repo/docs/research/elder/HANDOFF.md` - status and next step.
- Database: 21,900 new `candle` rows (perp, 4h, 10 coins). No other table touched.

### Contracts frozen
- Perp 4h history for the 10 coins starts at 2024-10-01 00:00 UTC and is continuous into the existing data. E2 may assume the first full weekly bar is the week of Monday 2024-10-07.
- Command run (from `repo/api`): `python -m app.worker.backfill --coins BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,LINKUSDT,TRXUSDT --timeframes 4h --markets perp --feeds klines --start 2024-10-01 --end 2025-10-01`, after the same command with `--dry-run`.

### Results
- Dry run: 0 stored, 21,900 expected and missing.
- Real run: 21,900 fetched and new, 0 already present, 0 unclosed, 0 malformed, 30 requests; 2,190 rows per coin, gaps 0 leading / 0 interior / 0 trailing.
- Coverage check in SQL, 2024-10-01 .. 2026-01-01 per coin: 2,742 of 2,742 4h bars, no step other than 4 hours (so the join at 2025-10-01 is seamless), last bar 2025-12-31 20:00 UTC, and 64 complete Monday weeks (42 bars each), 2024-10-07 .. 2025-12-22. Weekly MACD(12,26,9) needs 35, so it is warm well before the train window opens.

### Deferred to a later milestone
- Indicators and resampling -> E2.

### Deviations from the plan
None.

## E2 - Indicators and resampling

- Date: 2026-10-17
- Model: Grok 4.7 High Fast (implementer; the first run was cut off after writing the modules, a second run reviewed them and wrote the tests), Claude Opus 5.5 (diff review)

### Files created or modified
- `repo/api/app/research/__init__.py`, `repo/api/app/research/elder/__init__.py` - package markers.
- `repo/api/app/research/elder/resample.py` - `OHLCV`, 1h -> 6H and 4h -> 1W, `align`, `last_closed_index`.
- `repo/api/app/research/elder/indicators.py` - EMA, slope, MACD histogram, Force Index, Stochastic, Elder-ray, Impulse, Wilder ATR, `ohlcv_fields`.
- `repo/api/tests/test_elder_resample.py`, `repo/api/tests/test_elder_indicators.py` - 60 tests.

### Contracts frozen
- `app.research.elder.resample`: `OHLCV(open_time, close_time, open, high, low, close, volume)` (frozen dataclass, tz-aware UTC, `close_time` = exclusive end = `open_time + length`; the E5 loader converts Binance's close, which is 1 ms earlier). `STEP_15M`, `STEP_1H`, `STEP_4H`, `STEP_6H`, `STEP_1W`. `bucket_start_6h(t)`, `bucket_start_1w(t)`. `to_6h(bars_1h) -> list[OHLCV]`, `to_1w(bars_4h) -> list[OHLCV]`: a bucket is emitted only with all 6 (or 42) source bars on the grid, else skipped. `align(bars, step) -> list[OHLCV | None]`. `last_closed_index(bars, as_of) -> int | None` (`close_time <= as_of`).
- `app.research.elder.indicators`, every function returns a list as long as its input; `None` in -> `None` out and the indicator restarts; cold is `None`, a computed zero is `0.0`: `ema(values, period)` (SMA seed at run index `period - 1`), `slope(values)`, `macd_hist(closes, fast=12, slow=26, signal=9)` (first value at index 33), `force_index(closes, volumes, period=2)`, `stochastic(highs, lows, closes, period=5, smooth=3)` (slow %K; a flat window is `None` while inside the smoothing window, no reset), `elder_ray(highs, lows, closes, period=13) -> (bull, bear)`, `impulse(closes, ema_period=13, fast=12, slow=26, signal=9) -> list[int | None]` (+1 / -1 only when both slopes strictly agree, else 0), `wilder_atr(highs, lows, closes, period=14)` (first value at run index 14), `ohlcv_fields(bars) -> (opens, highs, lows, closes, volumes)`.
- No-lookahead primitive for E3: a decision on 15m bar t calls `last_closed_index(bars_6h_or_1w, t.open_time)`.

### Results
- Tests: 60 Elder tests pass. Full suite: 1730 passed, 1 failed. The failure is `tests/test_calendar_import.py::test_the_shipped_template_is_header_only`, caused by 52 uncommitted rows the owner added to `data/calendar/events_template.csv`; not E2 code.
- Data check in SQL, perp, 10 coins: 105 complete weeks per coin from 2024-10-07 to the last closed week, none partial; 1,518 complete 6H buckets per coin from 2025-10-01 06:00 UTC (the one partial bucket per coin was the query's own cut at 21:00 UTC); 15m from 2025-10-01 00:00 to 2026-10-17 14:15 UTC with no gaps.
- `wilder_atr` matches `app.core.features.volatility.wilder_atr_from_ranges` (same seed and recursion; cross-check test). Core's public `wilder_atr` returns one reading and treats a non-positive ATR as missing; this one returns the series and keeps 0.0.

### Deferred to a later milestone
- `last_closed_index` validates and rebuilds the close-time list on every call (O(n)). Calling it for every 15m bar is roughly 25k calls per coin; E3 should precompute the close times once or walk a pointer -> E3.

### Deviations from the plan
None.

**Ambiguities**
- Force Index: a hole in volume also leaves the next bar's raw value undefined (the previous bar is needed). Chosen: yes, it restarts.
- Elder-ray: a missing high or low, not only a missing close, restarts the EMA.

## E3 - Signals

- Date: 2026-10-17
- Model: Claude Sonnet 5.5 high (implementer), Bugbot (review: no bugs), Claude Opus 5.5 (diff review)

### Files created or modified
- `repo/api/app/research/elder/params.py` - fixed constants and `ElderParams`.
- `repo/api/app/research/elder/signals.py` - context, tide, wave, ripple order, stop and filters.
- `repo/api/tests/test_elder_params.py` (44 tests), `repo/api/tests/test_elder_signals.py` (65 tests).

### Contracts frozen
- `app.research.elder.params`: `T2_R = 10.0`, `EMA_PERIOD = 13`, `MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9`, `ATR_PERIOD = 14`, `STOCH_PERIOD = 5`, `STOCH_SMOOTH = 3`, `FORCE_PERIOD = 2`, `VOLUME_SMA = 20`, `COST_ROUND_TRIP = 0.0012`, `MAX_COST_R = 0.1`, `TIDE_MODES`, `WAVE_OSCS`. `ElderParams(tide_mode, wave_osc, wave_threshold, wave_block_impulse, ripple_expiry_bars, stop_lookback_bars, stop_atr_buffer, min_stop_atr, t1_r, t1_share, be_after_t1, trail_atr)`, frozen; `from_dict` (rejects unknown and missing keys), `to_dict`, `params_hash()` (sha256 of sorted, compact JSON; ints in float fields become floats, so `1` and `1.0` hash alike).
- `wave_threshold` units depend on `wave_osc`: `force2` - Force Index(2) / (6H ATR x SMA20 of 6H volume), long below `-threshold`; `stoch` - slow %K(5,3), long below `threshold`, short above `100 - threshold`; `elder_ray` - long when bear power < `-threshold` x ATR and rising, short mirrored on bull power.
- `app.research.elder.signals`: `build_context(bars_15m, bars_6h, bars_1w, params) -> SignalContext` (fields include `bars_15m`, `bars_6h` aligned with `None` holes, `close_times_6h`, `atr_6h`, `impulse_6h`, `wave_long`, `wave_short`, `bars_1w`, `close_times_1w`, `tide_dir_1w`, `impulse_1w`, `params`). `tide_at(ctx, as_of) -> (direction, impulse)`, `tide_permits(ctx, d, as_of)`, `wave_active(ctx, d, as_of)`, `stop_for(ctx, direction, fill_price, as_of, params) -> (stop | None, reason | None)`, `generate_entries(coin, ctx, params) -> SignalResult`. `Entry(coin, direction, armed_time, fill_time, fill_index, fill_price, stop, r, atr_6h, wave_close_time, tide_close_time)`, `Dropped(coin, direction, fill_time, fill_price, reason)`, `SignalResult(entries, dropped, counts)`. `DROP_REASONS`: `no_6h_context`, `stop_wrong_side`, `stop_too_close_atr`, `costs_over_cap` (checked in that order). Direction is `+1` / `-1`.
- Per-bar order of steps in `generate_entries`: cancel (tide or wave lost), expire, arm (only on the first 15m bar that sees a new 6H close), try fill. Fill when the bar trades strictly past the previous 15m bar's high (low), at `max(open, level)` (`min` for shorts); no level after a 15m hole. Expiry counts bars tried, including a bar with no level.
- Positions are not tracked: every fill is emitted; the E5 runner skips fills while a position on that coin is open. `generate_entries` raises if its tide or wave params differ from the context's.
- `Entry.wave_close_time` and `tide_close_time` are the 6H and 1W bars visible at the fill bar.

### Results
- Synthetic test data (62 weeks of seeded random-walk 15m bars, about 0.4% holes; EMA tide, stoch wave at 30, expiry 3): 229 arms, 186 fills, 43 expired, 0 cancelled, 185 entries, 1 dropped as `stop_too_close_atr`.
- No-lookahead: truncation invariance holds at about 49 cut times (mid-6H, mid-week, exact boundaries, next to fills) for three parameter variants; distorting the in-progress 6H bar or week changes nothing before its close; a deliberately injected one-hour lookahead was caught by the test.
- Full suite: 1839 passed, 1 failed (the owner's calendar CSV, as in E2).

### Deferred to a later milestone
- The E5 loader must give all three timeframes the same end; past the last 6H or 1W bar the last one stays "latest closed" -> E5.
- Fills while a position is open are skipped by the runner -> E5.
- Behaviour of the entry bar itself (the fill bar's range after the fill is unknown) -> E4.
- Cancels never occurred on the synthetic data; they are covered only by the hand-built tests. Check the real cancel count in the first train report -> E5.

### Deviations from the plan
- Added `params.py` (not in the E0 module list) to hold `ElderParams` and the fixed constants, so E4 and E5 import them from one place.

**Ambiguities**
- The weekly Impulse veto can never fire once the tide is set. In any `tide_mode`, a tide of `+1` means a rising EMA or histogram, and an Impulse of `-1` needs both falling. It only blocks through a cold (`None`) Impulse. It is kept as Elder wrote it.
- A 6H close that arrives while an order is pending is spent; it does not re-arm after a later fill or expiry.
- `force2` uses a volume SMA that includes the current 6H bar.

## E4 - Simulator

- Date: 2026-10-17
- Model: Claude Sonnet 5.5 high (implementer), Bugbot (review: no bugs), Claude Opus 5.5 (diff review)

### Files created or modified
- `repo/api/app/research/elder/simulate.py` - one trade on 15m bars: parts, breakeven, trailing, 10R cap, tide exit, costs, funding, censoring.
- `repo/api/tests/test_elder_simulate.py` - 98 tests, including a 1,500-trade randomized property test and a long/short mirror test.

### Contracts frozen
- `app.research.elder.simulate`: `FundingPrint(funding_time, rate, mark_price=None)`; `PartExit(share, price, time, reason, stop_kind, gapped, ambiguous)` with `reason` in `REASONS` = `stop`, `t1`, `t2`, `tide_exit` and `stop_kind` in `STOP_KINDS` = `initial`, `breakeven`, `trail` (stops only); `Trade(coin, direction, fill_time, fill_index, entry_price, initial_stop, r, exits, hit_t1, hit_t2, stopped_before_t1, censored, censor_reason, last_index, closed_time, price_r, cost_r, funding_r, total_r, open_share, mark_r, mae_r, mfe_r, bars_held)`; `CENSOR_REASONS` = `end_of_period`, `hole`.
- `simulate_trade(entry, ctx, params, funding, end_time) -> Trade`. Uses only 15m bars with `close_time <= end_time`. The runner frees the coin for fills with `fill_index > trade.last_index`.
- Fill bar: only the stop can trigger (ambiguous); no target credit. Later bars, at the open: breakeven and trailing updates, then tide exit (weekly direction `-d` or weekly Impulse `-d`; 0 or `None` does not exit), then a gap through the stop at the open. Intrabar: stop first (same bar as a target -> stop), else T1 at the T1 price, then T2 at the T2 price on the same bar. A gap past a target books at the target, never better.
- After T1, breakeven (if `be_after_t1`) and trailing start from the next bar. The first trailing update is at the next 6H close after that: highest high (lowest low) of bars before the current one minus `trail_atr` x 6H ATR at that close; never loosens.
- `cost_r` = share x `COST_ROUND_TRIP` x entry / R per closed part. `funding_r` counts prints with `fill_time < funding_time < part exit time`, signed, notional = mark price else entry. `total_r = price_r - cost_r + funding_r` over closed shares only; a censored trade's open share is reported as `open_share` and `mark_r` and gets no funding.

### Results
- Full suite: 1937 passed, 1 failed (the owner's calendar CSV, as in E2 and E3). Including the current bar's high in the trailing max (a deliberate lookahead) failed six tests.

### Deferred to a later milestone
- Censored trades must stay out of E[R] even though `total_r` holds their closed T1 part -> E5.
- Runner position rule (`fill_index > last_index`) -> E5.

### Deviations from the plan
None.

**Ambiguities**
- A tide-exit bar that opens past T1 or T2 books that part at the target price (`t1` / `t2`) and only the rest as `tide_exit`, so no part can exceed 10R.
- With `t1_share = 0` the T1 event still starts breakeven and trailing; with `t1_share = 1` there is no runner.
- The 6H close that is already visible on the bar where T1 fills does not count as a trailing update; the first update is the following 6H close.
- `mae_r` and `mfe_r` are non-negative magnitudes; a stop-out bar counts at the exit price.

## E5 - Runner

- Date: 2026-10-17
- Model: Grok 4.7 High Fast (implementer), Claude Opus 5.5 (diff review)

### Files created or modified
- `repo/api/app/research/elder/load.py` - read-only load of `candle` (perp 15m, 1h, 4h) and settled `funding`.
- `repo/api/app/research/elder/report.py` - period run, one position per coin, summaries, baselines, markdown.
- `repo/api/app/research/elder/run.py` - CLI: train, freeze, holdout.
- `repo/api/app/research/elder/params_space.json` - tuning bounds with unit notes; `params_default.json` - starting params.
- `repo/api/tests/test_elder_load.py`, `test_elder_report.py`, `test_elder_run.py`.

### Contracts frozen
- CLI (from `repo/api`): `python -m app.research.elder.run --period train --params <file> [--out-dir DIR] [--end ISO]`; `--freeze --trial N [--out-dir DIR]`; `--period holdout --params <out-dir>/frozen.json [--force --reason TEXT] [--out-dir DIR] [--end ISO]`. Exit codes: 0 ok, 2 bad usage or params outside `params_space.json`, 3 refusal (broken trial chain, existing `frozen.json`, holdout lock).
- Train is `[2026-01-01, 2026-07-01)` with sub-windows `jan_apr` `[01-01, 05-01)` and `may_jun` `[05-01, 07-01)`; a train `--end` past 2026-07-01 is refused, and train loads no bar closing after its end. Holdout is `[2026-07-01, end)`, end defaulting to the smallest latest perp 15m close across the 10 coins.
- Files in `research_out/elder/`: `trials.jsonl` (append-only; each line carries `trial_id` and `prev_sha256` of the previous line; the chain is verified before every append and freeze), `train_<NNN>_<hash8>.json/.md`, `frozen.json`, `holdout.lock`, `holdout_log.jsonl`, `holdout_<hash8>.json/.md`. Holdout never writes `trials.jsonl` (checked byte for byte after the run).
- Report fields per window, overall and per coin: `n_closed`, `n_censored`, `trades_per_week`, `p_t1`, `p_t2`, `p_stop_before_t1` (Wilson 90%), `e_r` with `e_r_interval` (bootstrap 90%, closed trades only), profit factor, max drawdown, R quantiles and histogram, hold hours, the signal funnel and the min-distance drop rate. `objective` = lower bound of the E[R] interval when 1 <= trades/week <= 3 on the whole train, else `None` (`objective_feasible`).
- Baselines: `no_trade`; `random_tide` = entries at the open of random 15m bars where the weekly tide permits, same stop and exits, one position per coin, matched to the strategy's trade count per coin, 20 seeds.

### Results
- Smoke train run on real data with `params_default.json` (45 s; output deleted, so `trials.jsonl` starts empty for E6):

| Window | Closed | Censored | Trades/week | P(T1) | P(10R) | E[R] (90%) |
|---|---:|---:|---:|---:|---:|---|
| Train | 155 | 7 | 6.27 | 0.503 | 0.071 | +0.329 [+0.089, +0.576] |
| Jan-Apr | 58 | 0 | 3.38 | 0.621 | 0.086 | +0.693 [+0.291, +1.102] |
| May-Jun | 97 | 7 | 11.93 | 0.433 | 0.062 | +0.111 [-0.162, +0.417] |

- Funnel (train): 757 arms, 749 fills, 8 expired, 0 cancelled; drops 9 wrong side, 214 too close, 130 costs over cap (min-distance drop rate 0.459); 396 entries, 234 skipped while a position was open, 162 trades. Objective not feasible (6.27 trades/week); its lower bound would be +0.089.
- `random_tide` mean E[R] +0.277; 25% of seeds reach the strategy's E[R].
- Full suite: 1961 passed, 1 failed (the owner's calendar CSV).

### Deferred to a later milestone
- Tuning to 1-3 trades/week and judging whether the wave and ripple beat `random_tide` -> E6.
- `holdout.lock` is written after the holdout report, not before; a crash between the two would leave no lock. Acceptable for a one-person run; E7 checks the lock exists right after the run -> E7.

### Deviations from the plan
- Added `load.py` and `params_default.json` (not in the E0 file list).

**Ambiguities**
- No cancels on real data: with `ripple_expiry_bars` 8 (2 hours), the order expires before the next 6H close can switch the wave off; only a weekly flip could cancel it. Cancels become possible once expiry exceeds 24 bars.
- The ripple filters almost nothing (749 of 757 armed orders fill). Selection falls on the wave threshold and the stop-distance floor, as the E0 open items predicted.
- The May-June rate (11.9 a week) is 3.5 times Jan-Apr's, so the trade count depends heavily on the regime.

## V2a - Timeframes 30m / 4H / 1D

- Date: 2026-10-18
- Model: Claude Sonnet 5.5 high (implementer), Bugbot (review: no bugs), Claude Opus 5.5 (diff review)
- Why: owner rule change v2 (see `HANDOFF.md`), made after the E5 smoke train run and before any tuning trial or holdout run.

### Files created or modified
- `repo/api/app/research/elder/resample.py` - `to_30m`, `to_1d`.
- `repo/api/app/research/elder/signals.py`, `simulate.py` - timeframe-neutral names, ripple 30m, wave 4H, tide 1D.
- `repo/api/app/research/elder/report.py`, `load.py` - build 30m from 15m and 1D from 4h, use 4h as the wave; 1h no longer loaded.
- `repo/api/app/research/elder/params_space.json`, `params_default.json`, `params.py` (docstring).
- `repo/api/tests/test_elder_*.py` - updated; new `to_30m` / `to_1d` boundary tests; truncation and in-progress mutation tests on 30m / 4H / 1D.

### Contracts frozen
- These supersede the matching E2-E5 names:
  - `resample`: `STEP_30M`, `STEP_1D`, `bucket_start_30m`, `bucket_start_1d`, `to_30m(bars_15m)` (2 bars on :00 / :30), `to_1d(bars_4h)` (6 bars from 00:00 UTC). `to_6h` and `to_1w` remain but are unused.
  - `signals`: `RIPPLE_STEP = STEP_30M`, `WAVE_STEP = STEP_4H`, `TIDE_STEP = STEP_1D`; `build_context(bars_ripple, bars_wave, bars_tide, params)`. `SignalContext` fields: `bars_ripple`, `bars_wave`, `close_times_wave`, `atr_wave`, `impulse_wave`, `wave_long`, `wave_short`, `bars_tide`, `close_times_tide`, `tide_dir`, `impulse_tide`, `params`.
  - `Entry.atr_6h` is now `atr_wave`; `fill_index` indexes `bars_ripple`.
  - Drop reason `no_6h_context` is now `no_wave_context`, with count key `dropped_no_wave_context`.
  - `CoinData` has no `bars_1h`.
- Simulator resolves on the 30m ripple bars. Trailing updates at each 4H close with 4H ATR; the extreme is still taken over ripple bars since the fill.
- `params_space.json`: `ripple_expiry_bars` int [1, 48] (30m bars), `stop_lookback_bars` int [1, 12] (4H bars). `params_default.json`: `ripple_expiry_bars` 4, `stop_lookback_bars` 3.

### Results
- Synthetic signals fixture: 497 arms, 413 fills, 84 expired, 0 cancelled, 406 entries. Full suite: 1989 passed, 1 failed (the owner's calendar CSV).

### Deferred to a later milestone
- Pyramiding (up to 5 per coin), compounding 1% risk, ROI objective and drawdown limit -> V2b.

### Deviations from the plan
- Timeframes changed by the owner (rule change v2).

## V2b - Pyramiding, compounding sizing, ROI objective

- Date: 2026-10-18
- Model: Grok 4.7 High Fast (implementer), Bugbot (review: no bugs), Claude Opus 5.5 (diff review of the pyramid rule and the equity run)

### Files created or modified
- `repo/api/app/research/elder/params.py` - `MAX_OPEN_PER_COIN = 5`, `RISK_PER_TRADE = 0.01`, `MAX_DRAWDOWN_LIMIT = 0.20`, `ROI_BOOTSTRAP_SEED = 20261018`, `ROI_BOOTSTRAP_RESAMPLES = 2000`.
- `repo/api/app/research/elder/simulate.py` - additive `PartExit.r` (net R of the part); every other output unchanged.
- `repo/api/app/research/elder/report.py`, `run.py` - pyramid rule, equity run, ROI, objective, baselines, trial record, markdown.
- `repo/api/tests/test_elder_params.py`, `test_elder_simulate.py`, `test_elder_report.py`, `test_elder_run.py`.

### Contracts frozen
- `report.apply_pyramiding(entries, simulate, *, max_open=MAX_OPEN_PER_COIN, limit=None)` is the only position rule, used by the strategy and `random_tide`. A taken trade is open for an entry while `last_index >= entry.fill_index`. Skips in order: `skipped_opposite`, `skipped_max_open`, `skipped_overlap` (long stop below the highest open long entry; touching allowed), summed as `skipped_total`.
- `report.run_equity(trades, start, end) -> EquityResult(equity, roi, max_drawdown_equity, max_open, mean_open, pnl_by_coin, max_open_by_coin, risk_by_id)`: equity starts at 1.0; each trade risks `RISK_PER_TRADE` x equity at its fill; a part exit adds risk x `part.r` at `part.time`; at equal times exits come before fills. Censored open shares never move equity.
- `report.roi_interval(trades, start, end)`: 7-day blocks from the window start (empty weeks included), trades by fill week, 2000 resamples, statistic `prod(1 + RISK_PER_TRADE x total_r) - 1`, 5th and 95th percentiles. It approximates concurrent compounding as sequential.
- `objective` = lower bound of `roi_interval` when `max_drawdown_equity <= 0.20`, else `None`. The trades/week condition is gone.
- `trials.jsonl` records: `objective`, `objective_feasible`, `roi`, `roi_interval`, `max_drawdown_equity`, `e_r`, `e_r_interval`, `n_closed`, `n_censored`, `trades_per_week`, `p_t1`, `p_t2`, `report`, plus the unchanged id, chain and params fields.
- Sub-windows run their own equity from 1.0 over trades filled inside them, including those trades' later exits.

### Results
- Smoke train run, v2 rules, `params_default.json` (43 s; output deleted, `trials.jsonl` still empty):

| Window | ROI | ROI 90% | Max DD | Closed | Trades/week | P(T1) | P(10R) | E[R] (90%) |
|---|---:|---|---:|---:|---:|---:|---:|---|
| Train | -48.1% | [-83.2%, +184.9%] | 75.3% | 505 | 19.7 | 0.384 | 0.036 | -0.074 [-0.187, +0.035] |
| Jan-Apr | -53.8% | [-81.5%, +94.0%] | 73.6% | 370 | 21.6 | 0.359 | 0.030 | -0.164 [-0.283, -0.037] |
| May-Jun | +12.5% | [-40.1%, +244.4%] | 21.2% | 135 | 15.8 | 0.452 | 0.052 | +0.171 [-0.071, +0.417] |

- Funnel (train): 1799 arms, 1644 fills, 155 expired, 0 cancelled, 856 entries; skipped 1 opposite, 0 max open, 347 overlap; 508 trades (3 censored). Max 23 trades open at once across the portfolio, 3.74 on average.
- `random_tide` mean ROI -31.3%; 80% of its seeds did at least as well as the strategy.
- Objective: not feasible on any window (drawdown above 20%).
- Full suite: 1998 passed, 1 failed (the owner's calendar CSV).

### Deferred to a later milestone
- Tuning under the v2 objective -> E6.
- `signals.py` docstrings still say "one position per coin" (the runner, not signals, enforces positions) -> fix when next touched.

### Deviations from the plan
- Rule change v2 (owner).

**Ambiguities**
- `PartExit.r` is filled in `simulate_trade` (the walk does not see funding).
- Exits sharing a timestamp are ordered by coin, fill index, part index; final equity does not depend on it, the drawdown path can.
- A censored trade counts as open until the window end in the open-trade average.
