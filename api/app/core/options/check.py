"""The options influence check: one pending candidate plus one book view in, one decision out.

Spec ("Options influence check"): "Runs after the candidate exists, only when that venue's book is
liquid. ... Thin open interest or a stale snapshot returns `ignore` and leaves size, stop, and
targets unchanged. The check may skip, delay, resize, widen the stop, or cap the target. It may set
`expression`. It does not change P. Max pain is not its own signal. It enters only as `expiry_pin`."

Pure: no database, no clock. ``decision_time`` is a field of the input, so a live run and a replay
that hand it the same book give the same decision to the last digit.

Prices and R
------------
The entry is the NEXT bar's open and is unknown at the decision, so every R here is an estimate
measured from the signal-bar close (``reference_price``), exactly as M4 does: ``1R = |reference -
pre-options stop|`` (``one_r``) and the pre-options upper level is ``reference +/- 1R``. The labeler
(M6) re-derives the real barriers from the real entry and the final stop; ``stop_price`` and
``target_cap_price`` are the two layers it takes from here.

The ladder (first match wins; ``OPTIONS_VERDICT_PRECEDENCE``)
------------------------------------------------------------
1. ``ignore``      no book for the coin; no visible snapshot; snapshot older than 15 minutes at the
                   decision; total open interest unknown; the per-coin OI floor unset or not met.
2. ``expiry_pin``  the expiry nearest the hold settles within 48h (and has not already settled), its
                   max pain lies between the entry and the upper level, and its largest strike holds
                   >= 15% of that expiry's open interest. -> status ``delayed``, ``wait_for_expiry``;
                   ``target_cap_price`` = the max pain strike.
3. ``headwind``    a near-dated strike between the entry and the upper level holds >= 10% of
                   near-dated open interest. The NEAREST such strike caps the target
                   (``target_cap_price``). Room to the cap under 0.5R -> status ``cancelled``.
4. ``amplify``     estimated dealers short gamma, flip within 1 ATR of the entry, and the signal is
                   with the break. Stop widened by 0.5 ATR away from the entry; size x1.15.
5. ``dampen``      estimated dealers long gamma. Size x0.70; ``target_cap_price`` = the nearer of the
                   model target and +1R.
6. ``align``       nothing above fired. Size x1.00.

``vol_rich`` / ``vol_cheap`` (the 90-day percentile of same-tenor IV minus realized vol) sit beside
any verdict that is still a live card, i.e. not ``ignore``, not ``expiry_pin``, not a cancelled
headwind. ``vol_cheap`` lets ``expression`` be ``defined_risk_option``.

Which rows change status
------------------------
Only a PENDING row (``watch`` / ``pending_options_check``). A shadow row written by M4 (stand-down,
CVD divergence, vote conflict) gets the full set of options fields and the price layers, so it resolves
on the barriers it would have had, but its status and reason never change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from app.core import constants as C
from app.core.options.metrics import StrikeOI
from app.core.rules.stops import PRICE_QUANTUM

STATUS_ALERT = 'alert'
STATUS_WATCH = 'watch'
STATUS_CANCELLED = 'cancelled'
STATUS_DELAYED = 'delayed'

VERDICT_IGNORE, VERDICT_EXPIRY_PIN, VERDICT_HEADWIND, VERDICT_AMPLIFY, VERDICT_DAMPEN, VERDICT_ALIGN = C.OPTIONS_VERDICT_PRECEDENCE
VOL_RICH, VOL_CHEAP = C.OPTIONS_VOL_TAGS
EXPRESSION_PERP, _EXPRESSION_SPOT, EXPRESSION_DEFINED_RISK, EXPRESSION_WAIT_FOR_EXPIRY = C.OPTIONS_EXPRESSIONS
VENUE_DERIBIT = 'deribit'


def _price(value: float | Decimal) -> Decimal:
    number = value if isinstance(value, Decimal) else Decimal(repr(float(value)))
    return number.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class BookView:
    """What the check needs of one visible snapshot / metric pair."""

    venue: str
    metric_id: int | None
    snapshot_time: datetime
    total_open_interest: float | None
    near_dated_open_interest: float | None
    expiry_time: datetime | None
    dealer_gamma_sign: int | None
    gamma_flip_price: float | None
    max_pain_strike: float | None
    top_strike_oi_share: float | None
    iv_minus_rv_pct: float | None
    #: Near-dated open interest per strike (calls and puts kept apart); empty if unknown.
    strikes: tuple[StrikeOI, ...] = ()


@dataclass(frozen=True)
class CheckInputs:
    coin: str
    direction: str  # long | short
    decision_time: datetime
    status: str
    status_reason: str | None
    #: Signal-bar close the chandelier distance was measured from: the stand-in for the entry.
    reference_price: Decimal
    one_r: Decimal
    atr_price: Decimal
    pre_options_stop_price: Decimal
    pre_options_target_price: Decimal | None
    #: The signal is in the direction of the 4h break (true for every M4 candidate by construction).
    with_break: bool
    book: BookView | None
    #: Per-coin OI floor in underlying-coin units; defaults to ``OPTIONS_OI_FLOOR_BY_COIN`` (empty today).
    oi_floor: float | None = None
    use_configured_floor: bool = True


@dataclass(frozen=True)
class OptionsDecision:
    verdict: str
    venue: str | None
    vol_tag: str | None
    dealer_gamma_sign: int | None
    nearest_path_strike: Decimal | None
    hours_to_expiry: float | None
    expression: str
    size_multiplier: float
    influence: float
    confidence: float | None
    metric_id: int | None
    snapshot_time: datetime | None
    stop_price: Decimal
    target_cap_price: Decimal | None
    status: str
    status_reason: str | None
    ignore_reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def is_pending(status: str, status_reason: str | None) -> bool:
    return status == STATUS_WATCH and status_reason == C.STATUS_REASON_PENDING_OPTIONS_CHECK


def _between(level: float, entry: Decimal, upper: Decimal, sign: int) -> bool:
    """``entry < level <= upper`` for a long, ``upper <= level < entry`` for a short."""
    value = Decimal(repr(float(level)))
    return entry < value <= upper if sign > 0 else upper <= value < entry


def _floor(inputs: CheckInputs) -> float | None:
    if not inputs.use_configured_floor:
        return inputs.oi_floor
    configured = C.OPTIONS_OI_FLOOR_BY_COIN.get(inputs.coin)
    return inputs.oi_floor if inputs.oi_floor is not None else (None if configured is None else float(configured))


def _confidence(total_oi: float | None, floor: float | None) -> float | None:
    if total_oi is None or floor is None or floor <= 0.0:
        return None
    return max(0.0, min(1.0, total_oi / (C.OPTIONS_CONFIDENCE_FULL_AT_FLOOR_MULTIPLE * floor)))


def _ignore_reason(inputs: CheckInputs, floor: float | None) -> str | None:
    book = inputs.book
    if inputs.coin not in C.OPTIONS_CURRENCY_BY_COIN:
        return C.OPTIONS_IGNORE_NO_BOOK_FOR_COIN
    if book is None:
        return C.OPTIONS_IGNORE_NO_SNAPSHOT
    if inputs.decision_time - book.snapshot_time > timedelta(minutes=C.OPTIONS_SNAPSHOT_MAX_AGE_MINUTES):
        return C.OPTIONS_IGNORE_SNAPSHOT_TOO_OLD
    if book.total_open_interest is None:
        return C.OPTIONS_IGNORE_METRIC_INCOMPLETE
    if floor is None:
        return C.OPTIONS_IGNORE_OI_FLOOR_UNSET
    if book.total_open_interest < floor:
        return C.OPTIONS_IGNORE_OI_BELOW_FLOOR
    return None


def _vol_tag(percentile: float | None) -> str | None:
    if percentile is None:
        return None
    if percentile >= C.VOL_RICH_QUANTILE:
        return VOL_RICH
    if percentile <= C.VOL_CHEAP_QUANTILE:
        return VOL_CHEAP
    return None


def check_options(inputs: CheckInputs) -> OptionsDecision:
    """Run the ladder. Never raises on missing data: a missing figure means that rung cannot fire."""
    sign = 1 if inputs.direction == 'long' else -1
    entry, one_r, atr = inputs.reference_price, inputs.one_r, inputs.atr_price
    upper = entry + sign * one_r
    pending = is_pending(inputs.status, inputs.status_reason)
    book = inputs.book
    floor = _floor(inputs)
    covered = inputs.coin in C.OPTIONS_CURRENCY_BY_COIN
    detail: dict[str, Any] = {'entry': entry, 'upper': upper, 'covered': covered, 'floor': floor}

    def finish(
        verdict: str,
        *,
        stop: Decimal | None = None,
        cap: Decimal | None = None,
        size: float = C.ALIGN_SIZE_MULTIPLIER,
        tag: str | None = None,
        expression: str = EXPRESSION_PERP,
        pending_status: tuple[str, str | None] = (STATUS_ALERT, None),
        ignore_reason: str | None = None,
    ) -> OptionsDecision:
        status, reason = pending_status if pending else (inputs.status, inputs.status_reason)
        if tag == VOL_CHEAP and expression == EXPRESSION_PERP:
            expression = EXPRESSION_DEFINED_RISK
        usable = book is not None and verdict != VERDICT_IGNORE  # an ignored book's figures are not recorded
        tte = None
        if usable and book is not None and book.expiry_time is not None:
            tte = (book.expiry_time - inputs.decision_time).total_seconds() / 3600.0
            tte = tte if tte > 0.0 else None  # the expiry has already settled since the snapshot
        venue = None if not covered else (book.venue if book is not None else VENUE_DERIBIT)
        return OptionsDecision(
            verdict=verdict,
            venue=venue,
            vol_tag=tag,
            dealer_gamma_sign=book.dealer_gamma_sign if (usable and book is not None) else None,
            nearest_path_strike=_nearest_path_strike(book, inputs, sign, upper) if usable else None,
            hours_to_expiry=tte,
            expression=expression,
            size_multiplier=size,
            influence=float(C.OPTIONS_INFLUENCE_BY_VERDICT[verdict]),
            confidence=_confidence(book.total_open_interest, floor) if book is not None else None,
            metric_id=book.metric_id if book is not None else None,
            snapshot_time=book.snapshot_time if book is not None else None,
            stop_price=stop if stop is not None else inputs.pre_options_stop_price,
            target_cap_price=cap,
            status=status,
            status_reason=reason,
            ignore_reason=ignore_reason,
            detail=detail,
        )

    # 1. ignore -----------------------------------------------------------------------------
    why = _ignore_reason(inputs, floor)
    if why is not None:
        detail['ignore_reason'] = why
        blocks = covered and C.OPTIONS_IGNORE_BLOCKS_ALERT_ON_COVERED_COINS
        status = (STATUS_WATCH if blocks else STATUS_ALERT, C.STATUS_REASON_OPTIONS_IGNORED_PREFIX + why)
        return finish(VERDICT_IGNORE, pending_status=status, ignore_reason=why)

    assert book is not None  # an ignore reason exists whenever the book is missing
    tte_decision = None if book.expiry_time is None else (book.expiry_time - inputs.decision_time).total_seconds() / 3600.0
    tag = _vol_tag(book.iv_minus_rv_pct)

    # 2. expiry_pin -------------------------------------------------------------------------
    if (
        tte_decision is not None
        and 0.0 < tte_decision <= C.EXPIRY_PIN_WINDOW_HOURS
        and book.max_pain_strike is not None
        and _between(book.max_pain_strike, entry, upper, sign)
        and book.top_strike_oi_share is not None
        and book.top_strike_oi_share >= C.EXPIRY_PIN_MIN_STRIKE_OI_SHARE
    ):
        detail.update(max_pain=book.max_pain_strike, top_strike_oi_share=book.top_strike_oi_share)
        return finish(
            VERDICT_EXPIRY_PIN,
            cap=_price(book.max_pain_strike),
            expression=EXPRESSION_WAIT_FOR_EXPIRY,
            pending_status=(STATUS_DELAYED, C.STATUS_REASON_OPTIONS_EXPIRY_PIN),
        )

    # 3. headwind ---------------------------------------------------------------------------
    near_oi = book.near_dated_open_interest
    if near_oi is not None and near_oi > 0.0:
        walls = [s for s in book.strikes if _between(s.strike, entry, upper, sign) and s.total / near_oi >= C.HEADWIND_MIN_STRIKE_OI_SHARE]
        if walls:
            wall = min(walls, key=lambda s: (abs(Decimal(repr(s.strike)) - entry), s.strike))
            cap = _price(wall.strike)
            room_r = abs(cap - entry) / one_r
            detail.update(wall_strike=wall.strike, wall_share=wall.total / near_oi, room_r=room_r)
            if room_r < Decimal(repr(C.HEADWIND_MIN_ROOM_R)):
                return finish(
                    VERDICT_HEADWIND,
                    cap=cap,
                    pending_status=(STATUS_CANCELLED, C.STATUS_REASON_OPTIONS_HEADWIND_ROOM),
                )
            return finish(VERDICT_HEADWIND, cap=cap, tag=tag)

    # 4. amplify ----------------------------------------------------------------------------
    gamma_sign = book.dealer_gamma_sign
    if gamma_sign == -1 and inputs.with_break and book.gamma_flip_price is not None:
        flip_atr = abs(entry - Decimal(repr(book.gamma_flip_price))) / atr
        detail['flip_distance_atr'] = flip_atr
        widened = inputs.pre_options_stop_price - sign * Decimal(repr(C.AMPLIFY_STOP_WIDEN_ATR)) * atr
        if flip_atr <= Decimal(repr(C.AMPLIFY_FLIP_MAX_ATR)) and widened > 0:
            return finish(VERDICT_AMPLIFY, stop=_price(widened), size=C.AMPLIFY_SIZE_MULTIPLIER, tag=tag)

    # 5. dampen -----------------------------------------------------------------------------
    if gamma_sign == 1:
        level = entry + sign * Decimal(repr(C.DAMPEN_TARGET_CAP_R)) * one_r
        model = inputs.pre_options_target_price
        nearer = level if model is None else (min(level, model) if sign > 0 else max(level, model))
        return finish(VERDICT_DAMPEN, cap=_price(nearer), size=C.DAMPEN_SIZE_MULTIPLIER, tag=tag)

    # 6. align ------------------------------------------------------------------------------
    return finish(VERDICT_ALIGN, tag=tag)


def _nearest_path_strike(book: BookView | None, inputs: CheckInputs, sign: int, upper: Decimal) -> Decimal | None:
    """Spec rank 8: the near-dated strike with the largest open interest between the stop and the upper level.

    Recorded on the candidate only; no rule reads it. The spec's expiry weighting (heavier inside
    48h of expiry) is NOT applied (deferred; see the M5 entry).
    """
    if book is None or not book.strikes:
        return None
    stop = inputs.pre_options_stop_price
    low, high = (stop, upper) if sign > 0 else (upper, stop)
    inside = [s for s in book.strikes if low <= Decimal(repr(s.strike)) <= high and s.total > 0.0]
    if not inside:
        return None
    best = max(inside, key=lambda s: (s.total, -abs(Decimal(repr(s.strike)) - inputs.reference_price), -s.strike))
    return _price(best.strike)
