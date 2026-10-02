"""Causality: a feature at bar T is a function of what was knowable at T and nothing else.

Every rolling statistic gets the same three-way test, at several bar closes:

* ``early``    - the market physically truncated at T (later data does not exist),
* ``late``     - the whole market, evaluated at T (later data exists and is visible
                 to a function that forgets to gate on ``available_at``),
* ``poisoned`` - the whole market with everything after T made absurd (x7 prices,
                 x9 open interest, sign-flipped funding).

All three must give the IDENTICAL value, and the value must not be missing
(otherwise the test proves nothing). A fourth test is the positive control: nudging
the NEWEST visible print DOES move every statistic, so equality above is not
because a statistic ignores its data.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.core.features import cross_asset, indicators, levels, positioning, trend, volatility
from app.core.features.assemble import compute_features
from app.core.features.params import PRODUCTION_POLICY as POLICY
from app.core.features.series import Sample
from app.core.features.stats import trailing_percentile, trailing_zscore

from feature_helpers import H1, H4, M15, at_bar, dt, nudge_latest, poison_future, synthetic_inputs, truncate_inputs


@pytest.fixture(scope='module')
def market():
    return synthetic_inputs(with_alt=True)


# bar closes: an ordinary 15m close, a 4h boundary, and the UTC midnight boundary
#             (the session-VWAP reset, where the bar closing at 00:00 belongs to the OLD session)
BAR_CLOSES = [dt(2026, 5, 20, 13, 45), dt(2026, 5, 25, 16, 0), dt(2026, 5, 28, 0, 0)]


# ---------------------------------------------------------------------------
# One extractor per rolling statistic: inputs -> a plain comparable value
# ---------------------------------------------------------------------------
def _atr(i):
    return volatility.wilder_atr(i.perp_4h, H4, i.bar_close).value


def _yz_vol(i):
    samples = volatility.realized_vol_samples(i.perp_1h, H1, i.bar_close)
    return samples[-1].time, samples[-1].value


def _yz_percentile(i):
    samples = volatility.realized_vol_samples(i.perp_1h, H1, i.bar_close)
    return trailing_percentile(samples, samples[-1].time, samples[-1].value, i.bar_close, POLICY).value


def _trend(i):
    state = trend.trend_state(i.perp_1h, H1, i.bar_close)
    return state.ratio, state.family, state.family_source, state.net_change


def _rsi(i):
    return indicators.rsi(i.perp_15m, i.bar_close, i.bar_close, M15).value


def _squeeze(i):
    result = indicators.squeeze(i.perp_15m, i.bar_close, i.bar_close, M15)
    return result.on.value, result.bandwidth_ratio.value


def _cvd(i):
    result = indicators.cvd_divergence(i.perp_15m, i.bar_close, i.bar_close, M15)
    return result.delta.value, result.divergence.value, result.against


def _session_vwap(i):
    return levels.session_vwap(i.perp_15m, i.bar_close, i.bar_close, M15).value


def _profile(i):
    profile, _ = levels.session_volume_profile(i.perp_15m, i.bar_close, i.bar_close, M15)
    return (profile.poc_price, profile.value_area_high, profile.value_area_low) if profile else None


def _swings(i):
    state, _ = levels.swing_state(i.perp_4h, i.bar_close, H4)
    return state


def _chandelier(i):
    state, _ = levels.swing_state(i.perp_4h, i.bar_close, H4)
    if state is None or state.broken is None:
        return None
    atr = volatility.wilder_atr(i.perp_4h, H4, i.bar_close).value
    return levels.chandelier_stop(state.run, state.broken.index, state.break_direction, atr)


def _imbalance_z(i):
    imbalance, z = positioning.aggressor_imbalance(i.perp_15m, i.bar_close, i.bar_close, POLICY)
    return imbalance.value, z.value


def _oi(i):
    q = positioning.oi_price_quadrant(i.oi_1h, i.perp_1h, i.bar_close, POLICY, oi_step=H1, price_step=H1, steps=1)
    return q.quadrant.value, q.oi_change_z.value


def _funding(i):
    return positioning.funding_z(i.funding_settled, i.bar_close, POLICY).z.value


def _basis(i):
    basis, z = positioning.perp_basis(i.perp_1h, i.spot_1h, i.bar_close, POLICY, expected_close=None)
    return basis.value, z.value


def _beta(i):
    alt = cross_asset.log_returns(i.perp_15m, M15, i.bar_close)
    btc = cross_asset.log_returns(i.btc_perp_15m, M15, i.bar_close)
    beta, residual, fit_end = cross_asset.alt_residual_z(alt, btc, i.bar_close, M15, POLICY)
    return beta.value, residual.value, fit_end


STATS = {
    'atr': _atr,
    'realized_vol_yang_zhang': _yz_vol,
    'realized_vol_percentile': _yz_percentile,
    'trend_efficiency_and_family': _trend,
    'rsi': _rsi,
    'squeeze_bollinger_keltner': _squeeze,
    'cvd_divergence': _cvd,
    'session_vwap': _session_vwap,
    'volume_profile': _profile,
    'swing_state': _swings,
    'chandelier_stop': _chandelier,
    'aggressor_imbalance_z': _imbalance_z,
    'oi_price_quadrant_z': _oi,
    'funding_z': _funding,
    'perp_basis_z': _basis,
    'lagged_beta_and_residual': _beta,
}


# The chandelier stop exists only while a 4h swing is broken, which is not true at every bar.
# Use 4h boundaries at which the (hand-verified generator, seeded) market HAS a break, found
# by scanning, and fail loudly if there are none: the test must not pass on missing values.
def _bar_closes_for(market, name):
    if name != 'chandelier_stop':
        return BAR_CLOSES
    found = []
    t = dt(2026, 5, 10)
    while t < dt(2026, 5, 30) and len(found) < 3:
        if STATS['chandelier_stop'](truncate_inputs(market, t)) is not None:
            found.append(t)
        t += H4 * 5
    assert found, 'the synthetic market has no 4h swing break: pick another seed'
    return found


@pytest.mark.parametrize('name', sorted(STATS))
def test_a_statistic_at_T_ignores_everything_after_T(market, name):
    for bar_close in _bar_closes_for(market, name):
        _assert_causal(market, name, bar_close)


def _assert_causal(market, name, bar_close):
    extract = STATS[name]
    early = extract(truncate_inputs(market, bar_close))
    late = extract(at_bar(market, bar_close))
    poisoned = extract(poison_future(market, bar_close))

    assert early is not None, f'{name} is missing at {bar_close}: the test would prove nothing'
    assert late == early, f'{name} changed when LATER data was present'
    assert poisoned == early, f'{name} moved when the future was made absurd'
    if isinstance(early, tuple):
        # at least one component of every tuple-valued statistic is a real number
        assert any(part is not None for part in early), f'{name} is entirely missing at {bar_close}'


@pytest.mark.parametrize('name', sorted(STATS))
def test_positive_control_the_newest_print_does_move_the_statistic(market, name):
    bar_close = dt(2026, 5, 25, 16, 0)  # 4h boundary, so every timeframe has its bar at T
    base = STATS[name](at_bar(market, bar_close))
    nudged = STATS[name](nudge_latest(market, bar_close))
    if name == 'lagged_beta_and_residual':
        # beta is fit WITHOUT the signal bar (that is the lag), so only the residual and not beta moves
        assert nudged[0] == base[0] and nudged[2] == base[2]
        assert nudged[1] != base[1]
    elif name == 'realized_vol_percentile':
        # a percentile is a coarse rank, so a 3% nudge might land in the same rank; the YZ value
        # it is a rank OF must move, and that is what shows the series feeds it
        nudged_vol = STATS['realized_vol_yang_zhang'](nudge_latest(market, bar_close))
        assert nudged_vol != STATS['realized_vol_yang_zhang'](at_bar(market, bar_close))
    else:
        assert nudged != base, f'{name} did not react to a change in the newest print'


# ---------------------------------------------------------------------------
# The whole row
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('bar_close', [dt(2026, 5, 25, 16, 0), dt(2026, 5, 20, 13, 45)], ids=lambda t: t.isoformat())
def test_the_whole_feature_row_is_identical_with_or_without_the_future(market, bar_close):
    early = compute_features(truncate_inputs(market, bar_close))
    late = compute_features(at_bar(market, bar_close))
    poisoned = compute_features(poison_future(market, bar_close))
    assert late.values == early.values and poisoned.values == early.values
    assert late.feature_hash == early.feature_hash == poisoned.feature_hash


def test_appending_later_bars_leaves_an_early_row_unchanged(market):
    """The headline form of the property: compute early, append more history, recompute."""
    bar_close = dt(2026, 5, 15, 8, 0)
    before = compute_features(truncate_inputs(market, bar_close))
    later = truncate_inputs(market, bar_close + timedelta(days=3))
    after = compute_features(at_bar(later, bar_close))
    assert after.values == before.values and after.feature_hash == before.feature_hash
    assert later.perp_15m[-1].close_time > bar_close  # data really was appended


def test_a_print_that_exists_but_was_not_yet_available_equals_no_print(market):
    """available_at, not close_time, is the gate: a late-arriving bar is not visible at T."""
    bar_close = dt(2026, 5, 25, 16, 0)
    base = at_bar(market, bar_close)

    def delay_signal_bar(seq):
        return [replace(b, available_at=bar_close + timedelta(minutes=1)) if b.close_time == bar_close else b for b in seq]

    delayed = replace(base, perp_15m=delay_signal_bar(base.perp_15m))
    assert any(b.close_time == bar_close for b in delayed.perp_15m)  # it exists, it is just not available yet
    removed = replace(base, perp_15m=[b for b in base.perp_15m if b.close_time < bar_close])
    assert compute_features(delayed).feature_hash == compute_features(removed).feature_hash


# ---------------------------------------------------------------------------
# The shared trailing-window rule
# ---------------------------------------------------------------------------
def _samples(values, start=dt(2026, 1, 1)):
    return [Sample(start + H1 * i, start + H1 * i, v) for i, v in enumerate(values)]


def test_trailing_zscore_and_percentile_ignore_appended_later_samples():
    history = _samples([float((i * 7) % 11) for i in range(400)])
    now = history[-1].time + H1
    z0 = trailing_zscore(history, now, 5.0, now, POLICY)
    p0 = trailing_percentile(history, now, 5.0, now, POLICY)
    assert z0.value is not None and p0.value is not None

    future = [Sample(now + H1 * (k + 1), now + H1 * (k + 1), 1.0e9) for k in range(50)]
    assert trailing_zscore(history + future, now, 5.0, now, POLICY) == z0
    assert trailing_percentile(history + future, now, 5.0, now, POLICY) == p0
    # a sample AT the evaluation time is the current observation, not history
    at_now = [Sample(now, now, 1.0e9)]
    assert trailing_zscore(history + at_now, now, 5.0, now, POLICY) == z0


def test_trailing_window_excludes_samples_not_yet_available():
    history = _samples([float((i * 7) % 11) for i in range(400)])
    now = history[-1].time + H1
    base = trailing_zscore(history, now, 5.0, now, POLICY)
    late = [replace(s, available_at=now + timedelta(minutes=1)) if i > 390 else s for i, s in enumerate(history)]
    dropped = history[:391]
    assert trailing_zscore(late, now, 5.0, now, POLICY) == trailing_zscore(dropped, now, 5.0, now, POLICY)
    assert trailing_zscore(late, now, 5.0, now, POLICY) != base


def test_a_statistic_never_includes_the_current_observation_in_its_own_baseline():
    history = _samples([1.0, 2.0, 3.0, 4.0] * 60)
    now = history[-1].time + H1
    z_small = trailing_zscore(history, now, 2.5, now, POLICY).value
    z_huge = trailing_zscore(history, now, 1000.0, now, POLICY).value
    # same baseline, so the z is linear in the observation: (1000 - m)/s vs (2.5 - m)/s
    baseline = [s.value for s in history]
    m = sum(baseline) / len(baseline)
    assert z_huge - z_small == pytest.approx((1000.0 - 2.5) / (_std(baseline)), rel=1e-9)
    assert m == pytest.approx(2.5)
    assert z_small == pytest.approx(0.0, abs=1e-9)


def _std(values):
    m = sum(values) / len(values)
    return (sum((v - m) ** 2 for v in values) / (len(values) - 1)) ** 0.5
