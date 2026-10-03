"""Independence at creation: the one-open-signal test and ``sample_group_id`` assignment.

Contract (``app.models.decisions``): the group is assigned when the candidate is
written, from decision-time data only, and never rewritten. This module is the pure
half; the worker supplies the earlier candidates.

The hold window
---------------
A candidate's frozen hold is ``[bar_close, bar_close + horizon)``. The fill is the
NEXT bar's open, which for contiguous 15m bars is exactly the signal bar's close, so
the deadline is computable at creation even though ``candidate.vertical_deadline``
itself stays NULL until the fill is observed. The tests use the FROZEN window, never
the actual resolution, so no outcome can influence the rules (an early-resolved
trade still holds its slot and its group).

They compare against the NEW candidate's ``bar_close`` (its fill time), not its
``decision_time``: on the 15m grid the two give the same answer, and the bar close is
identical in a live run and a replay.

Assignment (first match wins; "overlap" before "btc_impulse")
-------------------------------------------------------------
``overlap``      an earlier candidate on the same coin (any status) whose hold is still
                 open at this bar. Inherit the earliest such candidate's group.
``btc_impulse``  an alt candidate (not the reference coin) when the reference coin has a
                 candidate (any status) on this bar or earlier, in the SAME direction,
                 whose hold is still open. Inherit that candidate's group.
``new``          otherwise: a fresh UUID and no leader.

Known limits, deliberate (the group cannot be merged afterwards): when a candidate
overlaps its own coin's group AND a differently grouped BTC candidate, it inherits the
own-coin group; an alt that fires before BTC's candidate exists is ``new`` and BTC's
later candidate does not retro-join it. Rules run BTC first on every bar so that the
common case (same bar) groups.

``sample_group_id`` for ``new`` is a UUIDv5 of ``(coin, horizon, bar_close, source)``:
unique per candidate identity, and identical when the same bar is replayed, so two runs
of the same history are comparable row by row.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.core import constants as C
from app.core.hashing import canonical_serialize
from app.core.time import ensure_utc, vertical_barrier_deadline

REASON_NEW = 'new'
REASON_OVERLAP = 'overlap'
REASON_BTC_IMPULSE = 'btc_impulse'

#: Fixed namespace for ``new_group_id`` (random once, then frozen; not a threshold).
SAMPLE_GROUP_NAMESPACE = uuid.UUID('6f0c3f0e-5d58-4f0b-9a63-3b1e6c0f4d21')


@dataclass(frozen=True)
class PriorCandidate:
    """The columns of an earlier candidate that independence depends on."""

    id: int
    coin: str
    horizon: str
    bar_close: datetime
    decision_time: datetime
    direction: str
    status: str
    status_reason: str | None
    sample_group_id: uuid.UUID


@dataclass(frozen=True)
class GroupAssignment:
    sample_group_id: uuid.UUID
    leader_id: int | None
    reason: str


def hold_deadline(bar_close: datetime, horizon: str) -> datetime:
    """The frozen end of the hold for a candidate on this bar (fill = the bar's close)."""
    return vertical_barrier_deadline(bar_close, horizon)


def new_group_id(coin: str, horizon: str, bar_close: datetime, source: str) -> uuid.UUID:
    name = canonical_serialize({'coin': coin, 'horizon': horizon, 'bar_close': ensure_utc(bar_close), 'source': source})
    return uuid.uuid5(SAMPLE_GROUP_NAMESPACE, name)


def _earlier_and_open(
    priors: Sequence[PriorCandidate], horizon: str, bar_close: datetime, *, same_bar: bool = False
) -> list[PriorCandidate]:
    bar_close = ensure_utc(bar_close)
    return [
        p
        for p in priors
        if p.horizon == horizon
        and (ensure_utc(p.bar_close) <= bar_close if same_bar else ensure_utc(p.bar_close) < bar_close)
        and hold_deadline(p.bar_close, p.horizon) > bar_close
    ]


def _order(prior: PriorCandidate) -> tuple[datetime, datetime, int]:
    return (ensure_utc(prior.decision_time), ensure_utc(prior.bar_close), prior.id)


def occupies_slot(prior: PriorCandidate) -> bool:
    """Does this earlier candidate hold the coin's one open-signal slot?

    An alert does. So does a candidate still waiting for the options check
    (``watch`` / ``pending_options_check``), because the check may make it an alert.
    Every other status is a shadow row and does not.
    """
    if prior.status == 'alert':
        return True
    return prior.status == 'watch' and prior.status_reason == C.STATUS_REASON_PENDING_OPTIONS_CHECK


def has_open_signal(priors: Sequence[PriorCandidate], coin: str, horizon: str, bar_close: datetime) -> bool:
    """"One open signal per coin, for the whole horizon" - evaluated on the frozen hold."""
    return any(p.coin == coin and occupies_slot(p) for p in _earlier_and_open(priors, horizon, bar_close))


def assign_sample_group(
    *,
    coin: str,
    horizon: str,
    source: str,
    bar_close: datetime,
    direction: str,
    priors: Sequence[PriorCandidate],
    reference_coin: str,
) -> GroupAssignment:
    """Decide ``sample_group_id``, the leader and the reason for a candidate on this bar.

    ``priors`` must be candidates of the same ``source``; the worker filters. Those
    with ``bar_close`` at or after this bar are ignored, so a replay that re-runs a
    stretch of history cannot see its own future.
    """
    live = _earlier_and_open(priors, horizon, bar_close)
    same_bar_or_earlier = _earlier_and_open(priors, horizon, bar_close, same_bar=True)

    own = sorted((p for p in live if p.coin == coin), key=_order)
    if own:
        return GroupAssignment(own[0].sample_group_id, own[0].id, REASON_OVERLAP)

    if coin != reference_coin:
        btc = sorted(
            (p for p in same_bar_or_earlier if p.coin == reference_coin and p.direction == direction), key=_order
        )
        if btc:
            return GroupAssignment(btc[0].sample_group_id, btc[0].id, REASON_BTC_IMPULSE)

    return GroupAssignment(new_group_id(coin, horizon, bar_close, source), None, REASON_NEW)
