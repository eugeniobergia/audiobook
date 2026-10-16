import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# A raw iTunes response saved while the API was up, used when it is not.
SAMPLE_PATH = Path(__file__).parent / "samples" / "default.json"


class SearchParams(BaseModel):
    """Search parameters passed through to the iTunes Search API."""

    model_config = ConfigDict(str_strip_whitespace=True)

    term: str = Field(min_length=1, description="Text to search for.")
    limit: int = Field(default=50, ge=1, le=200, description="Results to fetch.")
    country: str = Field(
        default="US", pattern=r"^[A-Za-z]{2}$", description="Two-letter store code."
    )
    attribute: (
        Literal["titleTerm", "authorTerm", "genreIndex", "ratingIndex"] | None
    ) = Field(default=None, description="Restrict the search to one field.")


class UpstreamError(Exception):
    """iTunes could not be reached or returned something unusable."""


@dataclass(frozen=True)
class FetchResult:
    # Raw, unvalidated items: normalization checks each one.
    records: list[Any]
    source: Literal["itunes", "fallback"]


class ITunesClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        # Injected rather than created here: the app shares one client (connection
        # reuse), and tests pass one backed by httpx.MockTransport.
        self._http = http

    async def search(self, params: SearchParams) -> list[Any]:
        """Return the raw `results` of an audiobook search. Raises UpstreamError."""
        query = {"media": "audiobook", **params.model_dump(exclude_none=True)}
        try:
            response = await self._http.get("/search", params=query)
            response.raise_for_status()
            # iTunes serves JSON as text/javascript; .json() doesn't check that.
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            # Includes rate limiting, which iTunes reports as 429 or 403.
            raise UpstreamError(
                f"iTunes returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            # Timeouts, connection errors and other transport failures.
            raise UpstreamError(f"iTunes request failed: {exc!r}") from exc
        except ValueError as exc:
            raise UpstreamError("iTunes returned invalid JSON") from exc
        return _results(payload)


def _results(payload: Any) -> list[Any]:
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise UpstreamError("iTunes response has no 'results' list")
    return results


async def load_sample(path: Path = SAMPLE_PATH) -> list[Any]:
    # Reading and parsing both block, so they run in a worker thread instead of
    # the event loop.
    payload = await asyncio.to_thread(lambda: json.loads(path.read_bytes()))
    return _results(payload)


async def fetch_audiobooks(client: ITunesClient, params: SearchParams) -> FetchResult:
    """Search iTunes, falling back to the saved sample if it is unavailable.

    The sample is a fixed snapshot: it is returned whatever the search parameters,
    and `source` tells the caller which one they got.
    """
    try:
        records = await client.search(params)
    except UpstreamError as exc:
        logger.warning("iTunes unavailable (%s); using the saved sample", exc)
        return FetchResult(records=await load_sample(), source="fallback")
    return FetchResult(records=records, source="itunes")
