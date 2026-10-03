"""The frozen rule ladder: one feature row in, at most one candidate proposal out.

``evaluate(inputs, context)`` is pure. The order, and what happens at each step:

1. **Permission.** ``swing_break_state`` TRUE with a ``swing_break_direction``. This
   is the direction; without it there is no candidate (skip ``no_swing_break_permission``).
2. **Efficiency family.** ``trend_family`` must be known (skip ``trend_family_unknown``);
   only the triggers of that family may fire.
3. **Stop.** The chandelier stop must be readable on the protective side (skip
   ``no_usable_stop``): it is the 1R, so a candidate without one cannot be labeled.
4. **Trigger.** At least one allowed trigger fires (skip ``no_trigger``); the first in
   ``TRIGGER_PRIORITY`` is recorded.
5. **One BTC view.** An alt must clear the lagged-beta residual z gate IN the trade
   direction on the signal bar (skip ``alt_residual_gate``). NULL does not clear it.
6. **One open signal per coin, per horizon** (skip ``open_signal``; the worker computes
   ``context.has_open_signal`` from the frozen hold windows).

Steps 1-6 produce no row when they fail. What survives becomes a row, and its
status is the first of:

7. ``cancelled`` / ``stand_down:<kinds>`` - a feed past its available-at rule (live only,
   supplied by the worker), an active event window, or perp and spot bars more than 1 ATR
   apart. Shadow row: it resolves on the same barriers so the promotion gate can see
   whether the stand-down helped.
8. ``cancelled`` / ``cvd_divergence_against`` - CVD diverges against the direction.
9. ``watch`` / ``vote_conflict`` - the positioning vote opposes the direction.
10. ``watch`` / ``pending_options_check`` - everything passed; the options check (M5)
    turns it into ``alert`` / ``delayed`` / ``cancelled`` / ``watch``. The schema forbids
    an ``alert`` with no options verdict, so M4 never writes one.

The rules never abstain (that needs a pool, M8) and never apply the headwind cancel (needs
the options book, M5).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core import constants as C
from app.core.rules import direction as D
from app.core.rules import triggers as T
from app.core.rules.inputs import RuleInputs
from app.core.rules.stops import StopPlan, stop_plan

STATUS_WATCH = 'watch'
STATUS_CANCELLED = 'cancelled'

SKIP_NO_PERMISSION = 'no_swing_break_permission'
SKIP_FAMILY_UNKNOWN = 'trend_family_unknown'
SKIP_NO_STOP = 'no_usable_stop'
SKIP_NO_TRIGGER = 'no_trigger'
SKIP_ALT_GATE = 'alt_residual_gate'
SKIP_OPEN_SIGNAL = 'open_signal'
SKIP_CODES = (
    SKIP_NO_PERMISSION,
    SKIP_FAMILY_UNKNOWN,
    SKIP_NO_STOP,
    SKIP_NO_TRIGGER,
    SKIP_ALT_GATE,
    SKIP_OPEN_SIGNAL,
)


@dataclass(frozen=True)
class RuleContext:
    """Facts the rules need that are not on the feature row."""

    #: An earlier candidate on this coin and horizon still holds the open-signal slot.
    has_open_signal: bool = False
    #: Names of gating feeds past their available-at rule. The worker fills this for live
    #: rows from ``stand_down_breaches``; a replay passes ``()`` (no historical feed health).
    feed_breaches: tuple[str, ...] = ()


@dataclass(frozen=True)
class Proposal:
    direction: str
    trigger: str
    setup_family: str
    trend_family: str
    status: str
    status_reason: str
    plan: StopPlan
    fired_triggers: tuple[str, ...]
    tally: D.VoteTally
    stand_downs: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class Evaluation:
    proposal: Proposal | None
    skipped: str | None = None


def alt_gate_clears(inputs: RuleInputs, side: str) -> bool:
    """The reference coin needs no gate; an alt needs residual strength IN the trade direction."""
    if inputs.is_reference_coin:
        return True
    z = inputs.alt_residual_z_signal_bar
    return z is not None and D.SIDE_SIGN[side] * z >= C.ALT_RESIDUAL_Z_GATE


def stand_down_kinds(inputs: RuleInputs, context: RuleContext) -> tuple[str, ...]:
    """The stand-down conditions active on this bar, in a fixed order."""
    kinds: list[str] = []
    if context.feed_breaches:
        kinds.append(C.STAND_DOWN_FEED_BREACH)
    if inputs.event_flag is True or (inputs.event_flag is None and C.STAND_DOWN_ON_UNKNOWN_EVENT_CALENDAR):
        kinds.append(C.STAND_DOWN_EVENT_WINDOW)
    gap = inputs.perp_spot_disagreement_atr
    if (gap is None and C.STAND_DOWN_ON_UNKNOWN_PERP_SPOT) or (gap is not None and gap > C.PERP_SPOT_MAX_DISAGREEMENT_ATR):
        kinds.append(C.STAND_DOWN_PERP_SPOT)
    return tuple(kinds)


def cvd_cancels(inputs: RuleInputs, side: str) -> bool:
    return inputs.cvd_divergence is True and inputs.cvd_divergence_against == side


def evaluate(inputs: RuleInputs, context: RuleContext | None = None) -> Evaluation:
    context = context or RuleContext()

    side = inputs.swing_break_direction
    if inputs.swing_break_state is not True or side not in D.SIDE_SIGN:
        return Evaluation(None, SKIP_NO_PERMISSION)
    if inputs.trend_family not in C.FAMILIES:
        return Evaluation(None, SKIP_FAMILY_UNKNOWN)
    plan = stop_plan(inputs, side)
    if plan is None:
        return Evaluation(None, SKIP_NO_STOP)
    fired = T.fired_triggers(inputs, side)
    if not fired:
        return Evaluation(None, SKIP_NO_TRIGGER)
    if not alt_gate_clears(inputs, side):
        return Evaluation(None, SKIP_ALT_GATE)
    if context.has_open_signal:
        return Evaluation(None, SKIP_OPEN_SIGNAL)

    tally = D.tally(side, inputs)
    stand_downs = stand_down_kinds(inputs, context)
    if stand_downs:
        status, reason = STATUS_CANCELLED, C.STATUS_REASON_STAND_DOWN_PREFIX + '+'.join(stand_downs)
    elif cvd_cancels(inputs, side):
        status, reason = STATUS_CANCELLED, C.STATUS_REASON_CVD_DIVERGENCE
    elif not tally.confirmed:
        status, reason = STATUS_WATCH, C.STATUS_REASON_VOTE_CONFLICT
    else:
        status, reason = STATUS_WATCH, C.STATUS_REASON_PENDING_OPTIONS_CHECK

    trigger = fired[0]
    return Evaluation(
        Proposal(
            direction=side,
            trigger=trigger,
            setup_family=T.setup_family(trigger),
            trend_family=inputs.trend_family,
            status=status,
            status_reason=reason,
            plan=plan,
            fired_triggers=fired,
            tally=tally,
            stand_downs=stand_downs,
        )
    )
