from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from audiobook.api.routes import health as health_routes


async def get_health(app: FastAPI) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/health")  # no API key: it's public


async def test_healthy_when_the_database_answers(
    app: FastAPI, db_pool: AsyncConnectionPool
) -> None:
    app.state.pool = db_pool

    response = await get_health(app)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.fixture
async def unreachable_pool() -> AsyncIterator[AsyncConnectionPool]:
    # Nothing listens on port 1: the pool can never open a connection.
    pool = AsyncConnectionPool(
        "postgresql://user:password@127.0.0.1:1/db", min_size=1, open=False
    )
    await pool.open()  # without wait: connecting keeps failing in the background
    try:
        yield pool
    finally:
        await pool.close()


async def test_unhealthy_when_the_database_is_unreachable(
    app: FastAPI,
    unreachable_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health_routes, "HEALTH_DB_TIMEOUT_SECONDS", 0.2)
    app.state.pool = unreachable_pool

    response = await get_health(app)

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "database_unavailable",
            "message": "The database is unreachable.",
        }
    }


def test_health_is_documented(app: FastAPI) -> None:
    operation = app.openapi()["paths"]["/health"]["get"]

    assert operation["summary"] == "Health check"
    assert "SELECT 1" in operation["description"]
    assert {"200", "503"} <= set(operation["responses"])
    assert "security" not in operation  # public
