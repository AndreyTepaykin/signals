"""The database session time zone is UTC by construction, not by convention."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from app import db as async_db
from app.core import db_sync
from app.core.db_sync import create_utc_engine, get_sync_database_url
from app.core.time import ensure_utc

UTC = timezone.utc
APP = Path(__file__).resolve().parent.parent / 'app'

# Instants written with wildly different offsets: all must come back as the same instant at offset 0.
INSTANTS = [
    datetime(2026, 10, 2, 17, 0, tzinfo=UTC),
    datetime(2026, 10, 2, 17, 0, tzinfo=timezone(timedelta(hours=3))),  # the machine's own zone
    datetime(2026, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=-8))),
    datetime(2026, 10, 2, 23, 59, 59, 999000, tzinfo=timezone(timedelta(hours=14))),
]


@pytest.fixture
def sync_engine():
    engine = create_utc_engine(get_sync_database_url(), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            connection.execute(text('SELECT 1'))
    except SQLAlchemyError as exc:  # pragma: no cover - environment dependent
        engine.dispose()
        pytest.skip(f'Postgres not reachable: {exc.__class__.__name__}')
    yield engine
    engine.dispose()


def test_a_sync_connection_reports_a_utc_session_zone(sync_engine):
    with sync_engine.connect() as connection:
        assert connection.execute(text('SHOW TimeZone')).scalar_one() == 'UTC'


def test_the_workers_own_engine_is_utc(monkeypatch):
    """The engine the worker and the CLI really use (not a test double)."""
    monkeypatch.setattr(db_sync, '_engine', None)
    monkeypatch.setattr(db_sync, '_session_factory', None)
    try:
        try:
            with db_sync.get_sync_engine().connect() as connection:
                assert connection.execute(text('SHOW TimeZone')).scalar_one() == 'UTC'
        except SQLAlchemyError as exc:  # pragma: no cover - environment dependent
            pytest.skip(f'Postgres not reachable: {exc.__class__.__name__}')
    finally:
        db_sync.dispose_sync_engine()


def test_the_session_fixture_every_database_test_uses_is_utc(session):
    assert session.execute(text('SHOW TimeZone')).scalar_one() == 'UTC'


@pytest.mark.parametrize('instant', INSTANTS, ids=lambda i: i.isoformat())
def test_an_instant_round_trips_with_a_zero_offset_whatever_zone_it_was_written_in(session, instant):
    (back,) = session.execute(text('SELECT CAST(:t AS timestamptz)'), {'t': instant}).one()
    assert back == instant  # the same instant...
    assert back.utcoffset() == timedelta(0)  # ...expressed in UTC, so .hour and .date() are UTC fields
    assert (back.hour, back.date()) == (ensure_utc(instant).hour, ensure_utc(instant).date())


def test_calendar_fields_are_utc_even_when_the_server_default_is_not(session):
    """A bar at 22:00 UTC is 01:00 the next day in Moscow: that is exactly the Europe-labelled-Asia failure."""
    bar_close = datetime(2026, 10, 2, 22, 0, tzinfo=UTC)
    (back,) = session.execute(text('SELECT CAST(:t AS timestamptz)'), {'t': bar_close}).one()
    assert back.hour == 22 and back.day == 2
    assert session.execute(text("SELECT EXTRACT(hour FROM CAST(:t AS timestamptz))"), {'t': bar_close}).scalar_one() == 22
    assert session.execute(text('SELECT CAST(CAST(:t AS timestamptz) AS date)'), {'t': bar_close}).scalar_one().day == 2


def test_a_hostile_client_environment_cannot_move_the_session_zone(monkeypatch):
    """PGTZ is libpq's own client-side zone knob and it beats a startup option; the per-connection SET must win."""
    monkeypatch.setenv('PGTZ', 'Asia/Tokyo')
    engine = create_utc_engine(get_sync_database_url(), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            assert connection.execute(text('SHOW TimeZone')).scalar_one() == 'UTC'
            (back,) = connection.execute(text("SELECT TIMESTAMPTZ '2026-10-02 17:00:00+00'")).one()
            assert back.utcoffset() == timedelta(0) and back.hour == 17
    except SQLAlchemyError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f'Postgres not reachable: {exc.__class__.__name__}')
    finally:
        engine.dispose()


def test_without_the_option_this_database_would_not_be_utc(sync_engine):
    """Documents WHY the option exists: if this ever fails the server default is UTC and the option is redundant, not wrong."""
    bare = create_engine(get_sync_database_url(), poolclass=NullPool)
    try:
        with bare.connect() as connection:
            zone = connection.execute(text('SHOW TimeZone')).scalar_one()
    finally:
        bare.dispose()
    assert zone != 'UTC', 'server default is already UTC; SYNC_CONNECT_ARGS is now belt-and-braces only'


async def test_the_async_engine_is_utc_too():
    try:
        engine = async_db.get_engine()
        async with engine.connect() as connection:
            zone = (await connection.execute(text('SHOW TimeZone'))).scalar_one()
            back = (await connection.execute(text("SELECT TIMESTAMPTZ '2026-10-02 17:00:00+03'"))).scalar_one()
            day = (await connection.execute(text("SELECT CAST(TIMESTAMPTZ '2026-10-02 23:30:00+00' AS date)"))).scalar_one()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f'Postgres not reachable via asyncpg: {exc.__class__.__name__}')
    finally:
        await async_db.dispose_engine()
    assert zone == 'UTC'
    assert back.utcoffset() == timedelta(0) and back.hour == 14
    assert day.day == 2  # a session in Moscow would have said the 3rd


def test_the_zone_survives_a_rollback_and_pool_reuse():
    from sqlalchemy.pool import QueuePool

    engine = create_utc_engine(get_sync_database_url(), poolclass=QueuePool, pool_size=1, max_overflow=0)
    try:
        for _ in range(3):
            with engine.connect() as connection:
                assert connection.execute(text('SHOW TimeZone')).scalar_one() == 'UTC'
                connection.execute(text("SET TIME ZONE 'Asia/Tokyo'"))  # a careless caller...
            # ...is rolled back on return to the pool (SET is transactional), and the next checkout is UTC again
    except SQLAlchemyError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f'Postgres not reachable: {exc.__class__.__name__}')
    finally:
        engine.dispose()


def test_no_module_under_app_builds_an_engine_that_could_skip_utc():
    """A bare create_engine is only allowed inside create_utc_engine; async engines must set the zone."""
    bare: list[str] = []
    async_without_zone: list[str] = []
    for path in sorted(APP.rglob('*.py')):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ''
            where = f'{path.relative_to(APP)}:{node.lineno}'
            if name == 'create_engine' and path.name != 'db_sync.py':
                bare.append(where)
            if name == 'create_async_engine' and 'timezone' not in ast.unparse(node):
                async_without_zone.append(where)
    assert bare == [] and async_without_zone == []