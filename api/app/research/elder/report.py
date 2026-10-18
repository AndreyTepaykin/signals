"""Period report for the Elder Triple Screen: trades, summaries, baselines.

No database access. Bars that close after ``end`` are dropped before any
indicator is built, so a bar past the period cannot move a number. Censored
trades are listed with ``open_share`` and ``mark_r`` and are left out of E[R],
the R distribution, profit factor and the R-curve drawdown. Their open share
never moves equity; a closed part does.

Up to five trades may be open on one coin, all in one direction, with
non-overlapping risk zones. Portfolio equity starts at 1 and compounds
``RISK_PER_TRADE`` of equity at each fill, marked only on part exits.

The tuning objective is the lower bound of the 90% week-block bootstrap of
ROI when equity max drawdown is at most ``MAX_DRAWDOWN_LIMIT``. The bootstrap
compounds sampled weeks in a sequence; that approximates concurrent
compounding. There is no trades-per-week gate.
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
from app.research.elder.params import (
    MAX_DRAWDOWN_LIMIT,
    MAX_OPEN_PER_COIN,
    RISK_PER_TRADE,
    ROI_BOOTSTRAP_RESAMPLES,
    ROI_BOOTSTRAP_SEED,
    ElderParams,
)
from app.research.elder.resample import to_1d, to_30m
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
SKIP_REASONS = ('skipped_opposite', 'skipped_max_open', 'skipped_overlap')

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
    skipped: dict[str, list[Entry]]
    counts: dict[str, int]
    context: SignalContext = field(repr=False)
    funding: list[FundingPrint] = field(repr=False)
    full_counts: dict[str, int] = field(repr=False)
    signal_cache: dict[str, dict[str, int]] = field(default_factory=dict, repr=False)

    def counts_before(self, cut: datetime) -> dict[str, int]:
        """Signal counts on ripple bars with ``open_time < cut`` (not position-filtered)."""
        cut = _utc(cut)
        key = cut.isoformat()
        cached = self.signal_cache.get(key)
        if cached is not None:
            return dict(cached)
        bars = self.context.bars_ripple
        prefix = _prefix(bars, cut)
        if len(prefix) == len(bars):
            counts = dict(self.full_counts)
        elif not prefix:
            counts = _zeros()
        else:
            sliced = replace(self.context, bars_ripple=prefix)
            counts = dict(generate_entries(self.coin, sliced, self.context.params).counts)
        self.signal_cache[key] = counts
        return dict(counts)

    def signal_counts_between(self, start: datetime, end: datetime) -> dict[str, int]:
        return _sub_counts(self.counts_before(end), self.counts_before(start))


@dataclass
class PeriodResult:
    """Trades taken under the pyramid rule, plus the signal funnel."""

    start: datetime
    end: datetime
    trades: list[Trade]
    dropped: list[Any]
    counts: dict[str, int]
    per_coin: dict[str, CoinResult]


def _prepare(coin: CoinData, end: datetime, params: ElderParams) -> tuple[SignalContext, list[FundingPrint]]:
    bars_15m = [bar for bar in coin.bars_15m if bar.close_time <= end]
    bars_4h = [bar for bar in coin.bars_4h if bar.close_time <= end]
    funding = [item for item in coin.funding if item.funding_time <= end]
    if bars_15m and bars_15m[-1].open_time >= end:
        raise RuntimeError(f'{coin.coin} has a 15m bar that opens at or after {end.isoformat()}')
    context = build_context(to_30m(bars_15m), bars_4h, to_1d(bars_4h), params)
    return context, funding


def _window_signal_counts(coin: str, ctx: SignalContext, start: datetime, end: datetime):
    """Counts for events on bars with ``start <= open_time`` and ``close_time <= end``.

    The context is already truncated at ``end``. Counts before ``start`` are
    subtracted. The walk is causal, so the prefix of the ripple series reproduces
    the prefix of the counts. Returned entries keep ``fill_index`` into ``ctx``.
    """
    full = generate_entries(coin, ctx, ctx.params)
    ctx_end = end.isoformat()
    if not ctx.bars_ripple or ctx.bars_ripple[0].open_time >= start:
        window = dict(full.counts)
        prefix_counts = _zeros()
    else:
        prefix_bars = _prefix(ctx.bars_ripple, start)
        if len(prefix_bars) == len(ctx.bars_ripple):
            window = _zeros()
            prefix_counts = dict(full.counts)
        else:
            prefix_counts = dict(generate_entries(coin, replace(ctx, bars_ripple=prefix_bars), ctx.params).counts)
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


def _empty_skips() -> dict[str, list[Any]]:
    return {reason: [] for reason in SKIP_REASONS}


def _skip_reason(entry: Any, open_trades: Sequence[Any], max_open: int) -> str | None:
    """First matching pyramid skip, or None when the entry may be taken.

    An open trade is one the caller already decided is still open. Opposite
    direction is checked before the cap, and the cap before the risk-zone
    overlap. Touching zones are allowed: a long whose stop equals the highest
    open entry is taken, and a short whose stop equals the lowest open entry.
    """
    if any(trade.direction != entry.direction for trade in open_trades):
        return 'skipped_opposite'
    if len(open_trades) >= max_open:
        return 'skipped_max_open'
    if not open_trades:
        return None
    if entry.direction == 1:
        if entry.stop < max(trade.entry_price for trade in open_trades):
            return 'skipped_overlap'
    elif entry.stop > min(trade.entry_price for trade in open_trades):
        return 'skipped_overlap'
    return None


def apply_pyramiding(
    entries: Sequence[Any],
    simulate: Callable[[Any], Any],
    *,
    max_open: int = MAX_OPEN_PER_COIN,
    limit: int | None = None,
) -> tuple[list[Any], dict[str, list[Any]]]:
    """Walk entries in ``(fill_time, fill_index)`` order and take a pyramid.

    A taken trade is open for a new entry when ``trade.last_index >= entry.fill_index``.
    A trade that exits on the fill bar still counts as open. Skips are not
    simulated. ``limit`` stops once that many trades have been taken (the
    random-tide pack); further entries are neither taken nor counted as skips.

    This is the only pyramid rule. The strategy and ``random_tide`` both call it.
    """
    taken: list[Any] = []
    skipped = _empty_skips()
    ordered = sorted(entries, key=lambda item: (item.fill_time, item.fill_index))
    for entry in ordered:
        if limit is not None and len(taken) >= limit:
            break
        open_trades = [trade for trade in taken if trade.last_index >= entry.fill_index]
        reason = _skip_reason(entry, open_trades, max_open)
        if reason is not None:
            skipped[reason].append(entry)
            continue
        taken.append(simulate(entry))
    return taken, skipped


def _take_trades(
    entries: Sequence[Entry],
    ctx: SignalContext,
    params: ElderParams,
    funding: Sequence[FundingPrint],
    end: datetime,
) -> tuple[list[Trade], dict[str, list[Entry]]]:
    """Pyramid on one coin. Same function the random-tide baseline packs with."""

    def simulate(entry: Entry) -> Trade:
        return simulate_trade(entry, ctx, params, funding, end_time=end)

    return apply_pyramiding(entries, simulate)


def _add_skips(counts: dict[str, int], skipped: Mapping[str, Sequence[Any]]) -> None:
    total = 0
    for reason in SKIP_REASONS:
        count = len(skipped.get(reason, ()))
        counts[reason] = count
        total += count
    counts['skipped_total'] = total


def run_period(
    coin_data: Sequence[CoinData],
    params: ElderParams,
    start: datetime,
    end: datetime,
) -> PeriodResult:
    """Run one half-open window ``[start, end)`` over each coin.

    Higher-timeframe bars are built from source bars with ``close_time <= end``.
    Entries with ``fill_time`` outside the window are ignored. Fills that break
    the pyramid rule are counted under ``skipped_opposite``, ``skipped_max_open``
    or ``skipped_overlap`` and are not simulated.
    """
    start = _utc(start)
    end = _utc(end)
    if start >= end:
        raise ValueError('start must be before end')
    per_coin: dict[str, CoinResult] = {}
    trades: list[Trade] = []
    dropped: list[Any] = []
    totals = _zeros()
    _add_skips(totals, _empty_skips())
    for coin in coin_data:
        ctx, funding = _prepare(coin, end, params)
        window_counts, entries, coin_dropped, full_counts, cache = _window_signal_counts(coin.coin, ctx, start, end)
        coin_trades, skipped = _take_trades(entries, ctx, params, funding, end)
        kept = len(coin_trades) + sum(len(rows) for rows in skipped.values())
        if kept != len(entries):
            raise RuntimeError(f'{coin.coin} pyramid rule did not account for every window entry')
        _add_skips(window_counts, skipped)
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

    Skips come from the pyramid walk of the whole :func:`run_period` window, so
    a May fill blocked by a January trade counts in May.
    """
    start = _utc(start)
    end = _utc(end)
    totals = _zeros()
    _add_skips(totals, _empty_skips())
    for coin in period.per_coin.values():
        signal = coin.signal_counts_between(start, end)
        for key, value in signal.items():
            totals[key] = totals.get(key, 0) + value
        for reason in SKIP_REASONS:
            count = sum(1 for entry in coin.skipped.get(reason, ()) if start <= entry.fill_time < end)
            totals[reason] += count
            totals['skipped_total'] += count
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
    for reason in SKIP_REASONS:
        funnel[reason] = int(counts.get(reason, 0))
    funnel['skipped_total'] = int(counts.get('skipped_total', sum(funnel[reason] for reason in SKIP_REASONS)))
    funnel['trades'] = n_trades
    return funnel


def _week_count(start: datetime, end: datetime) -> int:
    """Consecutive 7-day blocks from ``start`` covering ``[start, end)``, including a short tail."""
    span = end - start
    count = int(span / WEEK)
    if start + count * WEEK < end:
        count += 1
    return count


def _week_index(fill: datetime, start: datetime, n_weeks: int) -> int:
    index = int((fill - start) / WEEK)
    if index < 0 or index >= n_weeks:
        raise RuntimeError(f'fill {fill.isoformat()} is outside week blocks of [{start.isoformat()}, +{n_weeks}w)')
    return index


def roi_interval(trades: Sequence[Trade], start: datetime, end: datetime) -> list[float] | None:
    """90% block-bootstrap interval of sequential ROI.

    Weeks are consecutive 7-day blocks from ``start``. Empty weeks are in the
    pool, and so is a short final block. Each trade is assigned to the week of
    its ``fill_time``. A resample draws that many weeks with replacement
    (``ROI_BOOTSTRAP_RESAMPLES`` draws, seed ``ROI_BOOTSTRAP_SEED``). The
    statistic is ``product(1 + RISK_PER_TRADE * trade.total_r) - 1`` over the
    sampled weeks' trades. ``total_r`` already covers closed shares only, so a
    censored trade contributes its closed parts and not its open share.

    Multiplying those factors in a sequence approximates the concurrent
    compounding of the equity path, where several open trades share one clock.
    The interval is the 5th and 95th percentiles of the resamples.
    """
    start = _utc(start)
    end = _utc(end)
    n_weeks = _week_count(start, end)
    if n_weeks <= 0:
        return None
    buckets: list[list[Trade]] = [[] for _ in range(n_weeks)]
    for trade in trades:
        if start <= trade.fill_time < end:
            buckets[_week_index(trade.fill_time, start, n_weeks)].append(trade)
    rng = np.random.default_rng(ROI_BOOTSTRAP_SEED)
    draws = rng.integers(0, n_weeks, size=(ROI_BOOTSTRAP_RESAMPLES, n_weeks))
    stats = np.empty(ROI_BOOTSTRAP_RESAMPLES, dtype=float)
    for index, draw in enumerate(draws):
        factor = 1.0
        for week_i in draw:
            for trade in buckets[int(week_i)]:
                factor *= 1.0 + RISK_PER_TRADE * trade.total_r
        stats[index] = factor - 1.0
    low, high = np.quantile(stats, [0.05, 0.95])
    return [float(low), float(high)]


@dataclass(frozen=True)
class EquityResult:
    """Realized equity path of one window. ``risk_by_id`` keys are ``id(trade)``."""

    equity: float
    roi: float
    max_drawdown_equity: float
    max_open: int
    mean_open: float
    pnl_by_coin: dict[str, float]
    max_open_by_coin: dict[str, int]
    risk_by_id: dict[int, float]


def _occupancy(trades: Sequence[Trade], start: datetime, end: datetime) -> tuple[int, float, dict[str, int]]:
    """Max and time-weighted mean of trades open on ``[start, end)``.

    A trade occupies ``[fill_time, closed_time)``. A censored trade, and any
    trade with no close time, stays open until ``end``. Closes at the same
    timestamp are applied before opens, so a trade that ends as another fills
    does not overlap it. A fill and a close at the same timestamp add nothing
    to the average.
    """
    events: list[tuple[datetime, int, str]] = []
    for trade in trades:
        raw_close = end if trade.censored or trade.closed_time is None else trade.closed_time
        open_at = trade.fill_time if trade.fill_time > start else start
        close_at = raw_close if raw_close < end else end
        if open_at >= close_at:
            continue
        events.append((open_at, 1, trade.coin))
        events.append((close_at, -1, trade.coin))
    events.sort(key=lambda item: (item[0], item[1], item[2]))
    count = 0
    by_coin: dict[str, int] = {}
    max_count = 0
    max_by: dict[str, int] = {}
    integral = 0.0
    prev = start
    for time, delta, coin in events:
        if time > prev:
            integral += count * (time - prev).total_seconds()
            prev = time
        count += delta
        by_coin[coin] = by_coin.get(coin, 0) + delta
        if count > max_count:
            max_count = count
        current = by_coin[coin]
        if current > max_by.get(coin, 0):
            max_by[coin] = current
    if end > prev:
        integral += count * (end - prev).total_seconds()
    duration = (end - start).total_seconds()
    mean = 0.0 if duration <= 0.0 else integral / duration
    return max_count, mean, max_by


def run_equity(trades: Sequence[Trade], start: datetime, end: datetime) -> EquityResult:
    """Compound ``RISK_PER_TRADE`` of equity at each fill. Equity starts at 1.

    Events are processed in time order across coins. At one timestamp every
    part exit is applied before any fill, so a fill sees equity after the
    exits that share its time. Among exits at the same timestamp the order is
    coin, then fill index, then part index: the final equity does not depend
    on that order, the path drawdown can.

    A part exit adds ``risk_amount * part.r`` at ``part.time``. The open share
    of a censored trade has no exit and does not move equity. ``roi`` is the
    final equity minus 1. ``max_drawdown_equity`` is the largest
    ``(peak - equity) / peak`` along that path, peak included.
    """
    start = _utc(start)
    end = _utc(end)
    events: list[tuple[datetime, int, str, int, int, int, Trade]] = []
    for trade_i, trade in enumerate(trades):
        events.append((trade.fill_time, 1, trade.coin, trade.fill_index, trade_i, -1, trade))
        for part_i, _part in enumerate(trade.exits):
            events.append((_part.time, 0, trade.coin, trade.fill_index, trade_i, part_i, trade))
    events.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4], item[5]))
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    risk_by_id: dict[int, float] = {}
    pnl_terms: dict[str, list[float]] = {}
    for _time, kind, _coin, _fill_index, trade_i, part_i, trade in events:
        if kind == 1:
            risk_by_id[id(trade)] = RISK_PER_TRADE * equity
            continue
        risk = risk_by_id.get(id(trade))
        if risk is None:
            raise RuntimeError(f'{trade.coin} part exit at {_time.isoformat()} has no fill yet')
        part = trade.exits[part_i]
        delta = risk * part.r
        equity += delta
        pnl_terms.setdefault(trade.coin, []).append(delta)
        if equity > peak:
            peak = equity
        if peak > 0.0:
            max_dd = max(max_dd, (peak - equity) / peak)
    max_open, mean_open, max_by = _occupancy(trades, start, end)
    return EquityResult(
        equity=equity,
        roi=equity - 1.0,
        max_drawdown_equity=max_dd,
        max_open=max_open,
        mean_open=mean_open,
        pnl_by_coin={coin: float(math.fsum(terms)) for coin, terms in pnl_terms.items()},
        max_open_by_coin=max_by,
        risk_by_id=risk_by_id,
    )


def summarize(
    trades: Sequence[Trade],
    start: datetime,
    end: datetime,
    counts: Mapping[str, int],
) -> dict[str, Any]:
    """One window: rates, E[R] on closed trades, equity ROI, the funnel, the objective.

    Equity is its own run from 1.0 on ``trades`` (a sub-window passes only the
    trades whose fill is inside it, including part exits after the window).
    ``objective`` is the lower bound of :func:`roi_interval` when
    ``max_drawdown_equity <= MAX_DRAWDOWN_LIMIT``. Otherwise it is ``None``
    and ``objective_feasible`` is false. The trial log uses the whole-train
    figure; every slice reports the same formula.
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
    equity = run_equity(trades, start, end)
    roi_band = roi_interval(trades, start, end)
    feasible = roi_band is not None and equity.max_drawdown_equity <= MAX_DRAWDOWN_LIMIT
    return {
        'n_closed': n_closed,
        'n_censored': n_censored,
        'n_trades': n_closed + n_censored,
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
        'roi': equity.roi,
        'roi_interval': roi_band,
        'max_drawdown_equity': equity.max_drawdown_equity,
        'max_open': equity.max_open,
        'mean_open': equity.mean_open,
        'r_quantiles': _quantiles(values),
        'r_histogram': _histogram(values),
        'hold_hours_mean': None if not hours else float(statistics.fmean(hours)),
        'hold_hours_median': None if not hours else float(statistics.median(hours)),
        'funnel': _funnel(counts, n_closed + n_censored),
        'min_distance_drop_rate': None if fills == 0 else floor_drops / fills,
        'censored_trades': [_censored_row(trade) for trade in censored],
        'objective': None if not feasible else roi_band[0],
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
    """Do nothing. Equity stays at 1, so ROI and drawdown are 0. E[R] is 0."""
    return {
        'name': 'no_trade',
        'e_r': 0.0,
        'roi': 0.0,
        'max_drawdown_equity': 0.0,
        'n_closed': 0,
        'n_censored': 0,
        'trades_per_week': 0.0,
    }


def pack_until_count(entries: Sequence[Any], target: int, rng: random.Random, simulate: Callable[[Any], Any]) -> list[Any]:
    """Draw entries uniformly (a shuffle) and pack them with :func:`apply_pyramiding`.

    A prefix of the random permutation is grown until the pyramid has ``target``
    trades, or the pool runs out. The result never exceeds ``target``.
    """
    if target <= 0 or not entries:
        return []
    order = list(entries)
    rng.shuffle(order)
    size = min(len(order), target)
    packed: list[Any] = []
    while True:
        packed, _skipped = apply_pyramiding(order[:size], simulate, limit=target)
        if len(packed) >= target or size >= len(order):
            return packed
        size = min(len(order), size * 2)


def _tide_entries(coin: str, ctx: SignalContext, params: ElderParams, start: datetime, end: datetime) -> list[Entry]:
    """Ripple bars in ``[start, end)`` where the tide permits its own direction, with a valid stop.

    The entry is at the bar's open. ``armed_time`` equals ``fill_time``. Wave and
    tide close times are the bars visible at that open. A dropped stop is not a
    candidate.
    """
    found: list[Entry] = []
    for index, bar in enumerate(ctx.bars_ripple):
        if bar.open_time < start or bar.open_time >= end:
            continue
        tide, _impulse = tide_at(ctx, bar.open_time)
        if tide not in (1, -1) or not tide_permits(ctx, tide, bar.open_time):
            continue
        stop, _reason = stop_for(ctx, tide, bar.open, bar.open_time, params)
        if stop is None:
            continue
        slot = _closed_index(ctx.close_times_wave, bar.open_time)
        tide_slot = _closed_index(ctx.close_times_tide, bar.open_time)
        if slot is None or tide_slot is None:
            continue
        atr = ctx.atr_wave[slot]
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
                atr_wave=float(atr),
                wave_close_time=ctx.close_times_wave[slot],
                tide_close_time=ctx.close_times_tide[tide_slot],
            )
        )
    return found


def random_tide_baseline(
    period: PeriodResult,
    params: ElderParams,
    *,
    seeds: Sequence[int] = RANDOM_SEEDS,
) -> dict[str, Any]:
    """Random entries in the tide's direction, same stop, exits, pyramid and equity.

    Each seed draws candidates uniformly and packs them with
    :func:`apply_pyramiding`, stopping at the number of trades the strategy
    took on that coin. ROI uses the same compounding equity run. E[R] is the
    mean ``total_r`` of closed trades across coins. Seeds are 0..19 unless the
    caller passes another sequence. The same seed repeats the same trades.
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

    strategy_equity = run_equity(period.trades, period.start, period.end)
    per_seed_e_r: list[float | None] = []
    per_seed_roi: list[float] = []
    per_seed_dd: list[float] = []
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
        seed_equity = run_equity(seed_trades, period.start, period.end)
        per_seed_roi.append(seed_equity.roi)
        per_seed_dd.append(seed_equity.max_drawdown_equity)
        per_seed_ids.append(
            [[trade.coin, _iso(trade.fill_time), trade.direction, trade.fill_index, trade.last_index] for trade in seed_trades]
        )
    return _baseline_summary(
        per_seed_e_r,
        per_seed_roi,
        per_seed_dd,
        per_seed_ids,
        per_seed_counts,
        _mean_closed(period.trades),
        strategy_equity.roi,
        len(seeds),
    )


def _spread(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {'mean': None, 'min': None, 'median': None, 'max': None}
    return {
        'mean': float(statistics.fmean(values)),
        'min': float(min(values)),
        'median': float(statistics.median(values)),
        'max': float(max(values)),
    }


def _baseline_summary(
    per_seed_e_r: Sequence[float | None],
    per_seed_roi: Sequence[float],
    per_seed_dd: Sequence[float],
    per_seed_ids: Sequence[list[list[Any]]],
    per_seed_counts: Sequence[Mapping[str, int]],
    strategy_e_r: float | None,
    strategy_roi: float,
    n_seeds: int,
) -> dict[str, Any]:
    defined = [value for value in per_seed_e_r if value is not None]
    e_r = _spread(defined)
    roi = _spread(list(per_seed_roi))
    if strategy_e_r is None or n_seeds == 0:
        share = None
    else:
        share = sum(1 for value in per_seed_e_r if value is not None and value >= strategy_e_r) / n_seeds
    if n_seeds == 0:
        share_roi = None
        dd_mean = None
    else:
        share_roi = sum(1 for value in per_seed_roi if value >= strategy_roi) / n_seeds
        dd_mean = float(statistics.fmean(per_seed_dd))
    return {
        'name': 'random_tide',
        'n_seeds': n_seeds,
        'per_seed_e_r': list(per_seed_e_r),
        'per_seed_roi': list(per_seed_roi),
        'per_seed_trade_ids': per_seed_ids,
        'per_seed_trade_counts': [dict(item) for item in per_seed_counts],
        'e_r_mean': e_r['mean'],
        'e_r_min': e_r['min'],
        'e_r_median': e_r['median'],
        'e_r_max': e_r['max'],
        'share_ge_strategy': share,
        'strategy_e_r': strategy_e_r,
        'roi_mean': roi['mean'],
        'roi_min': roi['min'],
        'roi_median': roi['median'],
        'roi_max': roi['max'],
        'share_roi_ge_strategy': share_roi,
        'strategy_roi': strategy_roi,
        'max_drawdown_equity_mean': dd_mean,
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
    """Overall, per coin, sub-windows, and both baselines. JSON-ready.

    A sub-window is its own equity run from 1.0 on trades filled inside it.
    Per-coin ``pnl_contribution`` is that coin's part of the portfolio equity
    run (sum of risk amount times part R), not a standalone coin curve.
    """
    period = run_period(coin_data, params, start, end)
    portfolio = run_equity(period.trades, period.start, period.end)
    overall = summarize(period.trades, period.start, period.end, period.counts)
    per_coin = {}
    for name, coin in period.per_coin.items():
        item = summarize(coin.trades, period.start, period.end, coin.counts)
        item['pnl_contribution'] = float(portfolio.pnl_by_coin.get(name, 0.0))
        item['max_open'] = int(portfolio.max_open_by_coin.get(name, 0))
        per_coin[name] = item
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


def _roi_line(summary: Mapping[str, Any]) -> str:
    return (
        f"ROI {_fmt(summary['roi'], '+.3f')}  90% {_interval(summary.get('roi_interval'))}  "
        f"max DD {_fmt(summary['max_drawdown_equity'], '.3f')}  "
        f"objective {_fmt(summary.get('objective'), '+.3f')}  "
        f"feasible {'yes' if summary.get('objective_feasible') else 'no'}"
    )


def _summary_lines(summary: Mapping[str, Any]) -> list[str]:
    funnel = summary['funnel']
    quantiles = summary['r_quantiles']
    histogram = summary['r_histogram']
    q = '  '.join(f'{name} {_fmt(quantiles[name], "+.2f")}' for name in ('p10', 'p25', 'p50', 'p75', 'p90'))
    hist = '  '.join(f'{name} {histogram[name]}' for name in HISTOGRAM_LABELS)
    lines = [
        _roi_line(summary),
        f"max open {summary['max_open']}  mean open {_fmt(summary['mean_open'], '.2f')}",
        f"closed {summary['n_closed']}  censored {summary['n_censored']}  "
        f"trades/week {_fmt(summary['trades_per_week'], '.2f')}  weeks {_fmt(summary['weeks'], '.2f')}",
        _prop_line('P(T1)', summary['p_t1']),
        _prop_line('P(10R)', summary['p_t2']),
        _prop_line('P(stop before T1)', summary['p_stop_before_t1']),
        f"E[R] {_fmt(summary['e_r'], '+.3f')}  90% {_interval(summary['e_r_interval'])}  "
        f"PF {_fmt(summary['profit_factor'], '.2f')}  max DD R {_fmt(summary['max_drawdown'], '.2f')}R",
        f"hold hours mean {_fmt(summary['hold_hours_mean'], '.1f')}  median {_fmt(summary['hold_hours_median'], '.1f')}",
        f'R quantiles  {q}',
        f'R histogram  {hist}',
        'funnel  ' + '  '.join(f'{key} {funnel[key]}' for key in (
            'arms', 'fills', 'expired', 'cancelled', 'entries',
            'skipped_opposite', 'skipped_max_open', 'skipped_overlap', 'skipped_total', 'trades',
        )),
        'drops  ' + '  '.join(f'{reason} {funnel[f"dropped_{reason}"]}' for reason in DROP_REASONS),
        f"min-distance drop rate {_fmt(summary['min_distance_drop_rate'], '.3f')}  "
        f"(stop_too_close_atr + costs_over_cap) / fills",
    ]
    if summary['profit_factor_note']:
        lines.append(f"PF note  {summary['profit_factor_note']}")
    return lines


def render_markdown(report: Mapping[str, Any]) -> str:
    """Compact text report. The tuning objective is the whole-window ROI bound."""
    lines = [
        f"Elder Triple Screen  {report['start']} .. {report['end']}",
        f"params {report['params_hash'][:8]}  {_roi_line(report['overall'])}",
        'Objective is the 90% ROI lower bound when max equity drawdown is at most 20%. '
        'The week bootstrap compounds trades sequentially and approximates concurrent compounding. '
        'Censored open shares are not in equity. E[R] excludes censored trades.',
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
            f"  {name}  n {summary['n_trades']}  "
            f"pnl {_fmt(summary.get('pnl_contribution'), '+.4f')}  "
            f"max open {summary['max_open']}  "
            f"closed {summary['n_closed']}  censored {summary['n_censored']}  "
            f"trades/week {_fmt(summary['trades_per_week'], '.2f')}  "
            f"P(T1) {_fmt(summary['p_t1']['p'], '.3f')}  "
            f"E[R] {_fmt(summary['e_r'], '+.3f')} {_interval(summary['e_r_interval'])}"
        )
    base = report['baselines']
    random_tide = base['random_tide']
    none = base['no_trade']
    lines.extend([
        '',
        'Baselines',
        f"  no_trade  ROI {_fmt(none['roi'], '+.3f')}  max DD {_fmt(none['max_drawdown_equity'], '.3f')}  "
        f"E[R] {_fmt(none['e_r'], '+.3f')}  trades 0",
        f"  random_tide  seeds {random_tide['n_seeds']}  "
        f"mean ROI {_fmt(random_tide['roi_mean'], '+.3f')}  "
        f"min {_fmt(random_tide['roi_min'], '+.3f')}  "
        f"median {_fmt(random_tide['roi_median'], '+.3f')}  "
        f"max {_fmt(random_tide['roi_max'], '+.3f')}  "
        f"share ROI >= strategy {_fmt(random_tide['share_roi_ge_strategy'], '.2f')}  "
        f"mean DD {_fmt(random_tide['max_drawdown_equity_mean'], '.3f')}  "
        f"mean E[R] {_fmt(random_tide['e_r_mean'], '+.3f')}  "
        f"share E[R] >= strategy {_fmt(random_tide['share_ge_strategy'], '.2f')}",
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


def _baseline_roi_line(label: str, report: Mapping[str, Any]) -> list[str]:
    none = report['baselines']['no_trade']
    tide = report['baselines']['random_tide']
    return [
        f"{label}  {_roi_line(report['overall'])}",
        f"{label} no_trade  ROI {_fmt(none['roi'], '+.3f')}  max DD {_fmt(none['max_drawdown_equity'], '.3f')}",
        f"{label} random_tide  mean ROI {_fmt(tide['roi_mean'], '+.3f')}  "
        f"min {_fmt(tide['roi_min'], '+.3f')}  "
        f"median {_fmt(tide['roi_median'], '+.3f')}  "
        f"max {_fmt(tide['roi_max'], '+.3f')}  "
        f"share >= strategy {_fmt(tide['share_roi_ge_strategy'], '.2f')}  "
        f"mean DD {_fmt(tide['max_drawdown_equity_mean'], '.3f')}",
    ]


def render_side_by_side(train: Mapping[str, Any], holdout: Mapping[str, Any]) -> str:
    """Train and holdout, with ROI and equity drawdown for both books and both baselines."""
    header = ['# Train vs holdout', *_baseline_roi_line('train', train), *_baseline_roi_line('holdout', holdout), '']
    return '\n'.join(header) + '\n# Train\n\n' + render_markdown(train) + '\n# Holdout\n\n' + render_markdown(holdout)
