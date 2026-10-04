"""The stats job against the throwaway test schema: loading, the rule-hash gate, cohort assignment from real rows, read-only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.hashing import rule_hash
from app.core.stats.observations import PATH_LAYER, PATH_NO_BOOK, PATH_NO_HISTORY
from app.models.decisions import Candidate
from app.models.labels import Label
from app.worker import stats_job
from conftest import BAR_CLOSE, make_candidate

UTC = timezone.utc
CURRENT = rule_hash()


def add_label(session, candidate, *, first_barrier='upper', stored_r='1.0', rule=CURRENT, **overrides):
    filled = first_barrier != 'no_entry'
    r = Decimal(stored_r) if filled else None
    values = dict(
        candidate_id=candidate.id,
        fill_time=candidate.bar_close,
        fill_price=Decimal('100'),
        resolved_at=candidate.bar_close + timedelta(hours=2),
        first_barrier=first_barrier,
        price_r=r,
        fees_r=Decimal('0') if filled else None,
        funding_r=Decimal('0') if filled else None,
        stored_r=r,
        mae_r=Decimal('0.2') if filled else None,
        hours_to_resolution=Decimal('2') if filled else None,
        regime_trend_family='trend',
        regime_vol_pct=Decimal('0.5'),
        regime_session='us',
        regime_event_flag=False,
        cost_assumption_bps=Decimal('12'),
        feature_hash='f' * 64,
        rule_hash=rule,
        code_version='deadbee',
    )
    values.update(overrides)
    label = Label(**values)
    session.add(label)
    session.flush()
    return label


def cand(session, minutes=0, **kw):
    kw.setdefault('rule_hash', CURRENT)
    return make_candidate(session, bar_close=BAR_CLOSE + timedelta(minutes=minutes), **kw)


def test_only_candidates_and_labels_on_the_current_rule_hash_are_used(session):
    good = cand(session, 0)
    add_label(session, good)
    old = cand(session, 15, rule_hash='0' * 64)
    add_label(session, old, rule='0' * 64)
    unlabeled = cand(session, 30)
    result = stats_job.load_observations(session)
    assert [o.candidate_id for o in result.observations] == [good.id]
    ctx = result.context
    assert ctx['candidates_total'] == 3
    assert ctx['candidates_other_rule_hash'] == 1
    assert ctx['candidates_current_hash_unlabeled'] == 1 and unlabeled.id
    assert ctx['observations_used'] == 1


def test_observations_carry_the_label_regime_the_funding_flags_and_unmodified_barriers(session):
    c = cand(session, 0, stop_price=Decimal('99.0'), pre_options_stop_price=Decimal('99.0'))
    add_label(session, c, first_barrier='lower', stored_r='-1.05', barrier_ambiguous=True)
    capped = cand(session, 15, target_cap_price=Decimal('105'))
    add_label(session, capped)
    moved = cand(session, 30, stop_price=Decimal('98.0'))
    add_label(session, moved)
    by_id = {o.candidate_id: o for o in stats_job.load_observations(session).observations}
    o = by_id[c.id]
    assert (o.first_barrier, o.stored_r, o.barrier_ambiguous) == ('lower', -1.05, True)
    assert (o.trend_family, o.session, o.event_flag, o.vol_pct) == ('trend', 'us', False, 0.5)
    assert o.barriers_unmodified
    assert not by_id[capped.id].barriers_unmodified  # target capped by the options layer
    assert not by_id[moved.id].barriers_unmodified  # stop widened by amplify


def test_no_entry_labels_load_with_no_r(session):
    c = cand(session, 0)
    add_label(session, c, first_barrier='no_entry')
    (o,) = stats_job.load_observations(session).observations
    assert o.is_no_entry and o.stored_r is None


def test_rows_from_real_candidates_split_into_the_three_documented_cohorts(session):
    # replay BTC shadow row that kept `vote_conflict` and lost the options_ignored marker: still no_options_history
    history = cand(session, 0, source='replay', coin='BTCUSDT', status='watch', status_reason='vote_conflict', options_verdict='ignore')
    # replay marker row
    marker = cand(session, 15, source='replay', coin='BTCUSDT', status='watch', status_reason='options_ignored:no_options_history', options_verdict='ignore')
    # live BTC alert with a real verdict and a snapshot
    live = cand(session, 30, source='live', coin='BTCUSDT', status='alert', options_verdict='align', options_snapshot_time=BAR_CLOSE)
    # replay alt
    alt = cand(session, 45, source='replay', coin='SOLUSDT', status='alert', options_verdict='ignore', status_reason='options_ignored:no_options_book')
    for c in (history, marker, live, alt):
        add_label(session, c)
    by_id = {o.candidate_id: o for o in stats_job.load_observations(session).observations}
    assert by_id[history.id].cohort.options_path == PATH_NO_HISTORY
    assert by_id[marker.id].cohort.options_path == PATH_NO_HISTORY
    assert by_id[live.id].cohort.options_path == PATH_LAYER and by_id[live.id].cohort.source == 'live'
    assert by_id[alt.id].cohort.options_path == PATH_NO_BOOK
    assert by_id[history.id].cohort != by_id[live.id].cohort


def test_run_stats_end_to_end_keeps_replay_no_history_apart_from_live_layer_rows(session):
    for i in range(3):
        add_label(session, cand(session, i * 15, source='replay', coin='BTCUSDT', status='watch', status_reason='vote_conflict', options_verdict='ignore'))
    for i in range(2):
        add_label(session, cand(session, 100 + i * 15, source='live', coin='BTCUSDT', status='alert', options_verdict='align', options_snapshot_time=BAR_CLOSE))
    report = stats_job.run_stats(session)
    seen = {c.cohort.label: c for c in report.cohorts}
    # the fixture signal bars (2026-01-01) are before the thin-lookback cutoff
    assert set(seen) == {'replay/no_options_history/thin', 'live/options_layer/thin'}
    assert seen['live/options_layer/thin'].populations['alert'].overall.n_labels_raw == 2
    assert seen['replay/no_options_history/thin'].populations['shadow'].overall.n_labels_raw == 3
    assert report.context['observations_used'] == 5


def test_source_filter(session):
    add_label(session, cand(session, 0, source='replay'))
    add_label(session, cand(session, 15, source='live', coin='ETHUSDT'))
    assert {o.source for o in stats_job.load_observations(session, source='live').observations} == {'live'}


def test_the_stats_session_is_read_only_at_the_database_level(db_engine, db_schema):
    with stats_job.readonly_session(db_engine, search_path=db_schema) as ro:
        assert ro.execute(select(Candidate.id).limit(1)).first() is None  # reading works
        with pytest.raises(DBAPIError):
            ro.execute(text('CREATE TABLE should_not_exist (x int)'))
        ro.rollback()
        with pytest.raises(DBAPIError):
            ro.execute(text("UPDATE candidate SET status = status"))
            ro.execute(text('DELETE FROM candidate'))
