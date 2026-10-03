"""The frozen rule ladder, one rule at a time. Pure: no database."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core import constants as C
from app.core.rules import direction as D
from app.core.rules import evaluate as EV
from app.core.rules import triggers as T
from app.core.rules.evaluate import RuleContext, evaluate
from app.core.rules.stops import stop_plan
from tests.rules_helpers import make_inputs, short_overrides


def proposal(context=None, **overrides):
    result = evaluate(make_inputs(**overrides), context)
    assert result.proposal is not None, result.skipped
    return result.proposal


def skipped(context=None, **overrides):
    result = evaluate(make_inputs(**overrides), context)
    assert result.proposal is None, 'a candidate was produced'
    return result.skipped


# ---------------------------------------------------------------------------
# The baseline and the permission
# ---------------------------------------------------------------------------
def test_the_baseline_is_a_pending_long_breakout():
    p = proposal()
    assert (p.direction, p.trigger, p.setup_family, p.trend_family) == ('long', 'swing_break', 'breakout', 'trend')
    assert (p.status, p.status_reason) == ('watch', C.STATUS_REASON_PENDING_OPTIONS_CHECK)


def test_no_candidate_without_swing_break_state():
    assert skipped(swing_break_state=False) == EV.SKIP_NO_PERMISSION
    assert skipped(swing_break_state=None) == EV.SKIP_NO_PERMISSION


def test_no_candidate_without_a_direction():
    assert skipped(swing_break_direction=None) == EV.SKIP_NO_PERMISSION


def test_the_direction_is_the_swing_break_direction_never_a_trigger_choice():
    # aggressor flow and OI both point long, the 4h break says short: the side stays short
    result = evaluate(make_inputs(swing_break_direction='short', chandelier_stop_price=Decimal('98000')))
    assert result.proposal.direction == 'short'


def test_state_true_with_a_stale_event_does_not_fire_the_swing_break_trigger():
    # an old break: the state holds, the event does not; no other trigger fires either
    assert skipped(swing_break_state=True, swing_break_event=False) == EV.SKIP_NO_TRIGGER


# ---------------------------------------------------------------------------
# The swing-break trigger fires on the EVENT, not the STATE
# ---------------------------------------------------------------------------
def test_swing_break_trigger_choice_is_the_event():
    assert C.SWING_BREAK_TRIGGER_FIRES_ON == 'event'
    assert T.swing_break_fires(make_inputs(swing_break_event=True, swing_break_state=False), 'long')
    assert not T.swing_break_fires(make_inputs(swing_break_event=False, swing_break_state=True), 'long')
    assert not T.swing_break_fires(make_inputs(swing_break_event=None, swing_break_state=True), 'long')


def test_a_day_old_break_does_not_re_fire_every_bar():
    # 40 consecutive 15m bars of the same state, event only on the first
    fired = [evaluate(make_inputs(swing_break_event=(i == 0))).proposal is not None for i in range(40)]
    assert fired.count(True) == 1 and fired[0]


def test_an_event_without_the_state_gives_no_candidate():
    # chandelier already breached on the break bar: no permission, no stop
    assert skipped(swing_break_event=True, swing_break_state=False) == EV.SKIP_NO_PERMISSION


# ---------------------------------------------------------------------------
# The efficiency family gates the triggers
# ---------------------------------------------------------------------------
def test_cold_start_family_fires_nothing():
    assert skipped(trend_family=None) == EV.SKIP_FAMILY_UNKNOWN


def test_chop_cannot_fire_the_break_or_the_pullback():
    assert skipped(trend_family='chop', pullback_reset=True) == EV.SKIP_NO_TRIGGER


def test_trend_cannot_fire_the_vwap_tag():
    assert skipped(trend_family='trend', swing_break_event=False, session_vwap_distance_atr=-3.0) == EV.SKIP_NO_TRIGGER


def test_allowed_triggers_follow_the_frozen_table_in_priority_order():
    assert T.allowed_triggers('trend') == ('swing_break', 'pullback_reset', 'squeeze')
    assert T.allowed_triggers('chop') == ('squeeze', 'vwap_tag')
    assert T.allowed_triggers(None) == ()
    assert T.allowed_triggers('sideways') == ()


# ---------------------------------------------------------------------------
# Each trigger
# ---------------------------------------------------------------------------
def test_pullback_reset_trigger_in_trend():
    p = proposal(swing_break_event=False, pullback_reset=True)
    assert (p.trigger, p.setup_family) == ('pullback_reset', 'pullback')


def test_pullback_reset_unknown_does_not_fire():
    assert skipped(swing_break_event=False, pullback_reset=None) == EV.SKIP_NO_TRIGGER


def test_squeeze_fires_when_the_aggressor_picks_the_allowed_side():
    p = proposal(swing_break_event=False, squeeze_on=True, aggressor_imbalance_z=1.0)
    assert (p.trigger, p.setup_family) == ('squeeze', 'squeeze')


def test_squeeze_needs_the_aggressor_to_pick_the_same_side():
    assert skipped(swing_break_event=False, squeeze_on=True, aggressor_imbalance_z=-1.5) == EV.SKIP_NO_TRIGGER
    assert skipped(swing_break_event=False, squeeze_on=True, aggressor_imbalance_z=0.99) == EV.SKIP_NO_TRIGGER
    assert skipped(swing_break_event=False, squeeze_on=True, aggressor_imbalance_z=None) == EV.SKIP_NO_TRIGGER


def test_squeeze_off_or_unknown_does_not_fire():
    assert skipped(swing_break_event=False, squeeze_on=False) == EV.SKIP_NO_TRIGGER
    assert skipped(swing_break_event=False, squeeze_on=None) == EV.SKIP_NO_TRIGGER


def test_squeeze_works_in_chop_too():
    p = proposal(trend_family='chop', swing_break_event=False, squeeze_on=True)
    assert (p.trigger, p.trend_family) == ('squeeze', 'chop')


@pytest.mark.parametrize(
    ('distance', 'fires'),
    [(-1.0, True), (-2.5, True), (-0.99, False), (0.0, False), (1.5, False), (None, False)],
)
def test_vwap_tag_long_needs_a_stretch_below_the_vwap(distance, fires):
    inputs = make_inputs(trend_family='chop', swing_break_event=False, session_vwap_distance_atr=distance)
    assert T.vwap_tag_fires(inputs, 'long') is fires


@pytest.mark.parametrize(('distance', 'fires'), [(1.0, True), (3.0, True), (0.99, False), (-2.0, False), (None, False)])
def test_vwap_tag_short_needs_a_stretch_above_the_vwap(distance, fires):
    inputs = make_inputs(trend_family='chop', swing_break_event=False, session_vwap_distance_atr=distance, **short_overrides())
    assert T.vwap_tag_fires(inputs, 'short') is fires


def test_vwap_tag_candidate_in_chop_is_the_reversion_family():
    p = proposal(trend_family='chop', swing_break_event=False, session_vwap_distance_atr=-1.4)
    assert (p.trigger, p.setup_family, p.direction) == ('vwap_tag', 'reversion', 'long')


def test_the_first_trigger_in_priority_order_is_recorded():
    p = proposal(swing_break_event=True, pullback_reset=True, squeeze_on=True)
    assert p.trigger == 'swing_break'
    assert p.fired_triggers == ('swing_break', 'pullback_reset', 'squeeze')
    p = proposal(trend_family='chop', swing_break_event=False, squeeze_on=True, session_vwap_distance_atr=-2.0)
    assert (p.trigger, p.fired_triggers) == ('squeeze', ('squeeze', 'vwap_tag'))


# ---------------------------------------------------------------------------
# The direction vote
# ---------------------------------------------------------------------------
def votes(**overrides):
    return D.source_votes(make_inputs(**overrides))


def test_aggressor_vote_has_a_dead_band_at_one_sigma():
    assert votes(aggressor_imbalance_z=1.0)['aggressor_imbalance'] == 1
    assert votes(aggressor_imbalance_z=0.99)['aggressor_imbalance'] == 0
    assert votes(aggressor_imbalance_z=-1.0)['aggressor_imbalance'] == -1
    assert votes(aggressor_imbalance_z=None)['aggressor_imbalance'] == 0


@pytest.mark.parametrize(
    ('quadrant', 'vote'),
    [
        ('price_up_oi_up', 1),
        ('price_down_oi_up', -1),
        ('price_up_oi_down', 0),
        ('price_down_oi_down', 0),
        ('price_up_oi_flat', 0),
        ('price_down_oi_flat', 0),
        (None, 0),
    ],
)
def test_oi_quadrant_votes_only_on_new_risk(quadrant, vote):
    assert votes(oi_price_quadrant=quadrant)['oi_price_quadrant'] == vote


def test_funding_and_basis_are_read_as_crowding_so_contrarian():
    assert votes(funding_z=2.0)['funding'] == -1  # crowded longs argue short
    assert votes(funding_z=-1.0)['funding'] == 1
    assert votes(funding_z=0.5)['funding'] == 0
    assert votes(perp_basis_z=1.5)['perp_basis'] == -1
    assert votes(perp_basis_z=-1.5)['perp_basis'] == 1
    assert votes(perp_basis_z=None)['perp_basis'] == 0


def test_tally_counts_for_and_against_the_break_direction():
    t = D.tally('long', make_inputs(aggressor_imbalance_z=1.5, oi_price_quadrant='price_down_oi_up', funding_z=-2.0))
    assert (t.votes_for, t.votes_against, t.net, t.confirmed) == (2, 1, 1, True)
    t = D.tally('short', make_inputs(aggressor_imbalance_z=1.5, oi_price_quadrant='price_down_oi_up', funding_z=-2.0))
    assert (t.votes_for, t.votes_against, t.net, t.confirmed) == (1, 2, -1, False)


def test_a_tie_confirms_and_an_all_abstain_vote_confirms():
    assert D.tally('long', make_inputs(aggressor_imbalance_z=1.5, oi_price_quadrant='price_down_oi_up')).confirmed
    quiet = make_inputs(aggressor_imbalance_z=None, oi_price_quadrant=None, funding_z=None, perp_basis_z=None)
    t = D.tally('long', quiet)
    assert (t.votes_for, t.votes_against, t.confirmed) == (0, 0, True)


def test_a_majority_against_makes_it_a_watch_with_a_reason():
    p = proposal(aggressor_imbalance_z=-1.5, oi_price_quadrant='price_down_oi_up', funding_z=None)
    assert (p.status, p.status_reason) == ('watch', C.STATUS_REASON_VOTE_CONFLICT)
    assert p.direction == 'long'  # the row still has a direction; Watch is a status


def test_null_votes_are_abstentions_not_zero_readings():
    t = D.tally('long', make_inputs(aggressor_imbalance_z=None, oi_price_quadrant=None, funding_z=None, perp_basis_z=-1.2))
    assert t.votes == {'aggressor_imbalance': 0, 'oi_price_quadrant': 0, 'funding': 0, 'perp_basis': 1}


# ---------------------------------------------------------------------------
# One BTC view
# ---------------------------------------------------------------------------
def test_btc_needs_no_residual():
    assert evaluate(make_inputs(is_reference_coin=True, alt_residual_z_signal_bar=None)).proposal is not None


@pytest.mark.parametrize(('z', 'clears'), [(1.0, True), (2.5, True), (0.99, False), (-3.0, False), (None, False)])
def test_alt_long_needs_residual_strength_of_one_sigma(z, clears):
    inputs = make_inputs(coin='ETHUSDT', is_reference_coin=False, alt_residual_z_signal_bar=z)
    assert EV.alt_gate_clears(inputs, 'long') is clears
    assert (evaluate(inputs).proposal is not None) is clears
    if not clears:
        assert evaluate(inputs).skipped == EV.SKIP_ALT_GATE


@pytest.mark.parametrize(('z', 'clears'), [(-1.0, True), (-4.0, True), (-0.5, False), (2.0, False), (None, False)])
def test_alt_short_needs_negative_residual_strength(z, clears):
    inputs = make_inputs(coin='ETHUSDT', is_reference_coin=False, alt_residual_z_signal_bar=z, **short_overrides())
    assert EV.alt_gate_clears(inputs, 'short') is clears


# ---------------------------------------------------------------------------
# One open signal per coin per horizon
# ---------------------------------------------------------------------------
def test_an_open_signal_blocks_the_next_candidate_entirely():
    assert skipped(context=RuleContext(has_open_signal=True)) == EV.SKIP_OPEN_SIGNAL


def test_open_signal_is_checked_after_the_gates_that_make_a_candidate():
    # no permission wins over the open signal: nothing to suppress
    assert skipped(context=RuleContext(has_open_signal=True), swing_break_state=False) == EV.SKIP_NO_PERMISSION


# ---------------------------------------------------------------------------
# Stand-down and CVD (shadow rows, not silence)
# ---------------------------------------------------------------------------
def test_event_window_stands_down_with_a_shadow_row():
    p = proposal(event_flag=True)
    assert (p.status, p.status_reason) == ('cancelled', 'stand_down:event_window')


def test_unknown_event_calendar_does_not_stand_down_by_default():
    assert C.STAND_DOWN_ON_UNKNOWN_EVENT_CALENDAR is False
    assert proposal(event_flag=None).status_reason == C.STATUS_REASON_PENDING_OPTIONS_CHECK


@pytest.mark.parametrize(('gap', 'stands'), [(1.0, False), (1.0001, True), (3.0, True), (0.0, False)])
def test_perp_spot_disagreement_over_one_atr_stands_down(gap, stands):
    p = proposal(perp_spot_disagreement_atr=gap)
    assert (p.status == 'cancelled') is stands
    if stands:
        assert p.status_reason == 'stand_down:perp_spot_disagreement'


def test_missing_spot_comparison_stands_down():
    assert C.STAND_DOWN_ON_UNKNOWN_PERP_SPOT is True
    assert proposal(perp_spot_disagreement_atr=None).status_reason == 'stand_down:perp_spot_disagreement'


def test_a_feed_past_its_rule_stands_down():
    result = evaluate(make_inputs(), RuleContext(feed_breaches=('binance_kline_perp_15m:BTCUSDT',)))
    assert result.proposal.status_reason == 'stand_down:feed_stale'


def test_simultaneous_stand_downs_are_listed_in_a_fixed_order():
    result = evaluate(
        make_inputs(event_flag=True, perp_spot_disagreement_atr=2.0), RuleContext(feed_breaches=('x:BTCUSDT',))
    )
    assert result.proposal.status_reason == 'stand_down:feed_stale+event_window+perp_spot_disagreement'
    assert result.proposal.stand_downs == ('feed_stale', 'event_window', 'perp_spot_disagreement')


def test_cvd_divergence_against_the_direction_cancels():
    p = proposal(cvd_divergence=True, cvd_divergence_against='long')
    assert (p.status, p.status_reason) == ('cancelled', C.STATUS_REASON_CVD_DIVERGENCE)


def test_cvd_divergence_against_the_other_side_is_the_fade_and_is_allowed():
    p = proposal(cvd_divergence=True, cvd_divergence_against='short')
    assert p.status_reason == C.STATUS_REASON_PENDING_OPTIONS_CHECK


def test_cvd_unknown_does_not_cancel():
    assert proposal(cvd_divergence=None, cvd_divergence_against=None).status == 'watch'


def test_status_precedence_stand_down_then_cvd_then_vote():
    conflict = dict(aggressor_imbalance_z=-1.5, oi_price_quadrant='price_down_oi_up', funding_z=None)
    cvd = dict(cvd_divergence=True, cvd_divergence_against='long')
    assert proposal(event_flag=True, **cvd, **conflict).status_reason == 'stand_down:event_window'
    assert proposal(**cvd, **conflict).status_reason == C.STATUS_REASON_CVD_DIVERGENCE
    assert proposal(**conflict).status_reason == C.STATUS_REASON_VOTE_CONFLICT


def test_the_rules_never_emit_an_alert_or_an_abstain():
    # the schema forbids an alert without an options verdict, and abstain needs a pool (M8)
    statuses = {
        proposal().status,
        proposal(event_flag=True).status,
        proposal(cvd_divergence=True, cvd_divergence_against='long').status,
        proposal(aggressor_imbalance_z=-2.0, oi_price_quadrant='price_down_oi_up').status,
    }
    assert statuses == {'watch', 'cancelled'}


# ---------------------------------------------------------------------------
# The chandelier stop as 1R and the pre-options target
# ---------------------------------------------------------------------------
def test_long_stop_and_target_in_r():
    plan = proposal().plan
    assert plan.stop_price == Decimal('95000.0000000000')
    assert plan.reference_price == Decimal('96500.0000000000')
    assert plan.one_r == Decimal('1500.0000000000')
    assert plan.target_price == Decimal('98000.0000000000')  # +1R above the reference close


def test_short_stop_is_above_and_the_target_below():
    plan = proposal(**short_overrides()).plan
    assert plan.stop_price == Decimal('98000')
    assert plan.reference_price == Decimal('96500')
    assert plan.target_price == Decimal('95000')


def test_the_stop_is_read_from_the_feature_row_not_recomputed():
    plan = proposal(chandelier_stop_price=Decimal('90123.4567891234')).plan
    assert plan.stop_price == Decimal('90123.4567891234')


def test_a_breached_or_missing_stop_is_not_a_candidate():
    assert skipped(chandelier_stop_distance_atr=Decimal('0')) == EV.SKIP_NO_STOP
    assert skipped(chandelier_stop_distance_atr=Decimal('-0.2')) == EV.SKIP_NO_STOP
    assert skipped(chandelier_stop_distance_atr=None) == EV.SKIP_NO_STOP
    assert skipped(chandelier_stop_price=None) == EV.SKIP_NO_STOP
    assert skipped(atr_price=None) == EV.SKIP_NO_STOP
    assert skipped(atr_price=Decimal('0')) == EV.SKIP_NO_STOP


def test_a_short_target_below_zero_is_stored_as_null_not_negative():
    overrides = {
        **short_overrides(),
        'chandelier_stop_price': Decimal('150'),
        'chandelier_stop_distance_atr': Decimal('2'),
        'atr_price': Decimal('40'),
    }
    inputs = make_inputs(**overrides)
    plan = stop_plan(inputs, 'short')
    assert plan.reference_price == Decimal('70')
    assert plan.target_price is None  # 70 - 80 < 0


def test_prices_are_rounded_to_the_column_scale_half_even():
    plan = stop_plan(
        make_inputs(chandelier_stop_distance_atr=Decimal('1.0000000001'), atr_price=Decimal('0.5')), 'long'
    )
    assert plan.one_r == Decimal('0.5000000000')  # 0.50000000005 rounds half-even to the even digit
    assert plan.one_r.as_tuple().exponent == -10


def test_the_pure_rules_do_not_touch_the_clock_database_or_environment():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / 'app' / 'core' / 'rules'
    source = '\n'.join(p.read_text(encoding='utf-8') for p in root.glob('*.py'))
    for forbidden in ('utc_now', 'datetime.now', 'time.time', 'sqlalchemy', 'os.environ', 'get_settings', 'import random'):
        assert forbidden not in source, forbidden
