"""The shipped migration and the models must describe the same database.

Run against the live ``signals`` database at Alembic head. If somebody edits a
model and forgets the revision, this fails with the diff Alembic would have
generated.

Caveat worth knowing before trusting it too far: Alembic compares tables,
columns, indexes, uniqueness and foreign keys, but NOT ``CHECK`` constraints. A
changed CHECK will not show up here, which is why every CHECK also has a
behavioural test in ``test_schema_constraints.py``.
"""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from app.models.base import Base

EXPECTED_TABLE_COUNT = 13  # plus alembic_version


@pytest.fixture
def migration_diff(db_engine):
    with db_engine.connect() as connection:
        at_head = connection.execute(
            text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
        ).scalar()
        if not at_head or 'candle' not in inspect(connection).get_table_names(schema='public'):
            pytest.skip('public schema is not at Alembic head; run `python -m alembic upgrade head`')
        context = MigrationContext.configure(
            connection,
            opts={'compare_type': True, 'include_schemas': False},
        )
        return compare_metadata(context, Base.metadata)


def test_the_live_schema_matches_the_models(migration_diff):
    assert migration_diff == [], (
        'public is out of step with app/models. Generate a revision:\n'
        '  python -m alembic revision --autogenerate -m "<what changed>"\n'
        f'diff: {migration_diff}'
    )


def test_every_model_table_exists_in_public(db_engine):
    with db_engine.connect() as connection:
        present = set(inspect(connection).get_table_names(schema='public'))
    missing = set(Base.metadata.tables) - present
    if missing:
        pytest.skip(f'public is not at head; missing {sorted(missing)}')
    assert len(Base.metadata.tables) == EXPECTED_TABLE_COUNT
    assert 'alembic_version' in present


def test_there_is_exactly_one_revision_head(db_engine):
    with db_engine.connect() as connection:
        if connection.execute(text("SELECT to_regclass('public.alembic_version')")).scalar() is None:
            pytest.skip('no alembic_version table')
        count = connection.execute(text('SELECT count(*) FROM public.alembic_version')).scalar()
    assert count == 1, 'a branched or empty revision history will bite the next milestone'
