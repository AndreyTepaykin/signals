"""The options job against Postgres: write-once, as_of gating, staleness, the empty floor, shadow rows, CHECKs, replay.

Candidates are built directly (a feature row plus a candidate whose pre-options prices match
``stop_plan``): long BTC reference 96,500, stop 95,000, 1R = 1,500, model target 98,000, ATR 1,000. Books are inserted
directly so each test controls exactly one structural fact.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core import constants as C
from app.models import enums as E
from app.models.decisions import Candidate
from app.models.options import OptionsMetric, OptionsSnapshot
from app.worker import options_job as OJ
from tests.conftest import make_candidate
from tests.options_helpers import instrument_name
from tests.rules_helpers import PASSING_COLUMNS, T0, short_overrides

DECISION = T0 + timedelta(seconds=40)
D = Decimal


@pytest.fixture
def floor(monkeypatch):
    """A 100-coin floor, so the small test books clear it; the shipped floors are far larger."""
    monkeypatch.setattr(C, 'OPTIONS_OI_FLOOR_BY_COIN', {'BTCUSDT': 100.0, 'ETHUSDT': 100.0})


def candidate(session, *, coin='BTCUSDT', horizon='swing', direction='long', status='watch', reason=C.STATUS_REASON_PENDING_OPTIONS_CHECK,
              source=E.SOURCE_LIVE, decision_time=DECISION, bar_close=T0, columns=None) -> Candidate:
    cols: dict[str, Any] = {**PASSING_COLUMNS, **(columns or {})}
    if direction == 'short':
        cols.update(short_overrides())
    stop = cols['chandelier_stop_price']
    target = D('98000') if direction == 'long' else D('95000')
    return make_candidate(
        session, coin=coin, horizon=horizon, bar_close=bar_close, source=source, direction=direction, decision_time=decision_time,
        feature_overrides=cols, pre_options_stop_price=stop, pre_options_target_price=target, stop_price=stop,
        options_verdict=None, status=status, status_reason=reason, vertical_deadline=None,
    )


def add_book(session, *, coin='BTCUSDT', snapshot_time=None, available_at=None, total_oi=1000.0, near_oi=1000.0, expiry_hours=None,
             gamma_sign=None, flip=None, max_pain=None, top_share=0.05, iv_pct=None, strikes=()) -> OptionsSnapshot:
    """A snapshot plus one metric row per horizon. ``strikes`` = ``(strike, call_oi, put_oi)`` expiring tomorrow 08:00."""
    snapshot_time = snapshot_time or (DECISION - timedelta(minutes=5))
    available_at = available_at or snapshot_time + timedelta(seconds=10)
    tomorrow = (snapshot_time + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    instruments = []
    for strike, call_oi, put_oi in strikes:
        for side, oi in (('call', call_oi), ('put', put_oi)):
            if oi > 0:
                instruments.append({'instrument_name': instrument_name('BTC', tomorrow, strike, side), 'open_interest': oi, 'mark_iv': 50.0, 'underlying_price': 96500.0})
    snapshot = OptionsSnapshot(
        venue='deribit', coin=coin, snapshot_time=snapshot_time, available_at=available_at, instrument_count=len(instruments),
        total_open_interest=D(repr(total_oi)), payload={'currency': 'BTC', 'index_price': 96500.0, 'instruments': instruments},
    )
    session.add(snapshot)
    session.flush()
    hours = {'scalp': 20.0, 'swing': 20.0, 'position': 68.0}
    if expiry_hours is not None:
        hours = {h: expiry_hours.get(h, hours[h]) for h in hours} if isinstance(expiry_hours, dict) else {h: expiry_hours for h in hours}
    for horizon in C.HORIZONS:
        session.add(OptionsMetric(
            snapshot_id=snapshot.id, venue='deribit', coin=coin, snapshot_time=snapshot_time, horizon=horizon, available_at=available_at,
            dealer_gamma_sign=gamma_sign, dealer_gamma_estimated=True, dealer_gamma_proxy=C.OPTIONS_GAMMA_PROXY,
            gamma_flip_price=None if flip is None else D(repr(flip)), max_pain_strike=None if max_pain is None else D(repr(max_pain)),
            top_strike_oi_share=D(repr(top_share)), iv_minus_rv_pct=None if iv_pct is None else D(repr(iv_pct)),
            expiry_time=snapshot_time + timedelta(hours=hours[horizon]), hours_to_expiry=D(repr(hours[horizon])),
            total_open_interest=D(repr(total_oi)), near_dated_open_interest=D(repr(near_oi)),
        ))
    session.flush()
    return snapshot


def metric(session, snapshot: OptionsSnapshot, horizon='swing') -> OptionsMetric:
    return session.execute(select(OptionsMetric).where(OptionsMetric.snapshot_id == snapshot.id, OptionsMetric.horizon == horizon)).scalar_one()


def run(session, **kwargs) -> OJ.OptionsReport:
    report = OJ.run_options_job(session, **kwargs)
    session.expire_all()
    return report


def fresh(session, cand: Candidate) -> Candidate:
    session.refresh(cand)
    return cand


# ---------------------------------------------------------------------------
# what a checked candidate looks like
# ---------------------------------------------------------------------------
def test_an_aligned_candidate_becomes_an_alert_with_the_options_columns_written_once(session, floor):
    cand = candidate(session)
    snap = add_book(session)
    report = run(session)
    assert report.checked == 1
    row = fresh(session, cand)
    assert (row.options_verdict, row.status, row.status_reason) == ('align', 'alert', None)
    assert (row.options_venue, row.options_expression, row.options_vol_tag) == ('deribit', 'perp', None)
    assert (row.size_multiplier, row.options_influence) == (D('1'), D('0'))
    assert row.options_confidence == D('1')  # 1,000 / (2 x 100), capped
    assert row.options_metric_id == metric(session, snap).id
    assert row.options_snapshot_time == snap.snapshot_time
    assert (row.stop_price, row.target_cap_price) == (D('95000'), None)
    assert row.pre_options_stop_price == D('95000') and row.pre_options_target_price == D('98000')  # the M4 layer is never touched
    for name in ('entry_price', 'one_r', 'upper_barrier_price', 'lower_barrier_price', 'vertical_deadline', 'p_upper_before_stop', 'expected_r'):
        assert getattr(row, name) is None, name  # M6 / M8 own these


def test_the_options_columns_are_written_exactly_once(session, floor):
    cand = candidate(session)
    add_book(session)
    assert run(session).checked == 1
    first = fresh(session, cand).options_snapshot_time
    add_book(session, snapshot_time=DECISION - timedelta(minutes=1), gamma_sign=-1, flip=96600.0)  # a newer, different book
    again = run(session)
    assert again.outcomes == []  # nothing is unchecked any more
    row = fresh(session, cand)
    assert row.options_verdict == 'align' and row.options_snapshot_time == first


def test_a_stale_in_memory_candidate_cannot_overwrite_a_verdict(session, floor):
    cand = candidate(session)
    add_book(session)
    assert OJ.check_candidate(session, cand).result == OJ.RESULT_CHECKED
    add_book(session, snapshot_time=DECISION - timedelta(minutes=1), gamma_sign=1)
    assert OJ.check_candidate(session, cand).result == OJ.RESULT_RACE  # the guarded UPDATE matched no row
    assert fresh(session, cand).options_verdict == 'align'


def test_dry_run_decides_but_writes_nothing(session, floor):
    cand = candidate(session)
    add_book(session)
    report = run(session, dry_run=True)
    assert [o.result for o in report.outcomes] == [OJ.RESULT_DRY] and report.outcomes[0].verdict == 'align'
    assert fresh(session, cand).options_verdict is None


def test_a_candidate_whose_price_layer_no_longer_matches_its_feature_row_is_left_alone(session, floor):
    cand = candidate(session)
    add_book(session)
    session.execute(update(Candidate).where(Candidate.id == cand.id).values(pre_options_stop_price=D('94000'), stop_price=D('94000')))
    report = run(session)
    assert [o.result for o in report.outcomes] == [OJ.RESULT_PRICE_MISMATCH]
    assert fresh(session, cand).options_verdict is None


# ---------------------------------------------------------------------------
# ignore: empty floor, staleness, no snapshot, no book for the coin
# ---------------------------------------------------------------------------
def test_with_the_floor_unset_a_covered_coin_never_alerts(session, monkeypatch):
    monkeypatch.setattr(C, 'OPTIONS_OI_FLOOR_BY_COIN', {})
    cand = candidate(session)
    add_book(session, total_oi=10_000_000.0)  # an enormous book is still not "liquid" without a floor to compare to
    run(session)
    row = fresh(session, cand)
    assert (row.options_verdict, row.status, row.status_reason) == ('ignore', 'watch', 'options_ignored:oi_floor_unset')
    assert row.options_confidence is None and row.options_metric_id is not None
    assert (row.size_multiplier, row.stop_price, row.target_cap_price, row.options_influence) == (D('1'), D('95000'), None, D('0'))


def test_a_stale_snapshot_leads_to_no_alert(session, floor):
    cand = candidate(session)
    add_book(session, snapshot_time=DECISION - timedelta(minutes=16))
    run(session)
    row = fresh(session, cand)
    assert (row.options_verdict, row.status, row.status_reason) == ('ignore', 'watch', 'options_ignored:snapshot_stale')


def test_no_snapshot_at_all_leads_to_no_alert_on_a_covered_coin(session, floor):
    cand = candidate(session)
    run(session)
    row = fresh(session, cand)
    assert (row.options_verdict, row.status, row.status_reason) == ('ignore', 'watch', 'options_ignored:no_snapshot')
    assert row.options_venue == 'deribit' and row.options_metric_id is None


def test_an_alt_without_a_deribit_book_proceeds_to_alert(session, floor):
    cand = candidate(session, coin='SOLUSDT')
    run(session)
    row = fresh(session, cand)
    assert (row.options_verdict, row.status, row.status_reason) == ('ignore', 'alert', 'options_ignored:no_options_book')
    assert row.options_venue is None and row.options_metric_id is None
    assert (row.size_multiplier, row.stop_price, row.target_cap_price) == (D('1'), D('95000'), None)


def test_a_book_below_the_floor_is_ignored(session, floor):
    cand = candidate(session)
    add_book(session, total_oi=99.0)
    run(session)
    assert fresh(session, cand).status_reason == 'options_ignored:oi_below_floor'


# ---------------------------------------------------------------------------
# as_of gating
# ---------------------------------------------------------------------------
def test_a_snapshot_that_arrived_after_the_decision_does_not_exist_for_the_candidate(session, floor):
    cand = candidate(session)
    add_book(session, snapshot_time=DECISION - timedelta(minutes=1), available_at=DECISION + timedelta(seconds=1), gamma_sign=-1, flip=96600.0)
    run(session)
    row = fresh(session, cand)
    assert row.options_verdict == 'ignore' and row.status_reason == 'options_ignored:no_snapshot'  # not 'amplify'


def test_the_newest_visible_book_is_used_and_a_later_one_is_not(session, floor):
    cand = candidate(session)
    older = add_book(session, snapshot_time=DECISION - timedelta(minutes=10))
    newer = add_book(session, snapshot_time=DECISION - timedelta(minutes=2), gamma_sign=1)
    add_book(session, snapshot_time=DECISION - timedelta(seconds=30), available_at=DECISION + timedelta(minutes=1), gamma_sign=-1, flip=96600.0)
    run(session)
    row = fresh(session, cand)
    assert row.options_metric_id == metric(session, newer).id != metric(session, older).id
    assert (row.options_verdict, row.options_dealer_gamma_sign) == ('dampen', 1)


def test_each_horizon_reads_its_own_nearest_expiry(session, floor):
    swing = candidate(session, horizon='swing')
    position = candidate(session, horizon='position')
    add_book(session, expiry_hours={'swing': 20.0, 'position': 68.0}, max_pain=97000.0, top_share=0.20)
    run(session)
    assert fresh(session, swing).options_verdict == 'expiry_pin'  # settles in 20h
    assert fresh(session, position).options_verdict == 'align'  # its expiry is 68h away, outside the 48h window


# ---------------------------------------------------------------------------
# each structural verdict end to end (and the price-layer CHECKs)
# ---------------------------------------------------------------------------
def test_expiry_pin_delays_and_caps_the_target_at_max_pain(session, floor):
    cand = candidate(session)
    add_book(session, expiry_hours=20.0, max_pain=97000.0, top_share=0.20)
    run(session)
    row = fresh(session, cand)
    assert (row.options_verdict, row.status, row.status_reason, row.options_expression) == ('expiry_pin', 'delayed', 'options:expiry_pin', 'wait_for_expiry')
    assert row.target_cap_price == D('97000') and row.stop_price == D('95000') and row.options_hours_to_expiry is not None


def test_a_wall_in_the_path_caps_the_target_and_a_close_wall_cancels(session, floor):
    far = candidate(session, horizon='swing')
    near = candidate(session, horizon='scalp')
    # wall at 97,500 is 1,000 above the entry (0.667R); the SAME strike set serves both, so give the near one its own book
    add_book(session, strikes=((97500.0, 150.0, 0.0),))
    run(session, coins=['BTCUSDT'])
    assert fresh(session, far).target_cap_price == D('97500') and fresh(session, far).status == 'alert'
    assert fresh(session, near).target_cap_price == D('97500')  # same book, same room
    close = candidate(session, horizon='position', decision_time=DECISION + timedelta(hours=1), bar_close=T0 + timedelta(hours=1))
    add_book(session, snapshot_time=DECISION + timedelta(minutes=55), strikes=((96700.0, 0.0, 150.0),))  # 200 above the entry: 0.13R
    run(session)
    row = fresh(session, close)
    assert (row.options_verdict, row.status, row.status_reason) == ('headwind', 'cancelled', 'options:headwind_room')
    assert row.target_cap_price == D('96700')


def test_amplify_widens_the_stop_and_the_lower_barrier_check_follows_the_stop(session, floor):
    cand = candidate(session)
    add_book(session, gamma_sign=-1, flip=96800.0)
    run(session)
    row = fresh(session, cand)
    assert (row.options_verdict, row.status, row.size_multiplier) == ('amplify', 'alert', D('1.15'))
    assert row.stop_price == D('94500') and row.pre_options_stop_price == D('95000')  # 95,000 - 0.5 ATR
    assert row.options_dealer_gamma_sign == -1 and row.options_influence == D('1')
    with pytest.raises(IntegrityError):  # "lower_barrier_price = stop_price": the old, narrower stop is refused
        with session.begin_nested():
            session.execute(update(Candidate).where(Candidate.id == cand.id).values(lower_barrier_price=D('95000')))
    session.execute(update(Candidate).where(Candidate.id == cand.id).values(lower_barrier_price=D('94500')))


def test_dampen_caps_the_target_and_downsizes(session, floor):
    cand = candidate(session)
    add_book(session, gamma_sign=1)
    run(session)
    row = fresh(session, cand)
    assert (row.options_verdict, row.status, row.size_multiplier, row.target_cap_price) == ('dampen', 'alert', D('0.7'), D('98000'))
    assert row.options_influence == D('-1')


def test_cheap_vol_is_recorded_beside_the_verdict(session, floor):
    cand = candidate(session)
    add_book(session, iv_pct=0.1)
    run(session)
    row = fresh(session, cand)
    assert (row.options_verdict, row.options_vol_tag, row.options_expression) == ('align', 'vol_cheap', 'defined_risk_option')


def test_a_short_candidate_mirrors_the_long_one(session, floor):
    cand = candidate(session, direction='short')
    add_book(session, gamma_sign=-1, flip=96300.0)
    run(session)
    row = fresh(session, cand)
    assert row.options_verdict == 'amplify' and row.stop_price == D('98500') and row.pre_options_stop_price == D('98000')


# ---------------------------------------------------------------------------
# shadow rows (M4) get the fields, never a new status
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ('status', 'reason', 'columns'),
    [
        ('watch', 'vote_conflict', None),
        ('cancelled', 'cvd_divergence_against', {'cvd_divergence': True, 'cvd_divergence_against': 'long'}),
        ('cancelled', 'stand_down:event_window', {'event_flag': True}),
    ],
)
def test_shadow_rows_get_the_verdict_and_prices_but_keep_their_status(session, floor, status, reason, columns):
    cand = candidate(session, status=status, reason=reason, columns=columns)
    add_book(session, gamma_sign=-1, flip=96800.0)
    run(session)
    row = fresh(session, cand)
    assert row.options_verdict == 'amplify' and row.stop_price == D('94500')  # resolves on the stop it would have had
    assert (row.status, row.status_reason) == (status, reason)
    assert row.options_metric_id is not None


def test_a_shadow_row_with_a_stale_book_keeps_its_reason_not_the_ignore_reason(session, floor):
    cand = candidate(session, status='watch', reason='vote_conflict')
    run(session)  # no book
    row = fresh(session, cand)
    assert (row.options_verdict, row.status, row.status_reason) == ('ignore', 'watch', 'vote_conflict')


# ---------------------------------------------------------------------------
# the candidate CHECKs
# ---------------------------------------------------------------------------
def test_a_row_still_carrying_the_pending_marker_may_not_have_a_verdict(session):
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            candidate(session).options_verdict = 'align'
            session.flush()


def test_no_alert_may_lack_a_verdict_and_no_checked_row_keeps_the_marker(session, floor):
    candidate(session)
    candidate(session, coin='SOLUSDT')
    add_book(session)
    run(session)
    rows = session.execute(select(Candidate)).scalars().all()
    assert all(r.status_reason != C.STATUS_REASON_PENDING_OPTIONS_CHECK for r in rows)
    for r in rows:
        assert r.status != 'alert' or r.options_verdict is not None
        assert r.options_verdict == 'ignore' or r.options_metric_id is not None  # every non-ignore verdict names its metric row
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(update(Candidate).values(options_influence=D('1.5')))
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(update(Candidate).values(size_multiplier=D('0')))


# ---------------------------------------------------------------------------
# live equals replay; no wall clock
# ---------------------------------------------------------------------------
COLUMNS = ('options_verdict', 'options_venue', 'options_vol_tag', 'options_dealer_gamma_sign', 'options_nearest_path_strike', 'options_hours_to_expiry',
           'options_expression', 'size_multiplier', 'options_influence', 'options_confidence', 'options_metric_id', 'options_snapshot_time',
           'stop_price', 'target_cap_price', 'status', 'status_reason')


def values(row: Candidate) -> tuple:
    return tuple(getattr(row, name) for name in COLUMNS)


def test_a_live_candidate_and_a_replay_candidate_with_the_same_inputs_get_the_same_decision(session, floor):
    live = candidate(session, source=E.SOURCE_LIVE)
    replay = candidate(session, source=E.SOURCE_REPLAY)  # same bar, same decision instant, same books
    add_book(session, gamma_sign=-1, flip=96800.0, iv_pct=0.9, strikes=((97500.0, 150.0, 0.0),))
    run(session)
    assert values(fresh(session, live)) == values(fresh(session, replay))
    assert fresh(session, live).options_verdict == 'headwind' and fresh(session, live).options_vol_tag == 'vol_rich'


def test_the_decision_does_not_depend_on_when_the_job_runs_or_in_what_order(session, floor, monkeypatch):
    import app.core.time as core_time

    def boom() -> datetime:
        raise AssertionError('the options job read the wall clock')

    monkeypatch.setattr(core_time, 'utc_now', boom)
    first = candidate(session, horizon='swing')
    second = candidate(session, horizon='scalp')
    add_book(session, gamma_sign=1)
    OJ.check_candidate(session, second)
    OJ.check_candidate(session, first)
    session.expire_all()
    a, b = fresh(session, first), fresh(session, second)
    assert (a.options_verdict, b.options_verdict) == ('dampen', 'dampen')
    assert a.options_snapshot_time == b.options_snapshot_time


def test_the_pure_options_code_and_the_job_never_read_a_clock():
    root = Path(OJ.__file__).resolve().parents[1]
    paths = [Path(OJ.__file__), *sorted((root / 'core' / 'options').glob('*.py'))]
    banned = {'utc_now', 'now', 'today', 'utcnow', 'time', 'monotonic', 'perf_counter', 'sleep'}
    offenders = []
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', '')
                if name in banned:
                    offenders.append(f'{path.name}:{node.lineno} {name}')
    assert offenders == []


def test_every_feed_read_in_the_options_job_goes_through_as_of():
    source = Path(OJ.__file__).read_text(encoding='utf-8')
    assert 'select(OptionsMetric)' in source and 'reader.select(' in source
    assert 'sa_select(OptionsMetric)' not in source and 'sa_select(OptionsSnapshot)' not in source
