from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from psycopg import AsyncConnection

from audiobook.api.dependencies import get_conn
from audiobook.api.errors import AppError, ErrorResponse
from audiobook.api.schemas import AudiobookPage, AudiobookResponse
from audiobook.repositories import audiobooks as repository

router = APIRouter(prefix="/audiobooks", tags=["audiobooks"])

Connection = Annotated[AsyncConnection, Depends(get_conn)]


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
