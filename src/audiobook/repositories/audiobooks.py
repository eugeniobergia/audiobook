from collections.abc import AsyncGenerator, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from audiobook.models import Audiobook

# All functions take a connection from the caller. Reads run in whatever
# transaction the connection has; see upsert_many for how writes commit.


class UpsertResult(NamedTuple):
    created: int
    updated: int


@dataclass(frozen=True)
class GenreCount:
    genre: str | None  # None: audiobooks without a genre
    count: int


@dataclass(frozen=True)
class YearCount:
    year: int | None  # None: audiobooks without a release date
    count: int


@dataclass(frozen=True)
class Stats:
    by_genre: list[GenreCount]
    by_release_year: list[YearCount]


class StoredState(NamedTuple):
    """What ingestion needs to know about a stored audiobook to classify it."""

    content_hash: str
    missing_palette: bool


async def get_states(
    conn: AsyncConnection, ids: Sequence[int]
) -> dict[int, StoredState]:
    """Stored hash and palette status for the given ids, in one query.

    Ids that aren't stored are simply absent from the result.
    """
    if not ids:
        return {}
    cursor = await conn.execute(
        """
        SELECT id, content_hash, palette IS NULL
        FROM audiobooks
        WHERE id = ANY(%s)
        """,
        (list(ids),),
    )
    return {
        id_: StoredState(content_hash, missing_palette)
        for id_, content_hash, missing_palette in await cursor.fetchall()
    }


_UPSERT = """
INSERT INTO audiobooks (
    id, title, author, description, genre, release_date, cover_url, palette,
    content_hash
)
VALUES (
    %(id)s, %(title)s, %(author)s, %(description)s, %(genre)s, %(release_date)s,
    %(cover_url)s, %(palette)s, %(content_hash)s
)
ON CONFLICT (id) DO UPDATE SET
    title = EXCLUDED.title,
    author = EXCLUDED.author,
    description = EXCLUDED.description,
    genre = EXCLUDED.genre,
    release_date = EXCLUDED.release_date,
    cover_url = EXCLUDED.cover_url,
    palette = EXCLUDED.palette,
    content_hash = EXCLUDED.content_hash,
    updated_at = now()
-- Only touch the row if something we store actually changed (a palette counts:
-- that's how a retried palette gets saved). Otherwise it's a no-op and
-- updated_at keeps its value.
WHERE (audiobooks.content_hash, audiobooks.palette)
    IS DISTINCT FROM (EXCLUDED.content_hash, EXCLUDED.palette)
-- One row back per insert or real update, none for a no-op. xmax is a Postgres
-- system column that is 0 on a row this statement freshly inserted.
RETURNING (xmax = 0) AS inserted
"""


async def upsert_many(
    conn: AsyncConnection, books: Iterable[Audiobook]
) -> UpsertResult:
    """Insert new audiobooks and update changed ones (matched by id).

    Returns how many rows were really inserted and updated, as counted by the
    database: a book identical to the stored row (same hash and palette) is
    neither. `updated_at` is set by the database (the column default on insert,
    now() on update); the model's value is ignored.

    All or nothing, and committed when it returns: if any book fails, none are
    stored. To combine it with other writes, call it inside your own
    `async with conn.transaction():` block; it then joins that transaction (as a
    savepoint) and you decide whether it all commits.
    """
    params = [_to_params(book) for book in books]
    if not params:
        return UpsertResult(created=0, updated=0)
    created = updated = 0
    # Its own transaction, committed when the block ends, unless the caller
    # already opened one with conn.transaction(): then it's a savepoint inside it.
    async with conn.transaction(), conn.cursor() as cursor:
        await cursor.executemany(_UPSERT, params, returning=True)
        # One result set per book: a single row for an insert/update, empty for
        # a no-op.
        async for result in cursor.results():
            for (inserted,) in await result.fetchall():
                if inserted:
                    created += 1
                else:
                    updated += 1
    return UpsertResult(created=created, updated=updated)


def _to_params(book: Audiobook) -> dict[str, Any]:
    return {
        "id": book.id,
        "title": book.title,
        "author": book.author,
        "description": book.description,
        "genre": book.genre,
        "release_date": book.release_date,
        "cover_url": book.cover_url,
        # JSONB has no tuple type: store the palette as a JSON array.
        "palette": Jsonb(list(book.palette)) if book.palette is not None else None,
        "content_hash": book.content_hash,
    }


# --- Reads ----------------------------------------------------------------------

_SELECT_AUDIOBOOK = sql.SQL("""
SELECT id, title, author, description, genre, release_date, cover_url, palette,
       content_hash, updated_at
FROM audiobooks
""")


async def get_audiobook(conn: AsyncConnection, id_: int) -> Audiobook | None:
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(_SELECT_AUDIOBOOK + sql.SQL("WHERE id = %s"), (id_,))
        row = await cursor.fetchone()
    return _to_audiobook(row) if row is not None else None


async def list_audiobooks(
    conn: AsyncConnection,
    *,
    q: str | None = None,
    genre: str | None = None,
    limit: int,
    offset: int,
) -> tuple[list[Audiobook], int]:
    """One page of audiobooks sorted by title (then id), and the total matching.

    `q` matches a case-insensitive substring of the title or the author; `genre`
    matches the genre exactly, ignoring case. Both are optional and combine.
    """
    conditions: list[sql.Composable] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if q:
        conditions.append(sql.SQL("(title ILIKE %(q)s OR author ILIKE %(q)s)"))
        params["q"] = f"%{_escape_like(q)}%"
    if genre:
        conditions.append(sql.SQL("lower(genre) = lower(%(genre)s)"))
        params["genre"] = genre
    where = (
        sql.SQL("WHERE ") + sql.SQL(" AND ").join(conditions)
        if conditions
        else sql.SQL("")
    )

    async with conn.cursor(row_factory=dict_row) as cursor:
        # id breaks ties between equal titles, so pages never overlap or skip.
        await cursor.execute(
            _SELECT_AUDIOBOOK
            + where
            + sql.SQL(" ORDER BY title, id LIMIT %(limit)s OFFSET %(offset)s"),
            params,
        )
        rows = await cursor.fetchall()
    # A separate count: a page past the end has no rows to carry a window count.
    cursor_ = await conn.execute(
        sql.SQL("SELECT count(*) FROM audiobooks ") + where, params
    )
    count_row = await cursor_.fetchone()
    total = count_row[0] if count_row is not None else 0
    return [_to_audiobook(row) for row in rows], total


async def stream_audiobooks(
    conn: AsyncConnection, batch_size: int = 1000
) -> AsyncGenerator[list[Audiobook]]:
    """Every audiobook, ordered by id, in batches of at most `batch_size`.

    Uses a server-side cursor: Postgres keeps the query's results and we fetch
    one batch at a time, so memory stays flat however big the catalog is. It's
    a single query, so it sees one consistent snapshot of the table even while
    ingestions write to it.

    Holds the connection (and an open transaction) until the iteration ends.
    """
    # A named cursor is a server-side one; it only exists inside a transaction.
    async with (
        conn.transaction(),
        conn.cursor(name="audiobooks_export", row_factory=dict_row) as cursor,
    ):
        await cursor.execute(_SELECT_AUDIOBOOK + sql.SQL("ORDER BY id"))
        while rows := await cursor.fetchmany(batch_size):
            yield [_to_audiobook(row) for row in rows]


async def get_stats(conn: AsyncConnection) -> Stats:
    """Audiobook counts by genre (most common first) and by release year (oldest
    first). Missing genres and dates are counted under None, listed last."""
    genres = await conn.execute("""
        SELECT genre, count(*)
        FROM audiobooks
        GROUP BY genre
        ORDER BY genre IS NULL, count(*) DESC, genre
    """)
    years = await conn.execute("""
        SELECT EXTRACT(YEAR FROM release_date)::int AS year, count(*)
        FROM audiobooks
        GROUP BY year
        ORDER BY year NULLS LAST
    """)
    return Stats(
        by_genre=[
            GenreCount(genre=genre, count=count)
            for genre, count in await genres.fetchall()
        ],
        by_release_year=[
            YearCount(year=year, count=count) for year, count in await years.fetchall()
        ],
    )


def _escape_like(text: str) -> str:
    """Make LIKE's wildcards literal, so searching "100%" doesn't match "1000"."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _to_audiobook(row: dict[str, Any]) -> Audiobook:
    palette = row["palette"]
    # JSONB comes back as a list; the model (strict) wants a tuple.
    return Audiobook.model_validate(
        {**row, "palette": tuple(palette) if palette is not None else None}
    )
