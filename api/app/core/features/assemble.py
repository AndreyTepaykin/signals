"""Inputs -> the ``feature_row`` mapping and its ``feature_hash``.

:func:`compute_features` is a pure function of a :class:`FeatureInputs`. It
reads no database, no clock and no environment. The worker's job fills a
``FeatureInputs`` from ``as_of(bar_close)`` reads; a replay can fill one from
memory. Both then get the same mapping and the same hash from the same data.

Everything is evaluated at ``as_of = bar_close``. The pure functions each
re-filter their inputs by ``available_at <= bar_close``, so even a caller that
passes the future gets no future in the answer.

**Staleness is not currency.** A series whose newest visible bar is not the bar
that SHOULD be newest at ``bar_close`` (a 1h feed that stopped, a 4h bar that is
late) gives NULL for the features built on it. Reusing the previous bar's
values under this bar's key would be a number that looks current and is not.

The one input that is not point-in-time is ``funding_predicted``: the feed
stores only its latest estimate. It is used for the live carry band alone, only
when ``source == 'live'``, and ignored for a replay no matter what is passed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.core import constants as C
from app.core.features import cross_asset, indicators, levels, liquidation, positioning, trend, volatility
from app.core.features import params as P
from app.core.features.columns import ALWAYS_NULL, FEATURE_COLUMNS, normalise_mapping
from app.core.features.params import PRODUCTION_POLICY, HistoryPolicy, timeframe_delta
from app.core.features.reading import Reading, missing
from app.core.features.series import (
    Bar,
    CalendarEvent,
    ForcePrint,
    FundingPrint,
    OIPoint,
    floor_to,
    visible_bars,
    visible_oi,
)
from app.core.features.stats import trailing_percentile
from app.core.hashing import feature_hash
from app.core.time import active_event_windows, classify_session, ensure_utc, is_weekend

SOURCES = ('live', 'replay')


@dataclass(frozen=True)
class FeatureInputs:
    """Everything the engine may look at for one coin at one signal bar.

    Lists need not be sorted or pre-filtered. The 15m / 1h / 4h perp series
    drive almost everything; ``btc_perp_15m`` is the reference for beta and is
    ignored when ``is_reference_coin`` (BTC itself has no beta to BTC).
    """

    coin: str
    bar_close: datetime
    horizon: str
    source: str
    perp_15m: Sequence[Bar]
    spot_15m: Sequence[Bar]
    perp_1h: Sequence[Bar]
    spot_1h: Sequence[Bar]
    perp_4h: Sequence[Bar]
    btc_perp_15m: Sequence[Bar] | None = None
    is_reference_coin: bool = False
    oi_1h: Sequence[OIPoint] = ()
    funding_settled: Sequence[FundingPrint] = ()
    funding_predicted: FundingPrint | None = None
    force_orders: Sequence[ForcePrint] = ()
    events: Sequence[CalendarEvent] = ()
    calendar_loaded: bool = False


@dataclass(frozen=True)
class FeatureComputation:
    #: Every feature column -> its normalised value (None = NULL). Exactly the keys of FEATURE_COLUMNS.
    values: dict[str, Any]
    #: Why a NULL is NULL. Diagnostic only; not hashed, not stored.
    reasons: dict[str, str] = field(default_factory=dict)
    #: sha256 of ``values`` (see ``app.core.hashing``).
    feature_hash: str = ''


def _is_current(bars: Sequence[Bar], bar_close: datetime, step: timedelta) -> bool:
    seen = visible_bars(bars, bar_close)
    return bool(seen) and seen[-1].close_time == floor_to(bar_close, step)


def compute_features(inputs: FeatureInputs, policy: HistoryPolicy = PRODUCTION_POLICY) -> FeatureComputation:
    """Compute every ``feature_row`` feature column for one coin at one signal bar."""
    if inputs.source not in SOURCES:
        raise ValueError(f'source must be one of {SOURCES}, got {inputs.source!r}')
    if inputs.horizon not in C.HORIZONS:
        raise ValueError(f'horizon must be one of {C.HORIZONS}, got {inputs.horizon!r}')
    t = ensure_utc(inputs.bar_close)
    step15 = timeframe_delta(P.SIGNAL_TIMEFRAME)
    step1h = timeframe_delta('1h')
    step4h = timeframe_delta(P.ATR_TIMEFRAME)
    if floor_to(t, step15) != t:
        raise ValueError(f'bar_close {t.isoformat()} is not on a {P.SIGNAL_TIMEFRAME} boundary')

    raw: dict[str, Any] = {name: None for name in FEATURE_COLUMNS}
    raw['shadow_metrics'] = {}
    why: dict[str, str] = {}

    def put(name: str, reading: Reading) -> Any:
        if reading.ok:
            raw[name] = reading.value
            return reading.value
        why[name] = reading.reason or 'unavailable'
        return None

    def put_many(names: Sequence[str], reason: str) -> None:
        for name in names:
            why[name] = reason

    for name in ALWAYS_NULL:
        why[name] = 'no Phase 1 feed for this column'

    # ---- the signal bar and the ATR every distance is divided by -----------
    perp15 = visible_bars(inputs.perp_15m, t)
    signal_bar = perp15[-1] if perp15 and perp15[-1].close_time == t else None
    price = signal_bar.close if signal_bar is not None else None

    if _is_current(inputs.perp_4h, t, step4h):
        atr_reading = volatility.wilder_atr(inputs.perp_4h, step4h, t)
    else:
        atr_reading = missing('the 4h perp series is not current at the signal bar')
    atr = put('atr_price', atr_reading)
    if atr is not None:
        raw['atr_timeframe'] = P.ATR_TIMEFRAME
    else:
        why['atr_timeframe'] = why['atr_price']

    def distance(name: str, level: float | None, level_reason: str) -> None:
        if level is None:
            why.setdefault(name, level_reason)
        elif price is None:
            why[name] = 'signal bar is not in the visible 15m series'
        elif atr is None:
            why[name] = 'ATR unavailable'
        else:
            raw[name] = levels.distance_atr(price, level, atr)

    # ---- rank 1: aggressor imbalance --------------------------------------
    imbalance, imbalance_z = positioning.aggressor_imbalance(inputs.perp_15m, t, t, policy)
    put('aggressor_imbalance', imbalance)
    put('aggressor_imbalance_z', imbalance_z)

    # ---- rank 2: OI-price quadrant ----------------------------------------
    oi_step = timeframe_delta(P.OI_SERIES_TIMEFRAME)
    oi_seen = visible_oi(inputs.oi_1h, t)
    if oi_seen and t - oi_seen[-1].poll_time > oi_step * P.OI_MAX_AGE_PERIODS:
        stale = missing(f'newest visible open interest is from {oi_seen[-1].poll_time.isoformat()}, which is stale')
        put('oi_price_quadrant', stale)
        put('oi_change_z', stale)
    else:
        quadrant = positioning.oi_price_quadrant(
            inputs.oi_1h, inputs.perp_1h, t, policy, oi_step=oi_step, price_step=step1h, steps=P.OI_CHANGE_STEPS
        )
        put('oi_price_quadrant', quadrant.quadrant)
        put('oi_change_z', quadrant.oi_change_z)

    # ---- rank 3: funding ---------------------------------------------------
    funding = positioning.funding_z(inputs.funding_settled, t, policy)
    if funding.last_print is not None and t - funding.last_print.funding_time > P.FUNDING_MAX_AGE:
        put('funding_z', missing(f'newest settled funding print is from {funding.last_print.funding_time.isoformat()}, which is stale'))
    else:
        put('funding_z', funding.z)
    predicted = inputs.funding_predicted if inputs.source == 'live' else None
    if predicted is None:
        reason = 'predicted funding is not point-in-time, so replay rows carry no live band' if inputs.source == 'replay' else 'no predicted funding estimate'
        put_many(('funding_rate_current', 'funding_carry_bps_horizon'), reason)
    else:
        raw['funding_rate_current'] = predicted.rate
        if predicted.interval_hours is None:
            why['funding_carry_bps_horizon'] = 'the predicted estimate does not state its settlement interval'
        else:
            raw['funding_carry_bps_horizon'] = positioning.funding_carry_bps(
                predicted.rate, predicted.interval_hours, C.VERTICAL_BARRIER_HOURS[inputs.horizon]
            )

    # ---- rank 5: trend efficiency and family ------------------------------
    trend_net_change: float | None = None
    family: str | None = None
    if _is_current(inputs.perp_1h, t, step1h):
        state = trend.trend_state(inputs.perp_1h, step1h, t)
        if state.ratio is not None:
            raw['trend_efficiency'] = state.ratio
            trend_net_change = state.net_change
        else:
            why['trend_efficiency'] = state.reason or 'unavailable'
        family = state.family
        if family is not None:
            raw['trend_family'] = family
        else:
            why['trend_family'] = state.reason or 'unavailable'
    else:
        put_many(('trend_efficiency', 'trend_family'), 'the 1h perp series is not current at the signal bar')

    # ---- rank 6: realized volatility (annualised Yang-Zhang) --------------
    expected_1h = floor_to(t, step1h)
    vol_samples = volatility.realized_vol_samples(inputs.perp_1h, step1h, t)
    if vol_samples and vol_samples[-1].time == expected_1h:
        current_vol = vol_samples[-1]
        raw['realized_vol_yz'] = current_vol.value
        put('realized_vol_pct', trailing_percentile(vol_samples, current_vol.time, current_vol.value, t, policy))
    else:
        put_many(('realized_vol_yz', 'realized_vol_pct'), f'no {P.REALIZED_VOL_BARS + 1} consecutive 1h bars ending at {expected_1h.isoformat()}')

    # ---- rank 11: perp basis ----------------------------------------------
    basis_bps, basis_z = positioning.perp_basis(inputs.perp_1h, inputs.spot_1h, t, policy, expected_close=expected_1h)
    put('perp_basis_bps', basis_bps)
    put('perp_basis_z', basis_z)

    # ---- conditioners: lagged beta vs BTC ----------------------------------
    if inputs.is_reference_coin:
        put_many(('beta_vs_btc', 'beta_fit_end', 'alt_residual_z_signal_bar'), 'BTC is the reference: no beta to itself')
    elif inputs.btc_perp_15m is None:
        put_many(('beta_vs_btc', 'beta_fit_end', 'alt_residual_z_signal_bar'), 'no BTC reference series was supplied')
    else:
        alt_returns = cross_asset.log_returns(inputs.perp_15m, step15, t)
        btc_returns = cross_asset.log_returns(inputs.btc_perp_15m, step15, t)
        beta, residual, fit_end = cross_asset.alt_residual_z(alt_returns, btc_returns, t, step15, policy)
        put('beta_vs_btc', beta)
        put('alt_residual_z_signal_bar', residual)
        if fit_end is not None:
            raw['beta_fit_end'] = fit_end
        else:
            why['beta_fit_end'] = beta.reason or 'unavailable'

    # ---- indicators: filter ------------------------------------------------
    cvd = indicators.cvd_divergence(inputs.perp_15m, t, t, step15)
    put('cvd_delta', cvd.delta)
    put('cvd_divergence', cvd.divergence)
    if cvd.divergence.ok:
        raw['cvd_divergence_against'] = cvd.against
        if cvd.against is None:
            # a divergence that is not there argues against nothing: NULL is the true value, not a gap
            why['cvd_divergence_against'] = 'no CVD divergence, so no direction is argued against'
    else:
        why['cvd_divergence_against'] = cvd.reason or 'unavailable'

    # ---- indicators: location ----------------------------------------------
    vwap = levels.session_vwap(inputs.perp_15m, t, t, step15)
    session_vwap_value = vwap.value if vwap.ok else None
    distance('session_vwap_distance_atr', session_vwap_value, vwap.reason or 'unavailable')

    profile, profile_reason = levels.session_volume_profile(inputs.perp_15m, t, t, step15)
    if profile is not None:
        raw['poc_price'] = profile.poc_price
        raw['value_area_high'] = profile.value_area_high
        raw['value_area_low'] = profile.value_area_low
    else:
        put_many(('poc_price', 'value_area_high', 'value_area_low'), profile_reason or 'unavailable')
    distance('poc_distance_atr', profile.poc_price if profile else None, profile_reason or 'unavailable')
    distance('value_area_high_distance_atr', profile.value_area_high if profile else None, profile_reason or 'unavailable')
    distance('value_area_low_distance_atr', profile.value_area_low if profile else None, profile_reason or 'unavailable')

    # ---- indicators: 4h structure -------------------------------------------
    swings: levels.SwingState | None = None
    if _is_current(inputs.perp_4h, t, step4h):
        swings, swing_reason = levels.swing_state(inputs.perp_4h, t, step4h)
    else:
        swing_reason = 'the 4h perp series is not current at the signal bar'
    if swings is None:
        put_many(
            (
                'swing_break',
                'swing_break_direction',
                'last_swing_high',
                'last_swing_low',
                'swing_anchor_time',
                'swing_vwap_anchor_time',
                'chandelier_stop_price',
                'chandelier_stop_distance_atr',
            ),
            swing_reason or 'unavailable',
        )
        distance('swing_vwap_distance_atr', None, swing_reason or 'unavailable')
    else:
        raw['swing_break'] = swings.break_direction is not None
        raw['swing_break_direction'] = swings.break_direction
        if swings.last_high is not None:
            raw['last_swing_high'] = swings.last_high.price
        else:
            why['last_swing_high'] = 'no confirmed swing high in the window'
        if swings.last_low is not None:
            raw['last_swing_low'] = swings.last_low.price
        else:
            why['last_swing_low'] = 'no confirmed swing low in the window'
        if swings.broken is not None:
            raw['swing_anchor_time'] = swings.broken.open_time
        else:
            why['swing_anchor_time'] = 'no 4h close beyond the last swing'
        anchor = swings.latest_swing
        if anchor is not None:
            raw['swing_vwap_anchor_time'] = anchor.open_time
            anchored = levels.anchored_vwap(inputs.perp_15m, anchor.open_time, t, t, step15)
            distance('swing_vwap_distance_atr', anchored.value if anchored.ok else None, anchored.reason or 'unavailable')
        if swings.broken is not None and swings.break_direction is not None and atr is not None and price is not None:
            stop = levels.chandelier_stop(swings.run, swings.broken.index, swings.break_direction, atr)
            raw['chandelier_stop_price'] = stop
            raw['chandelier_stop_distance_atr'] = levels.chandelier_distance_atr(price, stop, swings.break_direction, atr)
        else:
            why['chandelier_stop_price'] = 'no 4h swing break (the stop is defined for the break direction), or ATR / price unavailable'
            why['chandelier_stop_distance_atr'] = why['chandelier_stop_price']

    # ---- indicators: triggers -----------------------------------------------
    sq = indicators.squeeze(inputs.perp_15m, t, t, step15)
    put('squeeze_on', sq.on)
    put('squeeze_bandwidth_ratio', sq.bandwidth_ratio)
    rsi = indicators.rsi(inputs.perp_15m, t, t, step15)
    rsi_value = put('rsi', rsi)
    put('pullback_reset', indicators.pullback_reset(family, trend_net_change, rsi_value, session_vwap_value, signal_bar))

    # ---- context available at decision time ---------------------------------
    info = classify_session(t)
    raw['session'] = info.session
    raw['session_bucket'] = info.bucket
    raw['session_date'] = info.session_date
    raw['is_weekend'] = is_weekend(t)
    if inputs.calendar_loaded:
        windowed = [
            (event.kind, event.event_time)
            for event in inputs.events
            if event.available_at <= t
            and event.kind in C.EVENT_WINDOW_MINUTES
            and not (event.kind == C.EVENT_KIND_EXPIRY and event.is_large is False)
        ]
        active = active_event_windows(t, windowed)
        raw['event_flag'] = bool(active)
        raw['active_event_kinds'] = sorted({event.kind for event in active})
    else:
        put_many(('event_flag', 'active_event_kinds'), 'no calendar rows are loaded, so "no event" cannot be asserted')

    # ---- stand-down input ---------------------------------------------------
    if atr is not None:
        put('perp_spot_disagreement_atr', positioning.perp_spot_disagreement(inputs.perp_15m, inputs.spot_15m, t, t, atr))
    else:
        why['perp_spot_disagreement_atr'] = 'ATR unavailable'

    # ---- shadow ----------------------------------------------------------------
    if price is not None:
        shadow, shadow_reason = liquidation.liquidation_shadow_metrics(inputs.force_orders, price, atr, t)
        raw['shadow_metrics'] = shadow
        if shadow_reason:
            why['shadow_metrics'] = shadow_reason
    else:
        why['shadow_metrics'] = 'signal bar is not in the visible 15m series'

    values = normalise_mapping(raw)
    return FeatureComputation(values=values, reasons=why, feature_hash=feature_hash(values))
