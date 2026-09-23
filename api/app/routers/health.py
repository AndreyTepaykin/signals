from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import check_database

router = APIRouter(tags=['health'])


class HealthResponse(BaseModel):
    status: str
    database: str | None = None
    postgres: str | None = None
    detail: str | None = None


@router.get('/health', response_model=HealthResponse)
async def health() -> HealthResponse:
    try:
        info = await check_database()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f'Database unavailable: {exc}',
        ) from exc

    return HealthResponse(
        status='ok',
        database=info['database'],
        postgres=info['version'],
    )
