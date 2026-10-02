"""Synchronous SQLAlchemy engine for worker jobs (psycopg 3).

The API keeps asyncpg (``app/db.py``). Worker jobs are pandas-shaped and
synchronous, so they use this engine. The URL is derived from the same
``DATABASE_URL`` setting, so there is one source of truth.
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

SYNC_DRIVERNAME = 'postgresql+psycopg'

#: Every connection runs in ``timezone=UTC``. Without it Postgres hands psycopg timestamptz values in
#: the SERVER's zone (Europe/Moscow here): the same instant, but ``.hour`` / ``.date()`` would be wrong
#: and a Europe bar could be labelled Asia. Two layers: a libpq startup option, and a ``SET`` on every
#: new connection (needed because libpq's own ``PGTZ`` environment variable overrides the option).
#: Build sync engines with :func:`create_utc_engine`, never with a bare ``create_engine``.
SYNC_CONNECT_ARGS = {'options': '-c timezone=UTC'}


def _force_utc(dbapi_connection, _connection_record) -> None:  # noqa: ANN001 - DBAPI types
    with dbapi_connection.cursor() as cursor:
        cursor.execute("SET TIME ZONE 'UTC'")
    dbapi_connection.commit()


def create_utc_engine(url: str, **kwargs) -> Engine:  # noqa: ANN003
    engine = create_engine(url, connect_args=SYNC_CONNECT_ARGS, **kwargs)
    event.listen(engine, 'connect', _force_utc)
    return engine

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_sync_database_url(database_url: str | None = None) -> str:
    """Sync (psycopg) URL derived from ``DATABASE_URL``.

    ``postgresql+asyncpg://...`` -> ``postgresql+psycopg://...``. Plain
    ``postgresql://`` and ``postgresql+psycopg://`` are accepted too. The
    password is preserved and rendered unmasked (the result is for the
    engine, so do not log it).
    """
    url = make_url(database_url if database_url is not None else get_settings().database_url)
    if url.get_backend_name() != 'postgresql':
        raise ValueError(f'expected a postgresql URL, got backend {url.get_backend_name()!r}')
    return url.set(drivername=SYNC_DRIVERNAME).render_as_string(hide_password=False)


def get_sync_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        _engine = create_utc_engine(get_sync_database_url(), pool_pre_ping=True)
        _session_factory = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sync_session_factory() -> sessionmaker[Session]:
    get_sync_engine()
    assert _session_factory is not None
    return _session_factory


def get_sync_db() -> Generator[Session, None, None]:
    session = get_sync_session_factory()()
    try:
        yield session
    finally:
        session.close()


def check_sync_database() -> dict[str, str]:
    with get_sync_engine().connect() as connection:
        row = connection.execute(text('SELECT current_database(), version()')).one()
        return {
            'database': row[0],
            'version': row[1].split(',')[0],
        }


def dispose_sync_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
        _engine = None
        _session_factory = None
