"""Alembic environment.

Uses the SYNC psycopg engine (never asyncpg). The URL comes from the existing
settings (``DATABASE_URL`` in ``.env``) via ``app.core.db_sync``.

Milestone 0 deliberately defines no models and no revisions. Milestone 1 sets
``target_metadata`` to the models' ``MetaData`` and adds the first revision.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# Make `import app...` work regardless of the working directory.
_API_ROOT = Path(__file__).resolve().parents[1]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.core.db_sync import get_sync_database_url  # noqa: E402

# Importing the package (not just `base`) is what makes the metadata complete:
# `app/models/__init__.py` imports every model module.
import app.models  # noqa: E402,F401
from app.models.base import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_sync_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(get_sync_database_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
