"""The chandelier stop as 1R, and the pre-options target estimate.

Spec: "Chandelier stop ... Extreme since that swing, minus 3x ATR (plus 3x ATR for
shorts). This stop is 1R." and "Upper barrier: +1R in the trade direction".

The feature row already holds the stop (``chandelier_stop_price``) and its
distance from the signal-bar close in ATR (``chandelier_stop_distance_atr``,
positive on the protective side) with the ATR (``atr_price``). The rules do not
recompute the stop; they read it. Two derived numbers:

``reference price``  ``stop +/- distance * atr`` - the signal-bar close the distance was
                     measured from. The real entry is the NEXT bar's open, unknown at
                     decision time, so every R here is an ESTIMATE.
``pre-options target``  ``reference +/- 1R`` where ``1R = distance * atr``. The options check
                     (M5) may cap or tighten it; the labeler (M6) re-derives the barriers
                     from the real entry and the final stop. A short whose target would sit
                     at or below zero has no target (NULL).

Prices are exact ``Decimal`` rounded half-even to the ``numeric(24, 10)`` price scale.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from app.core.rules import direction as D
from app.core.rules.inputs import RuleInputs

PRICE_QUANTUM = Decimal('1E-10')


def _q(value: Decimal) -> Decimal:
    return value.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class StopPlan:
    stop_price: Decimal
    reference_price: Decimal
    one_r: Decimal
    target_price: Decimal | None


def stop_plan(inputs: RuleInputs, side: str) -> StopPlan | None:
    """The stop and target for ``side``, or ``None`` when the stop cannot be read safely."""
    stop, distance, atr = inputs.chandelier_stop_price, inputs.chandelier_stop_distance_atr, inputs.atr_price
    if stop is None or distance is None or atr is None:
        return None
    if distance <= 0 or atr <= 0 or stop <= 0:
        return None  # stop breached, or an unusable ATR / price
    sign = D.SIDE_SIGN[side]
    one_r = distance * atr
    reference = stop + sign * one_r
    target = reference + sign * one_r
    return StopPlan(
        stop_price=_q(stop),
        reference_price=_q(reference),
        one_r=_q(one_r),
        target_price=_q(target) if target > 0 else None,
    )
