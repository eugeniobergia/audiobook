from collections.abc import AsyncIterator

import httpx
import psycopg
import pytest
from fastapi import APIRouter, Depends, FastAPI
from psycopg import sql
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from audiobook.api.app import create_app
from audiobook.api.auth import require_api_key
from audiobook.config import Settings, get_settings
from audiobook.db.db import SCHEMA_PATH, create_pool

API_KEY = "test-key"

# Database tests use their own database on the same Postgres server, so they never
# touch the data you ingested while developing.
TEST_DB_NAME = "audiobook_test"


def _test_settings() -> Settings:
    # Explicit values win over the environment and any local .env file.
    return Settings(
        api_key=API_KEY,
        postgres_user="test",
        postgres_password="test",
        postgres_db="test",
    )


# A protected route used only by tests, so auth is checked through the real
# dependency before any feature endpoint exists. It also takes a typed query
# parameter, so tests can trigger a validation error, and can raise on demand.
_test_router = APIRouter(dependencies=[Depends(require_api_key)])


@_test_router.get("/_test/protected")
async def protected(n: int = 0) -> dict[str, int]:
    return {"n": n}


@_test_router.get("/_test/boom")
async def boom() -> None:
    raise RuntimeError("secret internals")


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


@pytest.fixture
def app() -> FastAPI:
    app = create_app()
    app.dependency_overrides[get_settings] = _test_settings
    app.include_router(_test_router)
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    # ASGITransport does not run the app's lifespan, so these tests never touch
    # Postgres. Tests that need the database will get their own fixture.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# --- Database -------------------------------------------------------------------
# Only tests that ask for `db_pool` need Postgres; everything else runs without it.


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """Create the test database if needed and give it a fresh schema, once per run.

    Fails (rather than skipping) when Postgres is unreachable, so the database
    tests can't silently stop running.
    """
    try:
        # Real connection settings from the environment or .env; the API key is
        # irrelevant here.
        settings = Settings(api_key="unused")
    except ValidationError as exc:
        pytest.fail(
            f"Database tests need the POSTGRES_* settings (see .env.example).\n{exc}"
        )
    test_url = settings.model_copy(update={"postgres_db": TEST_DB_NAME}).database_url

    try:
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB_NAME,)
            ).fetchone()
            if not exists:
                conn.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB_NAME))
                )
        with psycopg.connect(test_url, autocommit=True) as conn:
            # Drop first: schema.sql only creates what's missing, so a changed
            # table definition would otherwise never reach the test database.
            conn.execute("DROP TABLE IF EXISTS audiobooks")
            conn.execute(SCHEMA_PATH.read_bytes())
    except psycopg.OperationalError as exc:
        pytest.fail(
            "Can't reach Postgres for the database tests. "
            f"Start it with `docker compose up -d db`.\n{exc}"
        )
    return test_url


@pytest.fixture
async def db_pool(test_database_url: str) -> AsyncIterator[AsyncConnectionPool]:
    """A connection pool to the test database, emptied before each test."""
    pool = create_pool(test_database_url)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            await conn.execute("TRUNCATE audiobooks")
        yield pool
    finally:
        await pool.close()
