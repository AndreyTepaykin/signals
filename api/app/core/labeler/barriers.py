"""Triple-barrier arithmetic, no I/O.

Spec ("Label contract"): entry is the next bar's open after the signal bar; 1R is the distance from that entry to the final
stop; the upper barrier is +1R in the trade direction or the strike cap if it is nearer; the lower barrier is the stop; the
vertical barrier is fill time plus the horizon; resolution is whichever trades first; a stop that gaps fills at the worse of
the stop and the open.

Choices the spec leaves open (see the M6 entry in ``docs/BUILD-LOG.md``). Each is a ``LABEL_*`` value in
``app.core.constants`` and so is part of ``rule_hash``; this module refuses to import if one names a convention it does not
implement:

* **Same-bar ambiguity.** The bars are 15m, so a bar that touches both barriers cannot be ordered. It resolves to the stop and
  is flagged (``LABEL_AMBIGUOUS_BAR_RESOLVES_TO``).
* **No entry.** A next open at or through the stop, or through the cap, leaves no valid barriers: :func:`plan_barriers` raises
  and the job writes a ``no_entry`` label with no R.
* **Gaps.** A bar after the entry bar that OPENS beyond the stop exits at that open (``gapped_stop``). A bar that opens beyond
  the target exits at the target, not at the better open (a limit order does not get price improvement here).
* **Timeout.** No barrier by the deadline exits at the close of the bar that ends at the deadline; ``first_barrier`` is
  ``vertical`` and the R is whatever that close is worth. The spec's "a miss is ... the horizon ending with stored R at or below
  zero" is a classification for the stats engine, not a different number.
* **resolved_at** is the close of the bar that held the exit, because a 15m bar cannot place the touch inside itself.
* **Visibility.** The bars handed in must be contiguous from the fill. A hole ends the scan: a hit before the hole stands, no
  hit before it leaves the label pending.
* **Excursions.** MAE and MFE run over the bars up to and including the resolving bar, except that a stop-out bar contributes
  its adverse side only (its high may have come after the stop).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from app.core import constants as C
from app.core.features import params as P
from app.core.time import ensure_utc, vertical_barrier_deadline
from app.models import enums as E

BAR_TIMEFRAME = P.SIGNAL_TIMEFRAME
BAR_LENGTH = P.timeframe_delta(BAR_TIMEFRAME)

_SUPPORTED_CONVENTIONS = {
    'LABEL_AMBIGUOUS_BAR_RESOLVES_TO': ('stop', 'target'),
    'LABEL_GAP_STOP_FILLS_AT': ('open', 'stop'),
    'LABEL_GAP_TARGET_FILLS_AT': ('barrier', 'open'),
    'LABEL_FEES_CHARGED_ON': ('entry_price',),
    'LABEL_VERTICAL_EXIT': ('deadline_bar_close',),
    'LABEL_RESOLVED_AT': ('bar_close',),
    'LABEL_NO_ENTRY_RULE': ('open_at_or_through_stop_or_through_cap',),
    'LABEL_FUNDING_WINDOW': ('after_fill_before_exit',),
    'LABEL_FUNDING_NOTIONAL': ('mark_price_else_entry',),
}


def check_conventions() -> None:
    """Raise if a `LABEL_*` constant names a convention this module does not implement."""
    for name, supported in _SUPPORTED_CONVENTIONS.items():
        if getattr(C, name) not in supported:
            raise ImportError(f'constants.{name} = {getattr(C, name)!r} is not implemented by the labeler; supported: {supported}')


check_conventions()


def ambiguous_resolves_to() -> str:
    return E.BARRIER_LOWER if C.LABEL_AMBIGUOUS_BAR_RESOLVES_TO == 'stop' else E.BARRIER_UPPER


FUNDING_MAX_GAP = timedelta(hours=C.LABEL_FUNDING_MAX_INTERVAL_HOURS, minutes=C.LABEL_FUNDING_GAP_SLACK_MINUTES)
FUNDING_STALL_CAP = timedelta(hours=C.LABEL_FUNDING_STALL_CAP_HOURS)

REASON_ENTRY_THROUGH_STOP = 'entry_through_stop'
REASON_ENTRY_THROUGH_CAP = 'entry_through_cap'

SIDE_SIGN = {E.DIRECTION_LONG: 1, E.DIRECTION_SHORT: -1}

R_QUANTUM = Decimal('0.000001')
HOURS_QUANTUM = Decimal('0.0001')
BPS_QUANTUM = Decimal('0.000001')
_BPS_PER_UNIT = Decimal(10000)
_SECONDS_PER_HOUR = Decimal(3600)


def _q(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_HALF_EVEN)


class BarrierPlanError(ValueError):
    """The fill cannot carry a valid set of barriers (it opened through the stop or the cap)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Barriers:
    entry: Decimal
    one_r: Decimal
    upper: Decimal
    lower: Decimal
    deadline: datetime


@dataclass(frozen=True)
class Bar:
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    available_at: datetime | None = None


@dataclass(frozen=True)
class Resolution:
    first_barrier: str
    exit_price: Decimal
    resolved_at: datetime
    visible_at: datetime
    price_r: Decimal
    ambiguous: bool
    gapped_stop: bool
    mae_r: Decimal
    mfe_r: Decimal
    mfe_at: datetime | None


@dataclass(frozen=True)
class Pending:
    """Not resolvable yet: the bar opening at ``next_open`` is the first one missing or not yet visible."""

    next_open: datetime


@dataclass(frozen=True)
class FundingPrint:
    funding_time: datetime
    rate: Decimal
    mark_price: Decimal | None = None


def plan_barriers(
    *,
    direction: str,
    entry: Decimal,
    stop: Decimal,
    cap: Decimal | None,
    fill_time: datetime,
    horizon: str,
) -> Barriers:
    """Entry, 1R, both barrier prices and the deadline from the real fill. Raises :class:`BarrierPlanError`."""
    sign = SIDE_SIGN[direction]
    one_r = (entry - stop) * sign
    if one_r <= 0:
        raise BarrierPlanError(REASON_ENTRY_THROUGH_STOP)
    upper = entry + sign * one_r
    if cap is not None:
        room = (cap - entry) * sign
        if room <= 0:
            raise BarrierPlanError(REASON_ENTRY_THROUGH_CAP)
        if room < one_r:
            upper = cap
    return Barriers(entry=entry, one_r=one_r, upper=upper, lower=stop, deadline=vertical_barrier_deadline(fill_time, horizon))


def resolve(direction: str, barriers: Barriers, bars: Sequence[Bar], fill_time: datetime) -> Resolution | Pending:
    """First barrier hit over contiguous bars from ``fill_time``, or the vertical exit, or ``Pending``."""
    sign = SIDE_SIGN[direction]
    entry, one_r = barriers.entry, barriers.one_r
    expected = ensure_utc(fill_time)
    worst_adverse = Decimal(0)
    best_favorable = Decimal(0)
    best_favorable_at: datetime | None = None
    last: Bar | None = None

    def excursions(adverse: Decimal, favorable: Decimal | None, at: datetime) -> None:
        nonlocal worst_adverse, best_favorable, best_favorable_at
        worst_adverse = max(worst_adverse, adverse)
        if favorable is not None and favorable > best_favorable:
            best_favorable, best_favorable_at = favorable, at

    def finish(
        first: str, exit_price: Decimal, bar: Bar, *, ambiguous: bool = False, gapped: bool = False, mae: Decimal | None = None
    ) -> Resolution:
        price_r = _q(sign * (exit_price - entry) / one_r, R_QUANTUM)
        mfe = _q(best_favorable / one_r, R_QUANTUM)
        return Resolution(
            first_barrier=first,
            exit_price=exit_price,
            resolved_at=bar.close_time,
            visible_at=bar.available_at or bar.close_time,
            price_r=price_r,
            ambiguous=ambiguous,
            gapped_stop=gapped,
            mae_r=_q(worst_adverse / one_r, R_QUANTUM) if mae is None else mae,
            mfe_r=mfe,
            mfe_at=best_favorable_at if mfe > 0 else None,
        )

    for index, bar in enumerate(sorted(bars, key=lambda b: b.open_time)):
        if bar.open_time != expected or bar.open_time >= barriers.deadline:
            break
        worst, best = (bar.low, bar.high) if sign > 0 else (bar.high, bar.low)
        adverse, favorable = sign * (entry - worst), sign * (best - entry)
        stop_touched = sign * (barriers.lower - worst) >= 0
        target_touched = sign * (best - barriers.upper) >= 0
        gapped = index > 0 and sign * (barriers.lower - bar.open) >= 0
        both = stop_touched and target_touched

        if gapped:
            fill = bar.open if C.LABEL_GAP_STOP_FILLS_AT == 'open' else barriers.lower
            return finish(E.BARRIER_LOWER, fill, bar, gapped=True, mae=_q(sign * (entry - fill) / one_r, R_QUANTUM))
        if stop_touched and (not target_touched or ambiguous_resolves_to() == E.BARRIER_LOWER):
            return finish(E.BARRIER_LOWER, barriers.lower, bar, ambiguous=both, mae=_q(sign * (entry - barriers.lower) / one_r, R_QUANTUM))
        excursions(adverse, favorable, bar.close_time)
        if target_touched:
            opens_beyond = index > 0 and sign * (bar.open - barriers.upper) >= 0
            fill = bar.open if opens_beyond and C.LABEL_GAP_TARGET_FILLS_AT == 'open' else barriers.upper
            return finish(E.BARRIER_UPPER, fill, bar, ambiguous=both)
        expected = bar.close_time
        last = bar

    if last is not None and expected == barriers.deadline:
        return finish(E.BARRIER_VERTICAL, last.close, last)
    return Pending(next_open=expected)


def fees_r(barriers: Barriers) -> Decimal:
    """Frozen round-trip taker fee plus slippage, as a non-negative number of R, charged on the entry price."""
    return _q(barriers.entry * C.ROUND_TRIP_COST_BPS / _BPS_PER_UNIT / barriers.one_r, R_QUANTUM)


def funding_window_complete(
    print_times: Sequence[datetime], fill_time: datetime, resolved_at: datetime, max_gap: timedelta
) -> bool:
    """Whether the visible settled prints prove none was missed between the fill and the exit.

    Needs a print at or before the fill, no hole longer than ``max_gap`` from there on, and a print within ``max_gap`` of the
    exit. A hold that contains no settlement is complete when the prints around it show that.
    """
    times = sorted(ensure_utc(t) for t in print_times if ensure_utc(t) <= ensure_utc(resolved_at))
    before = [t for t in times if t <= ensure_utc(fill_time)]
    if not before:
        return False
    chain = [before[-1]] + [t for t in times if t > ensure_utc(fill_time)]
    if any(later - earlier > max_gap for earlier, later in zip(chain, chain[1:])):
        return False
    return ensure_utc(resolved_at) - chain[-1] <= max_gap


def funding_stalled(visible_at: datetime, as_of_time: datetime) -> bool:
    """Whether a label has waited ``LABEL_FUNDING_STALL_CAP_HOURS`` for funding since its exit became visible."""
    return ensure_utc(as_of_time) - ensure_utc(visible_at) >= FUNDING_STALL_CAP


def realized_funding(
    direction: str, prints: Sequence[FundingPrint], barriers: Barriers, fill_time: datetime, resolved_at: datetime
) -> tuple[Decimal, Decimal]:
    """``(funding_r, realized_funding_bps)``: signed, positive when funding was received.

    Counts prints with ``fill_time < funding_time < resolved_at``. A long pays a positive rate. The notional is the print's
    mark price, or the entry when the print carries none.
    """
    sign = SIDE_SIGN[direction]
    start, end = ensure_utc(fill_time), ensure_utc(resolved_at)
    in_hold = [p for p in prints if start < ensure_utc(p.funding_time) < end]
    paid = sum((sign * p.rate * (barriers.entry if p.mark_price is None else p.mark_price) for p in in_hold), Decimal(0))
    rate_sum = sum((p.rate for p in in_hold), Decimal(0))
    return _q(-paid / barriers.one_r, R_QUANTUM), _q(-sign * rate_sum * _BPS_PER_UNIT, BPS_QUANTUM)


def hours_between(start: datetime, end: datetime) -> Decimal:
    return _q(Decimal((ensure_utc(end) - ensure_utc(start)).total_seconds()) / _SECONDS_PER_HOUR, HOURS_QUANTUM)


def stored_r(price_r: Decimal, fees: Decimal, funding: Decimal) -> Decimal:
    return price_r - fees + funding
