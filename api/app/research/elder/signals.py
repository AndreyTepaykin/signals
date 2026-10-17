"""Elder Triple Screen signals: tide (1W), wave (6H), ripple (15m), stop.

Direction is an int, ``+1`` long and ``-1`` short.

No-lookahead rule
-----------------
A decision on 15m bar ``t`` uses only 15m bars strictly before ``t`` (closed),
plus the last 6H bar and the last 1W bar with ``close_time <= t.open_time``.
The 6H bar and the week in progress are never read. All higher-timeframe
lookups are ``bisect`` on close-time lists built once in :func:`build_context`.
A slot that the resampler skipped (a ``None`` in the aligned list) is a hole,
not a reason to fall back to an older bar: decisions on it are "not active".

Stale data: if ``as_of`` lies far past the last higher-timeframe bar, the last
bar is still "the last closed one". The loader gives all timeframes the same end.

Tide
----
``tide_mode`` picks the sign of the slope of EMA13 (``ema``), of the MACD
histogram 12/26/9 (``macd``), or both when they agree (``both``, else 0), all
on the last closed weekly bar. Tide permits direction ``d`` if ``tide == d`` and
the weekly Impulse is not ``-d``. The Impulse veto is redundant for the single
modes by construction (impulse ``-d`` needs both slopes of sign ``-d``) and only
bites on a cold or missing Impulse (``None``: not permitted).

Wave (last closed 6H bar, ``ATR`` = 6H ATR14 at that bar), long; short mirrored.
``wave_threshold`` has different units for each oscillator:

* ``force2``: normalized = Force Index(2) / (ATR x SMA20 of 6H volume).
  Long if ``normalized < -wave_threshold``, short if ``> +wave_threshold``.
* ``stoch``: slow %K(5,3), in points. Long if ``< wave_threshold``, short if
  ``> 100 - wave_threshold``.
* ``elder_ray``: multiples of ATR. Long if ``bear_power < -wave_threshold x ATR``
  and slope(bear_power) > 0; short if ``bull_power > wave_threshold x ATR`` and
  slope(bull_power) < 0.

Any needed value ``None`` (cold, hole, zero denominator) means not active. With
``wave_block_impulse`` the 6H Impulse is also needed and must not be ``-d``
(a ``None`` Impulse counts as not active).

Ripple order lifecycle (15m)
----------------------------
On the first 15m bar ``t`` that sees a new closed 6H bar (``t.open_time`` >= its
``close_time``), with no order pending, tide permitting ``d`` and the wave active
for ``d``, a stop order is ARMED. A 6H close that arrives while an order is
pending is spent. A 6H close is used at most once: the first bar that sees it.

Per bar while pending, in this order: cancel if tide no longer permits ``d`` or
the last closed 6H bar has the wave inactive; expire if the order has already
been tried on ``ripple_expiry_bars`` bars (counted bars, a missing 15m bar does
not count); arm again if the bar is the first to see a new 6H close; try to
fill. The order may fill on bars ``t .. t + ripple_expiry_bars - 1``.

Level: high (long) / low (short) of the previous 15m bar, only when that bar is
exactly 15m earlier, else no fill on this bar. Fill when ``bar.high > level``
(long) / ``bar.low < level`` (short), strict. Fill price is ``max(open, level)``
(long) / ``min(open, level)`` (short), a gap through the level fills at the
open. No tick size.

Positions are NOT tracked here. :func:`generate_entries` emits every fill; the
E5 runner skips fills while a position is open for that coin.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.research.elder.indicators import (
    elder_ray,
    ema,
    force_index,
    impulse,
    macd_hist,
    ohlcv_fields,
    slope,
    stochastic,
    wilder_atr,
)
from app.research.elder.params import (
    ATR_PERIOD,
    COST_ROUND_TRIP,
    EMA_PERIOD,
    FORCE_PERIOD,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    MAX_COST_R,
    STOCH_PERIOD,
    STOCH_SMOOTH,
    VOLUME_SMA,
    ElderParams,
)
from app.research.elder.resample import OHLCV, STEP_1W, STEP_6H, STEP_15M, align

_UTC = timezone.utc
_EPOCH = datetime(1970, 1, 1, tzinfo=_UTC)

DROP_REASONS = ('stop_wrong_side', 'stop_too_close_atr', 'costs_over_cap', 'no_6h_context')


@dataclass(frozen=True)
class SignalContext:
    """Everything the decisions need, precomputed once per coin.

    Fields (all lists are index-aligned within their timeframe):

    * ``params``: the params the wave and tide series were built with.
    * ``bars_15m``: 15m bars as stored (may have holes), strictly sorted.
    * ``bars_6h``: 6H bars aligned to the 6H grid, ``None`` where a bucket is
      missing. ``close_times_6h[i]`` is the close time of slot ``i``, also for a
      ``None`` slot.
    * ``atr_6h``: Wilder ATR14 per 6H slot. ``impulse_6h``: Impulse per slot.
    * ``wave_long`` / ``wave_short``: wave active for that direction at the
      6H slot, including ``wave_block_impulse``.
    * ``bars_1w`` / ``close_times_1w``: same for the weekly bars.
    * ``tide_dir_1w``: tide direction per weekly slot (+1/-1/0, ``None`` when
      cold or a hole). ``impulse_1w``: weekly Impulse per slot.
    """

    params: ElderParams
    bars_15m: list[OHLCV]
    bars_6h: list[OHLCV | None]
    close_times_6h: list[datetime]
    atr_6h: list[float | None]
    impulse_6h: list[int | None]
    wave_long: list[bool]
    wave_short: list[bool]
    bars_1w: list[OHLCV | None]
    close_times_1w: list[datetime]
    tide_dir_1w: list[int | None]
    impulse_1w: list[int | None]


@dataclass(frozen=True)
class Entry:
    """A filled, filtered entry.

    ``armed_time``: open of the 15m bar that armed the order. ``fill_time``: open
    of the filling 15m bar. ``wave_close_time`` / ``tide_close_time``: close times
    of the last closed 6H and 1W bars at ``fill_time`` (audit trail; both must be
    ``<= fill_time``).
    """

    coin: str
    direction: int
    armed_time: datetime
    fill_time: datetime
    fill_index: int
    fill_price: float
    stop: float
    r: float
    atr_6h: float
    wave_close_time: datetime
    tide_close_time: datetime


@dataclass(frozen=True)
class Dropped:
    """A fill removed by the stop filter; ``reason`` is one of ``DROP_REASONS``."""

    coin: str
    direction: int
    fill_time: datetime
    fill_price: float
    reason: str


def _new_counts() -> dict[str, int]:
    counts = {'arms': 0, 'fills': 0, 'expired': 0, 'cancelled': 0, 'entries': 0}
    for reason in DROP_REASONS:
        counts[f'dropped_{reason}'] = 0
    return counts


@dataclass
class SignalResult:
    entries: list[Entry] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=_new_counts)


# ---------------------------------------------------------------- context


def _sma(values: Sequence[float | None], period: int) -> list[float | None]:
    """SMA of the last ``period`` values, ``None`` unless all of them are present."""
    out: list[float | None] = [None] * len(values)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1 : index + 1]
        if any(v is None for v in window):
            continue
        out[index] = math.fsum(v for v in window if v is not None) / period
    return out


def _sign(value: float | None) -> int | None:
    if value is None:
        return None
    if value > 0.0:
        return 1
    if value < 0.0:
        return -1
    return 0


def _validate_15m(bars: Sequence[OHLCV]) -> None:
    previous: datetime | None = None
    for bar in bars:
        opened = bar.open_time
        if opened.tzinfo is None or opened.utcoffset() is None:
            raise ValueError('15m bar open_time must be timezone-aware')
        if (opened - _EPOCH) % STEP_15M != timedelta(0):
            raise ValueError(f'15m bar open_time {opened.isoformat()} is not on the 15m grid')
        if previous is not None and opened <= previous:
            raise ValueError(f'15m bars are not strictly sorted at {opened.isoformat()}')
        previous = opened


def _slot_close_times(aligned: Sequence[OHLCV | None], step: timedelta) -> list[datetime]:
    if not aligned:
        return []
    first = aligned[0]
    assert first is not None  # align() starts at the first bar
    origin = first.open_time
    return [origin + (index + 1) * step for index in range(len(aligned))]


def _tide_series(closes: Sequence[float | None], mode: str) -> list[int | None]:
    ema_sign = [_sign(v) for v in slope(ema(closes, EMA_PERIOD))]
    macd_sign = [_sign(v) for v in slope(macd_hist(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL))]
    if mode == 'ema':
        return ema_sign
    if mode == 'macd':
        return macd_sign
    out: list[int | None] = []
    for a, b in zip(ema_sign, macd_sign):
        if a is None or b is None:
            out.append(None)
        else:
            out.append(a if a == b else 0)
    return out


def _wave_series(
    bars_6h: Sequence[OHLCV | None],
    atr: Sequence[float | None],
    impulse_6h: Sequence[int | None],
    params: ElderParams,
) -> tuple[list[bool], list[bool]]:
    _, highs, lows, closes, volumes = ohlcv_fields(bars_6h)
    n = len(bars_6h)
    thr = params.wave_threshold
    long_ok = [False] * n
    short_ok = [False] * n

    if params.wave_osc == 'force2':
        force = force_index(closes, volumes, FORCE_PERIOD)
        vol_sma = _sma(volumes, VOLUME_SMA)
        for i in range(n):
            f, a, v = force[i], atr[i], vol_sma[i]
            if f is None or a is None or v is None or a <= 0.0 or v <= 0.0:
                continue
            norm = f / (a * v)
            long_ok[i] = norm < -thr
            short_ok[i] = norm > thr
    elif params.wave_osc == 'stoch':
        percent_k = stochastic(highs, lows, closes, STOCH_PERIOD, STOCH_SMOOTH)
        for i in range(n):
            k = percent_k[i]
            if k is None:
                continue
            long_ok[i] = k < thr
            short_ok[i] = k > 100.0 - thr
    else:  # elder_ray
        bull, bear = elder_ray(highs, lows, closes, EMA_PERIOD)
        bull_slope = slope(bull)
        bear_slope = slope(bear)
        for i in range(n):
            a = atr[i]
            if a is None:
                continue
            bu, be, bus, bes = bull[i], bear[i], bull_slope[i], bear_slope[i]
            if be is not None and bes is not None:
                long_ok[i] = be < -thr * a and bes > 0.0
            if bu is not None and bus is not None:
                short_ok[i] = bu > thr * a and bus < 0.0

    if params.wave_block_impulse:
        for i in range(n):
            imp = impulse_6h[i]
            if imp is None:
                long_ok[i] = short_ok[i] = False
                continue
            if imp == 1:  # impulse against a short
                short_ok[i] = False
            elif imp == -1:  # impulse against a long
                long_ok[i] = False
    return long_ok, short_ok


def build_context(
    bars_15m: Sequence[OHLCV],
    bars_6h: Sequence[OHLCV],
    bars_1w: Sequence[OHLCV],
    params: ElderParams,
) -> SignalContext:
    """Align the 6H and 1W bars, compute every indicator once, prebuild close times.

    ``bars_6h`` / ``bars_1w`` come from ``to_6h`` / ``to_1w`` (skipped buckets
    become ``None`` slots). ``bars_15m`` is as stored (holes allowed). Must be
    strictly sorted by open time.
    """
    _validate_15m(bars_15m)
    aligned_6h = align(bars_6h, STEP_6H)
    aligned_1w = align(bars_1w, STEP_1W)

    _, highs6, lows6, closes6, _ = ohlcv_fields(aligned_6h)
    atr_6h = wilder_atr(highs6, lows6, closes6, ATR_PERIOD)
    impulse_6h = impulse(closes6, EMA_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    wave_long, wave_short = _wave_series(aligned_6h, atr_6h, impulse_6h, params)

    _, _, _, closes1w, _ = ohlcv_fields(aligned_1w)
    tide_dir = _tide_series(closes1w, params.tide_mode)
    impulse_1w = impulse(closes1w, EMA_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIGNAL)

    return SignalContext(
        params=params,
        bars_15m=list(bars_15m),
        bars_6h=aligned_6h,
        close_times_6h=_slot_close_times(aligned_6h, STEP_6H),
        atr_6h=atr_6h,
        impulse_6h=impulse_6h,
        wave_long=wave_long,
        wave_short=wave_short,
        bars_1w=aligned_1w,
        close_times_1w=_slot_close_times(aligned_1w, STEP_1W),
        tide_dir_1w=tide_dir,
        impulse_1w=impulse_1w,
    )


# ---------------------------------------------------------------- lookups


def _last_closed(close_times: Sequence[datetime], as_of: datetime) -> int | None:
    found = bisect.bisect_right(close_times, as_of) - 1
    return found if found >= 0 else None


def tide_at(ctx: SignalContext, as_of: datetime) -> tuple[int | None, int | None]:
    """``(tide direction, weekly impulse)`` from the last weekly bar closed by ``as_of``.

    Direction by ``tide_mode``; zero slope is ``0``; cold or hole is ``None``.
    """
    index = _last_closed(ctx.close_times_1w, as_of)
    if index is None:
        return None, None
    return ctx.tide_dir_1w[index], ctx.impulse_1w[index]


def tide_permits(ctx: SignalContext, direction: int, as_of: datetime) -> bool:
    """``tide == direction`` (nonzero) and weekly impulse is known and not ``-direction``."""
    _check_direction(direction)
    tide, imp = tide_at(ctx, as_of)
    if tide is None or tide != direction:
        return False
    return imp is not None and imp != -direction


def wave_active(ctx: SignalContext, direction: int, as_of: datetime) -> bool:
    """Wave for ``direction`` on the last 6H bar closed by ``as_of``."""
    _check_direction(direction)
    index = _last_closed(ctx.close_times_6h, as_of)
    return _wave_ok(ctx, direction, index)


def _wave_ok(ctx: SignalContext, direction: int, index: int | None) -> bool:
    if index is None:
        return False
    return ctx.wave_long[index] if direction > 0 else ctx.wave_short[index]


def _check_direction(direction: int) -> None:
    if direction not in (1, -1) or isinstance(direction, bool):
        raise ValueError('direction must be +1 or -1')


# ---------------------------------------------------------------- stop


def _stop_details(
    ctx: SignalContext,
    direction: int,
    fill_price: float,
    as_of: datetime,
    params: ElderParams,
) -> tuple[float | None, float | None, str | None]:
    """``(stop, atr, reason)``; ``stop`` is ``None`` iff ``reason`` is set."""
    _check_direction(direction)
    index = _last_closed(ctx.close_times_6h, as_of)
    window = params.stop_lookback_bars
    if index is None or index - window + 1 < 0:
        return None, None, 'no_6h_context'
    bars = ctx.bars_6h[index - window + 1 : index + 1]
    atr = ctx.atr_6h[index]
    if atr is None or any(bar is None for bar in bars):
        return None, None, 'no_6h_context'
    buffer = params.stop_atr_buffer * atr
    if direction > 0:
        stop = min(bar.low for bar in bars if bar is not None) - buffer
        loss_side = stop < fill_price
    else:
        stop = max(bar.high for bar in bars if bar is not None) + buffer
        loss_side = stop > fill_price
    r = abs(fill_price - stop)
    if r <= 0.0 or not loss_side:
        return None, atr, 'stop_wrong_side'
    if r < params.min_stop_atr * atr:
        return None, atr, 'stop_too_close_atr'
    if COST_ROUND_TRIP * fill_price > MAX_COST_R * r:
        return None, atr, 'costs_over_cap'
    return stop, atr, None


def stop_for(
    ctx: SignalContext,
    direction: int,
    fill_price: float,
    as_of: datetime,
    params: ElderParams,
) -> tuple[float | None, str | None]:
    """Stop for a fill at ``fill_price``, from the 6H bars closed by ``as_of``.

    Long: min low of the last ``stop_lookback_bars`` 6H bars minus
    ``stop_atr_buffer`` x ATR (ATR of the last closed 6H bar); short: max high
    plus the buffer. Returns ``(stop, None)`` or ``(None, reason)`` with reason
    in this check order: ``no_6h_context`` (a window bar or the ATR is missing),
    ``stop_wrong_side`` (R <= 0 or stop not on the loss side),
    ``stop_too_close_atr`` (R < ``min_stop_atr`` x ATR), ``costs_over_cap``
    (``COST_ROUND_TRIP`` x fill > ``MAX_COST_R`` x R).
    """
    stop, _, reason = _stop_details(ctx, direction, fill_price, as_of, params)
    return stop, reason


# ---------------------------------------------------------------- ripple


@dataclass
class _Pending:
    direction: int
    armed_time: datetime
    tried: int = 0


def _try_fill(bars: Sequence[OHLCV], k: int, direction: int) -> float | None:
    bar = bars[k]
    if k == 0:
        return None
    previous = bars[k - 1]
    if bar.open_time - previous.open_time != STEP_15M:
        return None
    if direction > 0:
        level = previous.high
        if bar.high > level:
            return max(bar.open, level)
    else:
        level = previous.low
        if bar.low < level:
            return min(bar.open, level)
    return None


def generate_entries(coin: str, ctx: SignalContext, params: ElderParams) -> SignalResult:
    """All entries over every 15m bar in ``ctx``; the runner filters by period later.

    Lifecycle in the module docstring. Positions are not tracked: every fill is
    emitted and the E5 runner skips fills while a position is open. ``params``
    supplies ``ripple_expiry_bars`` and the stop settings; its tide and wave
    fields must equal those ``ctx`` was built with (``ValueError`` otherwise).
    """
    built = ctx.params
    if (
        built.tide_mode != params.tide_mode
        or built.wave_osc != params.wave_osc
        or built.wave_threshold != params.wave_threshold
        or built.wave_block_impulse != params.wave_block_impulse
    ):
        raise ValueError('params tide/wave fields differ from the ones the context was built with')

    result = SignalResult()
    counts = result.counts
    bars = ctx.bars_15m
    expiry = params.ripple_expiry_bars
    pending: _Pending | None = None
    previous_slot: int | None = None

    for k, bar in enumerate(bars):
        t = bar.open_time
        slot = _last_closed(ctx.close_times_6h, t)
        new_close = slot is not None and slot != previous_slot
        previous_slot = slot

        if pending is not None:
            d = pending.direction
            if not tide_permits(ctx, d, t) or not _wave_ok(ctx, d, slot):
                counts['cancelled'] += 1
                pending = None
            elif pending.tried >= expiry:
                counts['expired'] += 1
                pending = None

        if pending is None and new_close:
            for d in (1, -1):
                if tide_permits(ctx, d, t) and _wave_ok(ctx, d, slot):
                    pending = _Pending(direction=d, armed_time=t)
                    counts['arms'] += 1
                    break

        if pending is None:
            continue

        pending.tried += 1
        d = pending.direction
        fill_price = _try_fill(bars, k, d)
        if fill_price is None:
            continue

        counts['fills'] += 1
        armed_time = pending.armed_time
        pending = None
        stop, atr, reason = _stop_details(ctx, d, fill_price, t, params)
        if stop is None or atr is None:
            counts[f'dropped_{reason}'] += 1
            result.dropped.append(
                Dropped(coin=coin, direction=d, fill_time=t, fill_price=fill_price, reason=str(reason))
            )
            continue
        week = _last_closed(ctx.close_times_1w, t)
        assert slot is not None and week is not None  # permitted tide and wave need both
        result.entries.append(
            Entry(
                coin=coin,
                direction=d,
                armed_time=armed_time,
                fill_time=t,
                fill_index=k,
                fill_price=fill_price,
                stop=stop,
                r=abs(fill_price - stop),
                atr_6h=atr,
                wave_close_time=ctx.close_times_6h[slot],
                tide_close_time=ctx.close_times_1w[week],
            )
        )
        counts['entries'] += 1
    return result
