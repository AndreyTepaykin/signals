import pytest

from app.core.db_sync import get_sync_database_url


def test_asyncpg_url_becomes_psycopg():
    url = get_sync_database_url('postgresql+asyncpg://signals:p%23ss@127.0.0.1:5432/signals')
    assert url == 'postgresql+psycopg://signals:p%23ss@127.0.0.1:5432/signals'


def test_plain_postgresql_url_gets_psycopg_driver():
    assert get_sync_database_url('postgresql://u:p@h/db').startswith('postgresql+psycopg://')


def test_non_postgres_url_rejected():
    with pytest.raises(ValueError):
        get_sync_database_url('sqlite:///x.db')
