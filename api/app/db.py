from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            # asyncpg already returns timestamptz as UTC-aware datetimes, but SQL that depends on the
            # session zone (date_trunc, ::date, extract, to_char) would still follow the server's.
            connect_args={'server_settings': {'timezone': 'UTC'}},
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        await session.close()


async def check_database() -> dict[str, str]:
    engine = get_engine()
    async with engine.connect() as connection:
        result = await connection.execute(text('SELECT current_database(), version()'))
        row = result.one()
        return {
            'database': row[0],
            'version': row[1].split(',')[0],
        }


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
