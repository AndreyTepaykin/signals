"""Triggers, the efficiency gate and the setup family.

"Only that family may fire": ``trend_family`` (the stateless 24-bar efficiency
family) selects which triggers may be considered, through
``constants.TRIGGERS_BY_FAMILY``. A NULL family (cold start) fires nothing.

Each trigger below is evaluated for the direction the 4h swing break allows
(``swing_break_direction``); none of them can pick a side.

``swing_break``     fires on ``swing_break_event`` - the 15m bar that closes the first 4h
                    close beyond the swing. NOT on ``swing_break_state``; see the M4
                    BUILD-LOG entry for why.
``pullback_reset``  the feature row's ``pullback_reset`` flag (RSI(14) back in the
                    40-50 / 50-60 band, or price tagging the session VWAP). The flag is
                    already FALSE outside the trend family.
``squeeze``         ``squeeze_on`` AND the aggressor imbalance picks the allowed side
                    ("the squeeze says a break is near; aggressor imbalance picks the side").
``vwap_tag``        chop mean-reversion entry: the close is stretched at least
                    ``VWAP_STRETCH_ATR`` from the session VWAP on the side OPPOSITE the trade
                    (long: below the VWAP; short: above it), so the trade is back toward it.

A NULL input never fires a trigger.
"""

from __future__ import annotations

from app.core import constants as C
from app.core.rules import direction as D
from app.core.rules.inputs import RuleInputs


def swing_break_fires(inputs: RuleInputs, side: str) -> bool:
    if C.SWING_BREAK_TRIGGER_FIRES_ON != 'event':  # pragma: no cover - frozen constant guard
        raise RuntimeError('only the event reading of the swing-break trigger is implemented')
    return inputs.swing_break_event is True


def pullback_reset_fires(inputs: RuleInputs, side: str) -> bool:
    return inputs.pullback_reset is True


def squeeze_fires(inputs: RuleInputs, side: str) -> bool:
    return inputs.squeeze_on is True and D.aggressor_picks(inputs) == side


def vwap_tag_fires(inputs: RuleInputs, side: str) -> bool:
    distance = inputs.session_vwap_distance_atr
    if distance is None:
        return False
    if side == D.LONG:
        return distance <= -C.VWAP_STRETCH_ATR
    return distance >= C.VWAP_STRETCH_ATR


_FIRES = {
    C.TRIGGER_SWING_BREAK: swing_break_fires,
    C.TRIGGER_PULLBACK_RESET: pullback_reset_fires,
    C.TRIGGER_SQUEEZE: squeeze_fires,
    C.TRIGGER_VWAP_TAG: vwap_tag_fires,
}


def allowed_triggers(family: str | None) -> tuple[str, ...]:
    """Triggers the efficiency ``family`` may use, in priority order (empty for an unknown family)."""
    if family is None:
        return ()
    allowed = C.TRIGGERS_BY_FAMILY.get(family, ())
    return tuple(trigger for trigger in C.TRIGGER_PRIORITY if trigger in allowed)


def fired_triggers(inputs: RuleInputs, side: str) -> tuple[str, ...]:
    """Every allowed trigger that fires on this bar, in priority order."""
    return tuple(t for t in allowed_triggers(inputs.trend_family) if _FIRES[t](inputs, side))


def setup_family(trigger: str) -> str:
    return C.SETUP_FAMILY_BY_TRIGGER[trigger]
