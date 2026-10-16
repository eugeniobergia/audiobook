from collections.abc import Iterable, Sequence
from typing import Any, NamedTuple

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from audiobook.models import Audiobook

# All functions take a connection from the caller. Reads run in whatever
# transaction the connection has; see upsert_many for how writes commit.


class UpsertResult(NamedTuple):
    created: int
    updated: int


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
