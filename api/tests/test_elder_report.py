"""Period runner: one position, censored trades, objective, random-tide baseline, truncation."""

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from app.core.stats.summary import bootstrap_interval, max_drawdown
from app.research.elder.load import CoinData
from app.research.elder.params import ElderParams
from app.research.elder.report import (
    HISTOGRAM_LABELS,
    build_report,
    pack_until_count,
    random_tide_baseline,
    render_markdown,
    run_period,
    subwindow_counts,
    summarize,
)
from app.research.elder.resample import STEP_15M, STEP_1H, STEP_4H, OHLCV
from app.research.elder.simulate import Trade

UTC = timezone.utc
WEEK = timedelta(days=7)
SERIES_START = datetime(2025, 1, 6, tzinfo=UTC)  # Monday
WEEKS = 48
WARM_WEEKS = 34  # weekly Impulse is cold before about week 34, and a cold Impulse blocks the tide

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
        'dropped_no_6h_context': 0,
        'skipped_position_open': 0,
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
        bars_1h=_fold(bars_15m, 4, STEP_1H),
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


def test_run_period_yields_trades_without_overlap(walk):
    period = walk['period']
    assert period.trades, period.counts
    assert {trade.direction for trade in period.trades} <= {1, -1}
    for coin in period.per_coin.values():
        ordered = sorted(coin.trades, key=lambda trade: trade.fill_index)
        for earlier, later in zip(ordered, ordered[1:]):
            assert later.fill_index > earlier.last_index
        assert len(coin.trades) + len(coin.skipped) == coin.counts['entries']
        assert coin.counts['skipped_position_open'] == len(coin.skipped)
    assert all(walk['start'] <= trade.fill_time < walk['end'] for trade in period.trades)


def test_one_position_rule_counts_skips(walk, monkeypatch):
    def blocking(entry, ctx, params, funding, end_time):
        return _trade(
            coin=entry.coin,
            direction=entry.direction,
            fill_time=entry.fill_time,
            fill_index=entry.fill_index,
            last_index=10**9,
            censored=True,
            censor_reason='end_of_period',
            closed_time=None,
            total_r=50.0,
            open_share=1.0,
            mark_r=0.25,
        )

    monkeypatch.setattr('app.research.elder.report.simulate_trade', blocking)
    period = run_period([walk['data']], PARAMS, walk['start'], walk['end'])
    assert period.counts['entries'] >= 2
    assert len(period.trades) == len(period.per_coin)
    assert period.counts['skipped_position_open'] == period.counts['entries'] - len(period.trades)
    # The blocked trade is censored and its closed-part R stays out of E[R].
    summary = summarize(period.trades, walk['start'], walk['end'], period.counts)
    assert summary['n_censored'] == len(period.trades)
    assert summary['n_closed'] == 0
    assert summary['e_r'] is None
    assert summary['censored_trades'][0]['closed_part_r'] == 50.0
    assert summary['censored_trades'][0]['mark_r'] == 0.25


def test_entries_outside_the_window_are_ignored(walk):
    period = walk['period']
    assert period.trades
    mid = period.trades[0].fill_time + STEP_15M
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
    for key in ('arms', 'fills', 'expired', 'cancelled', 'entries', 'skipped_position_open'):
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


def test_objective_is_the_lower_bound_only_inside_the_trade_rate_band():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    two = [_at(0, 4, 1.0, hit_t1=True), _at(1, 8, 3.0, stopped_before_t1=True)]
    interval = bootstrap_interval([1.0, 3.0], confidence=0.90)

    def rate(trades, end):
        return summarize(trades, start, end, _counts())

    inside = rate(two, start + 2 * WEEK)  # 2 trades / 2 weeks = 1.0
    assert inside['trades_per_week'] == pytest.approx(1.0)
    assert inside['e_r'] == pytest.approx(2.0)
    assert inside['objective_feasible'] is True
    assert inside['objective'] == pytest.approx(interval[0])
    assert inside['e_r_interval'][0] == pytest.approx(interval[0])

    three = two + [_at(2, 1, 0.0)]
    at_three = rate(three, start + WEEK)  # 3 / 1 week
    assert at_three['trades_per_week'] == pytest.approx(3.0)
    assert at_three['objective_feasible'] is True
    assert at_three['objective'] == pytest.approx(bootstrap_interval([1.0, 3.0, 0.0], confidence=0.90)[0])

    below = rate(two, start + 2 * WEEK + timedelta(seconds=1))
    assert below['trades_per_week'] < 1.0
    assert below['objective'] is None and below['objective_feasible'] is False

    above = rate(three, start + WEEK - timedelta(seconds=1))
    assert above['trades_per_week'] > 3.0
    assert above['objective'] is None and above['objective_feasible'] is False

    one = rate(two[:1], start + WEEK)  # rate 1, but no interval
    assert one['trades_per_week'] == pytest.approx(1.0)
    assert one['e_r_interval'] is None
    assert one['objective'] is None and one['objective_feasible'] is False

    # A censored trade counts toward the rate and not toward E[R].
    mixed = two + [_trade(
        fill_time=start + timedelta(days=2), censored=True, censor_reason='hole',
        closed_time=None, total_r=80.0, open_share=1.0, mark_r=0.1,
    )]
    mixed_summary = rate(mixed, start + WEEK)  # 3 trades / week, E[R] from the two closed only
    assert mixed_summary['trades_per_week'] == pytest.approx(3.0)
    assert mixed_summary['e_r'] == pytest.approx(2.0)
    assert mixed_summary['n_closed'] == 2 and mixed_summary['n_censored'] == 1
    assert mixed_summary['objective_feasible'] is True


def test_zero_fills_have_no_min_distance_rate_and_no_objective():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    summary = summarize([], start, start + WEEK, _counts())
    assert summary['e_r'] is None
    assert summary['max_drawdown'] == 0.0
    assert summary['min_distance_drop_rate'] is None
    assert summary['p_t1']['p'] is None and summary['p_t1']['interval'] is None
    assert summary['objective'] is None
    assert summary['trades_per_week'] == 0.0


class _Item:
    def __init__(self, index, last=None):
        self.fill_index = index
        self.fill_time = SERIES_START + index * STEP_15M
        self.last_index = index if last is None else last


def test_random_pack_is_deterministic_and_matches_the_count():
    entries = [_Item(index) for index in range(20)]

    def simulate(entry):
        return _Item(entry.fill_index, last=entry.fill_index)

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
        return _Item(entry.fill_index, last=entry.fill_index + 10)

    packed = pack_until_count(entries, 10, random.Random(2), simulate)
    assert len(packed) < 10
    for earlier, later in zip(packed, packed[1:]):
        assert later.fill_index > earlier.last_index


def test_random_tide_baseline_is_deterministic_and_respects_one_position(walk):
    period = walk['period']
    assert period.trades
    first = random_tide_baseline(period, PARAMS, seeds=(0, 1))
    second = random_tide_baseline(period, PARAMS, seeds=(0, 1))
    assert first['per_seed_trade_ids'] == second['per_seed_trade_ids']
    assert first['per_seed_e_r'] == second['per_seed_e_r']
    assert first['n_seeds'] == 2
    for counts in first['per_seed_trade_counts']:
        for name, taken in counts.items():
            assert taken <= len(period.per_coin[name].trades)
    for seed_ids in first['per_seed_trade_ids']:
        by_coin: dict[str, list] = {}
        for coin, _fill, _direction, fill_index, last_index in seed_ids:
            by_coin.setdefault(coin, []).append((fill_index, last_index))
        for spans in by_coin.values():
            ordered = sorted(spans)
            for (left_fill, left_last), (right_fill, _right_last) in zip(ordered, ordered[1:]):
                assert right_fill > left_last
    defined = [value for value in first['per_seed_e_r'] if value is not None]
    if defined:
        assert first['e_r_mean'] == pytest.approx(sum(defined) / len(defined))
        assert first['e_r_min'] == min(defined)
        assert first['e_r_max'] == max(defined)


def test_render_markdown_covers_the_report_shape():
    data = [CoinData('SYN', [], [], [], [])]
    report = build_report(
        data, PARAMS, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC),
        subwindows=(('jan_apr', datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 5, 1, tzinfo=UTC)),),
        seeds=(),
    )
    text = render_markdown(report)
    assert 'objective' in text
    assert 'no_trade' in text
    assert 'random_tide' in text
    assert 'jan_apr' in text
    assert 'Censored' in text
    assert report['objective'] is None
    assert report['baselines']['no_trade']['e_r'] == 0.0
