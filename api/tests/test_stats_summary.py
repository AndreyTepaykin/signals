"""M8 stats math, independent-sample counting, the too-small flag, and the M6 owner requirements."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.core.stats import params as P
from app.core.stats import summary as S
from app.core.stats.observations import CohortKey, MixedCohortError
from stats_helpers import T0, btc_no_history, live_btc_layer, no_entry, obs

COHORT = CohortKey('replay', 'no_options_book', 'full')


# ---------------------------------------------------------------------------
# Hand-worked math
# ---------------------------------------------------------------------------
def test_mean_profit_factor_win_rate_and_drawdown_on_a_worked_series():
    r = [1.0, -1.0, 2.0, -0.5, -1.5, 0.5]
    assert S.mean_r(r) == pytest.approx(0.0833333, abs=1e-6)
    pf, note = S.profit_factor(r)
    assert pf == pytest.approx(3.5 / 3.0) and note is None
    assert S.win_rate(r) == pytest.approx(3 / 6)
    # cumulative: 1, 0, 2, 1.5, 0, 0.5 ; peak 2 -> trough 0 is a 2R drawdown
    assert S.max_drawdown(r) == pytest.approx(2.0)


def test_drawdown_starts_from_zero_so_an_opening_loss_counts():
    assert S.max_drawdown([-1.0, -0.5, 0.25]) == pytest.approx(1.5)
    assert S.max_drawdown([]) == 0.0


def test_profit_factor_is_undefined_not_infinite_without_a_loss():
    assert S.profit_factor([1.0, 2.0]) == (None, 'no losing outcome: undefined')
    assert S.profit_factor([]) == (None, 'no winning or losing outcome')


def test_a_zero_stored_r_is_a_miss_for_win_rate():
    assert S.win_rate([0.0, 0.0, 1.0]) == pytest.approx(1 / 3)


def test_brier_score_worked():
    assert S.brier_score([(0.8, True), (0.8, False), (0.2, False)]) == pytest.approx((0.04 + 0.64 + 0.04) / 3)
    assert S.brier_score([]) is None


def test_bootstrap_interval_is_deterministic_brackets_the_mean_and_is_none_below_two_values():
    values = [1.0, -1.0, 0.5, -0.5, 2.0, -1.0, 0.25, 0.75]
    a, b = S.bootstrap_interval(values), S.bootstrap_interval(values)
    assert a == b
    assert a[0] < S.mean_r(values) < a[1]
    assert S.bootstrap_interval([0.3]) is None
    assert S.bootstrap_interval([]) is None
    assert S.bootstrap_interval([0.4] * 10) == (pytest.approx(0.4), pytest.approx(0.4))


# ---------------------------------------------------------------------------
# Independent sample: the first row of each group
# ---------------------------------------------------------------------------
def test_collapse_keeps_the_earliest_decision_per_group_whatever_the_input_order_or_outcome():
    group = str(uuid.uuid4())
    late_winner = obs(sample_group_id=group, decision_time=T0 + timedelta(hours=2), stored_r=3.0)
    first_loser = obs(sample_group_id=group, decision_time=T0 + timedelta(hours=1), stored_r=-1.0, first_barrier='lower')
    other = obs()
    for ordering in ([late_winner, first_loser, other], [other, first_loser, late_winner]):
        kept = S.collapse_independent(ordering)
        assert {o.candidate_id for o in kept} == {first_loser.candidate_id, other.candidate_id}


def test_ties_on_decision_time_break_by_candidate_id():
    group = str(uuid.uuid4())
    a = obs(sample_group_id=group, decision_time=T0)
    b = obs(sample_group_id=group, decision_time=T0)
    assert [o.candidate_id for o in S.collapse_independent([b, a])] == [min(a.candidate_id, b.candidate_id)]


def test_raw_and_independent_counts_sit_side_by_side_and_statistics_use_the_first_row_only():
    group = str(uuid.uuid4())
    rows = [
        obs(sample_group_id=group, decision_time=T0, stored_r=-1.0, first_barrier='lower'),
        obs(sample_group_id=group, decision_time=T0 + timedelta(hours=1), stored_r=1.0),
        obs(sample_group_id=group, decision_time=T0 + timedelta(hours=2), stored_r=1.0),
        obs(stored_r=0.5),
    ]
    s = S.summarize(rows, COHORT)
    assert (s.n_labels_raw, s.n_labels_independent) == (4, 2)
    assert (s.n_filled_raw, s.n_filled_independent) == (4, 2)
    # first-of-group, not the group mean: (-1.0 + 0.5) / 2, where the mean of the group would give (1/3 + 0.5) / 2
    assert s.expectancy_r == pytest.approx(-0.25)


def test_overlapping_rows_are_one_independent_sample_for_the_sample_floor():
    group = str(uuid.uuid4())
    rows = [obs(sample_group_id=group, decision_time=T0 + timedelta(minutes=15 * i)) for i in range(40)]
    s = S.summarize(rows, COHORT)
    assert s.n_filled_raw == 40 and s.n_filled_independent == 1
    assert s.insufficient_sample


# ---------------------------------------------------------------------------
# Too small to conclude
# ---------------------------------------------------------------------------
def test_twelve_independent_samples_are_flagged_in_plain_words():
    s = S.summarize([obs() for _ in range(12)], COHORT)
    assert s.insufficient_sample
    assert s.sample_note == 'n=12 independent: too small to conclude (need at least 30)'


def test_the_floor_is_the_documented_threshold_and_not_one_off():
    assert P.MIN_INDEPENDENT_TO_CONCLUDE == 30
    assert S.sample_note(29)[0] is True
    assert S.sample_note(30) == (False, 'n=30 independent')
    assert S.sample_note(0)[0] is True


def test_raw_rows_cannot_lift_a_cell_over_the_floor():
    groups = [str(uuid.uuid4()) for _ in range(12)]
    rows = [obs(sample_group_id=g, decision_time=T0 + timedelta(minutes=i)) for g in groups for i in range(5)]
    s = S.summarize(rows, COHORT)
    assert s.n_filled_raw == 60 and s.n_filled_independent == 12 and s.insufficient_sample


def test_an_empty_cell_is_all_zero_and_flagged():
    s = S.summarize([], COHORT)
    assert s.n_labels_raw == 0 and s.expectancy_r is None and s.insufficient_sample and s.max_drawdown_r == 0.0


# ---------------------------------------------------------------------------
# M6: no_entry, ambiguous, funding_partial
# ---------------------------------------------------------------------------
def test_no_entry_labels_are_excluded_from_every_r_statistic_and_counted_apart():
    rows = [obs(stored_r=1.0), obs(stored_r=-1.0, first_barrier='lower'), no_entry(), no_entry()]
    s = S.summarize(rows, COHORT)
    assert s.n_filled_raw == 2 and s.n_filled_independent == 2
    assert s.expectancy_r == pytest.approx(0.0)  # not (1 - 1 + 0 + 0) / 4 either way, but it must be exactly the filled mean
    assert s.win_rate == pytest.approx(0.5)
    assert s.max_drawdown_r == pytest.approx(1.0)
    assert (s.n_no_entry_raw, s.n_no_entry_independent) == (2, 2)
    assert s.no_entry_rate_raw == pytest.approx(0.5) and s.no_entry_rate_independent == pytest.approx(0.5)
    assert s.barrier_counts_independent == {'lower': 1, 'upper': 1}


def test_no_entry_is_not_a_zero_r_in_the_mean():
    s = S.summarize([obs(stored_r=1.0)] + [no_entry() for _ in range(9)], COHORT)
    assert s.expectancy_r == pytest.approx(1.0)
    assert s.no_entry_rate_raw == pytest.approx(0.9)


def test_the_representative_of_a_group_is_chosen_before_no_entry_is_dropped():
    group = str(uuid.uuid4())
    rows = [
        no_entry(sample_group_id=group, decision_time=T0),
        obs(sample_group_id=group, decision_time=T0 + timedelta(hours=1), stored_r=5.0),
    ]
    s = S.summarize(rows, COHORT)
    assert s.n_labels_independent == 1 and s.n_no_entry_independent == 1 and s.n_filled_independent == 0
    assert s.expectancy_r is None  # the later row is not promoted to stand in for a no-entry first row


def test_a_filled_label_without_stored_r_fails_loudly_instead_of_being_imputed():
    with pytest.raises(ValueError):
        S.summarize([obs(stored_r=None)], COHORT)


def test_ambiguous_share_is_over_filled_labels_and_flags_only_above_five_percent():
    at_five = [obs(barrier_ambiguous=i < 5) for i in range(100)] + [no_entry() for _ in range(50)]
    s = S.summarize(at_five, COHORT)
    assert s.n_ambiguous_raw == 5 and s.n_filled_raw == 100
    assert s.ambiguous_share_raw == pytest.approx(0.05)
    assert not s.ambiguous_flag  # exactly 5% is not above 5%

    above = [obs(barrier_ambiguous=i < 6) for i in range(100)]
    s2 = S.summarize(above, COHORT)
    assert s2.ambiguous_share_raw == pytest.approx(0.06) and s2.ambiguous_flag


def test_no_entry_rows_do_not_dilute_the_ambiguous_share():
    rows = [obs(barrier_ambiguous=True)] + [obs() for _ in range(9)] + [no_entry() for _ in range(90)]
    s = S.summarize(rows, COHORT)
    assert s.ambiguous_share_raw == pytest.approx(0.1) and s.ambiguous_flag


def test_funding_partial_share_is_reported_and_funding_sensitive_stats_drop_those_rows():
    rows = [
        obs(stored_r=1.0, funding_r=-0.1),
        obs(stored_r=-1.0, first_barrier='lower', funding_r=0.1),
        obs(stored_r=3.0, funding_r=0.0, funding_partial=True),
        obs(stored_r=1.0, funding_r=-0.3),
    ]
    s = S.summarize(rows, COHORT)
    assert s.n_funding_partial_raw == 1 and s.funding_partial_share_raw == pytest.approx(0.25)
    # headline expectancy keeps the row (stored R is the contract) ...
    assert s.expectancy_r == pytest.approx(1.0)
    # ... the funding-sensitive variants drop it
    assert s.n_ex_funding_partial == 3
    assert s.expectancy_r_ex_funding_partial == pytest.approx(1 / 3)
    assert s.funding_r_mean_complete == pytest.approx((-0.1 + 0.1 - 0.3) / 3)


# ---------------------------------------------------------------------------
# Brier rides beside expectancy and is never invented
# ---------------------------------------------------------------------------
def test_brier_is_not_computed_when_no_probability_is_stored():
    s = S.summarize([obs() for _ in range(5)], COHORT)
    assert s.brier is None and s.n_with_probability == 0
    assert 'p_upper_before_stop is NULL' in s.brier_note


def test_brier_is_computed_over_the_independent_rows_that_carry_a_probability():
    rows = [obs(p_upper=0.8), obs(p_upper=0.8, first_barrier='lower', stored_r=-1.0), obs(p_upper=None)]
    s = S.summarize(rows, COHORT)
    assert s.n_with_probability == 2
    assert s.brier == pytest.approx((0.04 + 0.64) / 2)


# ---------------------------------------------------------------------------
# One cohort per statistic
# ---------------------------------------------------------------------------
def test_summarize_refuses_to_pool_a_replay_no_history_row_with_a_live_options_layer_row():
    mixed = [btc_no_history(), live_btc_layer()]
    with pytest.raises(MixedCohortError):
        S.summarize(mixed, btc_no_history().cohort)
    with pytest.raises(MixedCohortError):
        S.summarize(mixed, live_btc_layer().cohort)
