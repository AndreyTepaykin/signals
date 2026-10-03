"""The pure options check: every verdict branch, the price layers, staleness and the empty OI floor.

A long BTC candidate: reference (entry stand-in) 100, 1R = 2, so the pre-options stop is 98, the model target 102
and the upper level 102. ATR 1.0. The decision is at ``T``; the book is 5 minutes old with 1,000 BTC of open
interest against a floor of 500 passed explicitly (the configured floor is empty by design).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core import constants as C
from app.core.options.check import BookView, CheckInputs, check_options
from app.core.options.metrics import StrikeOI

UTC = timezone.utc
T = datetime(2026, 3, 2, 12, 0, 40, tzinfo=UTC)
D = Decimal


def view(**overrides) -> BookView:
    values = dict(
        venue='deribit',
        metric_id=7,
        snapshot_time=T - timedelta(minutes=5),
        total_open_interest=1000.0,
        near_dated_open_interest=1000.0,
        expiry_time=T + timedelta(hours=100),  # outside the 48h pin window
        dealer_gamma_sign=None,
        gamma_flip_price=None,
        max_pain_strike=None,
        top_strike_oi_share=0.05,
        iv_minus_rv_pct=None,
        strikes=(),
    )
    values.update(overrides)
    return BookView(**values)


def inputs(book: BookView | None = None, **overrides) -> CheckInputs:
    values = dict(
        coin='BTCUSDT',
        direction='long',
        decision_time=T,
        status='watch',
        status_reason=C.STATUS_REASON_PENDING_OPTIONS_CHECK,
        reference_price=D('100'),
        one_r=D('2'),
        atr_price=D('1'),
        pre_options_stop_price=D('98'),
        pre_options_target_price=D('102'),
        with_break=True,
        book=view() if book is None else book,
        oi_floor=500.0,
        use_configured_floor=False,
    )
    values.update(overrides)
    return CheckInputs(**values)


def short_inputs(book: BookView, **overrides) -> CheckInputs:
    base = dict(direction='short', pre_options_stop_price=D('102'), pre_options_target_price=D('98'))
    base.update(overrides)
    return inputs(book, **base)


# ---------------------------------------------------------------------------
# align
# ---------------------------------------------------------------------------
def test_nothing_structural_aligns_and_the_card_becomes_an_alert_at_unchanged_size_stop_and_targets():
    d = check_options(inputs())
    assert d.verdict == 'align'
    assert (d.status, d.status_reason) == ('alert', None)
    assert (d.size_multiplier, d.stop_price, d.target_cap_price, d.expression) == (1.0, D('98'), None, 'perp')
    assert (d.venue, d.metric_id, d.snapshot_time) == ('deribit', 7, T - timedelta(minutes=5))
    assert d.influence == 0.0 and d.vol_tag is None
    assert d.confidence == 1.0  # 1000 / (2 x 500), capped at 1


def test_confidence_scales_with_book_depth_and_is_capped():
    d = check_options(inputs(view(total_open_interest=500.0)))
    assert d.confidence == pytest.approx(500.0 / (C.OPTIONS_CONFIDENCE_FULL_AT_FLOOR_MULTIPLE * 500.0))
    assert 0.0 <= d.confidence <= 1.0


# ---------------------------------------------------------------------------
# ignore: missing, stale, thin, and the empty floor
# ---------------------------------------------------------------------------
def test_an_alt_deribit_does_not_list_is_ignored_and_proceeds_to_alert():
    d = check_options(replace(inputs(), coin='SOLUSDT', book=None))
    assert d.verdict == 'ignore' and d.ignore_reason == C.OPTIONS_IGNORE_NO_BOOK_FOR_COIN
    assert d.status == 'alert' and d.venue is None
    assert (d.size_multiplier, d.stop_price, d.target_cap_price, d.influence) == (1.0, D('98'), None, 0.0)
    assert d.status_reason == 'options_ignored:no_options_book'


def test_a_covered_coin_with_no_visible_snapshot_does_not_alert():
    d = check_options(replace(inputs(), book=None))
    assert d.verdict == 'ignore' and d.ignore_reason == C.OPTIONS_IGNORE_NO_SNAPSHOT
    assert (d.status, d.status_reason) == ('watch', 'options_ignored:no_snapshot')
    assert d.venue == 'deribit' and d.metric_id is None and d.stop_price == D('98')


def test_a_snapshot_older_than_15_minutes_is_ignored_and_does_not_alert():
    assert C.OPTIONS_SNAPSHOT_MAX_AGE_MINUTES == 15
    stale = check_options(inputs(view(snapshot_time=T - timedelta(minutes=15, seconds=1))))
    assert stale.verdict == 'ignore' and stale.ignore_reason == C.OPTIONS_IGNORE_SNAPSHOT_TOO_OLD
    assert (stale.status, stale.status_reason) == ('watch', 'options_ignored:snapshot_stale')
    assert stale.dealer_gamma_sign is None and stale.hours_to_expiry is None  # an ignored book's figures are not recorded
    assert stale.metric_id == 7  # but which snapshot was judged is
    exactly = check_options(inputs(view(snapshot_time=T - timedelta(minutes=15))))
    assert exactly.verdict == 'align'  # "older than 15 minutes": exactly 15 is still fresh


def test_unknown_total_open_interest_is_ignored():
    d = check_options(inputs(view(total_open_interest=None)))
    assert d.verdict == 'ignore' and d.ignore_reason == C.OPTIONS_IGNORE_METRIC_INCOMPLETE and d.status == 'watch'


def test_open_interest_below_the_floor_is_ignored():
    d = check_options(inputs(view(total_open_interest=499.9)))
    assert d.verdict == 'ignore' and d.ignore_reason == C.OPTIONS_IGNORE_OI_BELOW_FLOOR
    assert (d.status, d.status_reason) == ('watch', 'options_ignored:oi_below_floor')
    assert check_options(inputs(view(total_open_interest=500.0))).verdict == 'align'  # at the floor is enough


def test_an_unset_floor_is_never_treated_as_met(monkeypatch):
    monkeypatch.setattr(C, 'OPTIONS_OI_FLOOR_BY_COIN', {})
    d = check_options(inputs(use_configured_floor=True, oi_floor=None))
    assert d.verdict == 'ignore' and d.ignore_reason == C.OPTIONS_IGNORE_OI_FLOOR_UNSET
    assert (d.status, d.status_reason) == ('watch', 'options_ignored:oi_floor_unset')
    assert d.confidence is None  # no floor, no depth score


def test_a_configured_floor_is_read_from_the_constants(monkeypatch):
    monkeypatch.setattr(C, 'OPTIONS_OI_FLOOR_BY_COIN', {'BTCUSDT': 300})
    d = check_options(inputs(use_configured_floor=True, oi_floor=None))
    assert d.verdict == 'align' and d.status == 'alert'


def test_the_spec_literal_reading_of_ignore_is_one_constant_away(monkeypatch):
    monkeypatch.setattr(C, 'OPTIONS_IGNORE_BLOCKS_ALERT_ON_COVERED_COINS', False)
    d = check_options(inputs(view(snapshot_time=T - timedelta(hours=2))))
    assert d.verdict == 'ignore' and d.status == 'alert'  # "ignore leaves the card unchanged"


# ---------------------------------------------------------------------------
# expiry_pin
# ---------------------------------------------------------------------------
def pin_view(**overrides):
    values = dict(expiry_time=T + timedelta(hours=20), max_pain_strike=101.0, top_strike_oi_share=0.20)
    values.update(overrides)
    return view(**values)


def test_expiry_pin_delays_the_card_and_caps_the_target_at_max_pain():
    d = check_options(inputs(pin_view()))
    assert d.verdict == 'expiry_pin'
    assert (d.status, d.status_reason) == ('delayed', 'options:expiry_pin')
    assert d.expression == 'wait_for_expiry' and d.target_cap_price == D('101') and d.stop_price == D('98')
    assert d.influence == -1.0 and d.hours_to_expiry == pytest.approx(20.0)


@pytest.mark.parametrize(
    'overrides',
    [
        {'max_pain_strike': 100.0},  # at the entry: not between
        {'max_pain_strike': 102.5},  # beyond the upper level
        {'max_pain_strike': 97.0},  # behind the entry
        {'top_strike_oi_share': 0.1499},  # strike not concentrated enough
        {'expiry_time': T + timedelta(hours=48, minutes=1)},  # outside the 48h window
        {'expiry_time': T - timedelta(minutes=1)},  # already settled
        {'max_pain_strike': None},
    ],
)
def test_expiry_pin_needs_all_of_its_conditions(overrides):
    assert check_options(inputs(pin_view(**overrides))).verdict != 'expiry_pin'


def test_expiry_pin_boundaries_are_inclusive_at_the_upper_level_and_the_share():
    assert check_options(inputs(pin_view(max_pain_strike=102.0, top_strike_oi_share=0.15))).verdict == 'expiry_pin'
    assert check_options(inputs(pin_view(expiry_time=T + timedelta(hours=48)))).verdict == 'expiry_pin'


def test_expiry_pin_for_a_short_looks_below_the_entry():
    d = check_options(short_inputs(pin_view(max_pain_strike=99.0)))
    assert d.verdict == 'expiry_pin' and d.target_cap_price == D('99')
    assert check_options(short_inputs(pin_view(max_pain_strike=101.0))).verdict != 'expiry_pin'


def test_expiry_pin_outranks_headwind_and_amplify():
    book = pin_view(strikes=(StrikeOI(101.0, 400.0, 0.0),), dealer_gamma_sign=-1, gamma_flip_price=100.5)
    assert check_options(inputs(book)).verdict == 'expiry_pin'


# ---------------------------------------------------------------------------
# headwind
# ---------------------------------------------------------------------------
def test_a_strike_with_10_percent_of_near_dated_oi_in_the_path_caps_the_target():
    book = view(strikes=(StrikeOI(101.0, 100.0, 0.0),))  # 10% of 1000, 0.5R of room
    d = check_options(inputs(book))
    assert d.verdict == 'headwind'
    assert (d.status, d.status_reason) == ('alert', None)
    assert d.target_cap_price == D('101') and d.stop_price == D('98') and d.size_multiplier == 1.0
    assert d.influence == -1.0 and d.detail['room_r'] == D('0.5')


def test_less_than_half_an_r_of_room_cancels_the_card():
    book = view(strikes=(StrikeOI(100.9, 0.0, 120.0),))
    d = check_options(inputs(book))
    assert d.verdict == 'headwind'
    assert (d.status, d.status_reason) == ('cancelled', 'options:headwind_room')
    assert d.target_cap_price == D('100.9')
    assert d.vol_tag is None  # a cancelled card carries no vol tag


@pytest.mark.parametrize(
    'strike',
    [StrikeOI(101.0, 99.0, 0.0), StrikeOI(100.0, 300.0, 0.0), StrikeOI(103.0, 300.0, 0.0), StrikeOI(97.0, 300.0, 0.0)],
    ids=['9.9% share', 'at the entry', 'beyond the upper level', 'behind the entry'],
)
def test_a_wall_that_is_too_small_or_not_in_the_path_is_not_a_headwind(strike):
    assert check_options(inputs(view(strikes=(strike,)))).verdict == 'align'


def test_the_nearest_wall_sets_the_cap():
    book = view(strikes=(StrikeOI(101.5, 120.0, 0.0), StrikeOI(101.0, 100.0, 0.0)))
    d = check_options(inputs(book))
    assert d.verdict == 'headwind' and d.target_cap_price == D('101')


def test_a_headwind_wall_for_a_short_sits_below_the_entry():
    book = view(strikes=(StrikeOI(99.0, 0.0, 150.0),))  # 1.0 below the entry = 0.5R
    d = check_options(short_inputs(book))
    assert d.verdict == 'headwind' and d.target_cap_price == D('99') and d.status == 'alert'
    assert check_options(short_inputs(view(strikes=(StrikeOI(101.0, 0.0, 150.0),)))).verdict == 'align'  # the wrong side


def test_headwind_outranks_amplify():
    book = view(strikes=(StrikeOI(101.0, 100.0, 0.0),), dealer_gamma_sign=-1, gamma_flip_price=100.2)
    assert check_options(inputs(book)).verdict == 'headwind'


# ---------------------------------------------------------------------------
# amplify
# ---------------------------------------------------------------------------
def test_short_gamma_with_the_flip_within_one_atr_widens_the_stop_and_sizes_up():
    d = check_options(inputs(view(dealer_gamma_sign=-1, gamma_flip_price=100.8)))
    assert d.verdict == 'amplify'
    assert d.stop_price == D('97.5')  # 98 - 0.5 ATR, away from the entry
    assert d.size_multiplier == C.AMPLIFY_SIZE_MULTIPLIER == 1.15
    assert d.target_cap_price is None and d.status == 'alert' and d.influence == 1.0


def test_amplify_widens_a_shorts_stop_upward():
    d = check_options(short_inputs(view(dealer_gamma_sign=-1, gamma_flip_price=99.5)))
    assert d.verdict == 'amplify' and d.stop_price == D('102.5')


@pytest.mark.parametrize(
    ('overrides', 'with_break'),
    [
        ({'gamma_flip_price': 101.5}, True),  # 1.5 ATR away
        ({'gamma_flip_price': None}, True),
        ({'gamma_flip_price': 100.5}, False),  # not with the break
    ],
)
def test_amplify_needs_short_gamma_a_near_flip_and_the_break(overrides, with_break):
    book = view(dealer_gamma_sign=-1, **overrides)
    assert check_options(inputs(book, with_break=with_break)).verdict == 'align'


def test_amplify_at_exactly_one_atr_still_fires():
    assert check_options(inputs(view(dealer_gamma_sign=-1, gamma_flip_price=101.0))).verdict == 'amplify'


# ---------------------------------------------------------------------------
# dampen
# ---------------------------------------------------------------------------
def test_long_gamma_dampens_size_and_caps_the_target_at_one_r():
    d = check_options(inputs(view(dealer_gamma_sign=1), pre_options_target_price=D('105')))
    assert d.verdict == 'dampen'
    assert d.size_multiplier == C.DAMPEN_SIZE_MULTIPLIER == 0.70
    assert d.target_cap_price == D('102')  # entry + 1R
    assert d.stop_price == D('98') and d.status == 'alert' and d.influence == -1.0


def test_dampen_keeps_a_model_target_that_is_already_nearer():
    d = check_options(inputs(view(dealer_gamma_sign=1), pre_options_target_price=D('101.5')))
    assert d.target_cap_price == D('101.5')


def test_dampen_for_a_short_caps_below_the_entry():
    d = short_inputs(view(dealer_gamma_sign=1), pre_options_target_price=D('95'))
    assert check_options(d).target_cap_price == D('98')


def test_undetermined_gamma_does_not_dampen():
    assert check_options(inputs(view(dealer_gamma_sign=0))).verdict == 'align'


# ---------------------------------------------------------------------------
# vol tags and expression
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(('pct', 'tag'), [(0.80, 'vol_rich'), (0.95, 'vol_rich'), (0.20, 'vol_cheap'), (0.05, 'vol_cheap'), (0.5, None), (0.81, 'vol_rich'), (0.19, 'vol_cheap'), (None, None)])
def test_the_vol_tag_sits_beside_the_verdict(pct, tag):
    d = check_options(inputs(view(iv_minus_rv_pct=pct)))
    assert d.verdict == 'align' and d.vol_tag == tag


def test_cheap_vol_lets_the_expression_be_a_defined_risk_option_and_rich_vol_does_not():
    assert check_options(inputs(view(iv_minus_rv_pct=0.1))).expression == 'defined_risk_option'
    assert check_options(inputs(view(iv_minus_rv_pct=0.9))).expression == 'perp'


def test_a_pin_or_an_ignore_carries_no_vol_tag():
    assert check_options(inputs(pin_view(iv_minus_rv_pct=0.1))).vol_tag is None
    assert check_options(inputs(view(iv_minus_rv_pct=0.1, total_open_interest=1.0))).vol_tag is None


# ---------------------------------------------------------------------------
# shadow rows keep their status; the price layers still resolve
# ---------------------------------------------------------------------------
def test_a_shadow_row_gets_the_verdict_and_prices_but_never_changes_status():
    shadow = dict(status='watch', status_reason=C.STATUS_REASON_VOTE_CONFLICT)
    amplify = check_options(inputs(view(dealer_gamma_sign=-1, gamma_flip_price=100.5), **shadow))
    assert amplify.verdict == 'amplify' and amplify.stop_price == D('97.5')
    assert (amplify.status, amplify.status_reason) == ('watch', C.STATUS_REASON_VOTE_CONFLICT)
    cancelling = check_options(inputs(view(strikes=(StrikeOI(100.5, 200.0, 0.0),)), status='cancelled', status_reason='stand_down:event_window'))
    assert cancelling.verdict == 'headwind' and cancelling.target_cap_price == D('100.5')
    assert (cancelling.status, cancelling.status_reason) == ('cancelled', 'stand_down:event_window')  # not re-labelled
    pinned = check_options(inputs(pin_view(), **shadow))
    assert pinned.status == 'watch'  # a shadow row is never "delayed"
    ignored = check_options(inputs(view(snapshot_time=T - timedelta(hours=1)), **shadow))
    assert (ignored.status, ignored.status_reason) == ('watch', C.STATUS_REASON_VOTE_CONFLICT)


# ---------------------------------------------------------------------------
# the nearest path strike (recorded on the candidate only)
# ---------------------------------------------------------------------------
def test_the_nearest_path_strike_is_the_biggest_strike_between_the_stop_and_the_upper_level():
    book = view(strikes=(StrikeOI(97.0, 999.0, 0.0), StrikeOI(99.0, 10.0, 0.0), StrikeOI(101.0, 0.0, 30.0), StrikeOI(105.0, 999.0, 0.0)))
    d = check_options(inputs(book, status='watch', status_reason=C.STATUS_REASON_VOTE_CONFLICT))
    assert d.nearest_path_strike == D('101')  # 30 beats 10; 97 and 105 are outside [98, 102]


def test_no_strike_in_the_path_means_no_nearest_path_strike():
    assert check_options(inputs(view(strikes=(StrikeOI(150.0, 99.0, 0.0),)))).nearest_path_strike is None


# ---------------------------------------------------------------------------
# invariants every decision must satisfy for the candidate CHECKs
# ---------------------------------------------------------------------------
BOOKS = [
    view(),
    pin_view(),
    view(strikes=(StrikeOI(101.0, 100.0, 0.0),)),
    view(strikes=(StrikeOI(100.9, 100.0, 50.0),)),
    view(dealer_gamma_sign=-1, gamma_flip_price=100.3),
    view(dealer_gamma_sign=1),
    view(snapshot_time=T - timedelta(hours=3)),
    view(total_open_interest=None),
]


@pytest.mark.parametrize('book', BOOKS)
@pytest.mark.parametrize('direction', ['long', 'short'])
def test_every_decision_fits_the_candidate_columns(book, direction):
    d = check_options(inputs(book) if direction == 'long' else short_inputs(book))
    assert d.verdict in C.OPTIONS_VERDICT_PRECEDENCE
    assert d.size_multiplier > 0.0
    assert -1.0 <= d.influence <= 1.0
    assert d.confidence is None or 0.0 <= d.confidence <= 1.0
    assert d.expression in C.OPTIONS_EXPRESSIONS
    assert d.vol_tag in (None, *C.OPTIONS_VOL_TAGS)
    assert d.influence == C.OPTIONS_INFLUENCE_BY_VERDICT[d.verdict]
    assert d.status != 'alert' or d.verdict is not None
    if d.status != 'alert':
        assert d.status_reason  # the M1 CHECK: a non-alert status names its reason
    if d.verdict != 'ignore':
        assert d.metric_id is not None  # every non-ignore verdict names the derived row it came from
    # the stop is on the protective side of the entry, so lower_barrier_price = stop_price can never invert a barrier
    entry = D('100')
    assert d.stop_price < entry if direction == 'long' else d.stop_price > entry


def test_the_decision_is_a_pure_function_of_its_input():
    book = view(dealer_gamma_sign=-1, gamma_flip_price=100.3, iv_minus_rv_pct=0.9)
    first, second = check_options(inputs(book)), check_options(inputs(book))
    assert first == second
    later = check_options(replace(inputs(book), decision_time=T))  # no clock anywhere: same instant, same answer
    assert later == first
