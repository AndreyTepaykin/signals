"""The assembly job: every feed read goes through ``as_of``; rows are written once and reproducibly.

The database tests use the same throwaway-schema ``session`` fixture as the rest of
the suite (nothing persists), a small 4-day market, and a relaxed history policy
(the 90-day statistics need history this fixture does not have; production policy is
exercised on the real database in the milestone report, not here).
"""

from __future__ import annotations

import ast
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete, event, func, insert, select, update

from app.core import constants as C
from app.core.features import columns
from app.core.features.params import HistoryPolicy
from app.core.hashing import feature_hash
from app.models import enums as E
from app.models.feeds import Candle, Funding, OpenInterest
from app.models.features import FeatureRow
from app.worker import feature_job as job

from feature_helpers import dt, synthetic_inputs

JOB_SOURCE = Path(job.__file__)
BAR_CLOSE = dt(2026, 6, 1)
POLICY = HistoryPolicy(window_days=90, min_span_days=0.5, min_samples=5)
FEED_MODEL_NAMES = {'Candle', 'OpenInterest', 'Funding', 'ForceOrder', 'EventCalendar'}


# ---------------------------------------------------------------------------
# Source scan: there is no feed read that bypasses as_of
# ---------------------------------------------------------------------------
def _bypasses(source: str) -> list[str]:
    tree = ast.parse(source)
    problems: list[str] = []
    for func_def in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        names = {n.id for n in ast.walk(func_def) if isinstance(n, ast.Name)}
        if names & FEED_MODEL_NAMES and 'as_of' not in names:
            problems.append(f'{func_def.name}: touches a feed model without as_of')
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Name) and callee.id in ('sa_select', 'select'):
                text = ast.unparse(node)
                if not text.startswith(f'{callee.id}(FeatureRow'):
                    problems.append(f'line {node.lineno}: a bare select that is not on FeatureRow: {text[:60]}')
            if isinstance(callee, ast.Name) and callee.id == 'text':
                problems.append(f'line {node.lineno}: raw text() SQL')
            if isinstance(callee, ast.Attribute) and callee.attr in ('query', 'exec_driver_sql', 'connection'):
                problems.append(f'line {node.lineno}: .{callee.attr}()')
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.search(r'\bselect\b.+\bfrom\b', node.value, re.I | re.S):
            problems.append(f'line {node.lineno}: a raw SQL string')
    return problems


def test_source_scan_the_job_has_no_feed_read_that_bypasses_as_of():
    assert _bypasses(JOB_SOURCE.read_text(encoding='utf-8')) == []


def test_source_scan_the_whole_features_package_touches_no_database():
    for path in sorted((JOB_SOURCE.parents[1] / 'core' / 'features').glob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module or '' for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not any(m.startswith(('sqlalchemy', 'app.models', 'app.worker', 'psycopg')) for m in imported), path.name


def test_the_source_scan_would_catch_a_bypass():
    """Negative controls: the scanner is not vacuous."""
    assert _bypasses('def f(session):\n    return session.execute(sa_select(Candle)).all()')
    assert _bypasses('def f(session):\n    return session.execute(select(Funding)).all()')
    assert _bypasses('def f(s):\n    return s.execute(text("SELECT * FROM candle"))')
    assert _bypasses('def f(s):\n    return s.execute("select a from candle")')
    assert _bypasses('def f(s):\n    return s.query(Candle).all()')
    assert _bypasses('def ok(s, t):\n    return s.execute(as_of(t).select(Candle))') == []
    assert _bypasses('def ok(s):\n    return s.execute(sa_select(FeatureRow.feature_hash))') == []


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------
def _dec(value: float) -> Decimal:
    return Decimal(repr(value))


def store_market(session, inputs, coin: str = 'BTCUSDT') -> None:
    candles = []
    for market, timeframe, bars in (
        (E.MARKET_PERP, '15m', inputs.perp_15m),
        (E.MARKET_SPOT, '15m', inputs.spot_15m),
        (E.MARKET_PERP, '1h', inputs.perp_1h),
        (E.MARKET_SPOT, '1h', inputs.spot_1h),
        (E.MARKET_PERP, '4h', inputs.perp_4h),
    ):
        for b in bars:
            candles.append(
                dict(
                    coin=coin, market=market, timeframe=timeframe, open_time=b.open_time, close_time=b.close_time,
                    open_price=_dec(b.open), high_price=_dec(b.high), low_price=_dec(b.low), close_price=_dec(b.close),
                    base_volume=_dec(b.volume), taker_buy_base_volume=_dec(b.taker_buy_volume), available_at=b.available_at,
                )
            )
    session.execute(insert(Candle), candles)
    session.execute(
        insert(OpenInterest),
        [dict(coin=coin, timeframe='1h', poll_time=p.poll_time, open_interest_base=_dec(p.open_interest), available_at=p.available_at) for p in inputs.oi_1h],
    )
    session.execute(
        insert(Funding),
        [dict(coin=coin, kind=E.FUNDING_SETTLED, funding_time=p.funding_time, funding_rate=_dec(p.rate), interval_hours=8, available_at=p.available_at) for p in inputs.funding_settled],
    )
    session.flush()


@pytest.fixture
def market():
    return synthetic_inputs(days=4, end=BAR_CLOSE)


@pytest.fixture
def stored(session, market):
    store_market(session, market)
    return market


def stored_mapping(row: FeatureRow) -> dict:
    """The row as read back from the database, in the same Python types the engine hashed."""
    out = {}
    for name, spec in columns.FEATURE_COLUMNS.items():
        value = getattr(row, name)
        if spec.kind == 'float' and value is not None:
            assert isinstance(value, Decimal)
            value = float(value)
        out[name] = value
    return out


# ---------------------------------------------------------------------------
# As-of behaviour, observed on the wire
# ---------------------------------------------------------------------------
def test_every_feed_query_the_job_issues_is_gated_on_available_at(session, stored):
    statements: list[tuple[str, object]] = []
    connection = session.connection()

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    event.listen(connection, 'before_cursor_execute', capture)
    try:
        job.load_inputs(session, 'BTCUSDT', BAR_CLOSE, C.HORIZON_SWING, E.SOURCE_LIVE, observed_at=BAR_CLOSE + timedelta(seconds=40))
    finally:
        event.remove(connection, 'before_cursor_execute', capture)

    feed = re.compile(r'\bFROM (candle|open_interest|funding|force_order|event_calendar)\b')
    feed_queries = [(s, p) for s, p in statements if feed.search(s)]
    assert len(feed_queries) >= 9  # 5 perp/spot candle series + BTC ref skipped (reference) + OI + funding x2 + force + calendar
    for statement, parameters in feed_queries:
        assert re.search(r'\.available_at <= ', statement), f'ungated feed read: {statement[:120]}'
        instants = {v for v in (parameters.values() if isinstance(parameters, dict) else parameters) if hasattr(v, 'tzinfo')}
        assert instants & {BAR_CLOSE, BAR_CLOSE + timedelta(seconds=40)}, 'gated, but not at the signal bar'
    # only the predicted-funding read uses the observation time; everything else uses bar_close
    at_observed = [s for s, p in feed_queries if BAR_CLOSE + timedelta(seconds=40) in (p.values() if isinstance(p, dict) else p)]
    assert len(at_observed) == 1 and 'funding' in at_observed[0]


def test_a_bar_that_exists_but_was_not_available_at_the_signal_bar_is_not_read(session, stored):
    session.execute(
        update(Candle)
        .where(Candle.coin == 'BTCUSDT', Candle.market == E.MARKET_PERP, Candle.timeframe == '15m', Candle.close_time == BAR_CLOSE)
        .values(available_at=BAR_CLOSE + timedelta(minutes=1))
    )
    session.execute(
        insert(Candle),
        [dict(coin='BTCUSDT', market=E.MARKET_PERP, timeframe='15m', open_time=BAR_CLOSE, close_time=BAR_CLOSE + timedelta(minutes=15),
              open_price=Decimal('1'), high_price=Decimal('9'), low_price=Decimal('1'), close_price=Decimal('9'),
              base_volume=Decimal('1'), taker_buy_base_volume=Decimal('0'), available_at=BAR_CLOSE + timedelta(minutes=15))],
    )
    session.flush()
    inputs = job.load_inputs(session, 'BTCUSDT', BAR_CLOSE, C.HORIZON_SWING, E.SOURCE_REPLAY)
    assert inputs.perp_15m[-1].close_time == BAR_CLOSE - timedelta(minutes=15)  # the late bar and the future bar are both absent
    assert all(b.close <= 100000 for b in inputs.perp_15m)  # the 9.0 future print is not here either
    assert job.missing_required_bars(inputs) == [f'perp 15m bar closing {BAR_CLOSE.isoformat()}']


def test_open_interest_and_funding_published_after_the_bar_are_not_read(session, stored):
    # the fixture already stores the OI poll for BAR_CLOSE itself, published 5 minutes AFTER the bar
    assert session.scalar(select(func.count()).select_from(OpenInterest).where(OpenInterest.poll_time == BAR_CLOSE)) == 1
    session.execute(insert(Funding), [dict(coin='BTCUSDT', kind=E.FUNDING_SETTLED, funding_time=BAR_CLOSE, funding_rate=Decimal('0.5'), interval_hours=8, available_at=BAR_CLOSE + timedelta(seconds=30))])
    session.flush()
    inputs = job.load_inputs(session, 'BTCUSDT', BAR_CLOSE, C.HORIZON_SWING, E.SOURCE_REPLAY)
    assert max(p.poll_time for p in inputs.oi_1h) < BAR_CLOSE
    assert max(p.funding_time for p in inputs.funding_settled) < BAR_CLOSE
    assert all(p.rate != 0.5 for p in inputs.funding_settled)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def assemble(session, **kwargs):
    kwargs.setdefault('source', E.SOURCE_REPLAY)
    kwargs.setdefault('policy', POLICY)
    kwargs.setdefault('code_version', 'testver')
    return job.assemble_feature_rows(session, 'BTCUSDT', BAR_CLOSE, **kwargs)


def rows(session):
    return session.execute(select(FeatureRow).order_by(FeatureRow.horizon)).scalars().all()


def test_one_row_per_horizon_is_written_with_a_hash_the_stored_row_reproduces(session, stored):
    report = assemble(session)
    assert (report.written, report.already_present, report.hash_mismatches) == (3, 0, 0)
    stored_rows = rows(session)
    assert sorted(r.horizon for r in stored_rows) == sorted(C.HORIZONS)
    for row in stored_rows:
        assert (row.coin, row.bar_close, row.signal_timeframe, row.source, row.code_version) == ('BTCUSDT', BAR_CLOSE, '15m', 'replay', 'testver')
        # The headline: the hash survives the database round trip (Decimal(12,8) <-> float).
        assert feature_hash(stored_mapping(row)) == row.feature_hash
    assert any(r.atr_price is not None and r.rsi is not None for r in stored_rows)


def test_a_missing_input_is_a_database_null_not_a_zero(session, stored):
    assemble(session)
    for row in rows(session):
        for name in ('cme_basis_z', 'etf_net_flow_z', 'ibit_dealer_gamma_sign', 'cme_dealer_gamma_sign', 'beta_vs_btc',
                     'funding_rate_current', 'event_flag', 'active_event_kinds'):
            assert getattr(row, name) is None, name
        assert row.shadow_metrics == {}
    # JSONB: Python None must be SQL NULL, not the JSON value 'null' (which also reads back as None)
    sql_nulls = session.scalar(select(func.count()).select_from(FeatureRow).where(FeatureRow.active_event_kinds.is_(None)))
    assert sql_nulls == 3


def test_running_again_writes_nothing_and_finds_no_mismatch(session, stored):
    assemble(session)
    again = assemble(session)
    assert (again.written, again.already_present, again.hash_mismatches) == (0, 3, 0)
    assert session.scalar(select(func.count()).select_from(FeatureRow)) == 3


def test_a_changed_input_under_an_existing_row_is_reported_and_the_row_is_left_alone(session, stored):
    assemble(session)
    before = {r.horizon: r.feature_hash for r in rows(session)}
    session.execute(
        update(Candle)
        .where(Candle.coin == 'BTCUSDT', Candle.market == E.MARKET_PERP, Candle.timeframe == '15m', Candle.close_time == BAR_CLOSE)
        .values(close_price=Candle.close_price * Decimal('1.01'), high_price=Candle.high_price * Decimal('1.02'))
    )
    session.flush()
    again = assemble(session)
    assert (again.written, again.already_present, again.hash_mismatches) == (0, 3, 3)
    assert {r.horizon: r.feature_hash for r in rows(session)} == before  # immutable


def test_live_job_waits_for_the_bar_then_proceeds_with_nulls_after_the_grace(session, stored):
    session.execute(
        delete(Candle).where(Candle.coin == 'BTCUSDT', Candle.market == E.MARKET_PERP, Candle.timeframe == '15m', Candle.close_time == BAR_CLOSE)
    )
    session.flush()
    with pytest.raises(job.InputsNotReady) as caught:
        assemble(session, source=E.SOURCE_LIVE, now=BAR_CLOSE + timedelta(seconds=30))
    assert caught.value.missing == [f'perp 15m bar closing {BAR_CLOSE.isoformat()}']
    assert session.scalar(select(func.count()).select_from(FeatureRow)) == 0  # nothing written on the retry path

    report = assemble(session, source=E.SOURCE_LIVE, now=BAR_CLOSE + timedelta(minutes=11))
    assert report.written == 3 and report.skipped_not_ready
    for row in rows(session):
        assert row.rsi is None and row.aggressor_imbalance is None and row.cvd_delta is None  # NULL, not the previous bar's values


def test_replay_does_not_wait(session, stored):
    session.execute(
        delete(Candle).where(Candle.coin == 'BTCUSDT', Candle.market == E.MARKET_PERP, Candle.timeframe == '15m', Candle.close_time == BAR_CLOSE)
    )
    session.flush()
    assert assemble(session, source=E.SOURCE_REPLAY).written == 3


def test_live_carry_band_reads_the_prediction_stamped_just_after_the_close(session, stored):
    stamp = BAR_CLOSE + timedelta(seconds=20)
    session.execute(
        insert(Funding),
        [dict(coin='BTCUSDT', kind=E.FUNDING_PREDICTED, funding_time=BAR_CLOSE + timedelta(hours=8), funding_rate=Decimal('0.0001'), interval_hours=8, available_at=stamp)],
    )
    session.flush()
    now = BAR_CLOSE + timedelta(seconds=40)
    assemble(session, source=E.SOURCE_LIVE, now=now, observed_at=now)
    by_horizon = {r.horizon: r for r in rows(session)}
    assert all(r.funding_rate_current == Decimal('0.000100000000') for r in by_horizon.values())
    assert float(by_horizon['scalp'].funding_carry_bps_horizon) == pytest.approx(0.5)
    assert float(by_horizon['swing'].funding_carry_bps_horizon) == pytest.approx(3.0)
    assert float(by_horizon['position'].funding_carry_bps_horizon) == pytest.approx(9.0)
    for row in by_horizon.values():
        assert feature_hash(stored_mapping(row)) == row.feature_hash


def test_the_prediction_is_not_read_as_of_the_bar_and_never_in_replay(session, stored):
    stamp = BAR_CLOSE + timedelta(seconds=20)
    session.execute(
        insert(Funding),
        [dict(coin='BTCUSDT', kind=E.FUNDING_PREDICTED, funding_time=BAR_CLOSE + timedelta(hours=8), funding_rate=Decimal('0.0001'), interval_hours=8, available_at=stamp)],
    )
    session.flush()
    # no observation time: the single predicted read is gated at bar_close and the print is not yet available
    assert job.load_inputs(session, 'BTCUSDT', BAR_CLOSE, C.HORIZON_SWING, E.SOURCE_LIVE).funding_predicted is None
    # replay never reads it, even with an observation time that would see it
    assert job.load_inputs(session, 'BTCUSDT', BAR_CLOSE, C.HORIZON_SWING, E.SOURCE_REPLAY, observed_at=BAR_CLOSE + timedelta(minutes=5)).funding_predicted is None
    assemble(session, source=E.SOURCE_REPLAY, observed_at=BAR_CLOSE + timedelta(minutes=5))
    assert all(r.funding_rate_current is None for r in rows(session))


# ---------------------------------------------------------------------------
# The scheduled entry point
# ---------------------------------------------------------------------------
def test_latest_closed_bar_waits_for_the_ingest_settle_time():
    assert job.latest_closed_bar(dt(2026, 6, 1, 12, 0, 29)) == dt(2026, 6, 1, 11, 45)
    assert job.latest_closed_bar(dt(2026, 6, 1, 12, 0, 30)) == dt(2026, 6, 1, 12, 0)
    assert job.latest_closed_bar(dt(2026, 6, 1, 12, 14, 59)) == dt(2026, 6, 1, 12, 0)


def test_run_feature_job_reports_a_coin_that_is_not_ready_without_failing_the_others(session, stored):
    summary = job.run_feature_job(session, ['BTCUSDT', 'ETHUSDT'], now=BAR_CLOSE + timedelta(seconds=40))
    assert summary['bar_close'] == BAR_CLOSE.isoformat()
    assert summary['written'] == 3 and summary['hash_mismatches'] == 0
    assert list(summary['not_ready']) == ['ETHUSDT']
    assert session.scalar(select(func.count()).select_from(FeatureRow).where(FeatureRow.coin == 'ETHUSDT')) == 0


def test_an_unknown_source_or_horizon_is_rejected(session, stored):
    with pytest.raises(ValueError):
        assemble(session, source='paper')
    with pytest.raises(ValueError):
        assemble(session, horizons=['monthly'])
