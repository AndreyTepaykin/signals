"""What the schema refuses to store.

Each of these is a mistake a later milestone could plausibly make, turned into a
constraint so it fails at the insert rather than months later in a statistic.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import DataError, IntegrityError

from app.core import constants as C
from app.models import enums as E
from app.models.feeds import Candle, EventCalendar, Funding, OpenInterest
from app.models.labels import Label
from app.models.observability import PromotionLog
from app.models.options import OptionsMetric, OptionsSnapshot
from tests.conftest import BAR_CLOSE, make_candidate, make_feature_row


def a_candle(**overrides) -> Candle:
    values = {
        'coin': 'BTCUSDT',
        'market': E.MARKET_PERP,
        'timeframe': '15m',
        'open_time': BAR_CLOSE - timedelta(minutes=15),
        'close_time': BAR_CLOSE,
        'available_at': BAR_CLOSE,
        'open_price': Decimal('100'),
        'high_price': Decimal('101'),
        'low_price': Decimal('99'),
        'close_price': Decimal('100.5'),
        'base_volume': Decimal('10'),
        'taker_buy_base_volume': Decimal('6'),
    }
    values.update(overrides)
    return Candle(**values)


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------
def test_duplicate_candle_violates_uniqueness(session):
    session.add(a_candle())
    session.flush()
    session.add(a_candle(close_price=Decimal('999')))
    with pytest.raises(IntegrityError):
        session.flush()


def test_the_same_bar_on_perp_and_spot_is_not_a_duplicate(session):
    session.add(a_candle(market=E.MARKET_PERP))
    session.add(a_candle(market=E.MARKET_SPOT))
    session.flush()


def test_a_candle_cannot_be_available_before_it_closed(session):
    session.add(a_candle(available_at=BAR_CLOSE - timedelta(minutes=1)))
    with pytest.raises(IntegrityError):
        session.flush()


def test_an_unknown_timeframe_is_refused(session):
    session.add(a_candle(timeframe='m15'))
    with pytest.raises(IntegrityError):
        session.flush()


def test_taker_buy_volume_cannot_exceed_total_volume(session):
    session.add(a_candle(base_volume=Decimal('5'), taker_buy_base_volume=Decimal('6')))
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_settled_funding_print_cannot_predate_its_settlement(session):
    session.add(
        Funding(
            coin='BTCUSDT',
            kind=E.FUNDING_SETTLED,
            funding_time=BAR_CLOSE,
            available_at=BAR_CLOSE - timedelta(seconds=1),
            funding_rate=Decimal('0.0001'),
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_predicted_funding_rate_may_point_at_a_future_settlement(session):
    session.add(
        Funding(
            coin='BTCUSDT',
            kind=E.FUNDING_PREDICTED,
            funding_time=BAR_CLOSE + timedelta(hours=8),
            available_at=BAR_CLOSE,
            funding_rate=Decimal('0.0001'),
        )
    )
    session.flush()


def test_settled_and_predicted_coexist_for_one_settlement(session):
    for kind in (E.FUNDING_SETTLED, E.FUNDING_PREDICTED):
        session.add(
            Funding(
                coin='BTCUSDT',
                kind=kind,
                funding_time=BAR_CLOSE,
                available_at=BAR_CLOSE,
                funding_rate=Decimal('0.0001'),
            )
        )
    session.flush()


def test_duplicate_open_interest_poll_is_refused(session):
    for _ in range(2):
        session.add(
            OpenInterest(
                coin='BTCUSDT',
                timeframe='15m',
                poll_time=BAR_CLOSE,
                available_at=BAR_CLOSE,
                open_interest_base=Decimal('1000'),
            )
        )
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_macro_calendar_entry_cannot_be_duplicated_despite_the_null_coin(session):
    """NULLS NOT DISTINCT: two CPI prints at one instant are one entry."""
    for _ in range(2):
        session.add(
            EventCalendar(
                kind=C.EVENT_KIND_CPI,
                coin=None,
                event_time=BAR_CLOSE,
                available_at=BAR_CLOSE - timedelta(days=7),
            )
        )
    with pytest.raises(IntegrityError):
        session.flush()


def test_an_unknown_event_kind_is_refused(session):
    session.add(EventCalendar(kind='halving_party', event_time=BAR_CLOSE, available_at=BAR_CLOSE))
    with pytest.raises(IntegrityError):
        session.flush()


# ---------------------------------------------------------------------------
# Options: venues are scored separately and never blended
# ---------------------------------------------------------------------------
def a_snapshot(session, venue=E.OPTIONS_VENUE_DERIBIT) -> OptionsSnapshot:
    snapshot = OptionsSnapshot(
        venue=venue,
        coin='BTC',
        snapshot_time=BAR_CLOSE,
        available_at=BAR_CLOSE,
        payload={'instruments': []},
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def a_metric(snapshot, **overrides) -> OptionsMetric:
    values = {
        'snapshot_id': snapshot.id,
        'venue': snapshot.venue,
        'coin': snapshot.coin,
        'snapshot_time': snapshot.snapshot_time,
        'horizon': E.HORIZONS[1],
        'available_at': snapshot.available_at,
    }
    values.update(overrides)
    return OptionsMetric(**values)


def test_two_venues_hold_separate_metrics_for_the_same_instant(session):
    deribit = a_snapshot(session, E.OPTIONS_VENUE_DERIBIT)
    ibit = a_snapshot(session, E.OPTIONS_VENUE_IBIT)
    session.add(a_metric(deribit, dealer_gamma_sign=-1))
    session.add(a_metric(ibit, dealer_gamma_sign=1))
    session.flush()


def test_one_venue_cannot_hold_two_metrics_for_the_same_instant_and_horizon(session):
    snapshot = a_snapshot(session)
    session.add(a_metric(snapshot))
    session.flush()
    session.add(a_metric(snapshot, dealer_gamma_sign=0))
    with pytest.raises(IntegrityError):
        session.flush()


def test_one_venue_may_hold_one_metric_per_horizon(session):
    snapshot = a_snapshot(session)
    for horizon in E.HORIZONS:
        session.add(a_metric(snapshot, horizon=horizon))
    session.flush()


def test_a_dealer_gamma_sign_must_be_minus_one_zero_or_one(session):
    snapshot = a_snapshot(session)
    session.add(a_metric(snapshot, dealer_gamma_sign=2))
    with pytest.raises(IntegrityError):
        session.flush()


def test_an_oi_share_above_one_is_refused(session):
    snapshot = a_snapshot(session)
    session.add(a_metric(snapshot, top_strike_oi_share=Decimal('1.5')))
    with pytest.raises(IntegrityError):
        session.flush()


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------
def test_duplicate_candidate_for_one_coin_horizon_and_bar_is_refused(session):
    make_candidate(session)
    with pytest.raises(IntegrityError):
        make_candidate(session)


def test_a_suppressed_candidate_must_say_why(session):
    with pytest.raises(IntegrityError):
        make_candidate(session, status=E.STATUS_CANCELLED, status_reason=None)


def test_an_alert_must_have_been_through_the_options_check(session):
    with pytest.raises(IntegrityError):
        make_candidate(session, options_verdict=None)


def test_options_influence_outside_minus_one_to_one_is_refused(session):
    with pytest.raises(IntegrityError):
        make_candidate(session, options_influence=Decimal('1.5'))


def test_an_edge_number_without_its_pool_provenance_is_refused(session):
    with pytest.raises(IntegrityError):
        make_candidate(session, expected_r=Decimal('0.25'))


def test_an_edge_number_with_its_provenance_is_fine(session):
    make_candidate(
        session,
        expected_r=Decimal('0.25'),
        pool_level=E.POOL_LEVELS[0],
        pool_n=42,
        expected_r_ci_low=Decimal('0.05'),
        expected_r_ci_high=Decimal('0.45'),
        pool_interval_confidence=Decimal('0.9'),
    )


def test_an_inverted_interval_is_refused(session):
    with pytest.raises(IntegrityError):
        make_candidate(
            session,
            expected_r=Decimal('0.25'),
            pool_level=E.POOL_LEVELS[0],
            pool_n=42,
            expected_r_ci_low=Decimal('0.45'),
            expected_r_ci_high=Decimal('0.05'),
        )


def test_a_long_cannot_have_its_upper_barrier_below_the_entry(session):
    with pytest.raises(IntegrityError):
        make_candidate(
            session,
            direction=E.DIRECTION_LONG,
            entry_price=Decimal('100'),
            one_r=Decimal('1'),
            upper_barrier_price=Decimal('99'),
            lower_barrier_price=Decimal('99'),
            stop_price=Decimal('99'),
        )


def test_a_short_puts_its_upper_barrier_below_the_entry(session):
    """The spec's 'upper barrier' is +1R in R space, not in price space."""
    make_candidate(
        session,
        direction=E.DIRECTION_SHORT,
        pre_options_stop_price=Decimal('101'),
        stop_price=Decimal('101'),
        entry_price=Decimal('100'),
        one_r=Decimal('1'),
        upper_barrier_price=Decimal('99'),
        lower_barrier_price=Decimal('101'),
    )


def test_a_short_with_a_price_ordered_upper_barrier_is_refused(session):
    with pytest.raises(IntegrityError):
        make_candidate(
            session,
            direction=E.DIRECTION_SHORT,
            pre_options_stop_price=Decimal('101'),
            stop_price=Decimal('101'),
            entry_price=Decimal('100'),
            one_r=Decimal('1'),
            upper_barrier_price=Decimal('101'),
            lower_barrier_price=Decimal('99'),
        )


def test_the_lower_barrier_must_be_the_stop(session):
    with pytest.raises(IntegrityError):
        make_candidate(
            session,
            entry_price=Decimal('100'),
            one_r=Decimal('1'),
            upper_barrier_price=Decimal('101'),
            lower_barrier_price=Decimal('98'),
            stop_price=Decimal('99'),
        )


def test_a_candidate_cannot_borrow_another_coins_feature_row(session):
    other = make_feature_row(coin='ETHUSDT')
    session.add(other)
    session.flush()
    with pytest.raises(IntegrityError):
        make_candidate(session, coin='BTCUSDT', feature_row_id=other.id)


def test_a_decision_cannot_predate_its_signal_bar(session):
    with pytest.raises(IntegrityError):
        make_candidate(session, decision_time=BAR_CLOSE - timedelta(minutes=1))


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------
def a_label(candidate, **overrides) -> Label:
    values = {
        'candidate_id': candidate.id,
        'fill_time': BAR_CLOSE + timedelta(minutes=15),
        'fill_price': Decimal('100'),
        'resolved_at': BAR_CLOSE + timedelta(hours=6),
        'first_barrier': E.BARRIER_UPPER,
        'hours_to_resolution': Decimal('5.75'),
        'price_r': Decimal('1.0'),
        'fees_r': Decimal('0.012'),
        'funding_r': Decimal('-0.004'),
        'stored_r': Decimal('0.984'),
        'mae_r': Decimal('0.3'),
        'cost_assumption_bps': Decimal('12'),
    }
    values.update(overrides)
    return Label(**values)


def test_a_candidate_can_only_have_one_label(session):
    candidate = make_candidate(session)
    session.add(a_label(candidate))
    session.flush()
    session.add(a_label(candidate, first_barrier=E.BARRIER_LOWER))
    with pytest.raises(IntegrityError):
        session.flush()


def test_stored_r_must_equal_its_decomposition(session):
    candidate = make_candidate(session)
    session.add(a_label(candidate, stored_r=Decimal('1.5')))
    with pytest.raises(IntegrityError):
        session.flush()


def test_rounding_of_one_ulp_is_tolerated(session):
    candidate = make_candidate(session)
    session.add(a_label(candidate, stored_r=Decimal('0.984001')))
    session.flush()


def test_fees_are_a_cost_magnitude_not_a_credit(session):
    candidate = make_candidate(session)
    session.add(a_label(candidate, fees_r=Decimal('-0.012'), stored_r=Decimal('1.008')))
    with pytest.raises(IntegrityError):
        session.flush()


def test_mae_is_a_non_negative_magnitude(session):
    candidate = make_candidate(session)
    session.add(a_label(candidate, mae_r=Decimal('-0.3')))
    with pytest.raises(IntegrityError):
        session.flush()


def test_the_mfe_diagnostic_cannot_sit_outside_the_hold(session):
    candidate = make_candidate(session)
    session.add(a_label(candidate, mfe_r=Decimal('1.2'), hours_to_mfe=Decimal('9')))
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_label_cannot_resolve_before_it_filled(session):
    candidate = make_candidate(session)
    session.add(a_label(candidate, resolved_at=BAR_CLOSE))
    with pytest.raises(IntegrityError):
        session.flush()


def test_label_only_columns_accept_nulls_and_are_never_zero_filled(session):
    """DXY and Nasdaq have no feed. A missing print is NULL, not 0."""
    candidate = make_candidate(session)
    label = a_label(candidate, cmi_btc_return=Decimal('0.031'))
    session.add(label)
    session.flush()
    assert label.cmi_dxy_return is None
    assert label.cmi_nasdaq_return is None


# ---------------------------------------------------------------------------
# Promotion log
# ---------------------------------------------------------------------------
def test_a_promotion_decision_without_bin_edges_is_refused(session):
    session.add(
        PromotionLog(
            layer='liquidation_distance_atr',
            decided_at=BAR_CLOSE,
            decision='rejected',
            bin_edges=None,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_rejection_records_its_bin_edges(session):
    session.add(
        PromotionLog(
            layer='liquidation_distance_atr',
            decided_at=BAR_CLOSE,
            decision='rejected',
            paired_outcomes=31,
            criterion_paired_outcomes_ok=False,
            bin_edges=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
        )
    )
    session.flush()


def test_bin_edges_must_be_an_array_or_object(session):
    session.add(
        PromotionLog(
            layer='iv_rank',
            decided_at=BAR_CLOSE,
            decision='rejected',
            bin_edges=7,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


# ---------------------------------------------------------------------------
# Feature rows
# ---------------------------------------------------------------------------
def test_the_shadow_bag_defaults_to_an_empty_object(session):
    row = make_feature_row()
    session.add(row)
    session.flush()
    session.refresh(row)
    assert row.shadow_metrics == {}


def test_a_beta_fit_may_not_run_past_the_signal_bar(session):
    session.add(make_feature_row(beta_fit_end=BAR_CLOSE + timedelta(minutes=1)))
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_realized_vol_percentile_above_one_is_refused(session):
    """Percentiles are stored as 0-1 quantiles; 1.5 trips the CHECK."""
    session.add(make_feature_row(realized_vol_pct=Decimal('1.5')))
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_percentile_written_as_80_does_not_even_fit_the_column(session):
    """numeric(7, 6) leaves one integer digit, so an '80th percentile' overflows."""
    session.add(make_feature_row(realized_vol_pct=Decimal('80')))
    with pytest.raises(DataError):
        session.flush()
