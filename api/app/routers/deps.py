"""FastAPI dependencies for read-only sync DB access."""

from __future__ import annotations

from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.db_sync import get_sync_engine
from app.models import enums as E
from app.worker.stats_job import readonly_session


def get_readonly_session() -> Generator[Session, None, None]:
    """Yield a sync session in a DB-level READ ONLY transaction (always rolled back)."""
    with readonly_session(get_sync_engine()) as session:
        yield session


ReadonlySession = Annotated[Session, Depends(get_readonly_session)]


def parse_source(source: str = Query('live', description='live or replay')) -> str:
    if source not in E.SOURCES:
        raise HTTPException(status_code=422, detail=f'source must be one of {E.SOURCES}')
    return source


SourceQuery = Annotated[str, Depends(parse_source)]
