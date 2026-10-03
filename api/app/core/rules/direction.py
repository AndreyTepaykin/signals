"""The direction vote.

Spec: "Directional sources vote on Long / Short / Watch." and "The higher-timeframe
swing break allows the direction."

How the two statements are combined
-----------------------------------
* The direction itself is ``swing_break_direction``, and only while
  ``swing_break_state`` is TRUE. No permission, no candidate: a 15m trigger can
  never pick a side against the 4h structure, and the chandelier stop (the 1R) is
  defined only for the break direction.
* The positioning sources then vote for that direction, against it, or abstain.
  The swing break is not counted again here: it is the proposal, not a voter.

Voters (``+1`` = argues long, ``-1`` = argues short, ``0`` = abstains or NULL):

``aggressor_imbalance``  z >= +DIRECTION_VOTE_Z_MIN -> long; z <= -DIRECTION_VOTE_Z_MIN -> short.
                         Momentum: who is crossing the spread now.
``oi_price_quadrant``    ``price_up_oi_up`` (new longs) -> long; ``price_down_oi_up``
                         (new shorts) -> short. Covering, capitulation and the dead
                         zone carry no directional information, so they abstain.
``funding``              CROWDING, so contrarian: z >= +Z -> short; z <= -Z -> long.
``perp_basis``           the same reading as funding ("leverage appetite").

The candidate is confirmed when ``votes_for - votes_against >= DIRECTION_VOTE_MIN_NET``
(zero: a majority has to oppose before a break becomes a Watch). A NULL input is
an abstention, never a zero reading.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core import constants as C
from app.core.rules.inputs import RuleInputs

LONG = 'long'
SHORT = 'short'
SIDE_SIGN = {LONG: 1, SHORT: -1}

SOURCE_AGGRESSOR = 'aggressor_imbalance'
SOURCE_OI_QUADRANT = 'oi_price_quadrant'
SOURCE_FUNDING = 'funding'
SOURCE_PERP_BASIS = 'perp_basis'
SOURCES = (SOURCE_AGGRESSOR, SOURCE_OI_QUADRANT, SOURCE_FUNDING, SOURCE_PERP_BASIS)


def _momentum(z: float | None) -> int:
    if z is None:
        return 0
    if z >= C.DIRECTION_VOTE_Z_MIN:
        return 1
    if z <= -C.DIRECTION_VOTE_Z_MIN:
        return -1
    return 0


def _crowding(z: float | None) -> int:
    return -_momentum(z)


def _quadrant(quadrant: str | None) -> int:
    if quadrant == 'price_up_oi_up':
        return 1
    if quadrant == 'price_down_oi_up':
        return -1
    return 0


def source_votes(inputs: RuleInputs) -> dict[str, int]:
    """Each voter's signed vote (``+1`` long, ``-1`` short, ``0`` abstain), in ``SOURCES`` order."""
    return {
        SOURCE_AGGRESSOR: _momentum(inputs.aggressor_imbalance_z),
        SOURCE_OI_QUADRANT: _quadrant(inputs.oi_price_quadrant),
        SOURCE_FUNDING: _crowding(inputs.funding_z),
        SOURCE_PERP_BASIS: _crowding(inputs.perp_basis_z),
    }


@dataclass(frozen=True)
class VoteTally:
    direction: str
    votes: dict[str, int]
    votes_for: int
    votes_against: int
    confirmed: bool

    @property
    def net(self) -> int:
        return self.votes_for - self.votes_against


def tally(direction: str, inputs: RuleInputs) -> VoteTally:
    """Count the positioning votes for and against ``direction``."""
    sign = SIDE_SIGN[direction]
    votes = source_votes(inputs)
    votes_for = sum(1 for value in votes.values() if value == sign)
    votes_against = sum(1 for value in votes.values() if value == -sign)
    return VoteTally(direction, votes, votes_for, votes_against, votes_for - votes_against >= C.DIRECTION_VOTE_MIN_NET)


def aggressor_picks(inputs: RuleInputs) -> str | None:
    """The side the aggressor imbalance picks (``long`` / ``short``), or ``None`` when it does not decide."""
    vote = _momentum(inputs.aggressor_imbalance_z)
    return LONG if vote > 0 else SHORT if vote < 0 else None
