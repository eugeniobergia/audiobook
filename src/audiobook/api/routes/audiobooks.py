from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import StreamingResponse
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from audiobook.api.dependencies import get_conn, get_pool
from audiobook.api.errors import AppError, ErrorResponse
from audiobook.api.schemas import AudiobookPage, AudiobookResponse
from audiobook.repositories import audiobooks as repository

router = APIRouter(prefix="/audiobooks", tags=["audiobooks"])

Connection = Annotated[AsyncConnection, Depends(get_conn)]

# Rows fetched from Postgres (and written to the client) per batch.
EXPORT_BATCH_SIZE = 1000


@router.get("", summary="List audiobooks")
async def list_audiobooks(
    conn: Connection,
    q: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=200,
            description="Case-insensitive text to find in the title or author.",
        ),
    ] = None,
    genre: Annotated[
        str | None,
        Query(min_length=1, max_length=200, description="Exact genre, any case."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Page size.")] = 20,
    offset: Annotated[int, Query(ge=0, description="Audiobooks to skip.")] = 0,
) -> AudiobookPage:
    """Audiobooks sorted by title, a page at a time. `total` counts every match,
    so clients can tell how many pages there are."""
    books, total = await repository.list_audiobooks(
        conn,
        q=_blank_to_none(q),
        genre=_blank_to_none(genre),
        limit=limit,
        offset=offset,
    )
    return AudiobookPage(
        items=[AudiobookResponse.from_audiobook(book) for book in books],
        total=total,
        limit=limit,
        offset=offset,
    )


# Registered before "/{audiobook_id}": routes match in order, and "export" would
# otherwise be read as an (invalid) id.
@router.get(
    "/export",
    summary="Export all audiobooks (NDJSON)",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "One JSON object per line, with the same fields as "
            "the items of `GET /audiobooks`, ordered by id.",
            "content": {"application/x-ndjson": {"schema": {"type": "string"}}},
        }
    },
)
async def export_audiobooks(
    pool: Annotated[AsyncConnectionPool, Depends(get_pool)],
) -> StreamingResponse:
    """Download the whole catalog as newline-delimited JSON.

    Streamed straight from the database a batch at a time, so it works the same
    for a hundred audiobooks or a million, without loading them all in memory.
    """
    return StreamingResponse(
        _ndjson_batches(pool),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="audiobooks.ndjson"'},
    )


async def _ndjson_batches(pool: AsyncConnectionPool) -> AsyncGenerator[str]:
    """NDJSON text, one chunk per batch of audiobooks.

    The connection is borrowed here, not through a dependency: the body is sent
    after the route returns, so it must live exactly as long as the stream.
    """
    # aclosing: if the client disconnects mid-download, the database stream is
    # closed right away (ending its transaction) before the connection goes back
    # to the pool, instead of whenever the garbage collector gets to it.
    async with (
        pool.connection() as conn,
        aclosing(
            repository.stream_audiobooks(conn, batch_size=EXPORT_BATCH_SIZE)
        ) as batches,
    ):
        async for batch in batches:
            yield "".join(
                AudiobookResponse.from_audiobook(book).model_dump_json() + "\n"
                for book in batch
            )


@router.get(
    "/{audiobook_id}",
    summary="Get an audiobook",
    responses={404: {"model": ErrorResponse, "description": "No such audiobook"}},
)
async def get_audiobook(
    conn: Connection,
    audiobook_id: Annotated[int, Path(gt=0, description="The iTunes collectionId.")],
) -> AudiobookResponse:
    book = await repository.get_audiobook(conn, audiobook_id)
    if book is None:
        raise AppError(404, "not_found", f"Audiobook {audiobook_id} not found.")
    return AudiobookResponse.from_audiobook(book)


def _blank_to_none(value: str | None) -> str | None:
    """Trim a text filter; a blank one means "no filter"."""
    if value is None:
        return None
    return value.strip() or None
