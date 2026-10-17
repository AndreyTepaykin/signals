"""Period report for the Elder Triple Screen: trades, summaries, baselines.

No database access. Bars that close after ``end`` are dropped before any
indicator is built, so a bar past the period cannot move a number. Censored
trades are listed with ``open_share`` and ``mark_r`` and are left out of E[R],
the R distribution, profit factor and drawdown.

The tuning objective is the lower bound of the 90% bootstrap interval of E[R]
on the whole window, and only when that window does 1 to 3 trades per week.
"""

from __future__ import annotations

import bisect
import math
import random
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from typing import Any, Callable

import numpy as np

from app.core.stats.summary import bootstrap_interval, max_drawdown, profit_factor
from app.research.elder.load import CoinData
from app.research.elder.params import ElderParams
from app.research.elder.resample import to_1w, to_6h
from app.research.elder.signals import (
    DROP_REASONS,
    Entry,
    SignalContext,
    build_context,
    generate_entries,
    stop_for,
    tide_at,
    tide_permits,
)
from app.research.elder.simulate import FundingPrint, Trade, simulate_trade

UTC = timezone.utc
WEEK = timedelta(days=7)
Z_90 = NormalDist().inv_cdf(0.95)
RANDOM_SEEDS = tuple(range(20))
HISTOGRAM_LABELS = ('<=-1', '(-1,0]', '(0,1]', '(1,2]', '(2,5]', '(5,10]', '>10')

_COUNT_KEYS = (
    'arms',
    'fills',
    'expired',
    'cancelled',
    'entries',
    *(f'dropped_{reason}' for reason in DROP_REASONS),
)


def _utc(instant: datetime) -> datetime:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError('timestamp must be timezone-aware')
    return instant.astimezone(UTC)


def _iso(instant: datetime) -> str:
    return _utc(instant).isoformat()


def _closed_index(close_times: Sequence[datetime], as_of: datetime) -> int | None:
    found = bisect.bisect_right(close_times, as_of) - 1
    return found if found >= 0 else None


def _zeros() -> dict[str, int]:
    return {key: 0 for key in _COUNT_KEYS}


def _sub_counts(full: Mapping[str, int], prefix: Mapping[str, int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in _COUNT_KEYS:
        value = int(full.get(key, 0)) - int(prefix.get(key, 0))
        if value < 0:
            raise RuntimeError(f'negative signal count {key}: {full.get(key)} - {prefix.get(key)}')
        out[key] = value
    return out


def _prefix(bars: Sequence[Any], open_before: datetime) -> list[Any]:
    cut = 0
    for bar in bars:
        if bar.open_time >= open_before:
            break
        cut += 1
    return list(bars[:cut])


# ---------------------------------------------------------------- period


@dataclass
class CoinResult:
    """One coin over the window passed to :func:`run_period`."""

    coin: str
    trades: list[Trade]
    dropped: list[Any]
    skipped: list[Entry]
    counts: dict[str, int]
    context: SignalContext = field(repr=False)
    funding: list[FundingPrint] = field(repr=False)
    full_counts: dict[str, int] = field(repr=False)
    signal_cache: dict[str, dict[str, int]] = field(default_factory=dict, repr=False)

    def counts_before(self, cut: datetime) -> dict[str, int]:
        """Signal counts on 15m bars with ``open_time < cut`` (not position-filtered)."""
        cut = _utc(cut)
        key = cut.isoformat()
        cached = self.signal_cache.get(key)
        if cached is not None:
            return dict(cached)
        bars = self.context.bars_15m
        prefix = _prefix(bars, cut)
        if len(prefix) == len(bars):
            counts = dict(self.full_counts)
        elif not prefix:
            counts = _zeros()
        else:
            sliced = replace(self.context, bars_15m=prefix)
            counts = dict(generate_entries(self.coin, sliced, self.context.params).counts)
        self.signal_cache[key] = counts
        return dict(counts)

    def signal_counts_between(self, start: datetime, end: datetime) -> dict[str, int]:
        return _sub_counts(self.counts_before(end), self.counts_before(start))


@dataclass
class PeriodResult:
    """Trades taken under the one-position rule, plus the signal funnel."""

    start: datetime
    end: datetime
    trades: list[Trade]
    dropped: list[Any]
    counts: dict[str, int]
    per_coin: dict[str, CoinResult]


def _prepare(coin: CoinData, end: datetime, params: ElderParams) -> tuple[SignalContext, list[FundingPrint]]:
    bars_15m = [bar for bar in coin.bars_15m if bar.close_time <= end]
    bars_1h = [bar for bar in coin.bars_1h if bar.close_time <= end]
    bars_4h = [bar for bar in coin.bars_4h if bar.close_time <= end]
    funding = [item for item in coin.funding if item.funding_time <= end]
    if bars_15m and bars_15m[-1].open_time >= end:
        raise RuntimeError(f'{coin.coin} has a 15m bar that opens at or after {end.isoformat()}')
    context = build_context(bars_15m, to_6h(bars_1h), to_1w(bars_4h), params)
    return context, funding


def _window_signal_counts(coin: str, ctx: SignalContext, start: datetime, end: datetime):
    """Counts for events on bars with ``start <= open_time`` and ``close_time <= end``.

    The context is already truncated at ``end``. Counts before ``start`` are
    subtracted. The walk is causal, so the prefix of the 15m series reproduces
    the prefix of the counts. Returned entries keep ``fill_index`` into ``ctx``.
    """
    full = generate_entries(coin, ctx, ctx.params)
    ctx_end = end.isoformat()
    if not ctx.bars_15m or ctx.bars_15m[0].open_time >= start:
        window = dict(full.counts)
        prefix_counts = _zeros()
    else:
        prefix_bars = _prefix(ctx.bars_15m, start)
        if len(prefix_bars) == len(ctx.bars_15m):
            window = _zeros()
            prefix_counts = dict(full.counts)
        else:
            prefix_counts = dict(generate_entries(coin, replace(ctx, bars_15m=prefix_bars), ctx.params).counts)
            window = _sub_counts(full.counts, prefix_counts)
    entries = [entry for entry in full.entries if start <= entry.fill_time < end]
    dropped = [item for item in full.dropped if start <= item.fill_time < end]
    if window['entries'] != len(entries):
        raise RuntimeError(f'{coin} window entries {window["entries"]} != {len(entries)} kept')
    drop_total = sum(window[f'dropped_{reason}'] for reason in DROP_REASONS)
    if window['fills'] != len(entries) + len(dropped) or drop_total != len(dropped):
        raise RuntimeError(f'{coin} window fill/drop counts do not match the kept rows')
    cache = {ctx_end: dict(full.counts), start.isoformat(): prefix_counts}
    return window, entries, dropped, dict(full.counts), cache


def _take_trades(
    entries: Sequence[Entry],
    ctx: SignalContext,
    params: ElderParams,
    funding: Sequence[FundingPrint],
    end: datetime,
) -> tuple[list[Trade], list[Entry]]:
    """One open position per coin. A fill at ``fill_index <= last_index`` is skipped."""
    taken: list[Trade] = []
    skipped: list[Entry] = []
    last_index = -1
    for entry in sorted(entries, key=lambda item: (item.fill_time, item.fill_index)):
        if entry.fill_index <= last_index:
            skipped.append(entry)
            continue
        trade = simulate_trade(entry, ctx, params, funding, end_time=end)
        taken.append(trade)
        last_index = trade.last_index
    return taken, skipped


def run_period(
    coin_data: Sequence[CoinData],
    params: ElderParams,
    start: datetime,
    end: datetime,
) -> PeriodResult:
    """Run one half-open window ``[start, end)`` over each coin.

    Higher-timeframe bars are built from source bars with ``close_time <= end``.
    Entries with ``fill_time`` outside the window are ignored. While a position
    is open, a later fill on that coin is counted as ``skipped_position_open``
    and is not simulated.
    """
    start = _utc(start)
    end = _utc(end)
    if start >= end:
        raise ValueError('start must be before end')
    per_coin: dict[str, CoinResult] = {}
    trades: list[Trade] = []
    dropped: list[Any] = []
    totals = _zeros()
    totals['skipped_position_open'] = 0
    for coin in coin_data:
        ctx, funding = _prepare(coin, end, params)
        window_counts, entries, coin_dropped, full_counts, cache = _window_signal_counts(coin.coin, ctx, start, end)
        coin_trades, skipped = _take_trades(entries, ctx, params, funding, end)
        if len(coin_trades) + len(skipped) != len(entries):
            raise RuntimeError(f'{coin.coin} position rule did not account for every window entry')
        window_counts['skipped_position_open'] = len(skipped)
        per_coin[coin.coin] = CoinResult(
            coin=coin.coin,
            trades=coin_trades,
            dropped=coin_dropped,
            skipped=skipped,
            counts=window_counts,
            context=ctx,
            funding=funding,
            full_counts=full_counts,
            signal_cache=cache,
        )
        trades.extend(coin_trades)
        dropped.extend(coin_dropped)
        for key, value in window_counts.items():
            totals[key] = totals.get(key, 0) + value
    return PeriodResult(start=start, end=end, trades=trades, dropped=dropped, counts=totals, per_coin=per_coin)


def subwindow_counts(period: PeriodResult, start: datetime, end: datetime) -> dict[str, int]:
    """Signal funnel of ``[start, end)`` plus skips whose fill falls in the window.

    Skips come from the position walk of the whole :func:`run_period` window, so
    a May fill blocked by a January position counts in May.
    """
    start = _utc(start)
    end = _utc(end)
    totals = _zeros()
    totals['skipped_position_open'] = 0
    for coin in period.per_coin.values():
        signal = coin.signal_counts_between(start, end)
        skipped = sum(1 for entry in coin.skipped if start <= entry.fill_time < end)
        for key, value in signal.items():
            totals[key] = totals.get(key, 0) + value
        totals['skipped_position_open'] += skipped
    return totals


# ---------------------------------------------------------------- summary


def wilson_interval(successes: int, n: int, *, z: float = Z_90) -> tuple[float, float] | None:
    """Wilson score interval. ``None`` when ``n`` is 0. Endpoints are clipped to [0, 1]."""
    if n <= 0:
        return None
    if successes < 0 or successes > n:
        raise ValueError(f'successes {successes} is outside 0..{n}')
    phat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (phat + z2 / (2.0 * n)) / denom
    margin = z * math.sqrt(phat * (1.0 - phat) / n + z2 / (4.0 * n * n)) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _proportion(flags: Sequence[bool]) -> dict[str, Any]:
    n = len(flags)
    k = sum(1 for flag in flags if flag)
    interval = wilson_interval(k, n)
    return {
        'k': k,
        'n': n,
        'p': None if n == 0 else k / n,
        'interval': None if interval is None else [interval[0], interval[1]],
    }


def _histogram(values: Sequence[float]) -> dict[str, int]:
    counts = {label: 0 for label in HISTOGRAM_LABELS}
    for value in values:
        if value <= -1.0:
            counts['<=-1'] += 1
        elif value <= 0.0:
            counts['(-1,0]'] += 1
        elif value <= 1.0:
            counts['(0,1]'] += 1
        elif value <= 2.0:
            counts['(1,2]'] += 1
        elif value <= 5.0:
            counts['(2,5]'] += 1
        elif value <= 10.0:
            counts['(5,10]'] += 1
        else:
            counts['>10'] += 1
    return counts


def _quantiles(values: Sequence[float]) -> dict[str, float | None]:
    keys = ('p10', 'p25', 'p50', 'p75', 'p90')
    if not values:
        return {key: None for key in keys}
    points = np.quantile(np.asarray(values, dtype=float), [0.10, 0.25, 0.50, 0.75, 0.90])
    return {key: float(point) for key, point in zip(keys, points)}


def _mean_closed(trades: Sequence[Trade]) -> float | None:
    values = [trade.total_r for trade in trades if not trade.censored]
    if not values:
        return None
    return float(math.fsum(values) / len(values))


def _funnel(counts: Mapping[str, int], n_trades: int) -> dict[str, int]:
    funnel = {key: int(counts.get(key, 0)) for key in _COUNT_KEYS}
    funnel['skipped_position_open'] = int(counts.get('skipped_position_open', 0))
    funnel['trades'] = n_trades
    return funnel


def summarize(
    trades: Sequence[Trade],
    start: datetime,
    end: datetime,
    counts: Mapping[str, int],
) -> dict[str, Any]:
    """One window: rates, E[R] on closed trades only, the funnel, the objective.

    ``objective`` is the 90% E[R] lower bound when ``1 <= trades_per_week <= 3``
    and the interval exists (at least two closed trades). Otherwise it is
    ``None`` and ``objective_feasible`` is false. The trial log uses this for
    the whole train across all coins; the same formula is reported on every
    slice so a sub-window can be read the same way.
    """
    start = _utc(start)
    end = _utc(end)
    if start >= end:
        raise ValueError('start must be before end')
    weeks = (end - start) / WEEK
    closed = [trade for trade in trades if not trade.censored]
    censored = [trade for trade in trades if trade.censored]
    ordered = sorted(closed, key=lambda trade: (trade.closed_time or trade.fill_time, trade.fill_time, trade.coin))
    values = [trade.total_r for trade in ordered]
    e_r = None if not values else float(math.fsum(values) / len(values))
    interval = bootstrap_interval(values, confidence=0.90)
    e_r_interval = None if interval is None else [float(interval[0]), float(interval[1])]
    pf, pf_note = profit_factor(values)
    hours = [
        (trade.closed_time - trade.fill_time).total_seconds() / 3600.0
        for trade in closed
        if trade.closed_time is not None
    ]
    n_closed = len(closed)
    n_censored = len(censored)
    per_week = (n_closed + n_censored) / weeks
    fills = int(counts.get('fills', 0))
    floor_drops = int(counts.get('dropped_stop_too_close_atr', 0)) + int(counts.get('dropped_costs_over_cap', 0))
    feasible = e_r_interval is not None and 1.0 <= per_week <= 3.0
    return {
        'n_closed': n_closed,
        'n_censored': n_censored,
        'weeks': weeks,
        'trades_per_week': per_week,
        'p_t1': _proportion([trade.hit_t1 for trade in closed]),
        'p_t2': _proportion([trade.hit_t2 for trade in closed]),
        'p_stop_before_t1': _proportion([trade.stopped_before_t1 for trade in closed]),
        'e_r': e_r,
        'e_r_interval': e_r_interval,
        'profit_factor': None if pf is None else float(pf),
        'profit_factor_note': pf_note,
        'max_drawdown': float(max_drawdown(values)),
        'r_quantiles': _quantiles(values),
        'r_histogram': _histogram(values),
        'hold_hours_mean': None if not hours else float(statistics.fmean(hours)),
        'hold_hours_median': None if not hours else float(statistics.median(hours)),
        'funnel': _funnel(counts, n_closed + n_censored),
        'min_distance_drop_rate': None if fills == 0 else floor_drops / fills,
        'censored_trades': [_censored_row(trade) for trade in censored],
        'objective': None if not feasible else e_r_interval[0],
        'objective_feasible': feasible,
    }


def _censored_row(trade: Trade) -> dict[str, Any]:
    return {
        'coin': trade.coin,
        'direction': trade.direction,
        'fill_time': _iso(trade.fill_time),
        'open_share': trade.open_share,
        'mark_r': trade.mark_r,
        'censor_reason': trade.censor_reason,
        'closed_part_r': trade.total_r,
    }


# ---------------------------------------------------------------- baselines


def no_trade_baseline() -> dict[str, Any]:
    """Do nothing. E[R] is 0 because no capital is at risk; there are no trades."""
    return {'name': 'no_trade', 'e_r': 0.0, 'n_closed': 0, 'n_censored': 0, 'trades_per_week': 0.0}


def pack_until_count(entries: Sequence[Any], target: int, rng: random.Random, simulate: Callable[[Any], Any]) -> list[Any]:
    """Draw entries uniformly (a shuffle) and pack them in time order.

    A prefix of the random permutation is grown until the time-ordered
    one-position pack has ``target`` trades, or the pool runs out. A candidate
    whose ``fill_index`` is at or before the previous taken trade's
    ``last_index`` is skipped. The result never exceeds ``target``.
    """
    if target <= 0 or not entries:
        return []
    order = list(entries)
    rng.shuffle(order)
    size = min(len(order), target)
    packed: list[Any] = []
    while True:
        packed = _pack_time_order(order[:size], target, simulate)
        if len(packed) >= target or size >= len(order):
            return packed
        size = min(len(order), size * 2)


def _pack_time_order(entries: Sequence[Any], target: int, simulate: Callable[[Any], Any]) -> list[Any]:
    taken: list[Any] = []
    last_index = -1
    for entry in sorted(entries, key=lambda item: (item.fill_time, item.fill_index)):
        if entry.fill_index <= last_index:
            continue
        trade = simulate(entry)
        taken.append(trade)
        last_index = trade.last_index
        if len(taken) >= target:
            break
    return taken


def _tide_entries(coin: str, ctx: SignalContext, params: ElderParams, start: datetime, end: datetime) -> list[Entry]:
    """15m bars in ``[start, end)`` where the tide permits its own direction, with a valid stop.

    The entry is at the bar's open. ``armed_time`` equals ``fill_time``. Wave and
    tide close times are the bars visible at that open. A dropped stop is not a
    candidate.
    """
    found: list[Entry] = []
    for index, bar in enumerate(ctx.bars_15m):
        if bar.open_time < start or bar.open_time >= end:
            continue
        tide, _impulse = tide_at(ctx, bar.open_time)
        if tide not in (1, -1) or not tide_permits(ctx, tide, bar.open_time):
            continue
        stop, _reason = stop_for(ctx, tide, bar.open, bar.open_time, params)
        if stop is None:
            continue
        slot = _closed_index(ctx.close_times_6h, bar.open_time)
        week = _closed_index(ctx.close_times_1w, bar.open_time)
        if slot is None or week is None:
            continue
        atr = ctx.atr_6h[slot]
        if atr is None:
            continue
        found.append(
            Entry(
                coin=coin,
                direction=tide,
                armed_time=bar.open_time,
                fill_time=bar.open_time,
                fill_index=index,
                fill_price=bar.open,
                stop=stop,
                r=abs(bar.open - stop),
                atr_6h=float(atr),
                wave_close_time=ctx.close_times_6h[slot],
                tide_close_time=ctx.close_times_1w[week],
            )
        )
    return found


def random_tide_baseline(
    period: PeriodResult,
    params: ElderParams,
    *,
    seeds: Sequence[int] = RANDOM_SEEDS,
) -> dict[str, Any]:
    """Random entries in the tide's direction, same stop and the same exits.

    Each seed draws candidates uniformly and packs them in time under the
    one-position rule, stopping at the number of trades the strategy took on
    that coin. E[R] is the mean ``total_r`` of closed trades across coins.
    Seeds are 0..19 unless the caller passes another sequence. The same seed
    repeats the same trades.
    """
    viable: dict[str, list[Entry]] = {}
    targets: dict[str, int] = {}
    caches: dict[str, dict[int, Trade]] = {}
    for name in sorted(period.per_coin):
        coin = period.per_coin[name]
        viable[name] = _tide_entries(name, coin.context, params, period.start, period.end)
        targets[name] = len(coin.trades)
        caches[name] = {}

    def simulate_cached(name: str, entry: Entry) -> Trade:
        cache = caches[name]
        hit = cache.get(entry.fill_index)
        if hit is None:
            coin = period.per_coin[name]
            hit = simulate_trade(entry, coin.context, params, coin.funding, end_time=period.end)
            cache[entry.fill_index] = hit
        return hit

    per_seed_e_r: list[float | None] = []
    per_seed_ids: list[list[list[Any]]] = []
    per_seed_counts: list[dict[str, int]] = []
    for seed in seeds:
        rng = random.Random(seed)
        seed_trades: list[Trade] = []
        counts: dict[str, int] = {}
        for name in sorted(viable):
            packed = pack_until_count(
                viable[name],
                targets[name],
                rng,
                lambda entry, coin_name=name: simulate_cached(coin_name, entry),
            )
            counts[name] = len(packed)
            seed_trades.extend(packed)
        per_seed_counts.append(counts)
        per_seed_e_r.append(_mean_closed(seed_trades))
        per_seed_ids.append(
            [[trade.coin, _iso(trade.fill_time), trade.direction, trade.fill_index, trade.last_index] for trade in seed_trades]
        )
    return _baseline_summary(per_seed_e_r, per_seed_ids, per_seed_counts, _mean_closed(period.trades), len(seeds))


def _baseline_summary(
    per_seed_e_r: Sequence[float | None],
    per_seed_ids: Sequence[list[list[Any]]],
    per_seed_counts: Sequence[Mapping[str, int]],
    strategy_e_r: float | None,
    n_seeds: int,
) -> dict[str, Any]:
    defined = [value for value in per_seed_e_r if value is not None]
    if strategy_e_r is None or n_seeds == 0:
        share = None
    else:
        share = sum(1 for value in per_seed_e_r if value is not None and value >= strategy_e_r) / n_seeds
    return {
        'name': 'random_tide',
        'n_seeds': n_seeds,
        'per_seed_e_r': list(per_seed_e_r),
        'per_seed_trade_ids': per_seed_ids,
        'per_seed_trade_counts': [dict(item) for item in per_seed_counts],
        'e_r_mean': None if not defined else float(statistics.fmean(defined)),
        'e_r_min': None if not defined else float(min(defined)),
        'e_r_median': None if not defined else float(statistics.median(defined)),
        'e_r_max': None if not defined else float(max(defined)),
        'share_ge_strategy': share,
        'strategy_e_r': strategy_e_r,
    }


# ---------------------------------------------------------------- report


def build_report(
    coin_data: Sequence[CoinData],
    params: ElderParams,
    start: datetime,
    end: datetime,
    *,
    subwindows: Sequence[tuple[str, datetime, datetime]] = (),
    seeds: Sequence[int] = RANDOM_SEEDS,
) -> dict[str, Any]:
    """Overall, per coin, sub-windows, and both baselines. JSON-ready."""
    period = run_period(coin_data, params, start, end)
    overall = summarize(period.trades, period.start, period.end, period.counts)
    per_coin = {
        name: summarize(coin.trades, period.start, period.end, coin.counts)
        for name, coin in period.per_coin.items()
    }
    windows: dict[str, Any] = {}
    for label, sub_start, sub_end in subwindows:
        clipped_start = max(period.start, _utc(sub_start))
        clipped_end = min(period.end, _utc(sub_end))
        if clipped_start >= clipped_end:
            continue
        sub_trades = [trade for trade in period.trades if clipped_start <= trade.fill_time < clipped_end]
        windows[label] = summarize(sub_trades, clipped_start, clipped_end, subwindow_counts(period, clipped_start, clipped_end))
    return {
        'start': _iso(period.start),
        'end': _iso(period.end),
        'params': params.to_dict(),
        'params_hash': params.params_hash(),
        'objective': overall['objective'],
        'objective_feasible': overall['objective_feasible'],
        'overall': overall,
        'per_coin': per_coin,
        'subwindows': windows,
        'baselines': {
            'no_trade': no_trade_baseline(),
            'random_tide': random_tide_baseline(period, params, seeds=seeds),
        },
    }


def _fmt(value: Any, spec: str) -> str:
    if value is None:
        return 'n/a'
    return format(value, spec)


def _interval(pair: Sequence[float] | None, spec: str = '+.3f') -> str:
    if not pair:
        return 'n/a'
    return f'[{format(pair[0], spec)}, {format(pair[1], spec)}]'


def _prop_line(label: str, block: Mapping[str, Any]) -> str:
    return f'{label} {_fmt(block.get("p"), ".3f")}  90% {_interval(block.get("interval"), ".3f")}  ({block.get("k")}/{block.get("n")})'


def _summary_lines(summary: Mapping[str, Any]) -> list[str]:
    funnel = summary['funnel']
    quantiles = summary['r_quantiles']
    histogram = summary['r_histogram']
    q = '  '.join(f'{name} {_fmt(quantiles[name], "+.2f")}' for name in ('p10', 'p25', 'p50', 'p75', 'p90'))
    hist = '  '.join(f'{name} {histogram[name]}' for name in HISTOGRAM_LABELS)
    lines = [
        f"closed {summary['n_closed']}  censored {summary['n_censored']}  "
        f"trades/week {_fmt(summary['trades_per_week'], '.2f')}  weeks {_fmt(summary['weeks'], '.2f')}",
        _prop_line('P(T1)', summary['p_t1']),
        _prop_line('P(10R)', summary['p_t2']),
        _prop_line('P(stop before T1)', summary['p_stop_before_t1']),
        f"E[R] {_fmt(summary['e_r'], '+.3f')}  90% {_interval(summary['e_r_interval'])}  "
        f"PF {_fmt(summary['profit_factor'], '.2f')}  max DD {_fmt(summary['max_drawdown'], '.2f')}R",
        f"hold hours mean {_fmt(summary['hold_hours_mean'], '.1f')}  median {_fmt(summary['hold_hours_median'], '.1f')}",
        f'R quantiles  {q}',
        f'R histogram  {hist}',
        'funnel  ' + '  '.join(f'{key} {funnel[key]}' for key in (
            'arms', 'fills', 'expired', 'cancelled', 'entries', 'skipped_position_open', 'trades',
        )),
        'drops  ' + '  '.join(f'{reason} {funnel[f"dropped_{reason}"]}' for reason in DROP_REASONS),
        f"min-distance drop rate {_fmt(summary['min_distance_drop_rate'], '.3f')}  "
        f"(stop_too_close_atr + costs_over_cap) / fills",
        f"slice objective {_fmt(summary['objective'], '+.3f')}  "
        f"feasible {'yes' if summary['objective_feasible'] else 'no'}",
    ]
    if summary['profit_factor_note']:
        lines.append(f"PF note  {summary['profit_factor_note']}")
    return lines


def render_markdown(report: Mapping[str, Any]) -> str:
    """Compact text report. The tuning objective is the whole-window one."""
    lines = [
        f"Elder Triple Screen  {report['start']} .. {report['end']}",
        f"params {report['params_hash'][:8]}  "
        f"objective {_fmt(report['objective'], '+.3f')}  "
        f"feasible {'yes' if report['objective_feasible'] else 'no'}",
        'Objective is the 90% E[R] lower bound on this window, all coins, '
        'and only when trades/week is between 1 and 3. Censored trades are excluded from E[R].',
        '',
        'Overall',
        *('  ' + line for line in _summary_lines(report['overall'])),
    ]
    subwindows = report.get('subwindows') or {}
    if subwindows:
        lines.append('')
        lines.append('Sub-windows')
        for label, summary in subwindows.items():
            lines.append(f'  {label}')
            lines.extend('    ' + line for line in _summary_lines(summary))
    lines.append('')
    lines.append('Per coin')
    for name, summary in report['per_coin'].items():
        lines.append(
            f"  {name}  closed {summary['n_closed']}  censored {summary['n_censored']}  "
            f"trades/week {_fmt(summary['trades_per_week'], '.2f')}  "
            f"P(T1) {_fmt(summary['p_t1']['p'], '.3f')}  "
            f"E[R] {_fmt(summary['e_r'], '+.3f')} {_interval(summary['e_r_interval'])}"
        )
    base = report['baselines']
    random_tide = base['random_tide']
    lines.extend([
        '',
        'Baselines',
        f"  no_trade  E[R] {_fmt(base['no_trade']['e_r'], '+.3f')}  trades 0",
        f"  random_tide  seeds {random_tide['n_seeds']}  "
        f"mean E[R] {_fmt(random_tide['e_r_mean'], '+.3f')}  "
        f"min {_fmt(random_tide['e_r_min'], '+.3f')}  "
        f"median {_fmt(random_tide['e_r_median'], '+.3f')}  "
        f"max {_fmt(random_tide['e_r_max'], '+.3f')}  "
        f"share >= strategy {_fmt(random_tide['share_ge_strategy'], '.2f')}",
    ])
    censored = report['overall']['censored_trades']
    lines.append('')
    lines.append(f'Censored ({len(censored)}; closed_part_r is not in E[R])')
    if not censored:
        lines.append('  none')
    for row in censored:
        lines.append(
            f"  {row['coin']} dir {row['direction']}  {row['fill_time']}  "
            f"open_share {_fmt(row['open_share'], '.2f')}  mark_r {_fmt(row['mark_r'], '+.3f')}  "
            f"{row['censor_reason']}  closed_part_r {_fmt(row['closed_part_r'], '+.3f')}"
        )
    return '\n'.join(lines) + '\n'


def render_side_by_side(train: Mapping[str, Any], holdout: Mapping[str, Any]) -> str:
    return '# Train\n\n' + render_markdown(train) + '\n# Holdout\n\n' + render_markdown(holdout)
