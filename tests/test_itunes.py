import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from audiobook.clients.itunes import (
    ITunesClient,
    SearchParams,
    UpstreamError,
    fetch_audiobooks,
    load_sample,
)

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler) -> ITunesClient:
    # MockTransport answers every request with `handler`: no network involved.
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://itunes.test"
    )
    return ITunesClient(http)


def respond_with(results: list[Any]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"resultCount": len(results), "results": results}
        )

    return handler


def raise_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("timed out", request=request)


# --- SearchParams ---------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"term": ""},
        {"term": "   "},
        {"term": "dune", "limit": 0},
        {"term": "dune", "limit": 201},
        {"term": "dune", "country": "USA"},
        {"term": "dune", "attribute": "artistTerm"},
    ],
)
def test_search_params_rejects_invalid_input(params: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SearchParams(**params)


# --- ITunesClient.search --------------------------------------------------------


async def test_search_sends_audiobook_query() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return respond_with([])(request)

    params = SearchParams(term=" dune ", limit=5, country="AR", attribute="titleTerm")
    await make_client(handler).search(params)

    [request] = requests
    assert request.url.path == "/search"
    assert dict(request.url.params) == {
        "media": "audiobook",
        "term": "dune",
        "limit": "5",
        "country": "AR",
        "attribute": "titleTerm",
    }


async def test_search_omits_attribute_when_not_set() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return respond_with([])(request)

    await make_client(handler).search(SearchParams(term="dune"))

    assert "attribute" not in requests[0].url.params


async def test_search_returns_raw_results() -> None:
    results = [{"collectionId": 1, "collectionName": "Dune"}, "not even a dict"]

    assert await make_client(respond_with(results)).search(SearchParams(term="x")) == (
        results
    )


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(lambda r: httpx.Response(500), id="server-error"),
        pytest.param(lambda r: httpx.Response(429), id="rate-limited-429"),
        pytest.param(lambda r: httpx.Response(403), id="rate-limited-403"),
        pytest.param(raise_timeout, id="timeout"),
        pytest.param(lambda r: httpx.Response(200, text="<html>"), id="invalid-json"),
        pytest.param(lambda r: httpx.Response(200, json={"a": 1}), id="no-results"),
        pytest.param(lambda r: httpx.Response(200, json=[1, 2]), id="not-an-object"),
    ],
)
async def test_search_raises_upstream_error(handler: Handler) -> None:
    with pytest.raises(UpstreamError):
        await make_client(handler).search(SearchParams(term="x"))


# --- fetch_audiobooks and the fallback ------------------------------------------


async def test_fetch_uses_itunes_when_available() -> None:
    results = [{"collectionId": 1}]

    fetched = await fetch_audiobooks(
        make_client(respond_with(results)), SearchParams(term="x")
    )

    assert fetched.source == "itunes"
    assert fetched.records == results


async def test_fetch_falls_back_to_sample_when_itunes_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = make_client(lambda r: httpx.Response(503))

    with caplog.at_level(logging.WARNING):
        fetched = await fetch_audiobooks(client, SearchParams(term="x"))

    assert fetched.source == "fallback"
    assert fetched.records == await load_sample()
    assert "HTTP 503" in caplog.text


async def test_bundled_sample_is_a_raw_itunes_response() -> None:
    records = await load_sample()

    assert records, "the fallback sample must not be empty"
    assert all(record["wrapperType"] == "audiobook" for record in records)


async def test_load_sample_rejects_malformed_file(tmp_path: Path) -> None:
    path = tmp_path / "sample.json"
    path.write_text(json.dumps({"no": "results"}))

    with pytest.raises(UpstreamError):
        await load_sample(path)
