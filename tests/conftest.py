from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI

from audiobook.api.app import create_app
from audiobook.api.auth import require_api_key
from audiobook.config import Settings, get_settings

API_KEY = "test-key"


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
