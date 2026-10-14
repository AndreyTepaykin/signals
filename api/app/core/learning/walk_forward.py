"""Purged walk-forward folds and a model-agnostic evaluation scaffold. Pure: no I/O, no clock. Nothing here fits a model.

Spec ("Learning pipeline"): LightGBM only after 300 independent, non-overlapping labels, inside purged walk-forward with an
embargo equal to the longest horizon, weekly, one decision per coin per horizon, compared on the same barriers with always
flat and the naive 4h swing-break rule. The locked book never updates weights; fast weights move only on outcomes outside an
event window.

Scope of one fold set
---------------------
ONE cohort ``(source, options_path, lookback)`` and ONE horizon. `build_folds` takes both as mandatory arguments and refuses a
foreign row (`MixedCohortError`, `MixedHorizonError`), as `summarize` does.

Samples
-------
The independent sample of `app.core.stats.summary.collapse_independent` (first row of each ``sample_group_id``), selected
BEFORE anything is dropped, then ``no_entry`` labels are removed (no trade, no R). Within one cohort and horizon there is one
decision per ``(coin, bar_close)``; a duplicate raises `DuplicateDecision`.

Folds
-----
Test windows are half-open ``[test_start, test_end)`` of ``params.TEST_WINDOW_DAYS`` on a calendar grid anchored at 00:00 UTC
on ``params.ANCHOR_WEEKDAY`` on or before the first sample. A sample is tested in the window that holds its
``decision_time``. Every sample decided before ``test_start`` lands in exactly one bucket, checked in this order:

1. ``outside_train_window``: older than ``max_train_days`` (rolling window only; expanding by default).
2. ``excluded_locked_book``: the locked book never trains. The stored ``locked_book`` is used; when the loader left it
   ``None``, the same deterministic hash the rules job writes (`is_locked_book(coin, bar_close)`) decides.
3. ``excluded_event_window``: ``event_flag is True``. ``None`` (calendar not loaded) is not an event window, as in the
   stand-down rule (`STAND_DOWN_ON_UNKNOWN_EVENT_CALENDAR = False`).
4. ``purged``: ``resolved_at > test_start``, the label was not known when the test began.
5. ``embargoed``: ``decision_time + embargo > test_start``. Equality trains (both point-in-time conditions are ``<=``).
6. otherwise: train.

The embargo is the longest vertical barrier in `constants.VERTICAL_BARRIER_HOURS`, for every horizon (the slow-path rule).
A window with no test sample is skipped silently; one whose training set is below ``min_train`` is skipped and counted.

Test windows keep locked-book and event-window rows and mark them (`Fold.test_locked_book`, `Fold.test_event_window`): the
locked book is exactly what a later promotion check scores on, and an out-of-sample score should face every decision the
live book faces. `evaluate(..., locked_book_only=True)` scores the locked book alone.

Label floor
-----------
Folds are built at any size (dry runs). `FoldSet.fit_allowed` compares the trainable count (independent, filled, not locked
book, not event window: the labels that could ever enter a training set) with `constants.LIGHTGBM_MIN_INDEPENDENT_LABELS`;
each `Fold.fit_allowed` compares its own training set. `evaluate` calls `assert_fit_allowed` on every fold before
`Policy.fit` unless the policy declares ``learns = False``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from app.core import constants as C
from app.core.learning import params as P
from app.core.rules.locked_book import is_locked_book
from app.core.stats.observations import CohortKey, MixedCohortError, Observation
from app.core.stats.summary import bootstrap_interval, collapse_independent, mean_r, require_single_cohort

EMBARGO = timedelta(hours=max(C.VERTICAL_BARRIER_HOURS.values()))

__all__ = [
    'EMBARGO',
    'BelowLabelFloor',
    'Comparison',
    'DuplicateDecision',
    'Evaluation',
    'Fold',
    'FoldResult',
    'FoldSet',
    'MixedCohortError',
    'MixedHorizonError',
    'Policy',
    'assert_fit_allowed',
    'build_folds',
    'evaluate',
    'fit_allowed',
    'in_event_window',
    'in_locked_book',
    'naive_outcomes',
]


class MixedHorizonError(ValueError):
    """Raised when one fold set is asked to hold observations of more than one horizon."""


class DuplicateDecision(ValueError):
    """Raised when two independent samples share ``(coin, horizon, bar_close)``: one decision per coin per horizon."""


class BelowLabelFloor(ValueError):
    """Raised when a fit is requested on fewer independent labels than the LightGBM floor."""


def fit_allowed(n_independent: int) -> bool:
    return n_independent >= C.LIGHTGBM_MIN_INDEPENDENT_LABELS


def assert_fit_allowed(n_independent: int) -> None:
    if not fit_allowed(n_independent):
        raise BelowLabelFloor(
            f'n={n_independent} independent labels, a fit needs at least {C.LIGHTGBM_MIN_INDEPENDENT_LABELS}'
        )


def in_locked_book(observation: Observation) -> bool:
    if observation.locked_book is not None:
        return observation.locked_book
    return is_locked_book(observation.coin, observation.bar_close)


def in_event_window(observation: Observation) -> bool:
    return observation.event_flag is True


# ---------------------------------------------------------------------------
# Folds
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Fold:
    index: int
    test_start: datetime
    test_end: datetime
    train: tuple[Observation, ...]
    test: tuple[Observation, ...]  # every test sample, locked book and event window included
    test_locked_book: frozenset[int]  # candidate ids
    test_event_window: frozenset[int]  # candidate ids
    outside_train_window: int
    excluded_locked_book: int
    excluded_event_window: int
    purged: int
    embargoed: int
    fit_allowed: bool

    @property
    def n_train(self) -> int:
        return len(self.train)

    @property
    def n_test(self) -> int:
        return len(self.test)

    @property
    def train_range(self) -> tuple[datetime, datetime] | None:
        return (self.train[0].decision_time, self.train[-1].decision_time) if self.train else None

    @property
    def test_range(self) -> tuple[datetime, datetime] | None:
        return (self.test[0].decision_time, self.test[-1].decision_time) if self.test else None


@dataclass(frozen=True)
class FoldSet:
    cohort: CohortKey
    horizon: str
    embargo: timedelta
    test_window: timedelta
    min_train: int
    max_train_days: int | None
    n_raw: int
    n_independent: int
    n_no_entry: int  # independent samples whose label is no_entry (dropped)
    n_samples: int  # independent filled samples: the ones folds are built from
    n_locked_book: int
    n_event_window: int
    n_trainable: int
    fit_allowed: bool
    grid_origin: datetime | None
    folds: tuple[Fold, ...]
    windows_below_min_train: int

    @property
    def floor(self) -> int:
        return C.LIGHTGBM_MIN_INDEPENDENT_LABELS


def build_folds(
    observations: Sequence[Observation],
    cohort: CohortKey,
    horizon: str,
    *,
    test_window_days: int = P.TEST_WINDOW_DAYS,
    min_train: int = P.MIN_TRAIN_SAMPLES,
    max_train_days: int | None = P.MAX_TRAIN_DAYS,
    anchor_weekday: int = P.ANCHOR_WEEKDAY,
) -> FoldSet:
    """Purged, embargoed, expanding walk-forward folds for one cohort and one horizon. An empty list gives no folds."""
    if test_window_days <= 0:
        raise ValueError('test_window_days must be positive')
    if min_train < 0:
        raise ValueError('min_train must be non-negative')
    if horizon not in C.HORIZONS:
        raise ValueError(f'unknown horizon {horizon!r}')
    require_single_cohort(observations, cohort)
    for observation in observations:
        if observation.horizon != horizon:
            raise MixedHorizonError(
                f'candidate {observation.candidate_id} is horizon {observation.horizon}, asked to fold it into {horizon}'
            )

    independent = collapse_independent(observations)
    _require_one_decision(independent)
    samples = [o for o in independent if not o.is_no_entry]
    locked = [o for o in samples if in_locked_book(o)]
    event = [o for o in samples if in_event_window(o)]
    trainable = [o for o in samples if not in_locked_book(o) and not in_event_window(o)]

    window = timedelta(days=test_window_days)
    rolling = None if max_train_days is None else timedelta(days=max_train_days)
    origin = _grid_origin(samples[0].decision_time, anchor_weekday) if samples else None
    folds: list[Fold] = []
    below_min = 0
    if origin is not None:
        last = samples[-1].decision_time
        start = origin
        while start <= last:
            end = start + window
            test = tuple(o for o in samples if start <= o.decision_time < end)
            if test:
                fold = _fold(len(folds), start, end, samples, test, rolling)
                if fold.n_train < min_train:
                    below_min += 1
                else:
                    folds.append(fold)
            start = end

    return FoldSet(
        cohort=cohort,
        horizon=horizon,
        embargo=EMBARGO,
        test_window=window,
        min_train=min_train,
        max_train_days=max_train_days,
        n_raw=len(observations),
        n_independent=len(independent),
        n_no_entry=len(independent) - len(samples),
        n_samples=len(samples),
        n_locked_book=len(locked),
        n_event_window=len(event),
        n_trainable=len(trainable),
        fit_allowed=fit_allowed(len(trainable)),
        grid_origin=origin,
        folds=tuple(folds),
        windows_below_min_train=below_min,
    )


def _require_one_decision(independent: Sequence[Observation]) -> None:
    seen: dict[tuple[str, str, datetime], int] = {}
    for o in independent:
        key = (o.coin, o.horizon, o.bar_close)
        if key in seen:
            raise DuplicateDecision(f'candidates {seen[key]} and {o.candidate_id} are both a decision for {key}')
        seen[key] = o.candidate_id


def _grid_origin(first: datetime, anchor_weekday: int) -> datetime:
    midnight = first.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=(midnight.weekday() - anchor_weekday) % 7)


def _fold(
    index: int,
    start: datetime,
    end: datetime,
    samples: Sequence[Observation],
    test: tuple[Observation, ...],
    rolling: timedelta | None,
) -> Fold:
    counts = {'outside': 0, 'locked': 0, 'event': 0, 'purged': 0, 'embargoed': 0}
    train: list[Observation] = []
    for o in samples:
        if o.decision_time >= start:
            break
        if rolling is not None and o.decision_time < start - rolling:
            counts['outside'] += 1
        elif in_locked_book(o):
            counts['locked'] += 1
        elif in_event_window(o):
            counts['event'] += 1
        elif o.resolved_at > start:
            counts['purged'] += 1
        elif o.decision_time + EMBARGO > start:
            counts['embargoed'] += 1
        else:
            train.append(o)
    return Fold(
        index=index,
        test_start=start,
        test_end=end,
        train=tuple(train),
        test=test,
        test_locked_book=frozenset(o.candidate_id for o in test if in_locked_book(o)),
        test_event_window=frozenset(o.candidate_id for o in test if in_event_window(o)),
        outside_train_window=counts['outside'],
        excluded_locked_book=counts['locked'],
        excluded_event_window=counts['event'],
        purged=counts['purged'],
        embargoed=counts['embargoed'],
        fit_allowed=fit_allowed(len(train)),
    )


# ---------------------------------------------------------------------------
# Naive baseline outcomes
# ---------------------------------------------------------------------------
def naive_outcomes(baseline: Sequence[Observation], horizon: str) -> list[tuple[datetime, float]]:
    """``(decision_time, stored_r)`` of the independent filled naive trades of ONE baseline cohort and ONE horizon.

    Feed it ``BaselineData.for_key(key, horizon)`` for the key matching the app cohort (`coin_set_for_path`)."""
    if baseline:
        require_single_cohort(baseline, baseline[0].cohort)
    for o in baseline:
        if o.horizon != horizon:
            raise MixedHorizonError(f'naive trade {o.candidate_id} is horizon {o.horizon}, asked for {horizon}')
    return [(o.decision_time, _stored_r(o)) for o in collapse_independent(baseline) if not o.is_no_entry]


def _stored_r(observation: Observation) -> float:
    if observation.stored_r is None:
        raise ValueError(f'filled candidate {observation.candidate_id} has no stored R')
    return observation.stored_r


# ---------------------------------------------------------------------------
# Evaluation scaffold
# ---------------------------------------------------------------------------
class Policy(Protocol):
    """A decision rule under test. ``learns = False`` declares that `fit` reads nothing, so the label floor does not apply."""

    learns: bool

    def fit(self, train: Sequence[Observation]) -> None: ...

    def take(self, sample: Observation) -> bool: ...


@dataclass(frozen=True)
class Comparison:
    """Taken test samples against always flat (0 R per decision) and the naive rule over the same window(s)."""

    n_test: int
    n_taken: int
    n_taken_locked_book: int
    total_r: float
    mean_r: float | None  # mean stored R of the taken samples
    interval_low: float | None
    interval_high: float | None
    r_per_decision: float | None  # total_r / n_test: an untaken sample counts as flat
    flat_r: float
    vs_flat: float | None  # mean_r - flat_r
    interval_above_flat: bool | None
    naive_n: int
    naive_mean_r: float | None
    naive_interval_low: float | None
    naive_interval_high: float | None
    vs_naive: float | None  # mean_r - naive_mean_r
    interval_above_naive: bool | None  # the taken interval's lower bound above the naive mean


@dataclass(frozen=True)
class FoldResult:
    index: int
    test_start: datetime
    test_end: datetime
    n_train: int
    comparison: Comparison


@dataclass(frozen=True)
class Evaluation:
    folds: tuple[FoldResult, ...]
    overall: Comparison  # every taken test sample of every fold; naive over the union of the test windows
    locked_book_only: bool


FLAT_R = 0.0


def evaluate(
    folds: Sequence[Fold] | FoldSet,
    policy_factory: Callable[[], Policy],
    naive_outcomes: Sequence[tuple[datetime, float]],
    *,
    locked_book_only: bool = False,
) -> Evaluation:
    """Fit a fresh policy on each fold's training set and score it on that fold's test window."""
    fold_list = folds.folds if isinstance(folds, FoldSet) else tuple(folds)
    results: list[FoldResult] = []
    all_test: list[Observation] = []
    all_taken: list[Observation] = []
    all_naive: list[float] = []
    for fold in fold_list:
        policy = policy_factory()
        if getattr(policy, 'learns', True):
            assert_fit_allowed(fold.n_train)
        policy.fit(fold.train)
        test = [o for o in fold.test if o.candidate_id in fold.test_locked_book] if locked_book_only else list(fold.test)
        taken = [o for o in test if policy.take(o)]
        naive = [r for t, r in naive_outcomes if fold.test_start <= t < fold.test_end]
        results.append(FoldResult(fold.index, fold.test_start, fold.test_end, fold.n_train, _compare(test, taken, naive)))
        all_test.extend(test)
        all_taken.extend(taken)
        all_naive.extend(naive)
    return Evaluation(tuple(results), _compare(all_test, all_taken, all_naive), locked_book_only)


def _compare(test: Sequence[Observation], taken: Sequence[Observation], naive: Sequence[float]) -> Comparison:
    values = [_stored_r(o) for o in taken]
    total = float(sum(values))
    mean = mean_r(values)
    interval = bootstrap_interval(values)
    naive_mean = mean_r(naive)
    naive_interval = bootstrap_interval(naive)
    low = None if interval is None else interval[0]
    return Comparison(
        n_test=len(test),
        n_taken=len(taken),
        n_taken_locked_book=sum(1 for o in taken if in_locked_book(o)),
        total_r=total,
        mean_r=mean,
        interval_low=low,
        interval_high=None if interval is None else interval[1],
        r_per_decision=total / len(test) if test else None,
        flat_r=FLAT_R,
        vs_flat=None if mean is None else mean - FLAT_R,
        interval_above_flat=None if low is None else low > FLAT_R,
        naive_n=len(naive),
        naive_mean_r=naive_mean,
        naive_interval_low=None if naive_interval is None else naive_interval[0],
        naive_interval_high=None if naive_interval is None else naive_interval[1],
        vs_naive=None if mean is None or naive_mean is None else mean - naive_mean,
        interval_above_naive=None if low is None or naive_mean is None else low > naive_mean,
    )
