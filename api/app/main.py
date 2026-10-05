from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import dispose_engine
from app.routers import (
    cards,
    feed_health_router,
    health,
    stand_down_router,
    stats_routes,
    tracker,
    ws,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    origins = [origin.strip() for origin in settings.cors_origins.split(',') if origin.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=['*'],
        allow_headers=['*'],
    )
    app.include_router(health.router)
    app.include_router(cards.router)
    app.include_router(tracker.router)
    app.include_router(stats_routes.router)
    app.include_router(feed_health_router.router)
    app.include_router(stand_down_router.router)
    app.include_router(ws.router)
    return app


app = create_app()
