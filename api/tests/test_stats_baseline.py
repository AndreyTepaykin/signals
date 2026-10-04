"""The naive swing-break baseline: selection / independence (pure), resolution by the labeler's own core, parity with the app."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core import constants as C
from app.core.labeler import barriers as B
from app.core.labeler import outcome as O
from app.core.stats import baseline as BL
from app.core.stats import params as P
from app.core.stats.observations import BaselineKey, BaselineObservation, CohortKey, MixedCohortError, coin_set_for_path
from app.core.stats.summary import summarize
from app.models.feeds import Candle
from app.worker import label_job as LJ
from app.worker import stats_job as SJ
from tests.conftest import make_candidate
from tests.test_label_job import (
    GAP,
    LATER,
    RULE_HASH,
    SCALP_BARS,
    add_bar,
    add_flat,
    add_funding_grid,
    only_label,
    run,
)
from tests.test_label_job import T0 as LABEL_T0

UTC = timezone.utc
D = Decimal
BAR = timedelta(minutes=15)
MISSING = 2 * B.BAR_LENGTH
AS_OF = datetime(2026, 12, 31, tzinfo=UTC)
START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)  # a full-lookback bar


def event(coin='SOLUSDT', horizon='scalp', source='replay', bar_close=START, direction='long', stop='98', **kw) -> BL.NaiveEvent:
    values = dict(
        feature_row_id=1, coin=coin, horizon=horizon, source=source, bar_close=bar_close, direction=direction,
        stop_price=None if stop is None else D(stop),
    )
    values.update(kw)
    return BL.NaiveEvent(**values)


# ---------------------------------------------------------------------------
# selection and independence
# ---------------------------------------------------------------------------
def test_a_later_event_on_the_same_coin_and_horizon_is_skipped_while_the_hold_is_open():
    trades, skipped = BL.select_trades(
        [event(bar_close=START), event(bar_close=START + timedelta(hours=2)), event(bar_close=START + timedelta(hours=4))],
        reference_coin='BTCUSDT',
    )
    assert [t.event.bar_close for t in trades] == [START, START + timedelta(hours=4)]  # scalp hold = 4h, [start, start+4h)
    assert [why for _e, why in skipped] == [BL.SKIP_HOLD_OPEN]


def test_the_hold_is_the_frozen_window_not_the_actual_resolution():
    # whatever the first trade does, the slot is held for the whole horizon: no outcome can influence selection
    first = event(bar_close=START)
    trades, skipped = BL.select_trades([first, event(bar_close=START + timedelta(minutes=15))], reference_coin='BTCUSDT')
    assert len(trades) == 1 and len(skipped) == 1


def test_different_coins_and_different_horizons_do_not_block_each_other():
    trades, skipped = BL.select_trades(
        [event(coin='SOLUSDT'), event(coin='XRPUSDT'), event(coin='SOLUSDT', horizon='swing')], reference_coin='BTCUSDT'
    )
    assert len(trades) == 3 and not skipped


def test_an_event_without_a_usable_stop_has_no_rule_to_trade_and_is_counted():
    trades, skipped = BL.select_trades([event(stop=None)], reference_coin='BTCUSDT')
    assert not trades and skipped[0][1] == BL.SKIP_NO_STOP


def test_an_alt_event_on_the_same_bar_and_direction_as_btc_shares_its_sample_group():
    btc, same, other = event(coin='BTCUSDT'), event(coin='SOLUSDT'), event(coin='XRPUSDT', direction='short', stop='102')
    trades, _ = BL.select_trades([other, same, btc], reference_coin='BTCUSDT')  # input order must not matter
    group = {t.event.coin: t.sample_group_id for t in trades}
    assert group['SOLUSDT'] == group['BTCUSDT'] != group['XRPUSDT']


def test_selection_is_deterministic():
    events = [event(coin=c, bar_close=START + timedelta(hours=h)) for c in ('SOLUSDT', 'BTCUSDT') for h in (0, 1, 5)]
    first, _ = BL.select_trades(events, reference_coin='BTCUSDT')
    second, _ = BL.select_trades(list(reversed(events)), reference_coin='BTCUSDT')
    assert [(t.event.coin, t.event.bar_close, t.sample_group_id) for t in first] == [
        (t.event.coin, t.event.bar_close, t.sample_group_id) for t in second
    ]


def test_the_baseline_has_no_votes_gates_or_options_so_any_event_with_a_stop_is_traded():
    odd = event(trend_family='chop', vol_pct=0.99, session='asia', event_flag=True)
    trades, skipped = BL.select_trades([odd], reference_coin='BTCUSDT')
    assert len(trades) == 1 and not skipped


# ---------------------------------------------------------------------------
# resolution with a fake reader
# ---------------------------------------------------------------------------
class FakeReader:
    def __init__(self, rows, funding=True):
        self.rows = rows  # [(open, high, low, close)] from START
        self.funding = funding

    def bars(self, coin, start, end):
        out = []
        for index, (o, h, l, c) in enumerate(self.rows):
            opened = START + index * BAR
            if start <= opened < end:
                out.append(B.Bar(opened, opened + BAR, D(str(o)), D(str(h)), D(str(l)), D(str(c)), opened + BAR))
        return out

    def prints(self, coin, start, end):
        if not self.funding:
            return []
        out, when = [], datetime(2026, 9, 30, 0, 0, tzinfo=UTC)
        while when <= datetime(2026, 10, 8, tzinfo=UTC):
            if start <= when <= end:
                out.append(B.FundingPrint(when, D('0'), D('100')))
            when += timedelta(hours=8)
        return out


def baseline(events, reader, as_of=AS_OF):
    return BL.run_naive_baseline(
        events, reader, reference_coin='BTCUSDT', as_of_time=as_of, funding_max_gap=GAP, missing_bar_after=MISSING
    )


STOP_ROWS = [(100, 100.5, 99.5, 100), (100, 100.5, 97.5, 98)]


def test_a_stop_hit_is_minus_one_r_less_the_frozen_cost_and_the_stats_partition_the_events():
    data = baseline([event(), event(bar_close=START + timedelta(hours=1)), event(stop=None, coin='XRPUSDT')], FakeReader(STOP_ROWS))
    (o,) = data.observations
    assert o.first_barrier == 'lower'
    assert o.stored_r == pytest.approx(-1.0 - 0.06)  # price_r -1, fees 12 bps of 100 over 1R = 2
    key = BaselineKey('replay', 'alts', 'full')
    s = data.stats[key]
    assert (s.events, s.no_stop, s.skipped_hold_open, s.taken, s.filled) == (3, 1, 1, 1, 1)


def test_an_entry_through_the_stop_is_a_no_entry_with_no_r():
    data = baseline([event()], FakeReader([(97.5, 98, 97, 97.5)]))
    (o,) = data.observations
    assert o.first_barrier == 'no_entry' and o.stored_r is None and o.is_no_entry
    assert data.stats[BaselineKey('replay', 'alts', 'full')].no_entry == 1


def test_events_whose_bars_or_funding_are_not_visible_yet_are_pending_not_losses():
    assert not baseline([event()], FakeReader([])).observations
    soon = START + timedelta(hours=1)
    data = baseline([event()], FakeReader(STOP_ROWS, funding=False), as_of=soon)
    assert not data.observations and data.stats[BaselineKey('replay', 'alts', 'full')].pending == 1
    # past the stall cap the labeler's rule applies: labeled with funding_partial, never held forever
    (late,) = baseline([event()], FakeReader(STOP_ROWS, funding=False)).observations
    assert late.funding_partial


def test_a_baseline_run_is_deterministic():
    events = [event(), event(coin='BTCUSDT')]
    assert baseline(events, FakeReader(STOP_ROWS)) == baseline(list(reversed(events)), FakeReader(STOP_ROWS))


# ---------------------------------------------------------------------------
# cohorts
# ---------------------------------------------------------------------------
def test_baseline_cohorts_are_source_coin_set_and_lookback():
    sol = baseline([event()], FakeReader(STOP_ROWS)).observations[0]
    btc = baseline([event(coin='BTCUSDT', source='live')], FakeReader(STOP_ROWS)).observations[0]
    thin = baseline([event(bar_close=P.THIN_LOOKBACK_CUTOFF - timedelta(days=1))], FakeReader([])).stats
    assert sol.cohort == BaselineKey('replay', 'alts', 'full')
    assert btc.cohort == BaselineKey('live', 'covered', 'full')
    assert BaselineKey('replay', 'alts', 'thin') in thin
    assert coin_set_for_path('no_options_book') == 'alts'
    assert {coin_set_for_path(p) for p in ('no_options_history', 'options_layer', 'options_layer_ignored')} == {'covered'}


def test_baseline_statistics_cannot_be_pooled_with_an_app_cohort():
    (o,) = baseline([event()], FakeReader(STOP_ROWS)).observations
    assert isinstance(o, BaselineObservation)
    assert summarize([o], o.cohort).n_filled_raw == 1
    with pytest.raises(MixedCohortError):
        summarize([o], CohortKey('replay', 'no_options_book', 'full'))


# ---------------------------------------------------------------------------
# parity with the app (database)
# ---------------------------------------------------------------------------
UP = datetime(2026, 3, 2, 16, 0, tzinfo=UTC)


def _stop(s):
    add_bar(s, 0, 100, 100.5, 99.5, 100)
    add_bar(s, 1, 100, 100.5, 97.5, 98)


def _target(s):
    add_bar(s, 0, 100, 100.5, 99.5, 100)
    add_bar(s, 1, 100, 102.5, 99.5, 102)


def _ambiguous(s):
    add_bar(s, 0, 100, 100.5, 99.5, 100)
    add_bar(s, 1, 100, 102.5, 97.5, 100)


def _gap_through_stop(s):
    add_bar(s, 0, 100, 100.5, 99.5, 100)
    add_bar(s, 1, 97, 97.5, 96.5, 97)


def _timeout(s):
    add_flat(s, 0, SCALP_BARS - 2)
    add_bar(s, SCALP_BARS - 1, 100, 100.5, 99.5, 101)


def _no_entry(s):
    add_bar(s, 0, 97.5, 98, 97, 97.5)


SCENARIOS = {
    'stop': _stop, 'target': _target, 'ambiguous': _ambiguous, 'gap_through_stop': _gap_through_stop,
    'timeout': _timeout, 'no_entry': _no_entry,
}


#: the bars are written for longs; the short case mirrors them around 100 (price -> 200 - price)
PARITY_CASES = [(name, 'long') for name in sorted(SCENARIOS)] + [('stop', 'short'), ('target', 'short'), ('timeout', 'short')]


@pytest.mark.parametrize(('scenario', 'direction'), PARITY_CASES)
def test_the_baseline_outcome_equals_the_label_of_an_untouched_app_candidate(session, scenario, direction):
    """Parity: the same event, once as an app candidate with unmodified barriers, once through the baseline."""
    SCENARIOS[scenario](session)
    add_funding_grid(session, rates={UP: 0.0007})
    stop = '98' if direction == 'long' else '102'
    if direction == 'short':  # mirror the stop bars for a short
        for candle in session.query(Candle).all():
            candle.open_price, candle.close_price = 200 - candle.open_price, 200 - candle.close_price
            candle.high_price, candle.low_price = 200 - candle.low_price, 200 - candle.high_price
        session.flush()
    candidate = make_candidate(
        session, coin='BTCUSDT', horizon='scalp', bar_close=LABEL_T0, source='live', direction=direction, status='alert',
        options_verdict='ignore', stop_price=D(stop), pre_options_stop_price=D(stop), target_cap_price=None,
        vertical_deadline=None, rule_hash=RULE_HASH,
        feature_overrides=dict(
            swing_break_event=True, swing_break_direction=direction, chandelier_stop_price=D(stop),
            chandelier_stop_distance_atr=D('2'), atr_price=D('1'),
        ),
    )
    run(session)
    label = only_label(session)
    assert label.candidate_id == candidate.id

    data = SJ.compute_baseline(session, as_of_time=LATER)
    (o,) = [x for x in data.observations]
    assert o.first_barrier == label.first_barrier
    assert o.resolved_at == label.resolved_at
    if scenario == 'no_entry':
        assert o.stored_r is None and label.stored_r is None
        return
    for mine, theirs in (
        (o.stored_r, label.stored_r), (o.funding_r, label.funding_r), (o.mae_r, label.mae_r),
        (o.hours_to_resolution, label.hours_to_resolution), (o.fees_r, label.fees_r),
    ):
        assert mine == pytest.approx(float(theirs))
    assert (o.barrier_ambiguous, o.gapped_stop, o.funding_partial) == (label.barrier_ambiguous, label.gapped_stop, label.funding_partial)


def test_the_labeler_and_the_baseline_share_one_resolution_core():
    import inspect

    assert 'O.resolve_outcome' in inspect.getsource(LJ.label_candidate)
    assert 'O.plan_fill' in inspect.getsource(LJ.freeze_fill)
    assert 'O.resolve_outcome' in inspect.getsource(BL.resolve_trade)
    assert O.plan_fill is not None and BL.O is O


def test_features_without_a_swing_break_event_produce_no_baseline_trade(session):
    make_candidate(session, coin='BTCUSDT', horizon='scalp', bar_close=LABEL_T0, source='live', rule_hash=RULE_HASH)
    assert SJ.load_naive_events(session) == []
    assert C.HORIZONS[0] == 'scalp'
