from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

ITUNES_RECORD: dict[str, Any] = {
    "wrapperType": "audiobook",
    "collectionId": 1,
    "collectionName": "Dune",
    "artistName": "Frank Herbert",
    "description": "<p>A desert planet.</p>",
    "primaryGenreName": "Sci-Fi & Fantasy",
    "releaseDate": "2007-01-01T08:00:00Z",
    "artworkUrl100": None,  # no cover: these tests are about the endpoint
}


class FakeITunes:
    def __init__(self) -> None:
        self.status = 200
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status)
        return httpx.Response(200, json={"resultCount": 1, "results": [ITUNES_RECORD]})


@pytest.fixture
def itunes() -> FakeITunes:
    return FakeITunes()


@pytest.fixture
async def wired_app(
    app: FastAPI, db_pool: AsyncConnectionPool, itunes: FakeITunes
) -> AsyncIterator[FastAPI]:
    # The test client doesn't run the lifespan, so provide what it would have
    # created: the pool (test database) and the shared HTTP client (fake iTunes).
    transport = httpx.MockTransport(itunes.handle)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://itunes.test"
    ) as http:
        app.state.pool = db_pool
        app.state.http = http
        yield app


@pytest.fixture
async def api(wired_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=wired_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        yield api


async def test_requires_api_key(api: httpx.AsyncClient) -> None:
    response = await api.post("/ingestions", json={"term": "dune"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_ingests_and_returns_the_summary(
    api: httpx.AsyncClient, auth_headers: dict[str, str], db_pool: AsyncConnectionPool
) -> None:
    response = await api.post(
        "/ingestions", json={"term": "dune"}, headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json() == {
        "source": "itunes",
        "fetched": 1,
        "created": 1,
        "updated": 0,
        "skipped": 0,
        "invalid": [],
    }
    async with db_pool.connection() as conn:
        cursor = await conn.execute("SELECT title FROM audiobooks WHERE id = 1")
        assert await cursor.fetchone() == ("Dune",)


async def test_repeating_the_request_skips(
    api: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    await api.post("/ingestions", json={"term": "dune"}, headers=auth_headers)

    response = await api.post(
        "/ingestions", json={"term": "dune"}, headers=auth_headers
    )

    body = response.json()
    assert (body["created"], body["updated"], body["skipped"]) == (0, 0, 1)


async def test_passes_search_parameters_to_itunes(
    api: httpx.AsyncClient, auth_headers: dict[str, str], itunes: FakeITunes
) -> None:
    body = {"term": "dune", "limit": 5, "country": "AR", "attribute": "titleTerm"}

    await api.post("/ingestions", json=body, headers=auth_headers)

    [request] = itunes.requests
    assert dict(request.url.params) == {
        "media": "audiobook",
        "term": "dune",
        "limit": "5",
        "country": "AR",
        "attribute": "titleTerm",
    }


async def test_reports_fallback_when_itunes_is_down(
    api: httpx.AsyncClient, auth_headers: dict[str, str], itunes: FakeITunes
) -> None:
    itunes.status = 503

    response = await api.post(
        "/ingestions", json={"term": "dune"}, headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json()["source"] == "fallback"


@pytest.mark.parametrize(
    ("body", "field"),
    [
        pytest.param({}, "term", id="missing-term"),
        pytest.param({"term": ""}, "term", id="empty-term"),
        pytest.param({"term": "dune", "limit": 500}, "limit", id="limit-too-big"),
        pytest.param({"term": "dune", "country": "USA"}, "country", id="bad-country"),
        pytest.param({"term": "dune", "attribute": "nope"}, "attribute", id="bad-attr"),
    ],
)
async def test_rejects_invalid_parameters(
    api: httpx.AsyncClient,
    auth_headers: dict[str, str],
    itunes: FakeITunes,
    body: dict[str, Any],
    field: str,
) -> None:
    response = await api.post("/ingestions", json=body, headers=auth_headers)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"][0]["loc"] == ["body", field]
    assert itunes.requests == []  # rejected before calling iTunes


async def test_is_documented_in_openapi(wired_app: FastAPI) -> None:
    operation = wired_app.openapi()["paths"]["/ingestions"]["post"]

    assert operation["summary"] == "Ingest audiobooks from iTunes"
    assert operation["security"] == [{"APIKeyHeader": []}]
    assert {"200", "401", "422"} <= set(operation["responses"])
