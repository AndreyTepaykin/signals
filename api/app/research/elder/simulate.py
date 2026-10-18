"""Elder Triple Screen simulator: one trade on ripple (30m) bars, with parts, stops, costs, funding.

Pure functions, floats, stdlib only. ``d`` is ``entry.direction`` (+1 long, -1 short); every
rule below is written for a long and mirrored for a short.

Levels
------
``R = entry.r``. ``T1 = entry + d * t1_r * R``; ``T2 = entry + d * T2_R * R``. The position has
two parts: ``share_1 = t1_share`` closes at T1, the runner ``share_2 = 1 - share_1`` is
trailed and capped at T2. ``t1_share = 0`` has no T1 part but the T1 event still turns on
breakeven and trailing; ``t1_share = 1`` has no runner (the trade ends at T1).

Stop state
----------
``stop`` starts at ``entry.stop`` with kind ``'initial'`` and only ever tightens. The T1
event closes ``share_1`` at T1; from the NEXT bar the stop becomes
``max(stop, entry)`` if ``be_after_t1`` (kind ``'breakeven'`` when that changed it) and
trailing is on. Trailing runs only after the T1 event, at each wave (4H) close that becomes visible
(the first ripple bar with ``open_time >= close``, same lookup as the signals): candidate =
(highest ripple-bar high from the fill bar through the bar BEFORE the current one) -
``trail_atr`` x ATR(14) of the wave bar that just closed; ``None`` ATR means no update; the new
stop applies from that bar's open. A wave close that became visible at or before the T1 bar is
not an update (trailing was off); the first update is at the next wave close.

Per-bar order
-------------
1. Fill bar: the path after the fill is unknown. ``low <= stop`` exits everything at the
   stop, ``ambiguous=True``. Otherwise nothing happens, not even a T1 or T2 credit; its
   high counts for the trailing max-high only from the next bar on.
2. Later bars, at the open: (a) pending breakeven / trail update; (b) tide exit: tide
   direction ``-d`` or tide impulse ``-d`` (0 or ``None`` do not exit) exits everything
   at the open; (c) gap: ``open <= stop`` exits everything at the open, ``gapped=True``.
3. Intrabar: ``low <= stop`` exits everything at the stop; ``ambiguous`` when this bar also
   touched a still-open target. Same-bar stop and target is the stop (labeler convention).
   Otherwise T1 (even if the bar opened above it: a limit gets no price improvement), then
   in the same bar T2 for the runner. Intrabar exits are timed at ``bar.close_time``.
4. A breakeven caused by this bar takes effect from the next bar.

Choices beyond the brief (conservative or consistency):

* A tide exit whose open lies at or beyond a target that is still open (a gap through the
  limit) books that target at its price (reason ``'t1'`` / ``'t2'``, no price improvement,
  timed at the open) and only the rest as ``'tide_exit'``. Exiting the whole position at the
  open would pay more than T2 and break the 10R cap.
* ``ambiguous`` on a stop exit is true whenever this bar touched T1 while T1 was not yet
  hit, even with ``t1_share = 0`` (the stop wins anyway; the flag is a count of
  unorderable bars, not of changed outcomes).
* ``mae_r`` / ``mfe_r`` are non-negative magnitudes in R. The fill bar, and a stop-out bar,
  contribute their adverse side only (a stop-out is measured at the exit price, not the
  bar's low). A bar that ends the trade at its open (tide exit, gap) counts that open.
* If the fill bar itself is not usable (``close_time > end_time``) the trade is censored
  ``'end_of_period'`` with ``last_index = fill_index``, ``open_share = 1``, ``mark_r = 0``,
  no exits.
* Funding counts closed parts only; the open share of a censored trade contributes nothing
  to ``total_r`` (neither price, cost nor funding).
* Data that simply ends (no next bar) is ``'end_of_period'``; ``'hole'`` needs a next bar
  that exists but is not exactly ``RIPPLE_STEP`` later.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.research.elder.params import COST_ROUND_TRIP, T2_R, ElderParams
from app.research.elder.signals import RIPPLE_STEP, Entry, SignalContext, tide_at

REASONS = ('stop', 't1', 't2', 'tide_exit')
STOP_KINDS = ('initial', 'breakeven', 'trail')
CENSOR_REASONS = ('end_of_period', 'hole')


@dataclass(frozen=True)
class FundingPrint:
    funding_time: datetime
    rate: float
    mark_price: float | None = None


@dataclass(frozen=True)
class PartExit:
    share: float
    price: float
    time: datetime
    reason: str
    stop_kind: str | None
    gapped: bool
    ambiguous: bool


@dataclass(frozen=True)
class Trade:
    coin: str
    direction: int
    fill_time: datetime
    fill_index: int
    entry_price: float
    initial_stop: float
    r: float
    exits: tuple[PartExit, ...]
    hit_t1: bool
    hit_t2: bool
    stopped_before_t1: bool
    censored: bool
    censor_reason: str | None
    last_index: int
    closed_time: datetime | None
    price_r: float
    cost_r: float
    funding_r: float
    total_r: float
    open_share: float
    mark_r: float | None
    mae_r: float
    mfe_r: float
    bars_held: int


@dataclass
class _Walk:
    exits: list[PartExit] = field(default_factory=list)
    hit_t1: bool = False
    hit_t2: bool = False
    censor_reason: str | None = None
    last_index: int = 0
    open_share: float = 0.0
    mark_r: float | None = None
    mae: float = 0.0
    mfe: float = 0.0
    stop_history: list[tuple[int, float, str]] = field(default_factory=list)


def _last_closed(close_times: Sequence[datetime], as_of: datetime) -> int | None:
    found = bisect.bisect_right(close_times, as_of) - 1
    return found if found >= 0 else None


def _walk(entry: Entry, ctx: SignalContext, params: ElderParams, end_time: datetime) -> _Walk:
    d = entry.direction
    if isinstance(d, bool) or d not in (1, -1):
        raise ValueError('direction must be +1 or -1')
    e = entry.fill_price
    r = entry.r
    if not (math.isfinite(e) and math.isfinite(r) and r > 0.0):
        raise ValueError('entry price and r must be finite, r > 0')
    if d * (e - entry.stop) <= 0.0 or not math.isclose(abs(e - entry.stop), r, rel_tol=1e-9, abs_tol=0.0):
        raise ValueError('entry.stop must lie on the loss side at distance entry.r')
    bars = ctx.bars_ripple
    k0 = entry.fill_index
    if not 0 <= k0 < len(bars) or bars[k0].open_time != entry.fill_time:
        raise ValueError('entry.fill_index does not point at the fill bar of ctx.bars_ripple')

    walk = _Walk(last_index=k0)
    share_1 = params.t1_share
    share_2 = 1.0 - share_1
    t1_price = e + d * params.t1_r * r
    t2_price = e + d * T2_R * r

    def adverse(price: float) -> float:
        return d * (e - price) / r

    def favorable(price: float) -> float:
        return d * (price - e) / r

    def worst(bar) -> float:
        return bar.low if d > 0 else bar.high

    def best(bar) -> float:
        return bar.high if d > 0 else bar.low

    def reached(price: float, level: float) -> bool:
        """``price`` is at or beyond ``level`` in the trade's favourable direction (``d * (price - level) >= 0``)."""
        return d * (price - level) >= 0.0

    def tighter(a: float, b: float) -> float:
        return max(a, b) if d > 0 else min(a, b)

    if bars[k0].close_time > end_time:
        walk.censor_reason = 'end_of_period'
        walk.open_share = 1.0
        walk.mark_r = 0.0
        walk.stop_history.append((k0, entry.stop, 'initial'))
        return walk

    stop = entry.stop
    stop_kind = 'initial'
    walk.stop_history.append((k0, stop, stop_kind))
    remaining = 1.0
    activation_pending = False
    trail_active = False
    run_ext: float | None = None  # highest high (long) / lowest low (short), bars before current
    prev_slot = _last_closed(ctx.close_times_wave, bars[k0].open_time)

    def book(share: float, price: float, when: datetime, reason: str, *, kind: str | None = None,
             gapped: bool = False, ambiguous: bool = False) -> None:
        walk.exits.append(PartExit(share, price, when, reason, kind if reason == 'stop' else None, gapped, ambiguous))

    k = k0
    while True:
        bar = bars[k]
        walk.last_index = k
        closed = False

        if k == k0:
            if reached(stop, worst(bar)):  # the bar's worst price is at or beyond the stop
                book(remaining, stop, bar.close_time, 'stop', kind=stop_kind, ambiguous=True)
                walk.mae = max(walk.mae, adverse(stop))
                closed = True
            else:
                walk.mae = max(walk.mae, adverse(worst(bar)))
        else:
            slot = _last_closed(ctx.close_times_wave, bar.open_time)
            # (a) pending breakeven and trail update, both known at this open
            if activation_pending:
                activation_pending = False
                trail_active = True
                if params.be_after_t1:
                    new_stop = tighter(stop, e)
                    if new_stop != stop:
                        stop, stop_kind = new_stop, 'breakeven'
                        walk.stop_history.append((k, stop, stop_kind))
            if trail_active and slot is not None and slot != prev_slot and run_ext is not None:
                atr = ctx.atr_wave[slot]
                if atr is not None:
                    candidate = run_ext - d * params.trail_atr * atr
                    new_stop = tighter(stop, candidate)
                    if new_stop != stop:
                        stop, stop_kind = new_stop, 'trail'
                        walk.stop_history.append((k, stop, stop_kind))
            prev_slot = slot

            tide, impulse = tide_at(ctx, bar.open_time)
            if tide == -d or impulse == -d:
                # (b) tide exit at the open; a gap through a still-open limit books that limit at its price
                if not walk.hit_t1 and reached(bar.open, t1_price):
                    walk.hit_t1 = True
                    if share_1 > 0.0:
                        book(share_1, t1_price, bar.open_time, 't1')
                    remaining = share_2
                if remaining > 0.0 and walk.hit_t1 and share_2 > 0.0 and reached(bar.open, t2_price):
                    book(remaining, t2_price, bar.open_time, 't2')
                    walk.hit_t2 = True
                    remaining = 0.0
                if remaining > 0.0:
                    book(remaining, bar.open, bar.open_time, 'tide_exit')
                    remaining = 0.0
                walk.mae = max(walk.mae, adverse(bar.open))
                walk.mfe = max(walk.mfe, favorable(bar.open))
                closed = True
            elif reached(stop, bar.open):
                # (c) gap through the stop fills at the open
                book(remaining, bar.open, bar.open_time, 'stop', kind=stop_kind, gapped=True)
                walk.mae = max(walk.mae, adverse(bar.open))
                closed = True
            else:
                # 3) intrabar
                t1_touch = not walk.hit_t1 and reached(best(bar), t1_price)
                t2_touch = share_2 > 0.0 and reached(best(bar), t2_price)
                if reached(stop, worst(bar)):
                    book(remaining, stop, bar.close_time, 'stop', kind=stop_kind,
                         ambiguous=t1_touch or t2_touch)
                    walk.mae = max(walk.mae, adverse(stop))
                    closed = True
                else:
                    walk.mae = max(walk.mae, adverse(worst(bar)))
                    walk.mfe = max(walk.mfe, favorable(best(bar)))
                    if t1_touch:
                        walk.hit_t1 = True
                        if share_1 > 0.0:
                            book(share_1, t1_price, bar.close_time, 't1')
                        remaining = share_2
                        activation_pending = True
                    if walk.hit_t1 and share_2 > 0.0 and t2_touch:
                        book(remaining, t2_price, bar.close_time, 't2')
                        walk.hit_t2 = True
                        remaining = 0.0
                    if remaining <= 0.0:
                        closed = True

        if closed:
            return walk

        if run_ext is None:
            run_ext = bar.high if d > 0 else bar.low
        else:
            run_ext = max(run_ext, bar.high) if d > 0 else min(run_ext, bar.low)

        expected_open = bar.open_time + RIPPLE_STEP
        nxt = k + 1
        if expected_open + RIPPLE_STEP > end_time or nxt >= len(bars):
            walk.censor_reason = 'end_of_period'
        elif bars[nxt].open_time != expected_open:
            walk.censor_reason = 'hole'
        elif bars[nxt].close_time > end_time:
            walk.censor_reason = 'end_of_period'
        if walk.censor_reason is not None:
            walk.open_share = remaining
            walk.mark_r = favorable(bar.close)
            return walk
        k = nxt


def simulate_trade(
    entry: Entry,
    ctx: SignalContext,
    params: ElderParams,
    funding: Sequence[FundingPrint],
    end_time: datetime,
) -> Trade:
    """Walk one entry over ``ctx.bars_ripple`` bars with ``close_time <= end_time`` and price it."""
    walk = _walk(entry, ctx, params, end_time)
    d = entry.direction
    e = entry.fill_price
    r = entry.r
    exits = tuple(walk.exits)
    censored = walk.censor_reason is not None

    price_r = math.fsum(x.share * d * (x.price - e) / r for x in exits)
    cost_r = math.fsum(x.share * COST_ROUND_TRIP * e / r for x in exits)
    parts: list[float] = []
    for x in exits:
        for p in funding:
            if entry.fill_time < p.funding_time < x.time:
                notional = e if p.mark_price is None else p.mark_price
                parts.append(x.share * d * p.rate * notional / r)
    paid = math.fsum(parts)
    funding_r = -paid
    total_r = price_r - cost_r + funding_r

    return Trade(
        coin=entry.coin,
        direction=d,
        fill_time=entry.fill_time,
        fill_index=entry.fill_index,
        entry_price=e,
        initial_stop=entry.stop,
        r=r,
        exits=exits,
        hit_t1=walk.hit_t1,
        hit_t2=walk.hit_t2,
        stopped_before_t1=(not walk.hit_t1) and bool(exits) and exits[-1].reason == 'stop',
        censored=censored,
        censor_reason=walk.censor_reason,
        last_index=walk.last_index,
        closed_time=None if censored else exits[-1].time,
        price_r=price_r,
        cost_r=cost_r,
        funding_r=funding_r,
        total_r=total_r,
        open_share=walk.open_share if censored else 0.0,
        mark_r=walk.mark_r if censored else None,
        mae_r=walk.mae,
        mfe_r=walk.mfe,
        bars_held=walk.last_index - entry.fill_index + 1,
    )
