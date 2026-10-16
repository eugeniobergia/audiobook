from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import APIRouter, Depends, FastAPI

from audiobook.api.auth import require_api_key
from audiobook.api.errors import ErrorResponse, register_error_handlers
from audiobook.api.routes import health, ingestions
from audiobook.config import get_settings
from audiobook.db.db import apply_schema, create_pool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    pool = create_pool(settings.database_url)
    # wait=True: fail at startup if the database is unreachable, not on the
    # first request.
    await pool.open(wait=True)
    try:
        await apply_schema(pool)
        # One shared HTTP client for iTunes, so connections are reused across
        # requests. The context manager closes it on shutdown.
        async with httpx.AsyncClient(
            base_url=settings.itunes_base_url,
            timeout=settings.itunes_timeout_seconds,
        ) as http:
            app.state.pool = pool
            app.state.http = http
            yield
    finally:
        await pool.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Audiobook Catalog",
        description="Ingest audiobooks from the iTunes Search API, then browse, "
        "search and export them.",
        lifespan=lifespan,
    )
    register_error_handlers(app)

    app.include_router(health.router)

    # Everything on this router requires the API key. Feature routers (audiobooks,
    # ingestions, stats) are included here, so new endpoints are protected by default.
    protected = APIRouter(
        dependencies=[Depends(require_api_key)],
        responses={
            401: {"model": ErrorResponse, "description": "Missing or invalid API key"}
        },
    )
    # Include feature routers before adding `protected` to the app: FastAPI copies
    # a router's routes when it's included, so later additions would be lost.
    protected.include_router(ingestions.router)
    app.include_router(protected)

    return app
