import json
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from audiobook.api.routes import audiobooks as audiobook_routes
from audiobook.models import Audiobook
from audiobook.repositories.audiobooks import upsert_many


def make_book(id_: int, title: str, **overrides: Any) -> Audiobook:
    fields: dict[str, Any] = {
        "id": id_,
        "title": title,
        "author": "An Author",
        "description": "About it.",
        "genre": "Fiction",
        "release_date": date(2020, 1, 1),
        "cover_url": f"https://covers.test/{id_}.png",
        "content_hash": f"hash-{id_}",
        "palette": ("#112233", "#aabbcc"),
    }
    fields.update(overrides)
    return Audiobook(**fields)


@pytest.fixture
async def api(
    app: FastAPI, db_pool: AsyncConnectionPool
) -> AsyncIterator[httpx.AsyncClient]:
    # The test client doesn't run the lifespan: give the app the test pool.
    app.state.pool = db_pool
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        yield api


@pytest.fixture
async def catalog(db_pool: AsyncConnectionPool) -> None:
    async with db_pool.connection() as conn:
        await upsert_many(
            conn,
            [
                make_book(1, "Dune", author="Frank Herbert", genre="Sci-Fi"),
                make_book(2, "Emma", author="Jane Austen", genre="Classics"),
                make_book(3, "Anathem", author="Neal Stephenson", genre="Sci-Fi"),
                make_book(4, "No Extras", author=None, genre=None, palette=None),
            ],
        )


# --- GET /audiobooks ------------------------------------------------------------


@pytest.mark.usefixtures("catalog")
async def test_list_returns_a_page(
    api: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await api.get("/audiobooks", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert (body["total"], body["limit"], body["offset"]) == (4, 20, 0)
    assert [item["title"] for item in body["items"]] == [
        "Anathem",
        "Dune",
        "Emma",
        "No Extras",
    ]


@pytest.mark.usefixtures("catalog")
async def test_list_item_shape(
    api: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await api.get("/audiobooks", params={"q": "dune"}, headers=auth_headers)

    [item] = response.json()["items"]
    updated_at = item.pop("updated_at")
    assert item == {
        "id": 1,
        "title": "Dune",
        "author": "Frank Herbert",
        "description": "About it.",
        "genre": "Sci-Fi",
        "release_date": "2020-01-01",
        "cover_url": "https://covers.test/1.png",
        "palette": ["#112233", "#aabbcc"],
    }  # no content_hash: it's internal
    assert updated_at.startswith(str(date.today().year))


@pytest.mark.usefixtures("catalog")
async def test_list_missing_values_are_null(
    api: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await api.get(
        "/audiobooks", params={"q": "extras"}, headers=auth_headers
    )

    [item] = response.json()["items"]
    assert (item["author"], item["genre"], item["palette"]) == (None, None, None)


@pytest.mark.usefixtures("catalog")
async def test_list_pagination(
    api: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await api.get(
        "/audiobooks", params={"limit": 2, "offset": 1}, headers=auth_headers
    )

    body = response.json()
    assert [item["title"] for item in body["items"]] == ["Dune", "Emma"]
    assert (body["total"], body["limit"], body["offset"]) == (4, 2, 1)


@pytest.mark.usefixtures("catalog")
@pytest.mark.parametrize(
    ("params", "expected_titles"),
    [
        pytest.param({"q": "HERBERT"}, ["Dune"], id="q-author"),
        pytest.param({"genre": "sci-fi"}, ["Anathem", "Dune"], id="genre"),
        pytest.param({"q": "an", "genre": "Sci-Fi"}, ["Anathem", "Dune"], id="both"),
        pytest.param({"q": "  dune  "}, ["Dune"], id="q-trimmed"),
        pytest.param(
            {"q": "   "}, ["Anathem", "Dune", "Emma", "No Extras"], id="q-blank"
        ),
    ],
)
async def test_list_filters(
    api: httpx.AsyncClient,
    auth_headers: dict[str, str],
    params: dict[str, str],
    expected_titles: list[str],
) -> None:
    response = await api.get("/audiobooks", params=params, headers=auth_headers)

    body = response.json()
    assert [item["title"] for item in body["items"]] == expected_titles
    assert body["total"] == len(expected_titles)


@pytest.mark.parametrize(
    ("params", "field"),
    [
        pytest.param({"limit": 0}, "limit", id="limit-zero"),
        pytest.param({"limit": 101}, "limit", id="limit-too-big"),
        pytest.param({"limit": "ten"}, "limit", id="limit-not-a-number"),
        pytest.param({"offset": -1}, "offset", id="negative-offset"),
        pytest.param({"q": ""}, "q", id="empty-q"),
        pytest.param({"q": "x" * 201}, "q", id="q-too-long"),
    ],
)
async def test_list_rejects_invalid_parameters(
    api: httpx.AsyncClient,
    auth_headers: dict[str, str],
    params: dict[str, Any],
    field: str,
) -> None:
    response = await api.get("/audiobooks", params=params, headers=auth_headers)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"][0]["loc"] == ["query", field]


async def test_list_of_empty_catalog(
    api: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await api.get("/audiobooks", headers=auth_headers)

    assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}


# --- GET /audiobooks/{id} -------------------------------------------------------


@pytest.mark.usefixtures("catalog")
async def test_get_audiobook(
    api: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await api.get("/audiobooks/2", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["title"], body["author"]) == (2, "Emma", "Jane Austen")
    assert "content_hash" not in body


async def test_get_missing_audiobook(
    api: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await api.get("/audiobooks/999", headers=auth_headers)

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "Audiobook 999 not found."}
    }


@pytest.mark.parametrize("bad_id", ["abc", "0", "-5", "1.5"])
async def test_get_with_invalid_id(
    api: httpx.AsyncClient, auth_headers: dict[str, str], bad_id: str
) -> None:
    response = await api.get(f"/audiobooks/{bad_id}", headers=auth_headers)

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["path", "audiobook_id"]


# --- GET /audiobooks/export ----------------------------------------------------


async def export_lines(
    api: httpx.AsyncClient, auth_headers: dict[str, str]
) -> list[dict[str, Any]]:
    response = await api.get("/audiobooks/export", headers=auth_headers)
    assert response.status_code == 200
    assert response.text == "" or response.text.endswith("\n")
    return [json.loads(line) for line in response.text.splitlines()]


@pytest.mark.usefixtures("catalog")
async def test_export_headers(
    api: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await api.get("/audiobooks/export", headers=auth_headers)

    assert response.status_code == 200  # not swallowed by /audiobooks/{id}
    assert response.headers["content-type"] == "application/x-ndjson"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="audiobooks.ndjson"'
    )


@pytest.mark.usefixtures("catalog")
async def test_export_lines_match_list_items(
    api: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    listed = (await api.get("/audiobooks", headers=auth_headers)).json()["items"]

    exported = await export_lines(api, auth_headers)

    # Same objects as the list endpoint (so no content_hash), ordered by id.
    assert exported == sorted(listed, key=lambda item: item["id"])
    assert [item["id"] for item in exported] == [1, 2, 3, 4]


@pytest.mark.usefixtures("catalog")
async def test_export_spanning_several_batches(
    api: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audiobook_routes, "EXPORT_BATCH_SIZE", 3)

    exported = await export_lines(api, auth_headers)

    assert [item["id"] for item in exported] == [1, 2, 3, 4]


async def test_export_of_empty_catalog(
    api: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    assert await export_lines(api, auth_headers) == []


@pytest.mark.usefixtures("catalog")
async def test_export_returns_its_connection_to_the_pool(
    api: httpx.AsyncClient, auth_headers: dict[str, str], db_pool: AsyncConnectionPool
) -> None:
    # More exports than the pool has connections: a leak would block here.
    for _ in range(12):
        await export_lines(api, auth_headers)

    stats = db_pool.get_stats()
    assert stats["pool_available"] == stats["pool_size"]


async def test_export_requires_api_key(api: httpx.AsyncClient) -> None:
    assert (await api.get("/audiobooks/export")).status_code == 401


def test_export_documented_as_ndjson(app: FastAPI) -> None:
    operation = app.openapi()["paths"]["/audiobooks/export"]["get"]

    assert "application/x-ndjson" in operation["responses"]["200"]["content"]
    assert "401" in operation["responses"]


# --- auth and docs --------------------------------------------------------------


@pytest.mark.parametrize("path", ["/audiobooks", "/audiobooks/1"])
async def test_requires_api_key(api: httpx.AsyncClient, path: str) -> None:
    response = await api.get(path)

    assert response.status_code == 401


def test_documented_in_openapi(app: FastAPI) -> None:
    paths = app.openapi()["paths"]

    list_params = {p["name"] for p in paths["/audiobooks"]["get"]["parameters"]}
    assert list_params == {"q", "genre", "limit", "offset"}
    assert {"200", "401", "422"} <= set(paths["/audiobooks"]["get"]["responses"])
    assert {"200", "401", "404", "422"} <= set(
        paths["/audiobooks/{audiobook_id}"]["get"]["responses"]
    )
