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
