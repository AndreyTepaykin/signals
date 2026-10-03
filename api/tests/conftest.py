"""Shared fixtures.

The database-backed tests build the whole schema in a throwaway Postgres schema
inside the live ``signals`` database, and each test runs inside a transaction
that is rolled back. Nothing is left behind and ``public`` is never touched, so
these tests can run against a database that is sitting at Alembic head.

If Postgres is not reachable the database fixtures skip rather than fail: the
pure-metadata tests still run and still catch most schema mistakes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.db_sync import create_utc_engine, get_sync_database_url
from app.models import enums as E
from app.models.base import Base
from app.models.decisions import Candidate
from app.models.features import FeatureRow

UTC = timezone.utc

#: The signal bar every available-at test is framed around.
BAR_CLOSE = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(scope='session')
def db_engine() -> Engine:
    engine = create_utc_engine(get_sync_database_url(), poolclass=NullPool, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text('SELECT 1'))
    except SQLAlchemyError as exc:  # pragma: no cover - environment dependent
        engine.dispose()
        pytest.skip(f'Postgres not reachable: {exc.__class__.__name__}')
    return engine


@pytest.fixture(scope='session')
def db_schema(db_engine: Engine) -> Any:
    """A throwaway schema holding the full model metadata, dropped at the end."""
    name = f'm1_test_{uuid.uuid4().hex[:10]}'
    with db_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{name}"'))
        connection.execute(text(f'SET search_path TO "{name}"'))
        Base.metadata.create_all(connection)
    yield name
    with db_engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA "{name}" CASCADE'))


@pytest.fixture
def session(db_engine: Engine, db_schema: str) -> Any:
    """A Session whose work is always rolled back."""
    connection = db_engine.connect()
    transaction = connection.begin()
    connection.execute(text(f'SET search_path TO "{db_schema}"'))
    db = Session(bind=connection, join_transaction_mode='create_savepoint', expire_on_commit=False)
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()


# ---------------------------------------------------------------------------
# Row factories
# ---------------------------------------------------------------------------
def make_feature_row(**overrides: Any) -> FeatureRow:
    values: dict[str, Any] = {
        'coin': 'BTCUSDT',
        'horizon': E.HORIZONS[1],
        'signal_timeframe': E.TIMEFRAMES[0],
        'bar_close': BAR_CLOSE,
        'source': E.SOURCE_LIVE,
        'feature_hash': 'f' * 64,
        'code_version': 'deadbee',
    }
    values.update(overrides)
    return FeatureRow(**values)


def make_candidate(session: Session, **overrides: Any) -> Candidate:
    """Insert a candidate plus the feature row its composite FK requires.

    ``leader`` is a convenience: pass another candidate and this row inherits its
    ``sample_group_id`` with reason ``overlap`` unless told otherwise.
    """
    leader: Candidate | None = overrides.pop('leader', None)
    feature_overrides: dict[str, Any] = overrides.pop('feature_overrides', {})

    coin = overrides.pop('coin', 'BTCUSDT')
    horizon = overrides.pop('horizon', E.HORIZONS[1])
    bar_close = overrides.pop('bar_close', BAR_CLOSE)
    source = overrides.pop('source', E.SOURCE_LIVE)

    feature_row = make_feature_row(
        coin=coin,
        horizon=horizon,
        bar_close=bar_close,
        source=source,
        **feature_overrides,
    )
    session.add(feature_row)
    session.flush()

    if leader is None:
        group = overrides.pop('sample_group_id', uuid.uuid4())
        reason = overrides.pop('sample_group_reason', E.SAMPLE_GROUP_NEW)
        leader_id = overrides.pop('sample_group_leader_id', None)
    else:
        group = overrides.pop('sample_group_id', leader.sample_group_id)
        reason = overrides.pop('sample_group_reason', E.SAMPLE_GROUP_OVERLAP)
        leader_id = overrides.pop('sample_group_leader_id', leader.id)

    values: dict[str, Any] = {
        'decision_time': bar_close,
        'bar_close': bar_close,
        'coin': coin,
        'horizon': horizon,
        'direction': E.DIRECTION_LONG,
        'source': source,
        'trigger': E.TRIGGERS[0],
        'setup_family': 'breakout',
        'trend_family': E.TREND_FAMILIES[0],
        'feature_hash': feature_row.feature_hash,
        'rule_hash': 'r' * 64,
        'code_version': 'deadbee',
        'feature_row_id': feature_row.id,
        'pre_options_stop_price': Decimal('99.0'),
        'stop_price': Decimal('99.0'),
        'options_verdict': 'align',
        'status': E.STATUS_ALERT,
        'locked_book': False,
        'sample_group_id': group,
        'sample_group_leader_id': leader_id,
        'sample_group_reason': reason,
        'vertical_deadline': bar_close + timedelta(hours=24),
    }
    values.update(overrides)
    candidate = Candidate(**values)
    session.add(candidate)
    session.flush()
    return candidate


@pytest.fixture
def feature_row_factory() -> Any:
    return make_feature_row


@pytest.fixture
def candidate_factory() -> Any:
    return make_candidate
