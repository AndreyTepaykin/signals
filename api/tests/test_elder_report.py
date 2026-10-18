"""Period runner: pyramid, equity ROI, objective, random-tide baseline, truncation."""

import math
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.stats.summary import max_drawdown
from app.research.elder.load import CoinData
from app.research.elder.params import (
    MAX_OPEN_PER_COIN,
    RISK_PER_TRADE,
    ROI_BOOTSTRAP_RESAMPLES,
    ROI_BOOTSTRAP_SEED,
    ElderParams,
)
from app.research.elder.report import (
    HISTOGRAM_LABELS,
    SKIP_REASONS,
    apply_pyramiding,
    build_report,
    pack_until_count,
    random_tide_baseline,
    render_markdown,
    render_side_by_side,
    roi_interval,
    run_equity,
    run_period,
    subwindow_counts,
    summarize,
)
from app.research.elder.resample import STEP_4H, STEP_15M, STEP_30M, OHLCV
from app.research.elder.simulate import PartExit, Trade

UTC = timezone.utc
WEEK = timedelta(days=7)
SERIES_START = datetime(2025, 1, 6, tzinfo=UTC)  # Monday
WEEKS = 48
WARM_WEEKS = 34  # the walk starts after this many weeks of history; the daily tide Impulse is warm after about 5 weeks

PARAMS = ElderParams(
    tide_mode='ema',
    wave_osc='stoch',
    wave_threshold=40.0,
    wave_block_impulse=False,
    ripple_expiry_bars=8,
    stop_lookback_bars=2,
    stop_atr_buffer=0.25,
    min_stop_atr=0.2,
    t1_r=1.2,
    t1_share=0.5,
    be_after_t1=True,
    trail_atr=3.0,
)


def _trade(**overrides) -> Trade:
    fill = overrides.get('fill_time', datetime(2026, 1, 2, tzinfo=UTC))
    base = dict(
        coin='BTCUSDT',
        direction=1,
        fill_time=fill,
        fill_index=0,
        entry_price=100.0,
        initial_stop=90.0,
        r=10.0,
        exits=(),
        hit_t1=False,
        hit_t2=False,
        stopped_before_t1=False,
        censored=False,
        censor_reason=None,
        last_index=1,
        closed_time=fill + timedelta(hours=4),
        price_r=0.0,
        cost_r=0.0,
        funding_r=0.0,
        total_r=0.0,
        open_share=0.0,
        mark_r=None,
        mae_r=0.0,
        mfe_r=0.0,
        bars_held=1,
    )
    base.update(overrides)
    return Trade(**base)


def _counts(**overrides):
    counts = {
        'arms': 0,
        'fills': 0,
        'expired': 0,
        'cancelled': 0,
        'entries': 0,
        'dropped_stop_wrong_side': 0,
        'dropped_stop_too_close_atr': 0,
        'dropped_costs_over_cap': 0,
        'dropped_no_wave_context': 0,
        'skipped_opposite': 0,
        'skipped_max_open': 0,
        'skipped_overlap': 0,
        'skipped_total': 0,
    }
    counts.update(overrides)
    return counts


def _fold(bars, per, step):
    out = []
    for index in range(0, len(bars) - per + 1, per):
        group = bars[index : index + per]
        out.append(
            OHLCV(
                group[0].open_time,
                group[0].open_time + step,
                group[0].open,
                max(bar.high for bar in group),
                min(bar.low for bar in group),
                group[-1].close,
                math.fsum(bar.volume for bar in group),
            )
        )
    return out


def _series():
    rng = random.Random(20260117)
    price = 100.0
    drift = 0.0
    bars = []
    for i in range(WEEKS * 7 * 24 * 4):
        drift = 0.998 * drift + rng.gauss(0.0, 0.00001)
        change = drift + rng.gauss(0.0, 0.0035)
        opened = price
        closed = opened * math.exp(change)
        high = max(opened, closed) * (1.0 + abs(rng.gauss(0.0, 0.0015)))
        low = min(opened, closed) * (1.0 - abs(rng.gauss(0.0, 0.0015)))
        volume = 100.0 * rng.uniform(0.5, 1.5)
        start = SERIES_START + i * STEP_15M
        bars.append(OHLCV(start, start + STEP_15M, opened, high, low, closed, volume))
        price = closed
    return bars


def _coin(bars_15m, funding=()):
    return CoinData(
        coin='SYN',
        bars_15m=list(bars_15m),
        bars_4h=_fold(bars_15m, 16, STEP_4H),
        funding=list(funding),
    )


@pytest.fixture(scope='module')
def walk():
    bars = _series()
    start = bars[WARM_WEEKS * 7 * 24 * 4].open_time
    end = bars[-1].close_time
    data = _coin(bars)
    period = run_period([data], PARAMS, start, end)
    return {'bars': bars, 'data': data, 'start': start, 'end': end, 'period': period}


def test_run_period_respects_the_pyramid(walk):
    period = walk['period']
    assert period.trades, period.counts
    assert {trade.direction for trade in period.trades} <= {1, -1}
    for coin in period.per_coin.values():
        skipped_n = sum(len(rows) for rows in coin.skipped.values())
        assert len(coin.trades) + skipped_n == coin.counts['entries']
        assert coin.counts['skipped_total'] == skipped_n
        assert sum(coin.counts[reason] for reason in SKIP_REASONS) == skipped_n
        views = [
            SimpleNamespace(
                fill_time=trade.fill_time,
                fill_index=trade.fill_index,
                direction=trade.direction,
                stop=trade.initial_stop,
                trade=trade,
            )
            for trade in coin.trades
        ]
        again, skips = apply_pyramiding(views, lambda view: view.trade)
        assert [item.fill_index for item in again] == [trade.fill_index for trade in coin.trades]
        assert all(not rows for rows in skips.values())
    assert all(walk['start'] <= trade.fill_time < walk['end'] for trade in period.trades)


def test_entries_outside_the_window_are_ignored(walk):
    period = walk['period']
    assert period.trades
    mid = period.trades[0].fill_time + STEP_30M
    later = run_period([walk['data']], PARAMS, mid, walk['end'])
    assert all(mid <= trade.fill_time < walk['end'] for trade in later.trades)
    assert all(mid <= item.fill_time < walk['end'] for item in later.dropped)
    assert period.trades[0].fill_time < mid
    assert all(trade.fill_time != period.trades[0].fill_time for trade in later.trades)


def test_subwindow_funnels_add_up(walk):
    period = walk['period']
    mid = period.start + (period.end - period.start) / 2
    left = subwindow_counts(period, period.start, mid)
    right = subwindow_counts(period, mid, period.end)
    for key in ('arms', 'fills', 'expired', 'cancelled', 'entries', *SKIP_REASONS, 'skipped_total'):
        assert left[key] + right[key] == period.counts[key]


def test_truncation_at_end_matches_truncated_input(walk):
    bars = walk['bars']
    cut = bars[len(bars) * 3 // 4].close_time
    assert walk['start'] < cut < walk['end']
    full = run_period([walk['data']], PARAMS, walk['start'], cut)
    truncated_bars = [bar for bar in bars if bar.close_time <= cut]
    truncated = run_period([_coin(truncated_bars)], PARAMS, walk['start'], cut)

    def signature(period):
        trades = [
            (trade.fill_time, trade.fill_index, trade.last_index, trade.censored, round(trade.total_r, 8), trade.hit_t1, trade.hit_t2)
            for trade in period.trades
        ]
        drops = [(item.fill_time, item.reason) for item in period.dropped]
        return period.counts, trades, drops

    assert signature(full) == signature(truncated)
    assert full.trades, 'the cut still contains trades, so the comparison is not vacuous'


def _at(day, hours, total_r, **overrides):
    fill = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day)
    return _trade(
        fill_time=fill,
        fill_index=day,
        closed_time=fill + timedelta(hours=hours),
        total_r=total_r,
        **overrides,
    )


def test_summarize_numbers_and_censored_trades_stay_out_of_expectancy():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    # Nine closed results, one per histogram bin edge, plus a censored trade whose R must not count.
    closed_spec = [
        (-1.5, False, False, True),
        (-1.0, False, False, True),
        (0.0, False, False, True),
        (0.5, True, False, False),
        (1.0, True, False, False),
        (2.0, True, False, False),
        (5.0, True, False, False),
        (10.0, True, True, False),
        (11.0, True, True, False),
    ]
    trades = []
    for index, (total, hit_t1, hit_t2, stopped) in enumerate(closed_spec):
        trades.append(_at(
            index, 2 + index, total,
            hit_t1=hit_t1, hit_t2=hit_t2, stopped_before_t1=stopped,
        ))
    trades.append(_trade(
        fill_time=start + timedelta(days=3, hours=1),
        fill_index=100,
        censored=True,
        censor_reason='end_of_period',
        closed_time=None,
        total_r=100.0,
        open_share=0.4,
        mark_r=-0.2,
    ))
    summary = summarize(trades, start, start + WEEK, _counts(
        fills=10, dropped_stop_too_close_atr=2, dropped_costs_over_cap=1, entries=7,
    ))
    closed_values = [spec[0] for spec in closed_spec]
    assert summary['n_closed'] == 9
    assert summary['n_censored'] == 1
    assert summary['e_r'] == pytest.approx(sum(closed_values) / 9)
    assert 100.0 not in [summary['e_r']]
    assert summary['censored_trades'][0]['open_share'] == 0.4
    assert summary['censored_trades'][0]['mark_r'] == -0.2
    assert summary['censored_trades'][0]['closed_part_r'] == 100.0
    assert summary['p_t1']['k'] == 6 and summary['p_t1']['n'] == 9
    assert summary['p_t2']['k'] == 2
    assert summary['p_stop_before_t1']['k'] == 3
    assert summary['p_t1']['interval'][0] <= summary['p_t1']['p'] <= summary['p_t1']['interval'][1]
    assert summary['r_histogram'] == {
        '<=-1': 2, '(-1,0]': 1, '(0,1]': 2, '(1,2]': 1, '(2,5]': 1, '(5,10]': 1, '>10': 1,
    }
    assert set(summary['r_histogram']) == set(HISTOGRAM_LABELS)
    assert summary['r_quantiles']['p50'] == pytest.approx(1.0)
    assert summary['max_drawdown'] == pytest.approx(max_drawdown(closed_values))
    assert summary['profit_factor'] == pytest.approx(sum(v for v in closed_values if v > 0) / 2.5)
    assert summary['hold_hours_mean'] == pytest.approx(sum(range(2, 11)) / 9)
    assert summary['min_distance_drop_rate'] == pytest.approx(0.3)
    assert summary['funnel']['trades'] == 10
    assert summary['funnel']['fills'] == 10


def _loss(index, start):
    fill = start + timedelta(days=index)
    exit_time = fill + timedelta(hours=1)
    part = PartExit(1.0, 90.0, exit_time, 'stop', 'initial', False, False, r=-1.0)
    return _trade(
        fill_time=fill,
        fill_index=index,
        total_r=-1.0,
        exits=(part,),
        closed_time=exit_time,
        stopped_before_t1=True,
    )


def _compounded_loss(n):
    equity = 1.0
    for _ in range(n):
        equity += RISK_PER_TRADE * equity * -1.0
    return equity


def test_objective_is_the_roi_lower_bound_unless_drawdown_exceeds_the_limit():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + 30 * WEEK
    under_equity = _compounded_loss(22)
    under = summarize([_loss(i, start) for i in range(22)], start, end, _counts())
    assert under['max_drawdown_equity'] == pytest.approx(1.0 - under_equity)
    assert under['max_drawdown_equity'] <= 0.20
    assert under['roi'] == pytest.approx(under_equity - 1.0)
    assert under['objective_feasible'] is True
    assert under['objective'] == pytest.approx(under['roi_interval'][0])
    # The old 1-3 trades/week gate is gone: this book trades far less than once a week.
    assert under['trades_per_week'] < 1.0

    over_equity = _compounded_loss(23)
    over = summarize([_loss(i, start) for i in range(23)], start, end, _counts())
    assert over['max_drawdown_equity'] == pytest.approx(1.0 - over_equity)
    assert over['max_drawdown_equity'] > 0.20
    assert over['objective'] is None
    assert over['objective_feasible'] is False


def test_zero_fills_have_no_min_distance_rate_and_a_flat_equity():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    summary = summarize([], start, start + WEEK, _counts())
    assert summary['e_r'] is None
    assert summary['max_drawdown'] == 0.0
    assert summary['min_distance_drop_rate'] is None
    assert summary['p_t1']['p'] is None and summary['p_t1']['interval'] is None
    assert summary['roi'] == pytest.approx(0.0)
    assert summary['max_drawdown_equity'] == pytest.approx(0.0)
    assert summary['roi_interval'] == [pytest.approx(0.0), pytest.approx(0.0)]
    assert summary['objective'] == pytest.approx(0.0)
    assert summary['objective_feasible'] is True
    assert summary['trades_per_week'] == 0.0
    assert summary['max_open'] == 0
    assert summary['mean_open'] == pytest.approx(0.0)


class _Item:
    def __init__(self, index, last=None, direction=1, entry_price=100.0, stop=90.0):
        self.fill_index = index
        self.fill_time = SERIES_START + index * STEP_15M
        self.last_index = index if last is None else last
        self.direction = direction
        self.entry_price = entry_price
        self.stop = stop


def test_random_pack_is_deterministic_and_matches_the_count():
    entries = [_Item(index) for index in range(20)]

    def simulate(entry):
        return _Item(
            entry.fill_index,
            last=entry.fill_index,
            direction=entry.direction,
            entry_price=entry.entry_price,
            stop=entry.stop,
        )

    first = pack_until_count(entries, 5, random.Random(0), simulate)
    again = pack_until_count(entries, 5, random.Random(0), simulate)
    other = pack_until_count(entries, 5, random.Random(1), simulate)
    assert [item.fill_index for item in first] == [item.fill_index for item in again]
    assert len(first) == 5
    assert [item.fill_index for item in first] != [item.fill_index for item in other]
    for earlier, later in zip(first, first[1:]):
        assert later.fill_index > earlier.last_index


def test_random_pack_stops_when_candidates_cannot_fill_the_count():
    entries = [_Item(index) for index in range(30)]

    def simulate(entry):
        return _Item(
            entry.fill_index,
            last=entry.fill_index + 10,
            direction=entry.direction,
            entry_price=entry.entry_price,
            stop=entry.stop,
        )

    packed = pack_until_count(entries, 10, random.Random(2), simulate)
    assert len(packed) < 10
    for earlier, later in zip(packed, packed[1:]):
        assert later.fill_index > earlier.last_index


def test_random_tide_baseline_is_deterministic_and_respects_the_pyramid(walk):
    period = walk['period']
    assert period.trades
    first = random_tide_baseline(period, PARAMS, seeds=(0, 1))
    second = random_tide_baseline(period, PARAMS, seeds=(0, 1))
    assert first['per_seed_trade_ids'] == second['per_seed_trade_ids']
    assert first['per_seed_e_r'] == second['per_seed_e_r']
    assert first['per_seed_roi'] == second['per_seed_roi']
    assert first['n_seeds'] == 2
    for counts in first['per_seed_trade_counts']:
        for name, taken in counts.items():
            assert taken <= len(period.per_coin[name].trades)
    for seed_ids in first['per_seed_trade_ids']:
        by_coin: dict[str, list] = {}
        for coin, _fill, direction, fill_index, last_index in seed_ids:
            by_coin.setdefault(coin, []).append((fill_index, last_index, direction))
        for spans in by_coin.values():
            ordered = sorted(spans)
            for index, (fill_index, _last, direction) in enumerate(ordered):
                still_open = [item for item in ordered[:index] if item[1] >= fill_index]
                assert len(still_open) < MAX_OPEN_PER_COIN
                assert all(item[2] == direction for item in still_open)
    defined = [value for value in first['per_seed_e_r'] if value is not None]
    if defined:
        assert first['e_r_mean'] == pytest.approx(sum(defined) / len(defined))
        assert first['e_r_min'] == min(defined)
        assert first['e_r_max'] == max(defined)
    rois = first['per_seed_roi']
    assert first['roi_mean'] == pytest.approx(sum(rois) / len(rois))
    assert first['roi_min'] == min(rois)
    assert first['roi_max'] == max(rois)
    assert first['share_roi_ge_strategy'] == pytest.approx(
        sum(1 for value in rois if value >= first['strategy_roi']) / len(rois)
    )


def test_render_markdown_covers_the_report_shape():
    data = [CoinData('SYN', [], [], [])]
    report = build_report(
        data, PARAMS, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC),
        subwindows=(('jan_apr', datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 5, 1, tzinfo=UTC)),),
        seeds=(),
    )
    text = render_markdown(report)
    assert text.splitlines()[1].startswith('params ')
    assert 'ROI' in text.splitlines()[1]
    assert 'max DD' in text.splitlines()[1]
    assert 'objective' in text.splitlines()[1]
    assert 'no_trade' in text
    assert 'random_tide' in text
    assert 'mean ROI' in text
    assert 'jan_apr' in text
    assert 'Censored' in text
    assert 'skipped_opposite' in text
    assert report['objective'] == pytest.approx(0.0)
    assert report['objective_feasible'] is True
    assert report['overall']['roi'] == pytest.approx(0.0)
    assert report['baselines']['no_trade']['e_r'] == 0.0
    assert report['baselines']['no_trade']['roi'] == 0.0
    side = render_side_by_side(report, report)
    assert side.startswith('# Train vs holdout')
    assert 'train  ROI' in side and 'holdout  ROI' in side
    assert 'train no_trade' in side and 'holdout random_tide' in side


def _pyramid(specs):
    """specs: (fill_index, direction, entry_price, stop, last_index)."""
    entries = []
    lasts = {}
    for fill_index, direction, entry_price, stop, last_index in specs:
        entries.append(SimpleNamespace(
            fill_index=fill_index,
            fill_time=SERIES_START + fill_index * STEP_30M,
            direction=direction,
            entry_price=entry_price,
            stop=stop,
        ))
        lasts[fill_index] = last_index

    def simulate(entry):
        return SimpleNamespace(
            fill_index=entry.fill_index,
            fill_time=entry.fill_time,
            direction=entry.direction,
            entry_price=entry.entry_price,
            last_index=lasts[entry.fill_index],
        )

    taken, skipped = apply_pyramiding(entries, simulate)
    return [trade.fill_index for trade in taken], {reason: [entry.fill_index for entry in rows] for reason, rows in skipped.items()}


def test_second_long_is_allowed_when_its_stop_touches_the_open_entry():
    taken, skipped = _pyramid([
        (0, 1, 100.0, 90.0, 10),
        (5, 1, 110.0, 100.0, 10),
    ])
    assert taken == [0, 5]
    assert all(not rows for rows in skipped.values())


def test_overlapping_risk_zone_is_skipped_and_shorts_mirror_it():
    taken, skipped = _pyramid([
        (0, 1, 100.0, 90.0, 10),
        (5, 1, 110.0, 99.99, 10),
    ])
    assert taken == [0]
    assert skipped['skipped_overlap'] == [5]
    assert skipped['skipped_opposite'] == [] and skipped['skipped_max_open'] == []

    taken, skipped = _pyramid([
        (0, -1, 100.0, 110.0, 10),
        (4, -1, 90.0, 100.0, 10),
        (6, -1, 80.0, 90.01, 10),
    ])
    assert taken == [0, 4]
    assert skipped['skipped_overlap'] == [6]


def test_opposite_direction_is_skipped_while_any_trade_is_open():
    taken, skipped = _pyramid([
        (0, 1, 100.0, 90.0, 10),
        (3, -1, 100.0, 110.0, 12),
    ])
    assert taken == [0]
    assert skipped['skipped_opposite'] == [3]
    assert skipped['skipped_max_open'] == [] and skipped['skipped_overlap'] == []


def test_sixth_open_trade_is_skipped_for_the_cap():
    specs = [(index, 1, 100.0 + 10.0 * index, 90.0 + 10.0 * index, 100) for index in range(MAX_OPEN_PER_COIN + 1)]
    taken, skipped = _pyramid(specs)
    assert taken == list(range(MAX_OPEN_PER_COIN))
    assert skipped['skipped_max_open'] == [MAX_OPEN_PER_COIN]
    assert skipped['skipped_overlap'] == [] and skipped['skipped_opposite'] == []


def test_skip_reasons_are_checked_opposite_then_cap_then_overlap():
    specs = [(index, 1, 100.0 + 10.0 * index, 90.0 + 10.0 * index, 100) for index in range(MAX_OPEN_PER_COIN)]
    specs.append((MAX_OPEN_PER_COIN, -1, 50.0, 60.0, 100))
    taken, skipped = _pyramid(specs)
    assert len(taken) == MAX_OPEN_PER_COIN
    assert skipped['skipped_opposite'] == [MAX_OPEN_PER_COIN]
    assert skipped['skipped_max_open'] == [] and skipped['skipped_overlap'] == []

    specs[-1] = (MAX_OPEN_PER_COIN, 1, 200.0, 90.0, 100)
    _taken, skipped = _pyramid(specs)
    assert skipped['skipped_max_open'] == [MAX_OPEN_PER_COIN]
    assert skipped['skipped_overlap'] == []


def test_a_trade_that_exits_on_the_fill_bar_still_counts_as_open():
    # Five open longs. The last one ends on bar 5, which is the new entry's bar.
    specs = [(index, 1, 100.0 + 10.0 * index, 90.0 + 10.0 * index, 100) for index in range(4)]
    specs.append((4, 1, 140.0, 130.0, 5))
    specs.append((5, 1, 150.0, 140.0, 100))
    taken, skipped = _pyramid(specs)
    assert taken == [0, 1, 2, 3, 4]
    assert skipped['skipped_max_open'] == [5]

    # A short proposed on that same bar is an opposite skip, not a fresh book.
    taken, skipped = _pyramid([
        (0, 1, 100.0, 90.0, 5),
        (5, -1, 100.0, 110.0, 8),
        (6, -1, 90.0, 100.0, 9),
    ])
    assert taken == [0, 6]
    assert skipped['skipped_opposite'] == [5]


def test_the_opposite_direction_is_allowed_after_every_trade_has_closed():
    taken, skipped = _pyramid([
        (0, 1, 100.0, 90.0, 4),
        (5, -1, 80.0, 90.0, 9),
    ])
    assert taken == [0, 5]
    assert all(not rows for rows in skipped.values())


def test_strategy_and_random_tide_call_the_same_pyramid(walk, monkeypatch):
    calls = []
    real = apply_pyramiding

    def wrapped(entries, simulate, **kwargs):
        calls.append(kwargs.get('limit'))
        return real(entries, simulate, **kwargs)

    monkeypatch.setattr('app.research.elder.report.apply_pyramiding', wrapped)
    run_period([walk['data']], PARAMS, walk['start'], walk['end'])
    assert calls and all(limit is None for limit in calls)
    calls.clear()
    random_tide_baseline(walk['period'], PARAMS, seeds=(0,))
    assert calls
    assert any(limit is not None for limit in calls)


def test_equity_compounds_at_the_fill_and_ignores_censored_open_shares():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=10)

    def part(when, r, reason='t1'):
        return PartExit(1.0, 100.0, when, reason, None if reason != 'stop' else 'initial', False, False, r=r)

    trade_a = _trade(
        coin='BTCUSDT', fill_time=start, fill_index=0, total_r=0.0,
        exits=(part(start + timedelta(hours=1), 10.0), part(start + timedelta(hours=2), -10.0, 'stop')),
        closed_time=start + timedelta(hours=2),
    )
    trade_b = _trade(
        coin='ETHUSDT', fill_time=start + timedelta(hours=1), fill_index=1, total_r=1.0,
        exits=(part(start + timedelta(hours=3), 1.0),),
        closed_time=start + timedelta(hours=3),
    )
    trade_c = _trade(
        coin='BTCUSDT', fill_time=start + timedelta(hours=4), fill_index=2,
        censored=True, censor_reason='end_of_period', closed_time=None,
        total_r=0.0, open_share=1.0, mark_r=50.0, exits=(),
    )
    trade_d = _trade(
        coin='BTCUSDT', fill_time=start + timedelta(hours=5), fill_index=3,
        censored=True, censor_reason='end_of_period', closed_time=None,
        total_r=999.0, open_share=0.4, mark_r=100.0,
        exits=(part(start + timedelta(hours=6), 2.0),),
    )
    result = run_equity([trade_a, trade_b, trade_c, trade_d], start, end)
    risk_a = RISK_PER_TRADE * 1.0
    equity = 1.0 + risk_a * 10.0
    risk_b = RISK_PER_TRADE * equity
    equity = equity + risk_a * -10.0
    equity = equity + risk_b * 1.0
    risk_d = RISK_PER_TRADE * equity
    equity = equity + risk_d * 2.0
    assert result.risk_by_id[id(trade_b)] == pytest.approx(RISK_PER_TRADE * (1.0 + RISK_PER_TRADE * 10.0))
    assert result.risk_by_id[id(trade_d)] == pytest.approx(risk_d)
    assert result.roi == pytest.approx(equity - 1.0)
    assert result.equity == pytest.approx(equity)
    peak = 1.0 + risk_a * 10.0
    assert result.max_drawdown_equity == pytest.approx((peak - 1.0) / peak)
    assert result.pnl_by_coin['ETHUSDT'] == pytest.approx(risk_b)
    assert result.pnl_by_coin['BTCUSDT'] == pytest.approx(risk_d * 2.0)
    assert result.max_open == 2
    assert result.max_open_by_coin['BTCUSDT'] == 2
    assert result.max_open_by_coin['ETHUSDT'] == 1
    assert result.mean_open == pytest.approx(1.5)


def test_roi_bootstrap_is_deterministic_includes_empty_weeks_and_brackets_a_simple_roi():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + 3 * WEEK
    trade = _trade(fill_time=start + timedelta(days=1), fill_index=1, total_r=10.0)
    first = roi_interval([trade], start, end)
    assert first == roi_interval([trade], start, end)
    rng = np.random.default_rng(ROI_BOOTSTRAP_SEED)
    draws = rng.integers(0, 3, size=(ROI_BOOTSTRAP_RESAMPLES, 3))
    weeks = [[10.0], [], []]
    stats = np.empty(ROI_BOOTSTRAP_RESAMPLES, dtype=float)
    for index, draw in enumerate(draws):
        factor = 1.0
        for week_i in draw:
            for value in weeks[int(week_i)]:
                factor *= 1.0 + RISK_PER_TRADE * value
        stats[index] = factor - 1.0
    low, high = np.quantile(stats, [0.05, 0.95])
    assert first[0] == pytest.approx(float(low))
    assert first[1] == pytest.approx(float(high))
    assert first[0] < 0.1 < first[1]

    exit_time = start + timedelta(hours=1)
    simple = _trade(
        fill_time=start, fill_index=0, total_r=2.0, hit_t1=True, closed_time=exit_time,
        exits=(PartExit(1.0, 120.0, exit_time, 't1', None, False, False, r=2.0),),
    )
    summary = summarize([simple], start, start + WEEK, _counts())
    assert summary['roi'] == pytest.approx(RISK_PER_TRADE * 2.0)
    assert summary['roi_interval'][0] <= summary['roi'] <= summary['roi_interval'][1]
    assert summary['roi_interval'][0] == pytest.approx(summary['roi'])
    assert summary['roi_interval'][1] == pytest.approx(summary['roi'])
