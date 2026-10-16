from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from audiobook.models import Audiobook
from audiobook.repositories.audiobooks import upsert_many


def make_book(id_: int, genre: str | None, release_date: date | None) -> Audiobook:
    return Audiobook(
        id=id_,
        title=f"Book {id_}",
        author=None,
        description=None,
        genre=genre,
        release_date=release_date,
        cover_url=None,
        content_hash=f"hash-{id_}",
    )


@pytest.fixture
async def api(
    app: FastAPI, db_pool: AsyncConnectionPool
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.pool = db_pool
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        yield api


async def test_stats(
    api: httpx.AsyncClient, auth_headers: dict[str, str], db_pool: AsyncConnectionPool
) -> None:
    async with db_pool.connection() as conn:
        await upsert_many(
            conn,
            [
                make_book(1, "Fiction", date(2018, 4, 26)),
                make_book(2, "Fiction", date(2018, 1, 1)),
                make_book(3, "Arts", date(2001, 1, 1)),
                make_book(4, None, None),
            ],
        )

    response = await api.get("/stats", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "by_genre": [
            {"genre": "Fiction", "count": 2},
            {"genre": "Arts", "count": 1},
            {"genre": None, "count": 1},
        ],
        "by_release_year": [
            {"year": 2001, "count": 1},
            {"year": 2018, "count": 2},
            {"year": None, "count": 1},
        ],
    }


async def test_stats_of_empty_catalog(
    api: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await api.get("/stats", headers=auth_headers)

    assert response.json() == {"by_genre": [], "by_release_year": []}


async def test_stats_requires_api_key(api: httpx.AsyncClient) -> None:
    assert (await api.get("/stats")).status_code == 401


def test_stats_documented_in_openapi(app: FastAPI) -> None:
    operation: dict[str, Any] = app.openapi()["paths"]["/stats"]["get"]

    assert operation["summary"] == "Audiobook counts by genre and release year"
    assert {"200", "401"} <= set(operation["responses"])
