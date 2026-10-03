"""Figures derived from one options snapshot: ranks 7-10 of the metric table, max pain, walls, large expiry.

Everything here is a pure function of the quotes (and, for realized vol, of 1h bars);
nothing reads a clock. Where the spec is silent the reading is a numbered Ambiguity in
the M5 BUILD-LOG entry and its number is a constant in ``app.core.constants``.

The expiry "nearest the hold"
-----------------------------
For horizon ``h`` with hold ``H`` hours, the expiry whose settlement instant is closest to
``snapshot_time + H`` (ties go to the earlier one). Only expiries after the snapshot count.
Scalp (4h) normally lands on the next daily, swing (24h) on the daily about a day away,
position (72h) on the daily or weekly about three days away. Every figure below that says
"the expiry" (max pain, top strike, ATM IV, skew, hours to expiry) uses this expiry.

Near-dated open interest
------------------------
The open interest of every expiry settling within ``OPTIONS_NEAR_DATED_HOURS`` (72h) of the
snapshot. It is the headwind denominator and the book the dealer-gamma estimate is taken over.

Dealer gamma (an ESTIMATE; see ``constants.OPTIONS_GAMMA_PROXY``)
-------------------------------------------------------------
``GEX(S) = sum_i s_i * OI_i * gamma_i(F_i * S / S0) * S^2`` over near-dated quotes with a
positive IV, ``s_i = +1`` for a call and ``-1`` for a put (dealers long call gamma, short put
gamma). The sign at the index ``S0`` is the dealer gamma sign. The flip is where GEX changes
sign as ``S`` moves within +/- ``OPTIONS_GAMMA_SCAN_RANGE`` of ``S0``, nearest to ``S0``,
linearly interpolated between grid points; NULL if it never does.

Max pain
--------
The settlement price at which the open interest of the expiry pays option holders least:
``pain(K) = sum_calls OI max(0, K - strike) + sum_puts OI max(0, strike - K)``, evaluated at
every listed strike; ties go to the lowest strike.

Skew and ATM IV
---------------
ATM IV is the mean IV of the call and the put at the strike nearest the expiry's forward
(a single side if only one exists). The 25-delta skew is ``IV(put with delta nearest -0.25) -
IV(call with delta nearest +0.25)``, both within ``OPTIONS_SKEW_DELTA_TOLERANCE`` of 0.25.

Same-tenor realized vol
-----------------------
Yang-Zhang over the last ``n`` 1h perp bars, ``n = ceil(hours to expiry)`` clamped to
``[REALIZED_VOL_BARS (24), OPTIONS_RV_MAX_BARS]``, annualised on 365 days by the very function
the feature engine uses (``volatility.yang_zhang_vol``), so IV and RV are on one footing.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core import constants as C
from app.core.features import params as P
from app.core.features.series import Bar, contiguous_suffix, visible_bars
from app.core.features.volatility import yang_zhang_vol
from app.core.options import greeks
from app.core.options.book import SIDE_CALL, SIDE_PUT, OptionQuote, OptionsBook, quotes_expiring_within, years_between


# ---------------------------------------------------------------------------
# strikes
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StrikeOI:
    strike: float
    call_oi: float
    put_oi: float

    @property
    def total(self) -> float:
        return self.call_oi + self.put_oi

    @property
    def side(self) -> str:
        """The side holding more open interest at this strike (calls on a tie)."""
        return SIDE_PUT if self.put_oi > self.call_oi else SIDE_CALL


def strike_profile(quotes: Sequence[OptionQuote]) -> tuple[StrikeOI, ...]:
    """Open interest per strike, calls and puts kept apart, summed over the expiries given."""
    calls: dict[float, list[float]] = defaultdict(list)
    puts: dict[float, list[float]] = defaultdict(list)
    for quote in quotes:
        (calls if quote.side == SIDE_CALL else puts)[quote.strike].append(quote.open_interest)
    strikes = sorted(set(calls) | set(puts))
    return tuple(StrikeOI(k, math.fsum(calls.get(k, ())), math.fsum(puts.get(k, ()))) for k in strikes)


def top_strike(quotes: Sequence[OptionQuote]) -> tuple[float, float] | None:
    """``(strike, share of the quotes' open interest)`` for the strike holding the most (lowest on a tie)."""
    profile = strike_profile(quotes)
    total = math.fsum(s.total for s in profile)
    if not profile or total <= 0.0:
        return None
    best = max(profile, key=lambda s: (s.total, -s.strike))
    return best.strike, best.total / total


def max_pain(quotes: Sequence[OptionQuote]) -> float | None:
    """The strike at which the expiry's open interest pays holders least (lowest on a tie)."""
    profile = strike_profile(quotes)
    if not profile or math.fsum(s.total for s in profile) <= 0.0:
        return None

    def pain(settle: float) -> float:
        return math.fsum(s.call_oi * max(0.0, settle - s.strike) + s.put_oi * max(0.0, s.strike - settle) for s in profile)

    return min(profile, key=lambda s: (pain(s.strike), s.strike)).strike


# ---------------------------------------------------------------------------
# expiries
# ---------------------------------------------------------------------------
def nearest_expiry(book: OptionsBook, hold_hours: float) -> datetime | None:
    """The expiry settling closest to ``snapshot_time + hold_hours`` (earlier on a tie); None if none is ahead."""
    target = book.snapshot_time + timedelta(hours=hold_hours)
    ahead = [e for e in book.expiries() if e > book.snapshot_time]
    if not ahead:
        return None
    return min(ahead, key=lambda e: (abs(e - target), e))


def hours_to(book: OptionsBook, expiry_time: datetime) -> float:
    return (expiry_time - book.snapshot_time).total_seconds() / 3600.0


def expiry_open_interest(book: OptionsBook, expiry_time: datetime) -> float:
    return math.fsum(q.open_interest for q in book.for_expiry(expiry_time))


def near_dated_quotes(book: OptionsBook) -> tuple[OptionQuote, ...]:
    return quotes_expiring_within(book.quotes, book.snapshot_time, C.OPTIONS_NEAR_DATED_HOURS)


def near_dated_open_interest(book: OptionsBook) -> float:
    return math.fsum(q.open_interest for q in near_dated_quotes(book))


def is_large_expiry(expiry_open_interest_value: float, total_open_interest: float) -> bool:
    """A large expiry holds at least ``LARGE_EXPIRY_MIN_OI_SHARE`` of the coin's options open interest."""
    if total_open_interest <= 0.0:
        return False
    return expiry_open_interest_value / total_open_interest >= C.LARGE_EXPIRY_MIN_OI_SHARE


# ---------------------------------------------------------------------------
# dealer gamma
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GammaResult:
    sign: int  # +1 dealers long gamma, -1 short, 0 exactly flat
    net_at_spot: float
    flip_price: float | None


def _net_gamma(quotes: Sequence[OptionQuote], snapshot_time: datetime, index_price: float, move: float) -> float:
    spot = index_price * (1.0 + move)
    total: list[float] = []
    for quote in quotes:
        years = years_between(snapshot_time, quote.expiry_time)
        if not quote.has_iv or years <= 0.0 or quote.open_interest <= 0.0:
            continue
        g = greeks.gamma(quote.forward * (1.0 + move), quote.strike, quote.iv or 0.0, years)
        total.append((1.0 if quote.side == SIDE_CALL else -1.0) * quote.open_interest * g * spot * spot)
    return math.fsum(total)


def dealer_gamma(quotes: Sequence[OptionQuote], snapshot_time: datetime, index_price: float) -> GammaResult | None:
    """Estimated dealer gamma sign and flip over ``quotes`` (None when no quote can be priced)."""
    usable = [q for q in quotes if q.has_iv and q.open_interest > 0.0 and q.expiry_time > snapshot_time]
    if not usable:
        return None
    base = _net_gamma(usable, snapshot_time, index_price, 0.0)
    sign = (base > 0.0) - (base < 0.0)
    if sign == 0:
        return GammaResult(0, base, index_price)
    steps = int(round(C.OPTIONS_GAMMA_SCAN_RANGE / C.OPTIONS_GAMMA_SCAN_STEP))
    best: float | None = None
    for direction in (1.0, -1.0):
        previous_move, previous_value = 0.0, base
        for i in range(1, steps + 1):
            move = direction * i * C.OPTIONS_GAMMA_SCAN_STEP
            value = _net_gamma(usable, snapshot_time, index_price, move)
            if value == 0.0 or (value > 0.0) != (base > 0.0):
                crossing = previous_move if value == previous_value else previous_move + (move - previous_move) * (
                    previous_value / (previous_value - value)
                )
                if best is None or abs(crossing) < abs(best):
                    best = crossing
                break
            previous_move, previous_value = move, value
    return GammaResult(sign, base, None if best is None else index_price * (1.0 + best))


# ---------------------------------------------------------------------------
# implied vol
# ---------------------------------------------------------------------------
def atm_iv(quotes: Sequence[OptionQuote]) -> float | None:
    """Mean IV of the call and the put at the strike nearest the forward (one side if only one exists)."""
    priced = [q for q in quotes if q.has_iv]
    if not priced:
        return None
    forward = priced[0].forward
    strike = min({q.strike for q in priced}, key=lambda k: (abs(k - forward), k))
    ivs = [q.iv for q in priced if q.strike == strike and q.iv is not None]
    return math.fsum(ivs) / len(ivs)


def skew_25d(quotes: Sequence[OptionQuote], snapshot_time: datetime) -> float | None:
    """Put IV at delta -0.25 minus call IV at delta +0.25 (None unless both lie within the tolerance)."""
    best: dict[str, tuple[float, float, float]] = {}  # side -> (distance from 0.25, strike, iv)
    for quote in quotes:
        years = years_between(snapshot_time, quote.expiry_time)
        if not quote.has_iv or years <= 0.0:
            continue
        iv = quote.iv or 0.0
        delta = greeks.call_delta(quote.forward, quote.strike, iv, years) if quote.side == SIDE_CALL else -greeks.put_delta(quote.forward, quote.strike, iv, years)
        candidate = (abs(delta - C.OPTIONS_SKEW_DELTA), quote.strike, iv)
        if quote.side not in best or candidate < best[quote.side]:
            best[quote.side] = candidate
    if SIDE_CALL not in best or SIDE_PUT not in best:
        return None
    if best[SIDE_CALL][0] > C.OPTIONS_SKEW_DELTA_TOLERANCE or best[SIDE_PUT][0] > C.OPTIONS_SKEW_DELTA_TOLERANCE:
        return None
    return best[SIDE_PUT][2] - best[SIDE_CALL][2]


# ---------------------------------------------------------------------------
# same-tenor realized vol
# ---------------------------------------------------------------------------
def rv_bar_count(hours_to_expiry: float) -> int:
    return max(P.REALIZED_VOL_BARS, min(C.OPTIONS_RV_MAX_BARS, math.ceil(hours_to_expiry)))


def same_tenor_rv(bars: Sequence[Bar], as_of: datetime, hours_to_expiry: float) -> float | None:
    """Annualised Yang-Zhang vol of the last ``rv_bar_count`` consecutive 1h bars visible at ``as_of``."""
    step = timedelta(hours=1)
    n = rv_bar_count(hours_to_expiry)
    run = contiguous_suffix(visible_bars(bars, as_of), step)
    if len(run) < n + 1:
        return None
    return yang_zhang_vol(run[-(n + 1):], step)


# ---------------------------------------------------------------------------
# everything for one horizon
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HorizonMetrics:
    horizon: str
    hold_hours: int
    expiry_time: datetime | None
    hours_to_expiry: float | None
    expiry_open_interest: float | None
    near_dated_open_interest: float
    total_open_interest: float
    top_strike: float | None
    top_strike_oi_share: float | None
    max_pain_strike: float | None
    dealer_gamma_sign: int | None
    gamma_flip_price: float | None
    atm_iv: float | None
    skew_25d: float | None
    same_tenor_rv: float | None
    iv_minus_rv: float | None
    rv_bars: int | None
    notes: dict[str, str] = field(default_factory=dict)


def derive_horizon_metrics(
    book: OptionsBook,
    horizon: str,
    hourly_bars: Sequence[Bar] = (),
    bars_as_of: datetime | None = None,
) -> HorizonMetrics:
    """Every direction-free figure for ``horizon`` from ``book``; a figure that cannot be built is None with a note."""
    hold = C.VERTICAL_BARRIER_HOURS[horizon]
    notes: dict[str, str] = {}
    near = near_dated_quotes(book)
    near_oi = math.fsum(q.open_interest for q in near)
    gamma_result = dealer_gamma(near, book.snapshot_time, book.index_price)
    if gamma_result is None:
        notes['dealer_gamma'] = 'no priced near-dated quote'
    expiry = nearest_expiry(book, hold)
    if expiry is None:
        notes['expiry'] = 'no expiry ahead of the snapshot'
        return HorizonMetrics(
            horizon, hold, None, None, None, near_oi, book.total_open_interest, None, None, None,
            None if gamma_result is None else gamma_result.sign,
            None if gamma_result is None else gamma_result.flip_price,
            None, None, None, None, None, notes,
        )  # fmt: skip
    quotes = book.for_expiry(expiry)
    tte = hours_to(book, expiry)
    top = top_strike(quotes)
    iv = atm_iv(quotes)
    rv = None
    bars = rv_bar_count(tte)
    if iv is None:
        notes['atm_iv'] = 'no quote with an implied vol'
    if hourly_bars and bars_as_of is not None:
        rv = same_tenor_rv(hourly_bars, bars_as_of, tte)
    if rv is None:
        notes['same_tenor_rv'] = f'fewer than {bars + 1} consecutive 1h bars visible'
    return HorizonMetrics(
        horizon=horizon,
        hold_hours=hold,
        expiry_time=expiry,
        hours_to_expiry=tte,
        expiry_open_interest=math.fsum(q.open_interest for q in quotes),
        near_dated_open_interest=near_oi,
        total_open_interest=book.total_open_interest,
        top_strike=None if top is None else top[0],
        top_strike_oi_share=None if top is None else top[1],
        max_pain_strike=max_pain(quotes),
        dealer_gamma_sign=None if gamma_result is None else gamma_result.sign,
        gamma_flip_price=None if gamma_result is None else gamma_result.flip_price,
        atm_iv=iv,
        skew_25d=skew_25d(quotes, book.snapshot_time),
        same_tenor_rv=rv,
        iv_minus_rv=None if (iv is None or rv is None) else iv - rv,
        rv_bars=bars,
        notes=notes,
    )
