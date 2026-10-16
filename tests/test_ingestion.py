import io
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from PIL import Image
from psycopg_pool import AsyncConnectionPool

from audiobook.clients.itunes import ITunesClient, SearchParams, load_sample
from audiobook.services.ingestion import IngestionSummary, ingest
from audiobook.services.normalize import InvalidRecord


def solid_png(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), color).save(buffer, "PNG")
    return buffer.getvalue()


# Runs one ingestion against the fake upstream and checks the summary invariant.
Run = Callable[[], Awaitable[IngestionSummary]]

RED_PNG = solid_png((200, 0, 0))
RED_PALETTE = ["#c80000"]


def raw_record(id_: int, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "wrapperType": "audiobook",
        "collectionId": id_,
        "collectionName": f"Book {id_}",
        "artistName": "An Author",
        "description": "<p>About the book.</p>",
        "primaryGenreName": "Fiction",
        "releaseDate": "2020-01-01T08:00:00Z",
        "artworkUrl100": f"https://covers.test/{id_}.png",
    }
    record.update(overrides)
    return record


@dataclass
class FakeUpstream:
    """Plays both iTunes (host itunes.test) and the cover image host."""

    records: list[Any] = field(default_factory=list)
    itunes_status: int = 200
    cover_status: int = 200
    cover_downloads: int = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "itunes.test":
            if self.itunes_status != 200:
                return httpx.Response(self.itunes_status)
            return httpx.Response(
                200, json={"resultCount": len(self.records), "results": self.records}
            )
        self.cover_downloads += 1
        if self.cover_status != 200:
            return httpx.Response(self.cover_status)
        return httpx.Response(
            200, content=RED_PNG, headers={"content-type": "image/png"}
        )


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
async def http(upstream: FakeUpstream) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.MockTransport(upstream.handle)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://itunes.test"
    ) as client:
        yield client


@pytest.fixture
def run(db_pool: AsyncConnectionPool, http: httpx.AsyncClient) -> Run:
    async def run_ingestion() -> IngestionSummary:
        summary = await ingest(
            SearchParams(term="anything"),
            pool=db_pool,
            itunes=ITunesClient(http),
            http=http,
            palette_concurrency=3,
        )
        # Must hold for every run, whatever happened.
        assert summary.fetched == summary.created + summary.updated + summary.skipped
        return summary

    return run_ingestion


async def stored(pool: AsyncConnectionPool) -> dict[int, dict[str, Any]]:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT id, title, palette, updated_at FROM audiobooks ORDER BY id"
        )
        rows = await cursor.fetchall()
    return {
        id_: {"title": title, "palette": palette, "updated_at": updated_at}
        for id_, title, palette, updated_at in rows
    }


# --- first and repeated runs ----------------------------------------------------


async def test_first_run_creates_everything(
    run: Run, upstream: FakeUpstream, db_pool: AsyncConnectionPool
) -> None:
    upstream.records = [raw_record(1), raw_record(2), raw_record(3)]

    summary = await run()

    assert summary == IngestionSummary(
        source="itunes", fetched=3, created=3, updated=0, skipped=0, invalid=[]
    )
    rows = await stored(db_pool)
    assert sorted(rows) == [1, 2, 3]
    assert all(row["palette"] == RED_PALETTE for row in rows.values())


async def test_repeated_run_skips_everything_without_downloading_covers(
    run: Run, upstream: FakeUpstream, db_pool: AsyncConnectionPool
) -> None:
    upstream.records = [raw_record(1), raw_record(2)]
    await run()
    before = await stored(db_pool)
    downloads_after_first_run = upstream.cover_downloads

    summary = await run()

    assert (summary.created, summary.updated, summary.skipped) == (0, 0, 2)
    assert upstream.cover_downloads == downloads_after_first_run
    assert await stored(db_pool) == before  # updated_at included


async def test_changed_upstream_record_is_updated(
    run: Run, upstream: FakeUpstream, db_pool: AsyncConnectionPool
) -> None:
    upstream.records = [raw_record(1), raw_record(2)]
    await run()
    before = await stored(db_pool)

    upstream.records = [raw_record(1, collectionName="New title"), raw_record(2)]
    summary = await run()

    assert (summary.created, summary.updated, summary.skipped) == (0, 1, 1)
    after = await stored(db_pool)
    assert after[1]["title"] == "New title"
    assert after[1]["updated_at"] > before[1]["updated_at"]
    assert after[2] == before[2]


async def test_change_in_a_field_we_dont_store_is_skipped(
    run: Run, upstream: FakeUpstream
) -> None:
    upstream.records = [raw_record(1, collectionPrice=9.99)]
    await run()

    upstream.records = [raw_record(1, collectionPrice=4.99)]
    summary = await run()

    assert (summary.created, summary.updated, summary.skipped) == (0, 0, 1)


async def test_new_and_existing_records_in_one_run(
    run: Run, upstream: FakeUpstream
) -> None:
    upstream.records = [raw_record(1)]
    await run()

    upstream.records = [raw_record(1), raw_record(2)]
    summary = await run()

    assert (summary.created, summary.updated, summary.skipped) == (1, 0, 1)


# --- invalid records ------------------------------------------------------------


async def test_invalid_records_are_skipped_and_reported(
    run: Run, upstream: FakeUpstream, db_pool: AsyncConnectionPool
) -> None:
    upstream.records = [
        raw_record(1),
        raw_record(2, collectionName=""),
        "garbage",
        raw_record(1, collectionName="Duplicate"),
    ]

    summary = await run()

    assert (summary.fetched, summary.created, summary.skipped) == (4, 1, 3)
    assert summary.invalid == [
        InvalidRecord(source_id=2, reason="missing title"),
        InvalidRecord(source_id=None, reason="not an object"),
        InvalidRecord(source_id=1, reason="duplicate collectionId in batch"),
    ]
    assert list(await stored(db_pool)) == [1]


async def test_nothing_fetched(run: Run, upstream: FakeUpstream) -> None:
    upstream.records = []

    summary = await run()

    assert summary == IngestionSummary(
        source="itunes", fetched=0, created=0, updated=0, skipped=0, invalid=[]
    )


# --- palettes and retries -------------------------------------------------------


async def test_failed_cover_still_stores_the_record(
    run: Run, upstream: FakeUpstream, db_pool: AsyncConnectionPool
) -> None:
    upstream.records = [raw_record(1)]
    upstream.cover_status = 404

    summary = await run()

    assert summary.created == 1
    assert (await stored(db_pool))[1]["palette"] is None


async def test_failed_palette_is_retried_on_the_next_run(
    run: Run, upstream: FakeUpstream, db_pool: AsyncConnectionPool
) -> None:
    upstream.records = [raw_record(1)]
    upstream.cover_status = 404
    await run()

    upstream.cover_status = 200
    summary = await run()

    assert (summary.created, summary.updated, summary.skipped) == (0, 1, 0)
    assert (await stored(db_pool))[1]["palette"] == RED_PALETTE


async def test_palette_retry_that_fails_again_is_skipped(
    run: Run, upstream: FakeUpstream, db_pool: AsyncConnectionPool
) -> None:
    upstream.records = [raw_record(1)]
    upstream.cover_status = 404
    await run()
    before = await stored(db_pool)

    summary = await run()

    assert (summary.created, summary.updated, summary.skipped) == (0, 0, 1)
    assert upstream.cover_downloads == 2  # it did try again
    assert await stored(db_pool) == before


async def test_record_without_cover_is_never_downloaded_or_retried(
    run: Run, upstream: FakeUpstream, db_pool: AsyncConnectionPool
) -> None:
    upstream.records = [raw_record(1, artworkUrl100=None)]

    first = await run()
    second = await run()

    assert first.created == 1
    assert (second.updated, second.skipped) == (0, 1)
    assert upstream.cover_downloads == 0
    assert (await stored(db_pool))[1]["palette"] is None


# --- fallback -------------------------------------------------------------------


async def test_falls_back_to_the_sample_when_itunes_fails(
    run: Run, upstream: FakeUpstream, db_pool: AsyncConnectionPool
) -> None:
    upstream.itunes_status = 503
    sample = await load_sample()

    summary = await run()

    assert summary.source == "fallback"
    assert summary.fetched == len(sample)
    assert summary.created == len(sample)
    assert len(await stored(db_pool)) == len(sample)
