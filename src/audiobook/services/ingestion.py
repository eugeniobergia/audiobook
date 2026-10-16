import logging
from typing import Literal

import httpx
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from audiobook.clients.itunes import ITunesClient, SearchParams, fetch_audiobooks
from audiobook.models import Audiobook
from audiobook.repositories.audiobooks import StoredState, get_states, upsert_many
from audiobook.services.normalize import InvalidRecord, normalize_batch
from audiobook.services.palette import compute_palettes

logger = logging.getLogger(__name__)


class IngestionSummary(BaseModel):
    """What one ingestion run did. Always: fetched == created + updated + skipped."""

    source: Literal["itunes", "fallback"] = Field(
        description="Where the records came from: iTunes, or the saved sample "
        "when iTunes was unavailable."
    )
    fetched: int = Field(description="Records received from the source.")
    created: int = Field(description="New audiobooks stored.")
    updated: int = Field(description="Stored audiobooks that changed.")
    skipped: int = Field(
        description="Records not written: unchanged ones, plus the invalid ones."
    )
    invalid: list[InvalidRecord] = Field(
        description="Records that couldn't be used, and why."
    )


async def ingest(
    params: SearchParams,
    *,
    pool: AsyncConnectionPool,
    itunes: ITunesClient,
    http: httpx.AsyncClient,
    palette_concurrency: int,
) -> IngestionSummary:
    """Fetch audiobooks for `params`, clean them and store what's new or changed.

    Safe to repeat: identical records are skipped, changed ones are updated. A
    database connection is only held for the two short queries, never while
    downloading covers.
    """
    fetched = await fetch_audiobooks(itunes, params)
    books, invalid = normalize_batch(fetched.records)

    async with pool.connection() as conn:
        states = await get_states(conn, [book.id for book in books])
    to_write = [book for book in books if _needs_write(book, states.get(book.id))]
    to_write = await _attach_palettes(to_write, http, palette_concurrency)

    # One transaction: the whole batch is stored, or none of it.
    async with pool.connection() as conn:
        result = await upsert_many(conn, to_write)

    # The upsert counts what it really inserted or changed; everything else we
    # fetched (unchanged, failed palette retries, invalid) was skipped.
    summary = IngestionSummary(
        source=fetched.source,
        fetched=len(fetched.records),
        created=result.created,
        updated=result.updated,
        skipped=len(fetched.records) - result.created - result.updated,
        invalid=invalid,
    )
    logger.info(
        "Ingested %r from %s: %d fetched, %d created, %d updated, %d skipped "
        "(%d invalid)",
        params.term,
        summary.source,
        summary.fetched,
        summary.created,
        summary.updated,
        summary.skipped,
        len(invalid),
    )
    return summary


def _needs_write(book: Audiobook, stored: StoredState | None) -> bool:
    """New, changed, or worth retrying a palette that failed before."""
    if stored is None or stored.content_hash != book.content_hash:
        return True
    # Same content: only a missing palette is worth another try, and only if
    # there is a cover to compute it from.
    return stored.missing_palette and book.cover_url is not None


async def _attach_palettes(
    books: list[Audiobook], http: httpx.AsyncClient, concurrency: int
) -> list[Audiobook]:
    """The same books, each with the palette of its cover (None if it has none,
    or the palette couldn't be computed)."""
    with_cover = [book for book in books if book.cover_url is not None]
    palettes = await compute_palettes(
        http, [book.cover_url for book in with_cover if book.cover_url], concurrency
    )
    palette_by_id = {
        book.id: palette for book, palette in zip(with_cover, palettes, strict=True)
    }
    # model_copy doesn't re-validate; the palettes come straight from
    # extract_palette, which only produces valid ones.
    return [
        book.model_copy(update={"palette": palette_by_id.get(book.id)})
        for book in books
    ]
