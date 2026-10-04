"""Cohorts: source x options path x lookback. Replay `no_options_history` rows never share a number with live options-layer rows."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.stats import params as P
from app.core.stats.observations import (
    LOOKBACK_FULL,
    LOOKBACK_THIN,
    PATH_LAYER,
    PATH_LAYER_IGNORED,
    PATH_NO_BOOK,
    PATH_NO_HISTORY,
    CohortKey,
    classify_lookback,
    classify_options_path,
    split_by_cohort,
    vol_bucket,
)
from app.core.stats.report import build_report
from stats_helpers import T0, btc_no_history, live_btc_layer, obs


def path(**kw):
    values = dict(source='replay', coin='BTCUSDT', options_verdict='ignore', status_reason=None, options_snapshot_time=None)
    values.update(kw)
    return classify_options_path(**values)


# ---------------------------------------------------------------------------
# Options path
# ---------------------------------------------------------------------------
def test_alts_have_no_options_book_in_live_and_replay_alike():
    assert path(coin='SOLUSDT', source='replay') == PATH_NO_BOOK
    assert path(coin='SOLUSDT', source='live') == PATH_NO_BOOK


def test_the_marker_reason_is_no_options_history():
    assert path(status_reason='options_ignored:no_options_history') == PATH_NO_HISTORY


def test_a_shadow_row_that_lost_the_marker_reason_is_still_no_options_history():
    # vote_conflict / cvd rows keep their own status_reason; the verdict and the missing snapshot give it away
    assert path(status_reason='vote_conflict') == PATH_NO_HISTORY
    assert path(status_reason='cvd_divergence_against') == PATH_NO_HISTORY


def test_a_real_verdict_on_a_covered_coin_went_through_the_layer():
    assert path(options_verdict='align', options_snapshot_time=T0) == PATH_LAYER
    assert path(options_verdict='headwind', source='live', options_snapshot_time=T0) == PATH_LAYER


def test_replay_that_saw_a_stored_snapshot_but_ignored_it_is_layer_ignored_not_history():
    assert path(options_snapshot_time=T0, status_reason='options_ignored:snapshot_stale') == PATH_LAYER_IGNORED


def test_live_with_no_snapshot_ran_the_layer_and_is_not_replay_history():
    assert path(source='live', status_reason='options_ignored:no_snapshot') == PATH_LAYER_IGNORED


def test_an_unchecked_candidate_has_no_path():
    with pytest.raises(ValueError):
        path(options_verdict=None)


# ---------------------------------------------------------------------------
# Never pooled
# ---------------------------------------------------------------------------
def test_replay_no_history_and_live_options_layer_rows_land_in_different_cohorts():
    rows = [btc_no_history(), btc_no_history(status='watch', status_reason='vote_conflict'), live_btc_layer(), live_btc_layer()]
    cohorts = split_by_cohort(rows)
    assert set(cohorts) == {
        CohortKey('replay', PATH_NO_HISTORY, LOOKBACK_FULL),
        CohortKey('live', PATH_LAYER, LOOKBACK_FULL),
    }
    for key, members in cohorts.items():
        assert {o.cohort for o in members} == {key}


def test_a_built_report_never_holds_both_in_one_cohort_and_the_totals_add_up():
    rows = [btc_no_history() for _ in range(3)] + [live_btc_layer() for _ in range(2)] + [obs() for _ in range(4)]
    report = build_report(rows)
    labels = {c.cohort.label: c.populations['alert'].overall.n_labels_raw + c.populations['shadow'].overall.n_labels_raw for c in report.cohorts}
    # the three BTC replay rows are watch (shadow); the live ones are alerts; the alts are alerts
    assert labels == {
        'replay/no_options_history/full': 3,
        'live/options_layer/full': 2,
        'replay/no_options_book/full': 4,
    }
    assert sum(labels.values()) == len(rows)


def test_source_is_a_key_even_when_the_options_path_matches():
    live_alt = obs(source='live')
    replay_alt = obs(source='replay')
    assert live_alt.cohort != replay_alt.cohort
    assert len(split_by_cohort([live_alt, replay_alt])) == 2


def test_cohort_key_rejects_unknown_parts():
    for bad in (('paper', PATH_LAYER, LOOKBACK_FULL), ('live', 'blended', LOOKBACK_FULL), ('live', PATH_LAYER, 'sort_of')):
        with pytest.raises(ValueError):
            CohortKey(*bad)


# ---------------------------------------------------------------------------
# Thin lookback
# ---------------------------------------------------------------------------
def test_the_cutoff_is_twenty_days_after_the_backfill_start():
    assert P.THIN_LOOKBACK_CUTOFF == P.BACKFILL_START + timedelta(days=P.THIN_LOOKBACK_DAYS)


def test_signal_bars_before_the_cutoff_are_thin_and_at_or_after_are_full():
    assert classify_lookback(P.THIN_LOOKBACK_CUTOFF - timedelta(minutes=15)) == LOOKBACK_THIN
    assert classify_lookback(P.THIN_LOOKBACK_CUTOFF) == LOOKBACK_FULL


def test_thin_rows_form_their_own_cohort_and_are_flagged_in_the_report():
    thin = obs(bar_close=P.THIN_LOOKBACK_CUTOFF - timedelta(days=3))
    full = obs(bar_close=P.THIN_LOOKBACK_CUTOFF + timedelta(days=3))
    cohorts = split_by_cohort([thin, full])
    assert {k.lookback for k in cohorts} == {LOOKBACK_THIN, LOOKBACK_FULL}
    report = build_report([thin, full])
    by_lookback = {c.cohort.lookback: c for c in report.cohorts}
    assert any('thin lookback' in f for f in by_lookback[LOOKBACK_THIN].flags)
    assert not any('thin lookback' in f for f in by_lookback[LOOKBACK_FULL].flags)
    assert by_lookback[LOOKBACK_THIN].populations['alert'].overall.n_labels_raw == 1


def test_no_options_history_cohort_is_flagged_as_not_comparable_with_live():
    report = build_report([btc_no_history()])
    assert any('never comparable' in f for f in report.cohorts[0].flags)


# ---------------------------------------------------------------------------
# Vol buckets (pool dimension)
# ---------------------------------------------------------------------------
def test_vol_buckets_are_terciles_with_an_explicit_unknown():
    assert [vol_bucket(v) for v in (0.0, 0.33, 0.34, 0.66, 0.67, 1.0, None)] == ['low', 'low', 'mid', 'mid', 'high', 'high', 'unknown']
