"""Positioning metrics: ranks 1, 2, 3 and 11.

Rank 1  aggressor imbalance      taker-buy volume / total volume of the signal bar,
                                 z-scored against the prior 90 days of 15m bars.
Rank 2  OI-price quadrant        price direction x open-interest direction over the
                                 same hour, with a 0.25 sigma dead zone on OI.
Rank 3  funding z-score + band   settled prints versus 90 days; the carry band reads
                                 the CURRENT (predicted) rate and is live-only.
Rank 11 perp basis               (perp close - spot close) / spot close, in bps,
                                 z-scored against the prior 90 days of 1h bars.

Sampling frequency behind each 90-day z-score (the brief asked for it explicitly):

=======================  =======================  ==============================
statistic                sample                   samples in a full 90 days
=======================  =======================  ==============================
aggressor_imbalance_z    one per 15m perp bar     <= 8,640
oi_change_z              one per hourly OI step   <= 2,160
funding_z                one per SETTLED print    ~270 at 8h (3 a day)
perp_basis_z             one per 1h bar pair      <= 2,160
realized_vol_pct         one per hourly 24-bar    <= 2,160 (overlapping windows)
alt_residual_z           15m returns' residuals   <= 8,640
=======================  =======================  ==============================

Each also needs ``HistoryPolicy.min_span_days`` and ``min_samples`` of prior
history (see ``params``) or it is NULL.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core import constants as C
from app.core.features import params as P
from app.core.features.params import HistoryPolicy
from app.core.features.reading import Reading, have, missing
from app.core.features.series import (
    Bar,
    FundingPrint,
    OIPoint,
    Sample,
    visible_bars,
    visible_funding,
    visible_oi,
)
from app.core.features.stats import mean, prior_window, sample_std, trailing_zscore

QUADRANT_PRICE_UP_OI_UP = 'price_up_oi_up'
QUADRANT_PRICE_UP_OI_DOWN = 'price_up_oi_down'
QUADRANT_PRICE_DOWN_OI_UP = 'price_down_oi_up'
QUADRANT_PRICE_DOWN_OI_DOWN = 'price_down_oi_down'
QUADRANT_PRICE_UP_OI_FLAT = 'price_up_oi_flat'
QUADRANT_PRICE_DOWN_OI_FLAT = 'price_down_oi_flat'

BPS = 10_000.0


# ---------------------------------------------------------------------------
# Rank 1: aggressor imbalance
# ---------------------------------------------------------------------------
def imbalance_of(bar: Bar) -> float | None:
    """``taker_buy_volume / volume``, or None for a bar that traded nothing."""
    if bar.volume <= 0.0:
        return None
    return bar.taker_buy_volume / bar.volume


def aggressor_imbalance(bars: Sequence[Bar], bar_close: datetime, as_of: datetime, policy: HistoryPolicy) -> tuple[Reading, Reading]:
    """``(raw imbalance, z-score)`` of the signal bar, the bar that closes at ``bar_close``.

    The raw ratio is returned even when the history is too thin for the z-score.
    Zero-volume bars in the history are skipped (they have no ratio), never
    counted as 0.0.
    """
    seen = visible_bars(bars, as_of)
    if not seen or seen[-1].close_time != bar_close:
        return missing('signal bar is not in the visible 15m series'), missing('signal bar is not in the visible 15m series')
    signal = seen[-1]
    raw = imbalance_of(signal)
    if raw is None:
        return missing('signal bar has zero volume'), missing('signal bar has zero volume')
    samples = [Sample(b.close_time, b.available_at, r) for b in seen if (r := imbalance_of(b)) is not None]
    return have(raw), trailing_zscore(samples, signal.close_time, raw, as_of, policy)


# ---------------------------------------------------------------------------
# Rank 2: OI-price quadrant
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OiQuadrant:
    quadrant: Reading
    oi_change_z: Reading
    oi_change: float | None = None  # relative change over the step
    price_return: float | None = None  # log return over the same step
    poll_time: datetime | None = None  # the boundary the reading describes


def oi_changes(points: Sequence[OIPoint], step: timedelta, steps: int) -> list[tuple[OIPoint, float]]:
    """Relative open-interest changes ``OI_t / OI_{t - steps*step} - 1``.

    A change exists only when the earlier reading sits EXACTLY ``steps * step``
    before (no bridging a gap) and is positive.
    """
    by_time = {p.poll_time: p for p in points}
    out: list[tuple[OIPoint, float]] = []
    for point in points:
        earlier = by_time.get(point.poll_time - step * steps)
        if earlier is None or earlier.open_interest <= 0.0:
            continue
        out.append((point, point.open_interest / earlier.open_interest - 1.0))
    return out


def oi_price_quadrant(
    oi: Sequence[OIPoint],
    price_bars: Sequence[Bar],
    as_of: datetime,
    policy: HistoryPolicy,
    *,
    oi_step: timedelta,
    price_step: timedelta,
    steps: int = P.OI_CHANGE_STEPS,
) -> OiQuadrant:
    """The OI-price state at the newest open-interest reading visible at ``as_of``.

    **Alignment.** Open interest becomes available after a publication lag
    (poll boundary + 300 s), so at a bar close the newest VISIBLE reading
    describes an earlier boundary ``B``. The price move paired with it is the
    move ending at ``B`` over the same ``steps`` periods, NOT the move to the
    bar close - pairing OI as of ``B`` with price as of now would compare two
    different intervals.

    **Direction and dead zone.** ``x`` is the relative OI change over the step.
    ``sigma`` is the std of the prior 90 days of the same change. OI is "flat"
    when ``|x| < 0.25 * sigma`` (centred on ZERO change, because the quadrant
    is about whether OI actually rose or fell; for OI the 90-day mean change is
    a tiny fraction of sigma). The stored ``oi_change_z`` is the ordinary
    mean-subtracted z-score ``(x - mean) / sigma``. Inside the dead zone the price
    side is kept (``price_up_oi_flat`` / ``price_down_oi_flat``).

    NULL when: no visible OI, no earlier reading exactly one step before, not
    enough history for sigma, a zero-variance history, either price bar absent,
    or a price return of exactly zero (the six states have no "price flat").
    """
    points = visible_oi(oi, as_of)
    if not points:
        absent = missing('no visible open-interest reading')
        return OiQuadrant(absent, absent)
    changes = oi_changes(points, oi_step, steps)
    latest = points[-1]
    current = [c for c in changes if c[0].poll_time == latest.poll_time]
    if not current:
        absent = missing(f'no reading exactly {steps} step(s) before the newest one ({latest.poll_time.isoformat()})')
        return OiQuadrant(absent, absent, poll_time=latest.poll_time)
    x = current[0][1]
    samples = [Sample(p.poll_time, p.available_at, c) for p, c in changes]
    window, why = prior_window(samples, latest.poll_time, as_of, policy)
    if window is None:
        absent = missing(why or 'no history')
        return OiQuadrant(absent, absent, oi_change=x, poll_time=latest.poll_time)
    sigma = sample_std(window.values)
    if sigma == 0.0:
        absent = missing('prior open-interest changes have zero variance')
        return OiQuadrant(absent, absent, oi_change=x, poll_time=latest.poll_time)
    z = have((x - mean(window.values)) / sigma)

    price = _log_return_between(price_bars, latest.poll_time - price_step * steps, latest.poll_time, as_of)
    if price is None:
        return OiQuadrant(
            missing('price bars for the open-interest interval are absent'),
            z,
            oi_change=x,
            poll_time=latest.poll_time,
        )
    if price == 0.0:
        return OiQuadrant(missing('price return is exactly zero'), z, oi_change=x, price_return=price, poll_time=latest.poll_time)

    price_side = 'up' if price > 0.0 else 'down'
    if abs(x) < C.OI_CHANGE_DEAD_ZONE_SIGMA * sigma:
        oi_side = 'flat'
    else:
        oi_side = 'up' if x > 0.0 else 'down'
    quadrant = f'price_{price_side}_oi_{oi_side}'
    return OiQuadrant(have(quadrant), z, oi_change=x, price_return=price, poll_time=latest.poll_time)


def _log_return_between(bars: Sequence[Bar], start: datetime, end: datetime, as_of: datetime) -> float | None:
    """``ln(close at end / close at start)`` using the bars that CLOSE at those instants."""
    closes = {b.close_time: b.close for b in visible_bars(bars, as_of)}
    first, last = closes.get(start), closes.get(end)
    if first is None or last is None or first <= 0.0 or last <= 0.0:
        return None
    return math.log(last / first)


# ---------------------------------------------------------------------------
# Rank 3: funding
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FundingZ:
    z: Reading
    last_print: FundingPrint | None


def funding_z(settled: Sequence[FundingPrint], as_of: datetime, policy: HistoryPolicy) -> FundingZ:
    """Z-score of the newest SETTLED print against the prior 90 days of settled prints.

    Only settled prints are passed in; the predicted rate is never an input
    here. The window ends at the newest print's own funding time and excludes it.
    Rates are used as published (per settlement interval); symbols whose
    interval changed inside the window mix intervals - see the BUILD-LOG.
    """
    prints = visible_funding(settled, as_of)
    if not prints:
        return FundingZ(missing('no visible settled funding print'), None)
    latest = prints[-1]
    samples = [Sample(p.funding_time, p.available_at, p.rate) for p in prints]
    return FundingZ(trailing_zscore(samples, latest.funding_time, latest.rate, as_of, policy), latest)


def funding_carry_bps(rate_per_interval: float, interval_hours: int, horizon_hours: int) -> float:
    """Funding paid by a LONG over the horizon, in bps: ``rate * (horizon / interval) * 10,000``.

    Positive means longs pay (the usual contango). The card's band is this
    figure and its sign flip. Linear scaling, as the spec says "scaled to the
    horizon"; no count of whole settlements, no compounding.
    """
    if interval_hours <= 0 or horizon_hours <= 0:
        raise ValueError('interval and horizon must be positive hours')
    return rate_per_interval * (horizon_hours / interval_hours) * BPS


# ---------------------------------------------------------------------------
# Rank 11: perp basis
# ---------------------------------------------------------------------------
def basis_bps(perp_close: float, spot_close: float) -> float | None:
    """``(perp - spot) / spot`` in basis points; the spot close stands in for the index."""
    if spot_close <= 0.0 or perp_close <= 0.0:
        return None
    return (perp_close - spot_close) / spot_close * BPS


def basis_samples(perp: Sequence[Bar], spot: Sequence[Bar], as_of: datetime) -> list[Sample]:
    """One basis sample per bar open time present in BOTH series, at the later availability."""
    spot_by_open = {b.open_time: b for b in visible_bars(spot, as_of)}
    samples: list[Sample] = []
    for bar in visible_bars(perp, as_of):
        partner = spot_by_open.get(bar.open_time)
        if partner is None:
            continue
        value = basis_bps(bar.close, partner.close)
        if value is not None:
            samples.append(Sample(bar.close_time, max(bar.available_at, partner.available_at), value))
    return samples


def perp_basis(
    perp: Sequence[Bar],
    spot: Sequence[Bar],
    as_of: datetime,
    policy: HistoryPolicy,
    *,
    expected_close: datetime | None = None,
) -> tuple[Reading, Reading]:
    """``(basis in bps, z-score)`` at the newest bar present in both series.

    ``expected_close`` is the close the newest pair SHOULD have; a series that
    stopped earlier is stale and gives missing rather than an old basis.
    """
    samples = basis_samples(perp, spot, as_of)
    if not samples:
        absent = missing('no bar present in both perp and spot series')
        return absent, absent
    latest = samples[-1]
    if expected_close is not None and latest.time != expected_close:
        stale = missing(f'newest perp/spot pair closed {latest.time.isoformat()}, expected {expected_close.isoformat()}')
        return stale, stale
    return have(latest.value), trailing_zscore(samples, latest.time, latest.value, as_of, policy)


def perp_spot_disagreement(perp: Sequence[Bar], spot: Sequence[Bar], bar_close: datetime, as_of: datetime, atr: float) -> Reading:
    """``|perp close - spot close| / ATR`` on the bar that closes at ``bar_close``.

    A magnitude, never signed: the stand-down is "disagree by more than 1 ATR",
    and a signed figure invites a one-sided comparison.
    """
    if atr <= 0.0:
        return missing('ATR unavailable')
    perp_bar = next((b for b in visible_bars(perp, as_of) if b.close_time == bar_close), None)
    spot_bar = next((b for b in visible_bars(spot, as_of) if b.close_time == bar_close), None)
    if perp_bar is None or spot_bar is None:
        return missing('perp or spot bar for the signal close is absent')
    return have(abs(perp_bar.close - spot_bar.close) / atr)
