"""Pure options maths, with hand-worked cases. No database, no clock, no network."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.core import constants as C
from app.core.features import params as P
from app.core.features.series import Bar
from app.core.features.volatility import yang_zhang_vol
from app.core.options import greeks
from app.core.options import metrics as M
from app.core.options.book import (
    InstrumentNameError,
    book_from_payload,
    parse_instrument_name,
    quotes_expiring_within,
    years_between,
)
from tests.options_helpers import book, deribit_row, expiry, option_quote

UTC = timezone.utc
SNAP = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
E1 = expiry(4, 10)  # 20h after SNAP
E2 = expiry(5, 10)  # 44h
E3 = expiry(6, 10)  # 68h
E4 = expiry(9, 10)  # 140h: beyond the 72h near-dated window


# ---------------------------------------------------------------------------
# instrument names and the stored payload
# ---------------------------------------------------------------------------
def test_an_instrument_name_gives_the_expiry_at_0800_utc_the_strike_and_the_side():
    got = parse_instrument_name('BTC-27NOV26-73000-P')
    assert (got.currency, got.expiry_time, got.strike, got.side) == ('BTC', datetime(2026, 11, 27, 8, tzinfo=UTC), 73000.0, 'put')
    got = parse_instrument_name('BTC-4OCT26-74000-C')  # the day is not zero padded
    assert got.expiry_time == datetime(2026, 10, 4, 8, tzinfo=UTC) and got.side == 'call'


@pytest.mark.parametrize('name', ['BTC-4OCT26-C', 'BTC-32OCT26-70000-C', 'BTC-4XYZ26-70000-C', 'BTC_USDC-4OCT26-70000-C', 'BTC-4OCT26-0-C', ''])
def test_a_name_that_is_not_an_option_is_refused(name):
    with pytest.raises(InstrumentNameError):
        parse_instrument_name(name)


def test_the_payload_rebuilds_the_book_and_converts_iv_from_percent_to_a_fraction():
    payload = {
        'currency': 'BTC',
        'index_price': 100.0,
        'instruments': [
            {'instrument_name': 'BTC-4OCT26-100-C', 'open_interest': 7.0, 'mark_iv': 39.4, 'underlying_price': 101.0},
            {'instrument_name': 'BTC-4OCT26-100-P', 'open_interest': 3.0, 'mark_iv': 0.0, 'underlying_price': 101.0},
            {'instrument_name': 'garbage', 'open_interest': 1.0, 'mark_iv': 10.0, 'underlying_price': 101.0},
            {'instrument_name': 'ETH-4OCT26-100-C', 'open_interest': 1.0, 'mark_iv': 10.0, 'underlying_price': 101.0},
        ],
    }
    built = book_from_payload(payload, SNAP)
    assert [q.name for q in built.quotes] == ['BTC-4OCT26-100-C', 'BTC-4OCT26-100-P']  # junk and the other currency are skipped
    call, put = built.quotes
    assert call.iv == pytest.approx(0.394) and call.forward == 101.0
    assert put.iv is None and not put.has_iv  # a zero mark IV is "no IV", never a real 0%
    assert built.total_open_interest == 10.0


def test_the_book_is_sorted_so_input_order_cannot_reach_the_arithmetic():
    quotes = [option_quote(E1, 110, 'call', 1), option_quote(E1, 90, 'put', 2), option_quote(E2, 100, 'call', 3)]
    assert book(quotes, SNAP).quotes == book(list(reversed(quotes)), SNAP).quotes


def test_time_to_expiry_is_on_a_365_day_year():
    assert years_between(SNAP, SNAP + timedelta(days=365)) == 1.0
    assert years_between(SNAP, SNAP + timedelta(days=73)) == pytest.approx(0.2)


def test_near_dated_means_after_the_snapshot_and_within_72_hours():
    quotes = [option_quote(e, 100, 'call', 1) for e in (E1, E2, E3, E4)]
    kept = quotes_expiring_within(quotes, SNAP, C.OPTIONS_NEAR_DATED_HOURS)
    assert [q.expiry_time for q in kept] == [E1, E2, E3]
    at_expiry = datetime(2026, 10, 4, 8, tzinfo=UTC)  # an option settling exactly at the snapshot is no longer ahead
    assert quotes_expiring_within([option_quote(E1, 100, 'call', 1)], at_expiry, 72) == ()


# ---------------------------------------------------------------------------
# Black-76 delta / gamma, worked by hand
# ---------------------------------------------------------------------------
def test_black76_at_the_money_with_f_100_vol_50_one_year():
    # d1 = (ln 1 + 0.5 * 0.25 * 1) / (0.5 * 1) = 0.25
    assert greeks.d1(100.0, 100.0, 0.5, 1.0) == pytest.approx(0.25)
    assert greeks.call_delta(100.0, 100.0, 0.5, 1.0) == pytest.approx(0.5987063, abs=1e-6)  # N(0.25)
    assert greeks.put_delta(100.0, 100.0, 0.5, 1.0) == pytest.approx(0.5987063 - 1.0, abs=1e-6)
    # gamma = phi(0.25) / (F sigma sqrt T) = 0.3866681 / 50
    assert greeks.gamma(100.0, 100.0, 0.5, 1.0) == pytest.approx(0.3866681 / 50.0, abs=1e-7)


def test_black76_refuses_a_non_positive_input():
    for args in ((0.0, 100.0, 0.5, 1.0), (100.0, 0.0, 0.5, 1.0), (100.0, 100.0, 0.0, 1.0), (100.0, 100.0, 0.5, 0.0)):
        with pytest.raises(ValueError):
            greeks.gamma(*args)


# ---------------------------------------------------------------------------
# strikes, top strike, max pain
# ---------------------------------------------------------------------------
def _pain_quotes():
    return [
        option_quote(E1, 90, 'put', 12),
        option_quote(E1, 100, 'put', 5),
        option_quote(E1, 100, 'call', 5),
        option_quote(E1, 110, 'call', 10),
    ]


def test_strike_profile_keeps_calls_and_puts_apart_and_sums_expiries():
    quotes = _pain_quotes() + [option_quote(E2, 100, 'call', 2)]
    profile = {s.strike: (s.call_oi, s.put_oi) for s in M.strike_profile(quotes)}
    assert profile == {90.0: (0.0, 12.0), 100.0: (7.0, 5.0), 110.0: (10.0, 0.0)}
    by_strike = {s.strike: s for s in M.strike_profile(quotes)}
    assert by_strike[90.0].side == 'put' and by_strike[100.0].side == 'call' and by_strike[100.0].total == 12.0


def test_top_strike_is_the_largest_total_and_its_share_of_the_expiry():
    # totals 90: 12, 100: 10, 110: 10 of 32
    assert M.top_strike(_pain_quotes()) == (90.0, pytest.approx(12 / 32))


def test_top_strike_ties_go_to_the_lowest_strike_and_an_empty_book_has_none():
    tie = [option_quote(E1, 100, 'call', 5), option_quote(E1, 110, 'put', 5)]
    assert M.top_strike(tie)[0] == 100.0
    assert M.top_strike([]) is None and M.top_strike([option_quote(E1, 100, 'call', 0)]) is None


def test_max_pain_worked_by_hand():
    # pain(90)  = puts: 100 pays 5 * (100-90) = 50                  -> 50
    # pain(100) = calls below: none (call 100 pays 0); puts above: none  -> 0
    # pain(110) = calls: 100 pays 5 * 10 = 50                        -> 50
    assert M.max_pain(_pain_quotes()) == 100.0


def test_max_pain_ties_go_to_the_lowest_strike_and_zero_oi_has_none():
    assert M.max_pain([option_quote(E1, 100, 'call', 5), option_quote(E1, 110, 'call', 5)]) == 100.0  # equal pain at both strikes... lowest wins
    assert M.max_pain([option_quote(E1, 100, 'call', 0)]) is None


# ---------------------------------------------------------------------------
# expiries and the large-expiry flag
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ('hold_hours', 'expected'),
    [(4, E1), (24, E1), (72, E3)],
)
def test_the_nearest_expiry_is_the_one_closest_to_snapshot_plus_hold(hold_hours, expected):
    # targets: 16:00 (diffs 16h to E1, 40h to E2), 12:00 next day (diff 4h to E1), 12:00 in 3 days (diff 4h to E3)
    quotes = [option_quote(e, 100, 'call', 1) for e in (E1, E2, E3, E4)]
    assert M.nearest_expiry(book(quotes, SNAP), hold_hours) == expected


def test_the_nearest_expiry_breaks_a_tie_to_the_earlier_one_and_ignores_the_past():
    quotes = [option_quote(e, 100, 'call', 1) for e in (E1, E2)]
    tie_snap = datetime(2026, 10, 3, 20, 0, tzinfo=UTC)  # +24h = 10-04 20:00, 12h from E1 (08:00) and from E2 (10-05 08:00)
    assert M.nearest_expiry(book(quotes, tie_snap), 24) == E1
    after_both = datetime(2026, 10, 6, 0, 0, tzinfo=UTC)
    assert M.nearest_expiry(book(quotes, after_both), 24) is None


def test_hours_to_expiry():
    assert M.hours_to(book([option_quote(E1, 100, 'call', 1)], SNAP), E1) == 20.0


def test_near_dated_open_interest_sums_only_the_72h_window():
    quotes = [option_quote(E1, 100, 'call', 1), option_quote(E3, 100, 'put', 2), option_quote(E4, 100, 'call', 100)]
    assert M.near_dated_open_interest(book(quotes, SNAP)) == 3.0
    assert M.expiry_open_interest(book(quotes, SNAP), E3) == 2.0


def test_large_expiry_is_the_configured_share_of_total_open_interest():
    share = C.LARGE_EXPIRY_MIN_OI_SHARE
    assert share == 0.05
    assert M.is_large_expiry(5.0, 100.0) is True  # exactly the share counts
    assert M.is_large_expiry(4.99, 100.0) is False
    assert M.is_large_expiry(1.0, 0.0) is False  # no open interest: not large


# ---------------------------------------------------------------------------
# dealer gamma (an estimate: calls +, puts -)
# ---------------------------------------------------------------------------
def _gamma_quotes(call_oi: float, put_oi: float, call_strike: float = 105.0, put_strike: float = 100.0):
    expiry_30d = datetime(2026, 11, 2, 8, tzinfo=UTC)  # 30 days after SNAP at 08:00 -> use a snapshot at 08:00
    return [
        option_quote(expiry_30d, call_strike, 'call', call_oi, iv=0.5, forward=100.0),
        option_quote(expiry_30d, put_strike, 'put', put_oi, iv=0.5, forward=100.0),
    ]


GAMMA_SNAP = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)


def test_only_calls_means_dealers_long_gamma_and_there_is_no_flip():
    result = M.dealer_gamma(_gamma_quotes(10, 0), GAMMA_SNAP, 100.0)
    assert result is not None and result.sign == 1 and result.net_at_spot > 0 and result.flip_price is None


def test_only_puts_means_dealers_short_gamma_and_there_is_no_flip():
    result = M.dealer_gamma(_gamma_quotes(0, 10), GAMMA_SNAP, 100.0)
    assert result is not None and result.sign == -1 and result.net_at_spot < 0 and result.flip_price is None


def test_a_put_wall_at_spot_and_a_bigger_call_wall_above_gives_a_flip_between_them():
    # At S = 100 the put (K=100) outweighs the call (K=105): net < 0. At S = 102 the call has overtaken it.
    result = M.dealer_gamma(_gamma_quotes(100, 100), GAMMA_SNAP, 100.0)
    assert result is not None and result.sign == -1
    assert result.flip_price is not None and 100.0 < result.flip_price < 102.0
    # The flip is a genuine sign change: the net gamma evaluated at it is ~0 compared with the size at spot.
    move = result.flip_price / 100.0 - 1.0
    assert abs(M._net_gamma(_gamma_quotes(100, 100), GAMMA_SNAP, 100.0, move)) < 0.1 * abs(result.net_at_spot)


def test_gamma_is_unknown_without_a_priced_near_quote():
    unpriced = [option_quote(E1, 100, 'call', 5, iv=None)]
    assert M.dealer_gamma(unpriced, SNAP, 100.0) is None
    assert M.dealer_gamma([option_quote(E1, 100, 'call', 0)], SNAP, 100.0) is None
    assert M.dealer_gamma([], SNAP, 100.0) is None


# ---------------------------------------------------------------------------
# ATM IV and 25-delta skew
# ---------------------------------------------------------------------------
def test_atm_iv_is_the_mean_of_the_call_and_put_at_the_strike_nearest_the_forward():
    quotes = [
        option_quote(E1, 95, 'put', 1, iv=0.70),
        option_quote(E1, 100, 'call', 1, iv=0.50),
        option_quote(E1, 100, 'put', 1, iv=0.54),
        option_quote(E1, 105, 'call', 1, iv=0.45),
    ]
    assert M.atm_iv(quotes) == pytest.approx(0.52)
    assert M.atm_iv([option_quote(E1, 100, 'call', 1, iv=0.5), option_quote(E1, 120, 'call', 1, iv=0.9)]) == 0.5  # one side only
    assert M.atm_iv([option_quote(E1, 100, 'call', 1, iv=None)]) is None


SKEW_EXPIRY = datetime(2027, 10, 3, 8, 0, tzinfo=UTC)  # exactly 365 days after GAMMA_SNAP: T = 1 year


def test_skew_25d_is_put_iv_minus_call_iv_at_the_strikes_nearest_25_delta():
    # F = 100, T = 1y. A 25-delta call sits near K = 160 (d1 = -0.69, N = 0.245) and a 25-delta put near K = 80
    # (put delta -0.243). With a flat 0.46 on every call and 0.55 on every put, whichever strikes are nearest,
    # the skew is 0.55 - 0.46.
    quotes = [option_quote(SKEW_EXPIRY, k, 'call', 1, iv=0.46) for k in (140, 150, 160, 170, 180)]
    quotes += [option_quote(SKEW_EXPIRY, k, 'put', 1, iv=0.55) for k in (60, 70, 80, 90, 100)]
    assert M.skew_25d(quotes, GAMMA_SNAP) == pytest.approx(0.09)


def test_skew_picks_the_quote_whose_delta_is_nearest_a_quarter():
    # Each quote's delta uses its OWN IV. Calls: K=150 at vol 0.5 has delta 0.2877 (|d - 0.25| = 0.038); K=200 at vol 0.9
    # has d1 = (ln 0.5 + 0.405) / 0.9 = -0.32, delta 0.374 (0.124 away): 150 wins. Puts: K=80 at vol 0.6 has d1 = 0.672,
    # |delta| = 0.251 (0.001 away); K=40 at vol 0.2 has |delta| ~ 0 (0.25 away): 80 wins.
    assert greeks.call_delta(100.0, 150.0, 0.5, 1.0) == pytest.approx(0.2877, abs=5e-4)
    assert greeks.call_delta(100.0, 200.0, 0.9, 1.0) == pytest.approx(0.374, abs=2e-3)
    assert -greeks.put_delta(100.0, 80.0, 0.6, 1.0) == pytest.approx(0.251, abs=2e-3)
    quotes = [
        option_quote(SKEW_EXPIRY, 150, 'call', 1, iv=0.5),
        option_quote(SKEW_EXPIRY, 200, 'call', 1, iv=0.9),
        option_quote(SKEW_EXPIRY, 80, 'put', 1, iv=0.6),
        option_quote(SKEW_EXPIRY, 40, 'put', 1, iv=0.2),
    ]
    assert M.skew_25d(quotes, GAMMA_SNAP) == pytest.approx(0.6 - 0.5)


def test_skew_is_unknown_when_a_side_is_missing_or_too_far_from_25_delta():
    only_calls = [option_quote(SKEW_EXPIRY, 160, 'call', 1, iv=0.5)]
    assert M.skew_25d(only_calls, GAMMA_SNAP) is None
    at_the_money = [option_quote(SKEW_EXPIRY, 100, 'call', 1, iv=0.5), option_quote(SKEW_EXPIRY, 100, 'put', 1, iv=0.5)]  # |delta| ~ 0.5-0.6
    assert M.skew_25d(at_the_money, GAMMA_SNAP) is None


# ---------------------------------------------------------------------------
# same-tenor realised vol (Yang-Zhang over the same 1h bars the feature engine uses)
# ---------------------------------------------------------------------------
def _hourly(n: int, start: datetime = datetime(2026, 9, 1, tzinfo=UTC), available_lag: timedelta = timedelta(0)) -> list[Bar]:
    bars = []
    price = 100.0
    for i in range(n):
        o = price
        c = price * (1.0 + (0.004 if i % 2 == 0 else -0.003))
        high, low = max(o, c) * 1.002, min(o, c) * 0.998
        opened = start + timedelta(hours=i)
        bars.append(Bar(opened, opened + timedelta(hours=1), o, high, low, c, 10.0, 5.0, opened + timedelta(hours=1) + available_lag))
        price = c
    return bars


@pytest.mark.parametrize(('hours', 'bars'), [(1.0, 24), (23.9, 24), (24.0, 24), (24.2, 25), (48.0, 48), (720.0, 720), (5000.0, 720)])
def test_the_rv_window_is_the_expiry_hours_clamped_to_24_and_720(hours, bars):
    assert M.rv_bar_count(hours) == bars
    assert P.REALIZED_VOL_BARS == 24 and C.OPTIONS_RV_MAX_BARS == 720


def test_same_tenor_rv_is_the_feature_engines_yang_zhang_over_the_last_n_plus_one_bars():
    bars = _hourly(80)
    now = bars[-1].close_time
    got = M.same_tenor_rv(bars, now, 30.0)  # 30h to expiry -> 30 bars -> 31 bars incl. the reference
    assert got == yang_zhang_vol(bars[-31:], timedelta(hours=1))
    assert got is not None and got > 0.0


def test_same_tenor_rv_needs_enough_consecutive_visible_bars():
    bars = _hourly(80)
    assert M.same_tenor_rv(bars[:20], bars[19].close_time, 10.0) is None  # 24 bars needed
    gappy = bars[:40] + bars[41:]
    assert M.same_tenor_rv(gappy, gappy[-1].close_time, 50.0) is None  # a gap breaks the run below 51 bars


def test_same_tenor_rv_does_not_see_a_bar_published_after_the_instant():
    late = _hourly(80, available_lag=timedelta(minutes=10))
    visible_at = late[-1].close_time  # the last bar's available_at is 10 minutes later
    full = M.same_tenor_rv(late, late[-1].available_at, 24.0)
    early = M.same_tenor_rv(late, visible_at, 24.0)
    assert full == yang_zhang_vol(late[-25:], timedelta(hours=1))
    assert early == yang_zhang_vol(late[-26:-1], timedelta(hours=1))
    assert early != full


# ---------------------------------------------------------------------------
# everything for one horizon
# ---------------------------------------------------------------------------
def test_horizon_metrics_for_a_small_book():
    quotes = [
        option_quote(E1, 100, 'call', 6, iv=0.50, forward=100.0),
        option_quote(E1, 100, 'put', 4, iv=0.54, forward=100.0),
        option_quote(E1, 110, 'call', 12, iv=0.45, forward=100.0),
        option_quote(E3, 100, 'put', 20, iv=0.50, forward=100.0),
        option_quote(E4, 100, 'call', 100, iv=0.50, forward=100.0),
    ]
    bars = _hourly(80)
    result = M.derive_horizon_metrics(book(quotes, SNAP), 'scalp', bars, bars_as_of=bars[-1].close_time)
    assert result.horizon == 'scalp' and result.hold_hours == C.VERTICAL_BARRIER_HOURS['scalp']
    assert result.expiry_time == E1 and result.hours_to_expiry == 20.0
    assert result.expiry_open_interest == 22.0  # 6 + 4 + 12
    assert result.near_dated_open_interest == 42.0 and result.total_open_interest == 142.0
    assert result.top_strike == 110.0 and result.top_strike_oi_share == pytest.approx(12 / 22)
    assert result.max_pain_strike == 100.0
    assert result.atm_iv == pytest.approx(0.52)
    assert result.same_tenor_rv is not None and result.iv_minus_rv == pytest.approx(0.52 - result.same_tenor_rv)
    assert result.rv_bars == 24
    assert result.dealer_gamma_sign in (-1, 1)


def test_horizon_metrics_report_what_is_missing_instead_of_inventing_it():
    quotes = [option_quote(E1, 100, 'call', 6, iv=None)]
    result = M.derive_horizon_metrics(book(quotes, SNAP), 'scalp')
    assert result.atm_iv is None and result.same_tenor_rv is None and result.iv_minus_rv is None
    assert result.dealer_gamma_sign is None
    assert 'atm_iv' in result.notes and 'same_tenor_rv' in result.notes
    past = M.derive_horizon_metrics(book(quotes, SNAP + timedelta(days=30)), 'scalp')
    assert past.expiry_time is None and 'expiry' in past.notes


def test_the_maths_is_deterministic_and_clock_free():
    quotes = [option_quote(E1, 100 + 5 * i, 'call' if i % 2 else 'put', 1 + i, iv=0.4 + 0.01 * i) for i in range(8)]
    first = M.derive_horizon_metrics(book(quotes, SNAP), 'swing')
    second = M.derive_horizon_metrics(book(list(reversed(quotes)), SNAP), 'swing')
    assert first == second
    assert not math.isnan(first.near_dated_open_interest)


def test_deribit_row_helper_matches_the_parsers_name_format():
    row = deribit_row('BTC', E1, 96500, 'call', 3.0)
    assert row['instrument_name'] == 'BTC-4OCT26-96500-C'
    assert parse_instrument_name(row['instrument_name']).expiry_time == E1
