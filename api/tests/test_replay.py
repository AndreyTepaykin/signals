"""M7 replay harness: live/replay parity, raw-feed e2e, lookahead, idempotency, dry-run."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import sessionmaker

from app.core import constants as C
from app.core.features import columns
from app.core.hashing import rule_hash as current_rule_hash
from app.models import enums as E
from app.models.decisions import Candidate
from app.models.features import FeatureRow
from app.models.feeds import Candle, Funding, OpenInterest
from app.models.labels import Label
from app.worker import feature_job as FJ
from app.worker import label_job as LJ
from app.worker import options_job as OJ
from app.worker import replay as RP
from app.worker import rules_job as RJ
from tests.rules_helpers import T0, add_feature_rows
from tests.test_feature_job import BAR_CLOSE, POLICY, store_market, stored_mapping
from tests.test_options_job import run as run_options
from feature_helpers import synthetic_inputs

UTC = timezone.utc
M15 = timedelta(minutes=15)
SETTLE = timedelta(seconds=30)
BAR = M15


@pytest.fixture
def floor(monkeypatch):
    monkeypatch.setattr(C, 'OPTIONS_OI_FLOOR_BY_COIN', {'BTCUSDT': 100.0, 'ETHUSDT': 100.0})


DECISION_FIELDS = (
    'direction',
    'trigger',
    'setup_family',
    'trend_family',
    'status',
    'status_reason',
    'pre_options_stop_price',
    'pre_options_target_price',
    'stop_price',
    'locked_book',
    'rule_hash',
    'feature_hash',
)

CAND_FILL_FIELDS = ('entry_price', 'one_r', 'upper_barrier_price', 'lower_barrier_price', 'vertical_deadline')

LABEL_OUTCOME_FIELDS = (
    'first_barrier',
    'fill_time',
    'fill_price',
    'resolved_at',
    'price_r',
    'fees_r',
    'funding_r',
    'stored_r',
    'mae_r',
    'mfe_r',
    'barrier_ambiguous',
    'gapped_stop',
    'funding_partial',
    'feature_hash',
    'rule_hash',
)


def _feature_columns(row: FeatureRow) -> dict:
    out = {}
    for name in columns.FEATURE_COLUMNS:
        value = getattr(row, name)
        if isinstance(value, Decimal):
            value = float(value)
        out[name] = value
    return out


def _run_rules(session, *, source: str, bar=T0, now=None):
    kwargs: dict = dict(source=source, feed_breaches={}, code_version='testver')
    if source == E.SOURCE_LIVE:
        kwargs['now'] = now or bar + timedelta(seconds=40)
    return RJ.run_rules_for_bar(session, ['BTCUSDT'], bar, **kwargs)


def add_perp_bar(session, index: int, o, h, l, c, *, signal_bar: datetime = T0, coin='BTCUSDT', available_at=None):
    opened = signal_bar + index * BAR
    row = Candle(
        coin=coin,
        market=E.MARKET_PERP,
        timeframe='15m',
        open_time=opened,
        close_time=opened + BAR,
        open_price=Decimal(str(o)),
        high_price=Decimal(str(h)),
        low_price=Decimal(str(l)),
        close_price=Decimal(str(c)),
        base_volume=Decimal('10'),
        taker_buy_base_volume=Decimal('5'),
        available_at=available_at if available_at is not None else opened + BAR + SETTLE,
    )
    session.add(row)
    session.flush()
    return row


def add_funding_at(session, funding_time: datetime, rate=0.0, *, available_at: datetime | None = None, coin='BTCUSDT'):
    session.add(
        Funding(
            coin=coin,
            kind=E.FUNDING_SETTLED,
            funding_time=funding_time,
            funding_rate=Decimal(str(rate)),
            interval_hours=8,
            mark_price=Decimal('100'),
            available_at=available_at if available_at is not None else funding_time + SETTLE,
        )
    )
    session.flush()


def _funding_grid(session, signal_bar: datetime, *, coin='BTCUSDT'):
    when = signal_bar - timedelta(days=2)
    while when <= signal_bar + timedelta(days=2):
        add_funding_at(session, when, coin=coin)
        when += timedelta(hours=8)


def _seed_btc_candidates(session, *, signal_bar: datetime = T0, shared_hash: str | None = None):
    h = shared_hash or ('b' * 64)
    add_feature_rows(session, 'BTCUSDT', signal_bar, source=E.SOURCE_LIVE, horizons=('scalp',), feature_hash=h)
    add_feature_rows(session, 'BTCUSDT', signal_bar, source=E.SOURCE_REPLAY, horizons=('scalp',), feature_hash=h)
    RJ.run_rules_for_bar(session, ['BTCUSDT'], signal_bar, source=E.SOURCE_LIVE, now=signal_bar + timedelta(seconds=40), feed_breaches={}, code_version='testver')
    RJ.run_rules_for_bar(session, ['BTCUSDT'], signal_bar, source=E.SOURCE_REPLAY, decision_time=signal_bar, feed_breaches={}, code_version='testver')
    from tests.test_options_job import add_book

    add_book(session, snapshot_time=signal_bar - timedelta(minutes=5))
    OJ.run_options_job(session, source=E.SOURCE_LIVE, coins=['BTCUSDT'])
    OJ.run_options_job(session, source=E.SOURCE_REPLAY, coins=['BTCUSDT'])
    session.flush()


def _compare_label_parity(session, *, signal_bar: datetime, label_at: datetime, setup_bars):
    _seed_btc_candidates(session, signal_bar=signal_bar)
    setup_bars(session, signal_bar)
    _funding_grid(session, signal_bar)
    LJ.run_label_job(session, label_at, source=E.SOURCE_LIVE, coins=['BTCUSDT'], code_version='testver')
    LJ.run_label_job(session, label_at, source=E.SOURCE_REPLAY, coins=['BTCUSDT'], code_version='testver')
    live_c = session.execute(select(Candidate).where(Candidate.source == E.SOURCE_LIVE)).scalar_one()
    replay_c = session.execute(select(Candidate).where(Candidate.source == E.SOURCE_REPLAY)).scalar_one()
    live_l = session.execute(select(Label).where(Label.candidate_id == live_c.id)).scalar_one()
    replay_l = session.execute(select(Label).where(Label.candidate_id == replay_c.id)).scalar_one()
    for name in CAND_FILL_FIELDS:
        assert getattr(live_c, name) == getattr(replay_c, name), name
    for name in LABEL_OUTCOME_FIELDS:
        assert getattr(live_l, name) == getattr(replay_l, name), name
    assert live_c.status_reason == replay_c.status_reason


# ---------------------------------------------------------------------------
# Live / replay parity (features, candidates, options)
# ---------------------------------------------------------------------------
def test_identical_feeds_give_identical_feature_rows_live_and_replay(session):
    market = synthetic_inputs(days=4, end=BAR_CLOSE)
    store_market(session, market)
    bar = BAR_CLOSE
    live_now = bar + timedelta(seconds=40)
    FJ.assemble_feature_rows(session, 'BTCUSDT', bar, source=E.SOURCE_LIVE, now=live_now, observed_at=live_now, policy=POLICY, code_version='t')
    FJ.assemble_feature_rows(session, 'BTCUSDT', bar, source=E.SOURCE_REPLAY, now=bar, policy=POLICY, code_version='t')
    live_rows = session.execute(select(FeatureRow).where(FeatureRow.source == E.SOURCE_LIVE).order_by(FeatureRow.horizon)).scalars().all()
    replay_rows = session.execute(select(FeatureRow).where(FeatureRow.source == E.SOURCE_REPLAY).order_by(FeatureRow.horizon)).scalars().all()
    assert len(live_rows) == len(replay_rows) == 3
    for live, replay in zip(live_rows, replay_rows, strict=True):
        assert live.feature_hash == replay.feature_hash
        assert _feature_columns(live) == _feature_columns(replay)


def test_identical_features_give_identical_candidates_except_source_and_decision_time(session):
    shared_hash = 'a' * 64
    add_feature_rows(session, 'BTCUSDT', T0, source=E.SOURCE_LIVE, horizons=('swing',), feature_hash=shared_hash)
    add_feature_rows(session, 'BTCUSDT', T0, source=E.SOURCE_REPLAY, horizons=('swing',), feature_hash=shared_hash)
    _run_rules(session, source=E.SOURCE_LIVE)
    _run_rules(session, source=E.SOURCE_REPLAY)
    live = session.execute(select(Candidate).where(Candidate.source == E.SOURCE_LIVE)).scalar_one()
    replay = session.execute(select(Candidate).where(Candidate.source == E.SOURCE_REPLAY)).scalar_one()
    for name in DECISION_FIELDS:
        assert getattr(live, name) == getattr(replay, name), name
    assert live.source == E.SOURCE_LIVE and replay.source == E.SOURCE_REPLAY
    assert live.decision_time == T0 + timedelta(seconds=40)
    assert replay.decision_time == T0


def test_options_no_snapshot_live_vs_no_options_history_replay(session, floor):
    add_feature_rows(session, 'BTCUSDT', T0, source=E.SOURCE_LIVE, horizons=('swing',))
    add_feature_rows(session, 'BTCUSDT', T0, source=E.SOURCE_REPLAY, horizons=('swing',))
    _run_rules(session, source=E.SOURCE_LIVE)
    _run_rules(session, source=E.SOURCE_REPLAY)
    run_options(session, source=E.SOURCE_LIVE)
    run_options(session, source=E.SOURCE_REPLAY)
    live = session.execute(select(Candidate).where(Candidate.source == E.SOURCE_LIVE)).scalar_one()
    replay = session.execute(select(Candidate).where(Candidate.source == E.SOURCE_REPLAY)).scalar_one()
    assert live.status_reason == 'options_ignored:no_snapshot'
    assert replay.status_reason == 'options_ignored:no_options_history'


# ---------------------------------------------------------------------------
# Label parity (SOLUSDT: same options path on both sources)
# ---------------------------------------------------------------------------
def test_label_parity_stop_hit(session, floor):
    label_at = T0 + timedelta(days=1)

    def setup(session, signal_bar):
        add_perp_bar(session, 0, 96500, 96600, 96400, 96500, signal_bar=signal_bar)
        add_perp_bar(session, 1, 96500, 96600, 94900, 95000, signal_bar=signal_bar)

    _compare_label_parity(session, signal_bar=T0, label_at=label_at, setup_bars=setup)
    live_l = session.execute(
        select(Label).join(Candidate, Label.candidate_id == Candidate.id).where(Candidate.source == E.SOURCE_LIVE)
    ).scalar_one()
    assert live_l.first_barrier == E.BARRIER_LOWER


def test_label_parity_target_hit(session, floor):
    label_at = T0 + timedelta(days=1)

    def setup(session, signal_bar):
        add_perp_bar(session, 0, 96500, 96600, 96400, 96500, signal_bar=signal_bar)
        add_perp_bar(session, 1, 96500, 98100, 96400, 98000, signal_bar=signal_bar)

    _compare_label_parity(session, signal_bar=T0, label_at=label_at, setup_bars=setup)
    live_l = session.execute(
        select(Label).join(Candidate, Label.candidate_id == Candidate.id).where(Candidate.source == E.SOURCE_LIVE)
    ).scalar_one()
    assert live_l.first_barrier == E.BARRIER_UPPER


def test_label_parity_vertical_timeout(session, floor):
    label_at = T0 + timedelta(hours=5)
    scalp_bars = 16

    def setup(session, signal_bar):
        for i in range(scalp_bars - 1):
            add_perp_bar(session, i, 96500, 96600, 96400, 96500, signal_bar=signal_bar)
        add_perp_bar(session, scalp_bars - 1, 96500, 96600, 96400, 96400, signal_bar=signal_bar)

    _compare_label_parity(session, signal_bar=T0, label_at=label_at, setup_bars=setup)
    live_l = session.execute(
        select(Label).join(Candidate, Label.candidate_id == Candidate.id).where(Candidate.source == E.SOURCE_LIVE)
    ).scalar_one()
    assert live_l.first_barrier == E.BARRIER_VERTICAL


# ---------------------------------------------------------------------------
# Lookahead: event time at/before bar, available_at after
# ---------------------------------------------------------------------------
def _replay_swing_hash(session, bar: datetime) -> str:
    FJ.assemble_feature_rows(session, 'BTCUSDT', bar, source=E.SOURCE_REPLAY, now=bar, policy=POLICY, code_version='t')
    return session.scalar(select(FeatureRow.feature_hash).where(FeatureRow.source == E.SOURCE_REPLAY, FeatureRow.horizon == 'swing', FeatureRow.bar_close == bar))


def test_lookahead_late_oi_does_not_change_features_until_visible(session):
    store_market(session, synthetic_inputs(days=4, end=BAR_CLOSE))
    h0 = _replay_swing_hash(session, BAR_CLOSE)
    late_avail = BAR_CLOSE + timedelta(hours=1)
    session.execute(
        insert(OpenInterest),
        [
            dict(
                coin='BTCUSDT',
                timeframe='1h',
                poll_time=BAR_CLOSE - timedelta(minutes=1),
                open_interest_base=Decimal('888888'),
                available_at=late_avail,
            )
        ],
    )
    session.flush()
    session.execute(delete(FeatureRow).where(FeatureRow.source == E.SOURCE_REPLAY, FeatureRow.bar_close == BAR_CLOSE))
    assert _replay_swing_hash(session, BAR_CLOSE) == h0
    visible_bar = late_avail + timedelta(minutes=15)
    session.execute(delete(FeatureRow).where(FeatureRow.source == E.SOURCE_REPLAY, FeatureRow.bar_close == visible_bar))
    assert _replay_swing_hash(session, visible_bar) != h0


def test_lookahead_late_funding_print_is_invisible_then_visible(session):
    store_market(session, synthetic_inputs(days=4, end=BAR_CLOSE))
    h0 = _replay_swing_hash(session, BAR_CLOSE)
    ft = BAR_CLOSE - timedelta(seconds=1)
    session.execute(
        insert(Funding),
        [
            dict(
                coin='BTCUSDT',
                kind=E.FUNDING_SETTLED,
                funding_time=ft,
                funding_rate=Decimal('0.42'),
                interval_hours=8,
                available_at=BAR_CLOSE + timedelta(minutes=30),
            )
        ],
    )
    session.flush()
    session.execute(delete(FeatureRow).where(FeatureRow.source == E.SOURCE_REPLAY, FeatureRow.bar_close == BAR_CLOSE))
    assert _replay_swing_hash(session, BAR_CLOSE) == h0
    visible_bar = BAR_CLOSE + timedelta(hours=1)
    session.execute(delete(FeatureRow).where(FeatureRow.source == E.SOURCE_REPLAY, FeatureRow.bar_close == visible_bar))
    assert _replay_swing_hash(session, visible_bar) != h0


def test_lookahead_late_1h_candle_is_invisible_then_visible(session):
    store_market(session, synthetic_inputs(days=4, end=BAR_CLOSE))
    h0 = _replay_swing_hash(session, BAR_CLOSE)
    late_avail = BAR_CLOSE + timedelta(minutes=45)
    session.execute(
        insert(Candle),
        [
            dict(
                coin='BTCUSDT',
                market=E.MARKET_PERP,
                timeframe='1h',
                open_time=BAR_CLOSE - timedelta(hours=1, minutes=1),
                close_time=BAR_CLOSE - timedelta(minutes=1),
                open_price=Decimal('80000'),
                high_price=Decimal('81000'),
                low_price=Decimal('79000'),
                close_price=Decimal('80500'),
                base_volume=Decimal('1'),
                taker_buy_base_volume=Decimal('0.5'),
                available_at=late_avail,
            )
        ],
    )
    session.flush()
    session.execute(delete(FeatureRow).where(FeatureRow.source == E.SOURCE_REPLAY, FeatureRow.bar_close == BAR_CLOSE))
    assert _replay_swing_hash(session, BAR_CLOSE) == h0
    visible_bar = late_avail + timedelta(minutes=15)
    session.execute(delete(FeatureRow).where(FeatureRow.source == E.SOURCE_REPLAY, FeatureRow.bar_close == visible_bar))
    assert _replay_swing_hash(session, visible_bar) != h0


def test_label_job_ignores_a_perp_bar_not_yet_available_at_as_of(session, floor):
    _seed_btc_candidates(session, signal_bar=T0)
    add_perp_bar(session, 0, 100, 100.5, 99.5, 100, signal_bar=T0, available_at=T0 + timedelta(hours=1))
    too_early = T0 + timedelta(minutes=30)
    report = LJ.run_label_job(session, too_early, source=E.SOURCE_REPLAY, coins=['BTCUSDT'], code_version='testver')
    assert report.count(LJ.RESULT_PENDING) == 1
    assert any(o.detail == LJ.PENDING_ENTRY_BAR for o in report.outcomes)
    assert session.scalar(select(func.count()).select_from(Label)) == 0
    _funding_grid(session, T0)
    later = T0 + timedelta(hours=2)
    report2 = LJ.run_label_job(session, later, source=E.SOURCE_REPLAY, coins=['BTCUSDT'], code_version='testver')
    assert report2.labeled + report2.no_entry >= 1


# ---------------------------------------------------------------------------
# Raw-feed end-to-end run_replay
# ---------------------------------------------------------------------------
def test_run_replay_from_raw_feeds_assembles_every_bar_and_matches_live(session, monkeypatch):
    monkeypatch.setattr(RP, 'current_code_version', lambda: 'e2efix01')
    end = BAR_CLOSE
    start = BAR_CLOSE - 2 * M15
    bars = RP.iter_bar_closes(start, end)
    store_market(session, synthetic_inputs(days=4, end=end))
    fixed_cv = 'e2efix01'
    report = RP.run_replay(
        session, coins=['BTCUSDT'], start=start, end=end, label_at=end + timedelta(days=1), history_policy=POLICY
    )
    assert report.bars_walked == len(bars)
    rows = session.execute(select(FeatureRow).where(FeatureRow.source == E.SOURCE_REPLAY, FeatureRow.coin == 'BTCUSDT')).scalars().all()
    assert len(rows) == len(bars) * len(C.HORIZONS)
    assert report.features_written + report.features_already_present == len(rows)
    for bar in bars:
        for horizon in C.HORIZONS:
            replay = session.execute(
                select(FeatureRow).where(
                    FeatureRow.source == E.SOURCE_REPLAY,
                    FeatureRow.bar_close == bar,
                    FeatureRow.horizon == horizon,
                )
            ).scalar_one()
            live_now = bar + timedelta(seconds=40)
            FJ.assemble_feature_rows(
                session, 'BTCUSDT', bar, source=E.SOURCE_LIVE, now=live_now, observed_at=bar, policy=POLICY, code_version=fixed_cv
            )
            live = session.execute(
                select(FeatureRow).where(FeatureRow.source == E.SOURCE_LIVE, FeatureRow.bar_close == bar, FeatureRow.horizon == horizon)
            ).scalar_one()
            assert replay.feature_hash == live.feature_hash
            assert replay.code_version == live.code_version == fixed_cv
            assert _feature_columns(replay) == _feature_columns(live)
    candidates = session.execute(select(Candidate).where(Candidate.source == E.SOURCE_REPLAY)).scalars().all()
    if candidates:
        assert all(c.rule_hash == current_rule_hash() for c in candidates)
        assert report.options_checked >= len(candidates)
    # The 4-day synthetic random-walk fixture does not guarantee a swing-break trigger on these three bars.


# ---------------------------------------------------------------------------
# Idempotency, resume, dry-run
# ---------------------------------------------------------------------------
def _label_bars_for_btc(session, signal_bar: datetime = T0):
    add_perp_bar(session, 0, 100, 100.5, 99.5, 100, signal_bar=signal_bar, coin='BTCUSDT')
    add_perp_bar(session, 1, 100, 100.5, 97.5, 98, signal_bar=signal_bar, coin='BTCUSDT')
    add_funding_at(session, signal_bar - timedelta(hours=8), coin='BTCUSDT')


def test_replay_is_idempotent_with_candidates_and_labels(session):
    store_market(session, synthetic_inputs(days=4, end=BAR_CLOSE))
    add_feature_rows(session, 'BTCUSDT', T0, source=E.SOURCE_REPLAY)
    _label_bars_for_btc(session)
    label_at = T0 + timedelta(days=1)
    first = RP.run_replay(session, coins=['BTCUSDT'], start=T0, end=T0, label_at=label_at)
    n_feat = session.scalar(select(func.count()).select_from(FeatureRow).where(FeatureRow.source == E.SOURCE_REPLAY))
    n_cand = session.scalar(select(func.count()).select_from(Candidate).where(Candidate.source == E.SOURCE_REPLAY))
    n_label = session.scalar(select(func.count()).select_from(Label))
    assert n_cand >= 1 and n_label >= 1
    second = RP.run_replay(session, coins=['BTCUSDT'], start=T0, end=T0, label_at=label_at)
    assert second.features_written == 0
    assert second.options_checked == 0
    assert second.labeled == 0
    assert second.no_entry == 0
    assert session.scalar(select(func.count()).select_from(FeatureRow).where(FeatureRow.source == E.SOURCE_REPLAY)) == n_feat
    assert session.scalar(select(func.count()).select_from(Candidate).where(Candidate.source == E.SOURCE_REPLAY)) == n_cand
    assert session.scalar(select(func.count()).select_from(Label)) == n_label
    assert first.candidates_by_status.get('watch', 0) >= 1


def test_dry_run_reports_like_real_but_persists_nothing(session):
    store_market(session, synthetic_inputs(days=4, end=BAR_CLOSE))
    add_feature_rows(session, 'BTCUSDT', T0, source=E.SOURCE_REPLAY)
    _label_bars_for_btc(session)
    label_at = T0 + timedelta(days=1)
    seeded_features = session.scalar(select(func.count()).select_from(FeatureRow).where(FeatureRow.source == E.SOURCE_REPLAY))
    dry = RP.run_replay(session, coins=['BTCUSDT'], start=T0, end=T0, label_at=label_at, dry_run=True)
    assert session.scalar(select(func.count()).select_from(Candidate)) == 0
    assert session.scalar(select(func.count()).select_from(Label)) == 0
    assert session.scalar(select(func.count()).select_from(FeatureRow).where(FeatureRow.source == E.SOURCE_REPLAY)) == seeded_features
    real = RP.run_replay(session, coins=['BTCUSDT'], start=T0, end=T0, label_at=label_at, dry_run=False)
    assert dry.candidates_by_status == real.candidates_by_status
    assert dry.options_verdicts == real.options_verdicts
    assert dry.labeled == real.labeled
    assert dry.no_entry == real.no_entry
    assert session.scalar(select(func.count()).select_from(Candidate).where(Candidate.source == E.SOURCE_REPLAY)) >= 1


def test_replay_resume_matches_one_uninterrupted_run(session):
    t1 = T0 + M15
    add_feature_rows(session, 'BTCUSDT', T0, source=E.SOURCE_REPLAY, horizons=('swing',))
    add_feature_rows(session, 'BTCUSDT', t1, source=E.SOURCE_REPLAY, horizons=('swing',))
    RP.run_replay(session, coins=['BTCUSDT'], start=T0, end=t1, label_at=t1)

    def snapshot():
        rows = session.execute(select(Candidate).where(Candidate.source == E.SOURCE_REPLAY).order_by(Candidate.bar_close)).scalars().all()
        return [(r.bar_close, r.coin, r.horizon, r.direction, r.trigger, r.rule_hash) for r in rows]

    expected = snapshot()
    session.execute(Candidate.__table__.delete())
    session.execute(FeatureRow.__table__.delete())
    add_feature_rows(session, 'BTCUSDT', T0, source=E.SOURCE_REPLAY, horizons=('swing',))
    add_feature_rows(session, 'BTCUSDT', t1, source=E.SOURCE_REPLAY, horizons=('swing',))
    RP.run_replay(session, coins=['BTCUSDT'], start=T0, end=T0, label_at=t1)
    RP.run_replay(session, coins=['BTCUSDT'], start=t1, end=t1, label_at=t1)
    assert snapshot() == expected


# ---------------------------------------------------------------------------
# CLI validation and source scan
# ---------------------------------------------------------------------------
def test_replay_args_reject_end_before_start():
    with pytest.raises(ValueError, match='end must not precede start'):
        RP.validate_replay_args(start=T0, end=T0 - M15, label_at=None, coins=['BTCUSDT'])


def test_replay_module_does_not_call_utc_now():
    source = Path(RP.__file__).read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', '')
            assert name != 'utc_now', f'replay must not call utc_now (line {node.lineno})'
