from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from audiobook.config import get_settings
from audiobook.db.db import apply_schema, create_pool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    pool = create_pool(get_settings().database_url)
    # wait=True: fail at startup if the database is unreachable, not on the
    # first request.
    await pool.open(wait=True)
    try:
        await apply_schema(pool)
        app.state.pool = pool
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

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
