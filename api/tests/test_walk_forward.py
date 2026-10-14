"""The purged walk-forward harness on synthetic observations only, plus a read-only dry run of the CLI on the test schema."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

import pytest

from app.core import constants as C
from app.core.learning import walk_forward as W
from app.core.rules.locked_book import is_locked_book
from app.core.stats.observations import CohortKey, MixedCohortError, Observation
from app.core.stats.summary import bootstrap_interval
from app.worker import walk_forward_job
from stats_helpers import baseline_obs, live_btc_layer, obs

UTC = timezone.utc
ORIGIN = datetime(2026, 9, 28, tzinfo=UTC)  # a Monday, after the thin-lookback cutoff
WEEK = timedelta(days=7)
COHORT = CohortKey('replay', 'no_options_book', 'full')
SWING = C.HORIZON_SWING


def at(hours: float, *, r: float | None = 1.0, resolve_after: float = 2.0, **kw) -> Observation:
    """A sample decided `hours` after ORIGIN, resolved `resolve_after` hours later. Not locked, not in an event window."""
    when = ORIGIN + timedelta(hours=hours)
    values = dict(decision_time=when, bar_close=when, resolved_at=when + timedelta(hours=resolve_after), stored_r=r)
    values.setdefault('locked_book', False)
    values.update(kw)
    return obs(**values)


def folds(observations: Sequence[Observation], **kw) -> W.FoldSet:
    return W.build_folds(observations, COHORT, SWING, **kw)


def fold_at(fold_set: W.FoldSet, test_start: datetime) -> W.Fold:
    (fold,) = [f for f in fold_set.folds if f.test_start == test_start]
    return fold


def ids(rows: Sequence[Observation]) -> list[int]:
    return [o.candidate_id for o in rows]


class TakeAll:
    learns = False

    def fit(self, train: Sequence[Observation]) -> None:
        pass

    def take(self, sample: Observation) -> bool:
        return True


class TakeNone(TakeAll):
    def take(self, sample: Observation) -> bool:
        return False


class Learner(TakeAll):
    learns = True


# ---------------------------------------------------------------------------
# Embargo and grid
# ---------------------------------------------------------------------------
def test_embargo_is_the_longest_vertical_barrier_for_every_horizon():
    assert W.EMBARGO == timedelta(hours=C.LONGEST_HORIZON_HOURS) == timedelta(hours=72)
    for h in C.HORIZONS:
        rows = [at(1, horizon=h)]
        assert W.build_folds(rows, COHORT, h).embargo == W.EMBARGO


def test_grid_is_anchored_on_the_monday_on_or_before_the_first_sample_and_windows_are_half_open():
    first = at(24 * 3 + 5)  # Thursday 05:00
    on_end = at(24 * 7)  # exactly the next Monday 00:00: belongs to the second window
    fs = folds([first, on_end], min_train=0)
    assert fs.grid_origin == ORIGIN
    assert [(f.test_start, ids(f.test)) for f in fs.folds] == [
        (ORIGIN, [first.candidate_id]),
        (ORIGIN + WEEK, [on_end.candidate_id]),
    ]


def test_empty_windows_are_skipped_and_small_training_sets_are_counted():
    rows = [at(1), at(24 * 21 + 1)]  # weeks 0 and 3; weeks 1 and 2 are empty
    fs = folds(rows, min_train=1)
    assert [f.test_start for f in fs.folds] == [ORIGIN + 3 * WEEK]
    assert fs.windows_below_min_train == 1  # week 0 has no training sample
    assert folds([]).folds == () and folds([]).grid_origin is None


# ---------------------------------------------------------------------------
# Purge and embargo boundaries
# ---------------------------------------------------------------------------
def test_purge_and_embargo_boundaries():
    ts = ORIGIN + WEEK
    h = (ts - ORIGIN).total_seconds() / 3600
    on_embargo = at(h - 72, resolve_after=71)  # decision + embargo == test_start: trains
    inside_embargo = at(h - 72 + 1 / 60, resolve_after=1)  # one minute later: embargoed
    resolved_after = at(h - 100, resolve_after=100 + 1 / 60)  # label known one minute after test_start: purged
    resolved_on = at(h - 99, resolve_after=99)  # resolved exactly at test_start: trains
    test = at(h + 1)
    fs = folds([on_embargo, inside_embargo, resolved_after, resolved_on, test], min_train=1)
    fold = fold_at(fs, ts)
    assert set(ids(fold.train)) == {on_embargo.candidate_id, resolved_on.candidate_id}
    assert (fold.purged, fold.embargoed) == (1, 1)
    assert ids(fold.test) == [test.candidate_id]
    assert fold.train_range == (resolved_on.decision_time, on_embargo.decision_time)


def test_a_purged_sample_is_counted_once_even_when_it_is_also_inside_the_embargo():
    ts = ORIGIN + WEEK
    h = 24 * 7
    both = at(h - 1, resolve_after=5)
    fold = fold_at(folds([at(1), both, at(h + 1)], min_train=1), ts)
    assert (fold.purged, fold.embargoed, fold.n_train) == (1, 0, 1)


def test_training_expands_and_a_rolling_window_counts_what_it_leaves_out():
    rows = [at(1), at(24 * 7 + 1), at(24 * 14 + 1)]
    expanding = folds(rows, min_train=1)
    assert [f.n_train for f in expanding.folds] == [1, 2]
    rolling = folds(rows, min_train=1, max_train_days=7)
    last = fold_at(rolling, ORIGIN + 2 * WEEK)
    assert (last.n_train, last.outside_train_window) == (1, 1)


# ---------------------------------------------------------------------------
# Locked book and event window
# ---------------------------------------------------------------------------
def test_locked_book_and_event_window_rows_never_train_but_are_tested_and_marked():
    plain = at(1)
    locked = at(2, locked_book=True)
    event = at(3, event_flag=True)
    unknown_calendar = at(4, event_flag=None)
    test_plain = at(24 * 7 + 1)
    test_locked = at(24 * 7 + 2, locked_book=True)
    test_event = at(24 * 7 + 3, event_flag=True)
    fs = folds([plain, locked, event, unknown_calendar, test_plain, test_locked, test_event], min_train=1)
    fold = fold_at(fs, ORIGIN + WEEK)
    assert ids(fold.train) == [plain.candidate_id, unknown_calendar.candidate_id]
    assert (fold.excluded_locked_book, fold.excluded_event_window) == (1, 1)
    assert ids(fold.test) == [test_plain.candidate_id, test_locked.candidate_id, test_event.candidate_id]
    assert fold.test_locked_book == {test_locked.candidate_id}
    assert fold.test_event_window == {test_event.candidate_id}
    assert (fs.n_samples, fs.n_locked_book, fs.n_event_window, fs.n_trainable) == (7, 2, 2, 3)


def test_unknown_locked_book_falls_back_to_the_rules_job_hash():
    hours = range(500)
    locked_h = next(h for h in hours if is_locked_book('SOLUSDT', ORIGIN + timedelta(hours=h)))
    free_h = next(h for h in hours if not is_locked_book('SOLUSDT', ORIGIN + timedelta(hours=h)))
    assert W.in_locked_book(at(locked_h, locked_book=None))
    assert not W.in_locked_book(at(free_h, locked_book=None))
    assert not W.in_locked_book(at(locked_h, locked_book=False))  # a stored value wins


# ---------------------------------------------------------------------------
# Independence, no_entry, one decision
# ---------------------------------------------------------------------------
def test_only_the_first_row_of_each_sample_group_is_a_sample():
    group = str(uuid.uuid4())
    first = at(1, sample_group_id=group)
    follower = at(2, sample_group_id=group)
    fs = folds([follower, first, at(24 * 7 + 1)], min_train=1)
    assert (fs.n_raw, fs.n_independent, fs.n_samples) == (3, 2, 2)
    assert ids(fs.folds[0].train) == [first.candidate_id]


def test_no_entry_is_dropped_after_the_group_representative_is_chosen():
    group = str(uuid.uuid4())
    no_entry_first = at(1, sample_group_id=group, first_barrier='no_entry', r=None)
    filled_follower = at(2, sample_group_id=group)
    lone_no_entry = at(24 * 7 + 2, first_barrier='no_entry', r=None)
    kept = at(3)
    test = at(24 * 7 + 1)
    fs = folds([no_entry_first, filled_follower, lone_no_entry, kept, test], min_train=1)
    assert (fs.n_independent, fs.n_no_entry, fs.n_samples) == (4, 2, 2)
    (fold,) = fs.folds
    assert ids(fold.train) == [kept.candidate_id]
    assert ids(fold.test) == [test.candidate_id]


def test_two_samples_for_one_coin_horizon_and_bar_are_refused():
    with pytest.raises(W.DuplicateDecision):
        folds([at(1), at(1)])


def test_mixed_cohort_is_refused():
    other = live_btc_layer(decision_time=ORIGIN, bar_close=ORIGIN, resolved_at=ORIGIN + timedelta(hours=1))
    with pytest.raises(MixedCohortError):
        folds([at(1), other])
    assert W.MixedCohortError is MixedCohortError


def test_mixed_horizon_is_refused():
    with pytest.raises(W.MixedHorizonError):
        folds([at(1), at(2, horizon=C.HORIZON_SCALP)])
    with pytest.raises(ValueError):
        W.build_folds([], COHORT, 'weekly')


# ---------------------------------------------------------------------------
# Label floor
# ---------------------------------------------------------------------------
def test_floor_guard_at_299_and_300():
    assert C.LIGHTGBM_MIN_INDEPENDENT_LABELS == 300
    with pytest.raises(W.BelowLabelFloor):
        W.assert_fit_allowed(299)
    W.assert_fit_allowed(300)
    assert not W.fit_allowed(299) and W.fit_allowed(300)


def test_fold_sets_report_the_floor_on_trainable_samples_and_each_fold_on_its_training_set():
    week0 = [at(i * 0.25) for i in range(300)]  # 75 hours of quarter-hour bars
    test = at(24 * 7 + 1)
    below = folds([*week0[:298], test])
    assert (below.n_trainable, below.fit_allowed) == (299, False)
    on = folds([*week0[:299], test])
    assert (on.n_trainable, on.fit_allowed) == (300, True)
    assert (on.folds[0].n_train, on.folds[0].fit_allowed) == (299, False)
    (fold,) = folds([*week0, test]).folds
    assert (fold.n_train, fold.fit_allowed) == (300, True)
    locked_one = folds([*week0[:299], at(299 * 0.25, locked_book=True), test])
    assert (locked_one.n_trainable, locked_one.fit_allowed) == (300, True)
    assert (locked_one.folds[0].n_train, locked_one.folds[0].fit_allowed) == (299, False)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
NAIVE = [
    (ORIGIN + timedelta(days=1), 10.0),  # week 0: no fold there
    (ORIGIN + WEEK + timedelta(hours=1), -0.5),
    (ORIGIN + WEEK + timedelta(days=1), 0.1),
    (ORIGIN + 2 * WEEK, 0.3),  # the start of week 2 (inclusive), the end of week 1 (exclusive)
    (ORIGIN + 3 * WEEK, 10.0),  # after the last window
]


def eval_rows() -> list[Observation]:
    week1 = 24 * 7
    week2 = 24 * 14
    return [
        at(1, r=1.0),  # week 0: trains, its own window has no training set
        at(week1 + 1, r=2.0),
        at(week1 + 2, r=-1.0),
        at(week1 + 3, r=0.5),
        at(week2 + 1, r=-2.0),
    ]


def test_take_all_against_flat_and_the_naive_rule_in_the_same_windows():
    fs = folds(eval_rows(), min_train=1)
    result = W.evaluate(fs, TakeAll, NAIVE)
    w1, w2 = result.folds
    assert (w1.test_start, w1.n_train, w2.test_start, w2.n_train) == (ORIGIN + WEEK, 1, ORIGIN + 2 * WEEK, 4)

    c = w1.comparison
    assert (c.n_test, c.n_taken, c.total_r, c.flat_r) == (3, 3, 2.5 - 1.0, 0.0)
    assert c.mean_r == pytest.approx(0.5) and c.vs_flat == pytest.approx(0.5) and c.r_per_decision == pytest.approx(0.5)
    assert (c.interval_low, c.interval_high) == bootstrap_interval([2.0, -1.0, 0.5])
    assert c.naive_n == 2 and c.naive_mean_r == pytest.approx(-0.2)
    assert (c.naive_interval_low, c.naive_interval_high) == bootstrap_interval([-0.5, 0.1])
    assert c.vs_naive == pytest.approx(0.7)
    assert c.interval_above_flat == (c.interval_low > 0)

    c = w2.comparison
    assert (c.n_taken, c.mean_r, c.naive_n, c.naive_mean_r) == (1, -2.0, 1, 0.3)
    assert c.interval_low is None and c.naive_interval_low is None  # one value has no spread
    assert c.vs_naive == pytest.approx(-2.3) and c.interval_above_naive is None

    o = result.overall
    assert (o.n_test, o.n_taken, o.total_r) == (4, 4, pytest.approx(-0.5))
    assert o.mean_r == pytest.approx(-0.125)
    assert (o.interval_low, o.interval_high) == bootstrap_interval([2.0, -1.0, 0.5, -2.0])
    assert o.naive_n == 3 and o.naive_mean_r == pytest.approx(-0.1 / 3)
    assert o.vs_naive == pytest.approx(-0.125 + 0.1 / 3)


def test_take_none_is_always_flat():
    result = W.evaluate(folds(eval_rows(), min_train=1), TakeNone, NAIVE)
    for c in [f.comparison for f in result.folds] + [result.overall]:
        assert (c.n_taken, c.total_r, c.mean_r, c.r_per_decision, c.vs_flat, c.vs_naive) == (0, 0.0, None, 0.0, None, None)
        assert c.interval_low is None and c.interval_above_flat is None
    assert result.overall.n_test == 4 and result.overall.naive_n == 3


def test_locked_book_only_scores_the_locked_test_rows():
    rows = eval_rows()
    rows[2] = at(24 * 7 + 2, r=-1.0, locked_book=True)
    result = W.evaluate(folds(rows, min_train=1), TakeAll, NAIVE, locked_book_only=True)
    assert [f.comparison.n_test for f in result.folds] == [1, 0]
    assert (result.overall.n_taken, result.overall.n_taken_locked_book, result.overall.mean_r) == (1, 1, -1.0)
    assert result.locked_book_only


def test_a_learning_policy_is_refused_below_the_floor():
    fs = folds(eval_rows(), min_train=1)
    with pytest.raises(W.BelowLabelFloor):
        W.evaluate(fs, Learner, NAIVE)


def test_each_fold_gets_a_fresh_policy_fitted_on_its_training_set():
    seen: list[list[int]] = []

    class Recorder(TakeAll):
        def fit(self, train: Sequence[Observation]) -> None:
            seen.append(ids(train))

    rows = eval_rows()
    W.evaluate(folds(rows, min_train=1), Recorder, [])
    assert seen == [[rows[0].candidate_id], ids(rows[:4])]


# ---------------------------------------------------------------------------
# Naive outcomes
# ---------------------------------------------------------------------------
def test_naive_outcomes_are_the_independent_filled_trades_of_one_horizon():
    group = str(uuid.uuid4())
    t = ORIGIN + timedelta(hours=1)
    first = baseline_obs(decision_time=t, bar_close=t, resolved_at=t, sample_group_id=group, stored_r=-1.0)
    later = baseline_obs(decision_time=t + timedelta(hours=1), bar_close=t, resolved_at=t, sample_group_id=group, stored_r=5.0)
    skipped = baseline_obs(decision_time=t + timedelta(hours=2), bar_close=t, resolved_at=t, first_barrier='no_entry', stored_r=None)
    assert W.naive_outcomes([later, skipped, first], SWING) == [(t, -1.0)]
    with pytest.raises(W.MixedHorizonError):
        W.naive_outcomes([first], C.HORIZON_SCALP)


# ---------------------------------------------------------------------------
# CLI dry run on the throwaway schema (read-only loader)
# ---------------------------------------------------------------------------
def test_dry_run_builds_one_fold_set_per_cohort_and_horizon(session):
    import test_stats_job as sj

    for minutes, locked in ((0, False), (15, True), (30, False)):
        sj.add_label(session, sj.cand(session, minutes, locked_book=locked))
    sj.add_label(session, sj.cand(session, 0, horizon=C.HORIZON_SCALP))
    fold_sets = walk_forward_job.run_walk_forward(session)
    assert [(fs.cohort.label, fs.horizon) for fs in fold_sets] == [
        ('live/options_layer/thin', C.HORIZON_SCALP),
        ('live/options_layer/thin', SWING),
    ]
    swing = fold_sets[1]
    assert (swing.n_samples, swing.n_locked_book, swing.n_trainable, swing.fit_allowed) == (3, 1, 2, False)
    assert swing.folds == () and swing.windows_below_min_train == 1
    assert [fs.horizon for fs in walk_forward_job.run_walk_forward(session, horizon=SWING)] == [SWING]
    text = walk_forward_job.format_text(fold_sets)
    assert 'fit allowed: no (n=2/300)' in text
    assert walk_forward_job.fold_set_to_dict(swing)['embargo_hours'] == 72


def test_dry_run_reads_through_the_read_only_session(db_engine, db_schema):
    from app.worker.stats_job import readonly_session

    with readonly_session(db_engine, search_path=db_schema) as ro:
        assert walk_forward_job.run_walk_forward(ro) == []
    assert 'no labeled observations' in walk_forward_job.format_text([])
