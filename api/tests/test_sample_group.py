"""Independence: ``sample_group_id`` and the invariants the database enforces.

"Overlapping holds, and alt signals that share one BTC impulse, count as one
independent sample. Card counts use that sample, not the raw alert count."

Every floor in the project counts those samples, so the counting query has to be
one line and it has to be right:

    SELECT count(DISTINCT c.sample_group_id) ...
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import distinct, func, select
from sqlalchemy.exc import IntegrityError

from app.models import enums as E
from app.models.decisions import Candidate
from tests.conftest import BAR_CLOSE, make_candidate


def count_independent(session, **filters) -> int:
    statement = select(func.count(distinct(Candidate.sample_group_id)))
    for column, value in filters.items():
        statement = statement.where(getattr(Candidate, column) == value)
    return session.execute(statement).scalar_one()


# ---------------------------------------------------------------------------
# The two shapes the spec names
# ---------------------------------------------------------------------------
def test_overlapping_holds_on_one_coin_count_once(session):
    """Three candidates on the same coin and horizon whose holds overlap."""
    first = make_candidate(session, bar_close=BAR_CLOSE)
    second = make_candidate(session, bar_close=BAR_CLOSE + timedelta(hours=1), leader=first)
    third = make_candidate(session, bar_close=BAR_CLOSE + timedelta(hours=2), leader=second)

    assert second.sample_group_id == first.sample_group_id
    assert third.sample_group_id == first.sample_group_id
    assert count_independent(session) == 1
    assert session.execute(select(func.count()).select_from(Candidate)).scalar_one() == 3


def test_alts_riding_one_btc_impulse_count_once(session):
    """A BTC signal plus four alts gated through on that impulse is one sample."""
    btc = make_candidate(session, coin='BTCUSDT')
    alts = [
        make_candidate(
            session,
            coin=coin,
            leader=btc,
            sample_group_reason=E.SAMPLE_GROUP_BTC_IMPULSE,
        )
        for coin in ('ETHUSDT', 'SOLUSDT', 'AVAXUSDT', 'LINKUSDT')
    ]

    assert all(alt.sample_group_id == btc.sample_group_id for alt in alts)
    assert count_independent(session) == 1


def test_a_later_non_overlapping_signal_is_a_new_sample(session):
    first = make_candidate(session, bar_close=BAR_CLOSE)
    later = make_candidate(session, bar_close=BAR_CLOSE + timedelta(days=5))
    assert later.sample_group_id != first.sample_group_id
    assert count_independent(session) == 2


def test_different_horizons_are_independent(session):
    scalp = make_candidate(session, horizon=E.HORIZONS[0])
    swing = make_candidate(session, horizon=E.HORIZONS[1])
    assert scalp.sample_group_id != swing.sample_group_id
    assert count_independent(session) == 2


def test_cancelled_and_abstained_rows_join_the_group(session):
    """A filtered candidate is still in the pair, so it is still in the group."""
    alert = make_candidate(session, bar_close=BAR_CLOSE)
    cancelled = make_candidate(
        session,
        bar_close=BAR_CLOSE + timedelta(minutes=15),
        leader=alert,
        status=E.STATUS_CANCELLED,
        status_reason='cvd_divergence_against_direction',
    )
    abstained = make_candidate(
        session,
        bar_close=BAR_CLOSE + timedelta(minutes=30),
        leader=alert,
        status=E.STATUS_ABSTAINED,
        status_reason='expected_r_lower_bound_below_cost',
    )
    assert {cancelled.sample_group_id, abstained.sample_group_id} == {alert.sample_group_id}
    assert count_independent(session) == 1


def test_replay_rows_count_separately_from_live(session):
    live = make_candidate(session, source=E.SOURCE_LIVE)
    replay = make_candidate(session, source=E.SOURCE_REPLAY)
    assert count_independent(session, source=E.SOURCE_LIVE) == 1
    assert count_independent(session, source=E.SOURCE_REPLAY) == 1
    assert live.sample_group_id != replay.sample_group_id


# ---------------------------------------------------------------------------
# What the database refuses
# ---------------------------------------------------------------------------
def test_a_leader_may_not_carry_a_different_group_id(session):
    leader = make_candidate(session)
    with pytest.raises(IntegrityError):
        make_candidate(
            session,
            bar_close=BAR_CLOSE + timedelta(hours=1),
            leader=leader,
            sample_group_id=uuid.uuid4(),
        )


def test_a_group_may_not_span_horizons(session):
    leader = make_candidate(session, horizon=E.HORIZONS[1])
    with pytest.raises(IntegrityError):
        make_candidate(session, horizon=E.HORIZONS[0], leader=leader)


def test_a_group_may_not_span_live_and_replay(session):
    leader = make_candidate(session, source=E.SOURCE_LIVE)
    with pytest.raises(IntegrityError):
        make_candidate(session, source=E.SOURCE_REPLAY, leader=leader)


def test_a_leader_must_exist(session):
    with pytest.raises(IntegrityError):
        make_candidate(
            session,
            sample_group_leader_id=10_000_000,
            sample_group_reason=E.SAMPLE_GROUP_OVERLAP,
        )


def test_reason_new_may_not_name_a_leader(session):
    leader = make_candidate(session)
    with pytest.raises(IntegrityError):
        make_candidate(
            session,
            bar_close=BAR_CLOSE + timedelta(hours=1),
            leader=leader,
            sample_group_reason=E.SAMPLE_GROUP_NEW,
        )


def test_an_inherited_group_must_name_a_leader(session):
    with pytest.raises(IntegrityError):
        make_candidate(session, sample_group_reason=E.SAMPLE_GROUP_OVERLAP)


def test_unknown_reason_is_refused(session):
    with pytest.raises(IntegrityError):
        make_candidate(session, sample_group_reason='because_i_said_so')


# ---------------------------------------------------------------------------
# The shape M8 will actually query
# ---------------------------------------------------------------------------
def test_one_row_per_group_by_earliest_decision_time(session):
    first = make_candidate(session, bar_close=BAR_CLOSE)
    make_candidate(session, bar_close=BAR_CLOSE + timedelta(hours=1), leader=first)
    make_candidate(session, bar_close=BAR_CLOSE + timedelta(hours=2), leader=first)
    independent = make_candidate(session, bar_close=BAR_CLOSE + timedelta(days=9))

    ranked = select(
        Candidate.id,
        func.row_number()
        .over(partition_by=Candidate.sample_group_id, order_by=(Candidate.decision_time, Candidate.id))
        .label('seq'),
    ).subquery()
    primaries = session.execute(select(ranked.c.id).where(ranked.c.seq == 1)).scalars().all()

    assert set(primaries) == {first.id, independent.id}
    assert len(primaries) == count_independent(session)
