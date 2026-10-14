"""The promotion job against Postgres: the append-only log (trigger and CHECKs), the record path, the read-only session,
and the counterfactual a blocked row is paired on (recomputed with the rules' and the options job's own functions)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core import constants as C
from app.core.promotion import layers as L
from app.core.promotion import run_gate
from app.core.rules import direction as D
from app.core.rules.inputs import RuleInputs
from app.models import enums as E
from app.models.decisions import Candidate
from app.models.features import FeatureRow
from app.models.promotion import PromotionDecision
from app.worker import options_job as OJ
from app.worker import promotion_job as PJ
from app.worker.stats_job import readonly_session
from tests.conftest import make_candidate
from tests.rules_helpers import PASSING_COLUMNS, T0
from tests.test_promotion_gate import ALERT_CF, START, cvd_row, row

DECISION = T0 + timedelta(seconds=40)


def decision(**overrides: Any) -> PromotionDecision:
    values: dict[str, Any] = {
        'layer': 'cvd_divergence_cancel',
        'source': 'live',
        'options_path': 'no_options_book',
        'lookback': 'full',
        'horizon': 'swing',
        'verdict': 'insufficient',
        'binding': True,
        'n_pairs': 0,
        'as_of': START,
        'window_days': Decimal('0'),
        'params': {'min_paired_outcomes': 50},
        'reasons': ['paired_outcomes_below_floor: 0 < 50'],
        'rule_hash': 'r' * 64,
        'code_version': 'deadbee',
    }
    values.update(overrides)
    return PromotionDecision(**values)


def stored(session, **overrides) -> PromotionDecision:
    row_ = decision(**overrides)
    session.add(row_)
    session.flush()
    return row_


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------
def test_the_trigger_refuses_update(session):
    logged = stored(session)
    with pytest.raises(DBAPIError, match='append-only'):
        with session.begin_nested():
            session.execute(update(PromotionDecision).where(PromotionDecision.id == logged.id).values(verdict='reject'))
    with pytest.raises(DBAPIError, match='append-only'):
        with session.begin_nested():
            session.execute(text('UPDATE promotion_decision SET params = \'{"bin_edges": "moved"}\''))


def test_the_trigger_refuses_delete(session):
    logged = stored(session)
    with pytest.raises(DBAPIError, match='append-only'):
        with session.begin_nested():
            session.execute(delete(PromotionDecision).where(PromotionDecision.id == logged.id))
    assert session.execute(select(PromotionDecision.id)).scalars().all() == [logged.id]


def test_a_changed_mind_is_a_new_row(session):
    first = stored(session)
    second = stored(session, verdict='reject', n_pairs=50, mean_diff_r=Decimal('0.1'), ci_low=Decimal('-0.1'),
                    ci_high=Decimal('0.3'), shadow_start=START - timedelta(days=30), window_days=Decimal('30'))
    assert second.id > first.id


@pytest.mark.parametrize(
    'overrides',
    [
        {'source': 'replay', 'binding': False, 'verdict': 'promote'},  # replay can never promote
        {'source': 'replay', 'binding': True, 'verdict': 'insufficient'},  # replay never binds
        {'source': 'live', 'binding': True, 'verdict': 'preview_reject'},  # live is never a preview
        {'verdict': 'promoted'},
        {'options_path': 'pooled'},
        {'n_pairs': 3},  # pairs but no mean
        {'params': ['not', 'an', 'object']},
        {'reasons': {'not': 'an array'}},
        {'n_pairs': 2, 'mean_diff_r': Decimal('0.1'), 'shadow_start': START, 'ci_low': Decimal('1'), 'ci_high': Decimal('0')},
    ],
)
def test_the_checks(session, overrides):
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            stored(session, **overrides)


def test_a_replay_preview_is_accepted(session):
    assert stored(session, source='replay', binding=False, verdict='preview_promote').id


# ---------------------------------------------------------------------------
# --record writes one row per cell and nothing else; the default session refuses writes
# ---------------------------------------------------------------------------
def test_record_cells_appends_one_row_per_cell(session):
    blocked = [cvd_row(i, stored_r=-1.0) for i in range(30)]
    rows = blocked + [row(100 + i, stored_r=1.0) for i in range(20)] + [row(200, source='replay')]
    facts = {o.candidate_id: ALERT_CF for o in blocked}
    cells = run_gate(L.CVD_DIVERGENCE_CANCEL, rows, as_of=START + timedelta(days=30), counterfactuals=facts)
    before = session.execute(select(Candidate.id)).all()
    assert PJ.record_cells(session, cells, rule_hash='h' * 64, code_version='abc') == 2
    logged = session.execute(select(PromotionDecision).order_by(PromotionDecision.id)).scalars().all()
    assert [(r.source, r.verdict, r.binding, r.n_pairs) for r in logged] == [
        ('live', 'promote', True, 50),
        ('replay', 'preview_insufficient', False, 1),
    ]
    live = logged[0]
    assert float(live.mean_diff_r) == pytest.approx(0.6) and live.ci_low > 0
    assert live.params['min_paired_outcomes'] == 50 and live.params['counts']['pairs'] == 50
    assert live.params['bin_edges'] and live.reasons == ['all_criteria_met']
    assert session.execute(select(Candidate.id)).all() == before


def test_the_default_session_is_read_only(db_engine, db_schema):
    with readonly_session(db_engine, search_path=db_schema) as ro:
        cells, context = PJ.run_promotion(ro, layers=list(L.LAYERS.values()), sources=list(E.SOURCES), now=START)
        assert cells == [] and set(context['sources']) == set(E.SOURCES)
        one = run_gate(L.CVD_DIVERGENCE_CANCEL, [row(0)], as_of=START + timedelta(days=1))
        with pytest.raises(DBAPIError) as caught:
            PJ.record_cells(ro, one, rule_hash='h', code_version='v')
        assert type(caught.value.orig).__name__ == 'ReadOnlySqlTransaction'


# ---------------------------------------------------------------------------
# The counterfactual
# ---------------------------------------------------------------------------
def shadow(session, *, coin='SOLUSDT', status, reason, columns=None) -> Candidate:
    cols: dict[str, Any] = {**PASSING_COLUMNS, **(columns or {})}
    stop = cols['chandelier_stop_price']
    cand = make_candidate(
        session, coin=coin, horizon='swing', bar_close=T0, source=E.SOURCE_LIVE, direction='long', decision_time=DECISION,
        feature_overrides=cols, pre_options_stop_price=stop, pre_options_target_price=Decimal('98000'), stop_price=stop,
        options_verdict=None, status=status, status_reason=reason, vertical_deadline=None, locked_book=True,
    )
    outcome = OJ.check_candidate(session, cand)
    assert outcome.result == OJ.RESULT_CHECKED
    session.refresh(cand)
    assert (cand.status, cand.status_reason) == (status, reason)  # the options job never edits a shadow status
    return cand


def test_a_cvd_cancelled_alt_would_have_been_an_alert(session):
    cand = shadow(session, status='cancelled', reason=C.STATUS_REASON_CVD_DIVERGENCE)
    fact, outcome = PJ.counterfactual_for(session, cand)
    assert outcome == PJ.CF_COMPUTED
    assert fact == L.Counterfactual(vote_confirmed=True, status_if_pending='alert')


def test_a_cvd_cancelled_row_hiding_a_vote_conflict_is_not_a_would_be_alert(session):
    against = {'aggressor_imbalance_z': -1.2, 'oi_price_quadrant': None, 'funding_z': 2.0}
    cand = shadow(session, status='cancelled', reason=C.STATUS_REASON_CVD_DIVERGENCE, columns=against)
    row_ = session.get(FeatureRow, cand.feature_row_id)
    assert not D.tally('long', RuleInputs.from_feature_row(row_, reference_coin='BTCUSDT')).confirmed
    fact, _ = PJ.counterfactual_for(session, cand)
    assert fact is not None and not fact.vote_confirmed
    assert L.CVD_DIVERGENCE_CANCEL.pair(cvd_row(0, stored_r=1.0), fact) is None


def test_a_covered_coin_with_no_book_would_have_stayed_watch(session):
    cand = shadow(session, coin='BTCUSDT', status='watch', reason=C.STATUS_REASON_VOTE_CONFLICT,
                  columns={'aggressor_imbalance_z': -1.2, 'oi_price_quadrant': None, 'funding_z': 2.0})
    fact, _ = PJ.counterfactual_for(session, cand)
    assert fact is not None and fact.status_if_pending == 'watch'


def test_a_recomputed_verdict_that_differs_from_the_stored_one_gives_no_counterfactual(session):
    cand = shadow(session, status='cancelled', reason=C.STATUS_REASON_CVD_DIVERGENCE)
    session.execute(update(Candidate).where(Candidate.id == cand.id).values(options_verdict='align'))
    session.refresh(cand)
    fact, outcome = PJ.counterfactual_for(session, cand)
    assert (fact, outcome) == (None, PJ.CF_MISMATCH)


def test_counterfactuals_are_loaded_only_for_blocked_locked_book_rows(session):
    cand = shadow(session, status='cancelled', reason=C.STATUS_REASON_CVD_DIVERGENCE)
    observations = [
        cvd_row(0, candidate_id=cand.id),
        cvd_row(1, candidate_id=cand.id + 10_000, locked_book=False),
        row(2, candidate_id=cand.id + 20_000),
    ]
    facts, outcomes = PJ.load_counterfactuals(session, observations, L.LAYERS.values())
    assert set(facts) == {cand.id}
    assert outcomes[PJ.CF_COMPUTED] == 1 and outcomes['status_if_pending:alert'] == 1
