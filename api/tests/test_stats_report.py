"""Reliability bins, walk-up, baselines, kill rule, shadow separation, and the guards that keep the stats out of rule_hash."""

from __future__ import annotations

import ast
import json
import pathlib
import uuid
from datetime import timedelta

import pytest

from app.core import constants as C
from app.core import hashing
from app.core.stats import params as P
from app.core.stats import report as R
from app.core.stats import summary as S
from app.core.stats.observations import CohortKey
from app.core.stats.reliability import reliability_dataset
from app.core.stats.render import format_text, to_jsonable
from stats_helpers import T0, no_entry, obs

COHORT = CohortKey('replay', 'no_options_book', 'full')


# ---------------------------------------------------------------------------
# Reliability diagram
# ---------------------------------------------------------------------------
def with_p(n, p, hit_share=0.5, r_hit=1.0, r_miss=-1.0):
    return [
        obs(p_upper=p, first_barrier='upper' if i < n * hit_share else 'lower', stored_r=r_hit if i < n * hit_share else r_miss)
        for i in range(n)
    ]


def test_a_bin_with_nineteen_independent_outcomes_is_hidden_and_twenty_is_shown():
    ds = reliability_dataset(with_p(19, 0.65) + with_p(20, 0.25), COHORT)
    by_low = {round(b.low, 1): b for b in ds.bins}
    assert by_low[0.6].hidden and by_low[0.6].n_independent == 19
    assert (by_low[0.6].mean_probability, by_low[0.6].observed_rate, by_low[0.6].mean_stored_r) == (None, None, None)
    assert not by_low[0.2].hidden and by_low[0.2].n_independent == 20
    assert by_low[0.2].mean_probability == pytest.approx(0.25) and by_low[0.2].observed_rate == pytest.approx(0.5)
    assert ds.visible_bins == 1 and ds.min_bin_outcomes == 20


def test_bins_count_independent_outcomes_not_raw_rows():
    group = str(uuid.uuid4())
    rows = [obs(p_upper=0.55, sample_group_id=group, decision_time=T0 + timedelta(minutes=i)) for i in range(60)]
    ds = reliability_dataset(rows, COHORT)
    bin_ = next(b for b in ds.bins if round(b.low, 1) == 0.5)
    assert bin_.n_raw == 60 and bin_.n_independent == 1 and bin_.hidden


def test_without_a_stored_probability_nothing_is_invented():
    ds = reliability_dataset([obs() for _ in range(50)], COHORT)
    assert not ds.available and ds.n_independent_with_probability == 0 and ds.visible_bins == 0
    assert all(b.hidden and b.n_independent == 0 for b in ds.bins)
    assert 'p_upper_before_stop is NULL' in ds.note


def test_no_entry_rows_do_not_enter_a_bin_and_probability_one_lands_in_the_top_bin():
    rows = with_p(20, 1.0) + [no_entry(p_upper=1.0) for _ in range(30)]
    ds = reliability_dataset(rows, COHORT)
    top = ds.bins[-1]
    assert top.n_independent == 20 and not top.hidden and top.high == 1.0


def test_calibration_gap_and_r_monotonicity_are_computed_over_visible_bins_only():
    rows = with_p(20, 0.25, hit_share=0.2, r_hit=1.0, r_miss=-0.5) + with_p(20, 0.85, hit_share=0.9, r_hit=1.0, r_miss=-1.0) + with_p(5, 0.5)
    ds = reliability_dataset(rows, COHORT)
    assert ds.visible_bins == 2
    assert ds.max_abs_gap == pytest.approx(max(abs(0.25 - 0.2), abs(0.85 - 0.9)))
    assert ds.r_non_decreasing is True


# ---------------------------------------------------------------------------
# Walk-up
# ---------------------------------------------------------------------------
def test_the_redundant_trend_x_vol_level_is_skipped_and_the_order_is_the_spec_order():
    names = [n for n, _ in R.distinct_walk_up_levels()]
    assert names == ['full_slice', 'drop_event_flag', 'drop_session', 'setup_family']


def test_walk_up_marks_a_cell_that_equals_the_previous_level_and_cells_roll_up():
    rows = [
        obs(session='us', event_flag=False),
        obs(session='us', event_flag=True),
        obs(session='asia', event_flag=False),
    ]
    skipped, levels = R.walk_up(rows, COHORT)
    assert skipped == ('trend_x_vol',)
    by_level = {lv.level: lv for lv in levels}
    assert len(by_level['full_slice'].cells) == 3
    # dropping the event flag merges the two `us` rows; the lone `asia` cell is unchanged
    drop_event = {tuple(c.key.values()): c for c in by_level['drop_event_flag'].cells}
    us = drop_event[('squeeze', 'chop', 'mid', 'us')]
    asia = drop_event[('squeeze', 'chop', 'mid', 'asia')]
    assert us.summary.n_labels_raw == 2 and not us.same_as_previous_level
    assert asia.same_as_previous_level
    assert by_level['setup_family'].cells[0].summary.n_labels_raw == 3


def test_every_walk_up_cell_carries_raw_and_independent_counts_and_the_flag():
    skipped, levels = R.walk_up([obs() for _ in range(5)], COHORT)
    for level in levels:
        for cell in level.cells:
            assert cell.summary.n_labels_raw >= cell.summary.n_labels_independent
            assert cell.summary.insufficient_sample


# ---------------------------------------------------------------------------
# Shadow vs alerts
# ---------------------------------------------------------------------------
def test_alerts_and_shadow_rows_are_separate_populations_and_shadow_is_itemized():
    rows = (
        [obs(stored_r=1.0) for _ in range(3)]
        + [obs(status='cancelled', status_reason='cvd_divergence_against', stored_r=-1.0, first_barrier='lower') for _ in range(2)]
        + [obs(status='watch', status_reason='vote_conflict', stored_r=0.5) for _ in range(4)]
    )
    cohort = R.build_report(rows).cohorts[0]
    assert cohort.populations['alert'].overall.n_labels_raw == 3
    assert cohort.populations['shadow'].overall.n_labels_raw == 6
    assert cohort.populations['alert'].overall.expectancy_r == pytest.approx(1.0)
    items = {(r.status, r.status_reason): r.summary for r in cohort.shadow_by_reason}
    assert items[('cancelled', 'cvd_divergence_against')].expectancy_r == pytest.approx(-1.0)
    assert items[('watch', 'vote_conflict')].n_labels_raw == 4


# ---------------------------------------------------------------------------
# no_entry per status / horizon, ambiguous flag at report level
# ---------------------------------------------------------------------------
def test_no_entry_rate_is_reported_overall_and_per_status_and_horizon():
    rows = [obs(horizon='scalp'), no_entry(horizon='scalp'), no_entry(horizon='swing'), obs(horizon='swing', status='cancelled', status_reason='x')]
    table = {(r.status, r.horizon): r for r in R.build_report(rows).cohorts[0].no_entry}
    assert table[('all', 'all')].n_no_entry_raw == 2 and table[('all', 'all')].rate_raw == pytest.approx(0.5)
    assert table[('alert', 'scalp')].rate_raw == pytest.approx(0.5)
    assert table[('alert', 'swing')].rate_raw == pytest.approx(1.0)
    assert table[('cancelled', 'swing')].rate_raw == pytest.approx(0.0)


def test_report_flags_ambiguous_share_above_five_percent_and_funding_partial():
    rows = [obs(barrier_ambiguous=True, funding_partial=True)] + [obs() for _ in range(9)]
    flags = R.build_report(rows).cohorts[0].flags
    assert any('barrier_ambiguous' in f and '1m klines' in f for f in flags)
    assert any('funding_partial' in f for f in flags)


# ---------------------------------------------------------------------------
# Baselines and the kill rule
# ---------------------------------------------------------------------------
def sb(**kw):
    values = dict(trigger='swing_break', setup_family='breakout')
    values.update(kw)
    return obs(**values)


def test_swing_break_baseline_takes_every_status_but_not_rows_the_options_layer_moved():
    rows = [
        sb(stored_r=1.0),
        sb(status='watch', status_reason='vote_conflict', stored_r=-1.0, first_barrier='lower'),
        sb(status='cancelled', status_reason='cvd_divergence_against', stored_r=-1.0, first_barrier='lower'),
        sb(barriers_unmodified=False, stored_r=9.0),
        obs(stored_r=5.0),  # a squeeze trigger is not the swing-break rule
    ]
    base = R.swing_break_baseline(rows, COHORT)
    assert base.n_swing_break_rows == 4 and base.n_excluded_options_modified == 1
    assert base.summary.n_filled_raw == 3
    assert base.summary.expectancy_r == pytest.approx(-1 / 3)


def test_baseline_is_per_horizon_on_request():
    rows = [sb(horizon='scalp', stored_r=1.0), sb(horizon='swing', stored_r=-1.0, first_barrier='lower')]
    assert R.swing_break_baseline(rows, COHORT, 'scalp').summary.expectancy_r == pytest.approx(1.0)


def test_comparison_verdicts_against_flat_and_the_baseline():
    winners = [obs(stored_r=1.0 + (i % 3) * 0.01) for i in range(40)]
    losers = [sb(stored_r=-1.0, first_barrier='lower') for _ in range(10)]
    cohort = R.build_report(winners + losers).cohorts[0]
    cmp_all = cohort.comparisons[0]
    assert cmp_all.scope == 'all'
    assert cmp_all.vs_flat == 'interval_above'
    assert cmp_all.vs_swing_break == 'interval_above'  # the baseline is only the ten losing swing breaks (-1 R)
    assert cmp_all.baseline.summary.expectancy_r == pytest.approx(-1.0)
    assert cmp_all.flat_expectancy_r == 0.0


def test_against_flat_an_interval_that_includes_zero_is_not_a_win():
    rows = [obs(stored_r=1.0 if i % 2 else -1.0, first_barrier='upper' if i % 2 else 'lower') for i in range(40)]
    assert R.build_report(rows).cohorts[0].comparisons[0].vs_flat == 'interval_includes'


def app_summary(n, r):
    return S.summarize([obs(stored_r=r) for _ in range(n)], COHORT)


def baseline_summary(r, n=40):
    return S.summarize([sb(stored_r=r) for _ in range(n)], COHORT)


def test_kill_rule_is_not_yet_applicable_below_three_hundred_whatever_the_interval_says():
    result = R.kill_rule_check(app_summary(299, -1.0), baseline_summary(2.0))
    assert result.status == R.KILL_NOT_APPLICABLE
    assert result.n_independent == 299 and result.floor == 300 == P.KILL_RULE_MIN_INDEPENDENT
    assert 'not yet applicable' in result.note


def test_kill_rule_triggers_at_three_hundred_when_the_app_interval_is_entirely_below_the_baseline():
    result = R.kill_rule_check(app_summary(300, -0.5), baseline_summary(0.2))
    assert result.status == R.KILL_TRIGGERED
    assert result.app_interval[1] < result.baseline_expectancy_r


def test_kill_rule_does_not_trigger_when_the_interval_reaches_the_baseline():
    assert R.kill_rule_check(app_summary(300, 0.5), baseline_summary(0.2)).status == R.KILL_NOT_TRIGGERED


def test_kill_rule_counts_independent_labels_not_raw_ones():
    group = str(uuid.uuid4())
    rows = [obs(sample_group_id=group, stored_r=-1.0, decision_time=T0 + timedelta(minutes=i)) for i in range(400)]
    cohort_report = R.build_report(rows).cohorts[0]
    assert cohort_report.kill_rule.status == R.KILL_NOT_APPLICABLE and cohort_report.kill_rule.n_independent == 1


def test_kill_rule_without_a_baseline_says_so():
    empty = S.summarize([], COHORT)
    assert R.kill_rule_check(app_summary(300, 0.1), empty).status == R.KILL_NO_BASELINE


# ---------------------------------------------------------------------------
# Output and guards
# ---------------------------------------------------------------------------
def test_text_and_json_output_carry_raw_independent_and_the_plain_words():
    rows = [obs() for _ in range(12)]
    report = R.build_report(rows, rule_hash='abc')
    text = format_text(report)
    assert 'n=12 independent: too small to conclude' in text
    assert 'labels raw/ind 12/12' in text
    assert 'kill rule: not_yet_applicable' in text
    assert 'COHORT OVERVIEW' in text
    parsed = json.loads(json.dumps(to_jsonable(report), default=str))
    assert parsed['cohorts'][0]['kill_rule']['status'] == 'not_yet_applicable'


def test_stats_parameters_do_not_feed_rule_hash():
    payload = json.dumps(hashing.rule_hash_input(), default=str)
    for name in ('THIN_LOOKBACK_CUTOFF', 'AMBIGUOUS_SHARE_FLAG', 'BOOTSTRAP_SEED', 'VOL_BUCKET_EDGES', 'MIN_INDEPENDENT_TO_CONCLUDE'):
        assert name not in payload
        assert not hasattr(C, name)


def test_stats_floors_are_read_from_the_frozen_constants_not_restated():
    assert P.MIN_INDEPENDENT_TO_CONCLUDE == C.POOL_MIN_INDEPENDENT_OUTCOMES
    assert P.MIN_BIN_OUTCOMES == C.CALIBRATION_BIN_MIN_OUTCOMES
    assert P.KILL_RULE_MIN_INDEPENDENT == C.KILL_RULE_INDEPENDENT_LABELS
    assert P.INTERVAL_CONFIDENCE == C.INTERVAL_CONFIDENCE


STATS_ROOT = pathlib.Path(__file__).resolve().parents[1] / 'app' / 'core' / 'stats'


@pytest.mark.parametrize('path', sorted(STATS_ROOT.glob('*.py')), ids=lambda p: p.name)
def test_no_stats_module_reads_the_wall_clock_or_the_database(path):
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not any(m.startswith(('sqlalchemy', 'app.models.as_of', 'app.db', 'app.worker')) for m in imported if m != 'app.models'), imported
    for forbidden in ('utc_now', 'datetime.now', 'utcnow', 'time.time'):
        assert forbidden not in source
