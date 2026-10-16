from typing import Annotated

from fastapi import APIRouter, Depends
from psycopg import AsyncConnection

from audiobook.api.dependencies import get_conn
from audiobook.api.schemas import GenreCount, StatsResponse, YearCount
from audiobook.repositories import audiobooks as repository

router = APIRouter(tags=["stats"])


@router.get("/stats", summary="Audiobook counts by genre and release year")
async def get_stats(
    conn: Annotated[AsyncConnection, Depends(get_conn)],
) -> StatsResponse:
    """How many audiobooks there are per genre and per release year. Audiobooks
    without a genre or a release date are counted under `null`."""
    stats = await repository.get_stats(conn)
    return StatsResponse(
        by_genre=[
            GenreCount(genre=row.genre, count=row.count) for row in stats.by_genre
        ],
        by_release_year=[
            YearCount(year=row.year, count=row.count) for row in stats.by_release_year
        ],
    )
