"""The pure core of "fill, resolve, charge funding", shared by the label job and the stats engine's naive baseline.

Both need the same arithmetic from the same hashed conventions, so the composition lives here once instead of being
re-implemented: :func:`plan_fill` (the entry bar to barriers, or a ``no_entry``), and :func:`resolve_outcome` (the contiguous
bars to a resolution, then settled funding with the completeness / stall-cap rule). The label job adds the database around
them; the stats job adds a different reader. No I/O and no clock: the caller passes the as-of instant.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.labeler import barriers as B

PENDING_ENTRY_BAR = 'entry_bar_not_visible'
PENDING_AWAITING_BARS = 'awaiting_bars'
PENDING_MISSING_BAR = 'missing_bar'
PENDING_FUNDING = 'funding_incomplete'


@dataclass(frozen=True)
class NoEntry:
    """The fill carries no barriers: the open was at or through the stop or the cap."""

    entry: Decimal
    reason: str


@dataclass(frozen=True)
class Outcome:
    """A resolved trade: the resolution plus the funding charged over the hold."""

    resolution: B.Resolution
    funding_r: Decimal
    funding_bps: Decimal
    funding_partial: bool


@dataclass(frozen=True)
class Waiting:
    """Not resolvable yet; ``detail`` is one of the ``PENDING_*`` codes."""

    detail: str


def plan_fill(
    *,
    direction: str,
    stop: Decimal,
    cap: Decimal | None,
    fill_time: datetime,
    horizon: str,
    entry_bars: Sequence[B.Bar],
) -> B.Barriers | NoEntry | str:
    """Barriers from the entry bar's open; a :class:`NoEntry` when it opened through the stop / cap; or the pending code."""
    if not entry_bars or entry_bars[0].open_time != fill_time:
        return PENDING_ENTRY_BAR
    entry = entry_bars[0].open
    try:
        return B.plan_barriers(direction=direction, entry=entry, stop=stop, cap=cap, fill_time=fill_time, horizon=horizon)
    except B.BarrierPlanError as exc:
        return NoEntry(entry, exc.reason)


def resolve_outcome(
    *,
    direction: str,
    barriers: B.Barriers,
    bars: Sequence[B.Bar],
    load_prints: Callable[[datetime, datetime], Sequence[B.FundingPrint]],
    fill_time: datetime,
    as_of_time: datetime,
    funding_max_gap: timedelta,
    missing_bar_after: timedelta,
) -> Outcome | Waiting:
    """Resolve over ``bars`` and charge realized funding. ``load_prints(start, end)`` returns the visible settled prints."""
    resolution = B.resolve(direction, barriers, bars, fill_time)
    if isinstance(resolution, B.Pending):
        late = resolution.next_open + missing_bar_after <= as_of_time
        return Waiting(PENDING_MISSING_BAR if late else PENDING_AWAITING_BARS)
    prints = load_prints(fill_time - funding_max_gap, resolution.resolved_at)
    partial = False
    if not B.funding_window_complete([p.funding_time for p in prints], fill_time, resolution.resolved_at, funding_max_gap):
        if not B.funding_stalled(resolution.visible_at, as_of_time):
            return Waiting(PENDING_FUNDING)
        partial = True
    funding_r, funding_bps = B.realized_funding(direction, prints, barriers, fill_time, resolution.resolved_at)
    return Outcome(resolution, funding_r, funding_bps, partial)
