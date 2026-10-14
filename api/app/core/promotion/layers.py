"""Layers the promotion gate can judge, and how each one turns a labeled observation into a pair.

Contract
--------
A `Layer` has a name, a description and a pure ``pair(observation, counterfactual)`` that returns
``(r_with_layer, r_without_layer)`` for a row in the layer's paired population, or ``None`` for a row outside it. R is
the label's stored R, already net of fees, slippage and realized funding ("costs on"). A trade the layer stopped was
not taken and scores `params.SKIPPED_TRADE_R` (0 R). The gate drops `no_entry` labels, so a pair always has two numbers.

The two registered layers audit Phase 1's own filters from data already stored
--------------------------------------------------------------------------------
``cvd_divergence_cancel``
    Status ``cancelled``, reason `constants.STATUS_REASON_CVD_DIVERGENCE`.
``vote_conflict_watch``
    Status ``watch``, reason `constants.STATUS_REASON_VOTE_CONFLICT`.

The paired population of a filter is "the same candidates with and without the layer":

* every ``alert``: the filter let it through, so it is the same trade in both arms, ``(R, R)``, difference 0;
* every row the filter blocked whose remaining ladder, with the filter switched off, would have produced an
  ``alert``: ``(0, R)``. The stored R is the R of that alert: the options job gives a shadow row the same stop and
  target cap it would give a pending one, the labeler resolves it on those barriers, and R is signed by the
  candidate's direction, which is the direction the alert would have had.

Why the remaining ladder needs a `Counterfactual`
-------------------------------------------------
The rule ladder (`app.core.rules.evaluate`) is first-match: stand-down -> CVD cancel -> vote conflict -> pending, and the
options check then turns a pending row into ``alert``, ``watch`` (BTC/ETH ``ignore``), ``delayed`` (``expiry_pin``) or
``cancelled`` (headwind with no room), and the abstain rule may turn an alert into ``abstained``. So a blocked row
records only the FIRST reason that fired:

* a CVD-cancelled row never had its vote checked (stand-down is excluded: it fires before CVD);
* neither shadow row kept the status the options check and the abstain rule would have given it (the job records the
  verdict and prices but keeps the shadow status, and abstain only tests alerts).

None of that is on the observation, so the job recomputes it from the stored feature row and book with the same pure
functions the rules and the options job ran, and passes it in. A blocked row with no counterfactual, or whose
counterfactual is anything but ``alert``, carries another blocking reason and is OUT of the pair: it would not have
been traded with the filter switched off either, so it says nothing about the filter.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from app.core import constants as C
from app.core.promotion import params as P
from app.core.stats.observations import Observation
from app.models import enums as E


@dataclass(frozen=True)
class Counterfactual:
    """What the rest of the frozen ladder decides for one candidate if its own blocking gate is switched off."""

    #: The positioning vote confirms the direction (`direction.tally(...).confirmed`).
    vote_confirmed: bool
    #: The status the options check and then the abstain rule give the same row as a pending one.
    status_if_pending: str


Pair = tuple[float, float]
PairFunction = Callable[[Observation, Counterfactual | None], Pair | None]


@dataclass(frozen=True)
class Layer:
    name: str
    description: str
    pair: PairFunction
    #: True for the rows this layer suppressed (the ones the gate counts as blocked).
    blocks: Callable[[Observation], bool]
    #: What the layer is, verbatim, for the log.
    definition: Mapping[str, Any]


def _filter_audit(
    *,
    name: str,
    description: str,
    status: str,
    reason: str,
    status_without: Callable[[Counterfactual], str],
    ladder_below: tuple[str, ...],
) -> Layer:
    def blocks(observation: Observation) -> bool:
        return observation.status == status and observation.status_reason == reason

    def pair(observation: Observation, counterfactual: Counterfactual | None = None) -> Pair | None:
        if observation.stored_r is None:
            return None
        if observation.status == E.STATUS_ALERT:
            return observation.stored_r, observation.stored_r
        if not blocks(observation) or counterfactual is None:
            return None
        if status_without(counterfactual) != E.STATUS_ALERT:
            return None
        return P.SKIPPED_TRADE_R, observation.stored_r

    return Layer(
        name=name,
        description=description,
        pair=pair,
        blocks=blocks,
        definition={
            'kind': 'filter_audit',
            'blocks_status': status,
            'blocks_reason': reason,
            'pass_through': E.STATUS_ALERT,
            'ladder_below_the_filter': list(ladder_below),
            'skipped_trade_r': P.SKIPPED_TRADE_R,
            'r': 'label.stored_r (net of fees, slippage and realized funding)',
        },
    )


def _without_cvd(counterfactual: Counterfactual) -> str:
    if not counterfactual.vote_confirmed:
        return E.STATUS_WATCH
    return counterfactual.status_if_pending


def _without_vote(counterfactual: Counterfactual) -> str:
    return counterfactual.status_if_pending


CVD_DIVERGENCE_CANCEL = _filter_audit(
    name='cvd_divergence_cancel',
    description=(
        'Phase 1 filter: cancel a candidate when CVD diverges against its direction. With the filter the trade is not '
        'taken (0 R); without it, the alert the rest of the ladder would have produced earns its stored R.'
    ),
    status=E.STATUS_CANCELLED,
    reason=C.STATUS_REASON_CVD_DIVERGENCE,
    status_without=_without_cvd,
    ladder_below=('vote_conflict', 'options_check', 'abstain'),
)

VOTE_CONFLICT_WATCH = _filter_audit(
    name='vote_conflict_watch',
    description=(
        'Phase 1 filter: hold a candidate at watch when the positioning vote opposes its direction. With the filter the '
        'trade is not taken (0 R); without it, the alert the options check would have produced earns its stored R.'
    ),
    status=E.STATUS_WATCH,
    reason=C.STATUS_REASON_VOTE_CONFLICT,
    status_without=_without_vote,
    ladder_below=('options_check', 'abstain'),
)

LAYERS: Mapping[str, Layer] = {layer.name: layer for layer in (CVD_DIVERGENCE_CANCEL, VOTE_CONFLICT_WATCH)}


def get_layer(name: str) -> Layer:
    try:
        return LAYERS[name]
    except KeyError:
        raise KeyError(f'unknown layer {name!r}; registered: {sorted(LAYERS)}') from None
