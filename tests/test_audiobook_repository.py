from datetime import date, datetime
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from audiobook.models import Audiobook
from audiobook.repositories.audiobooks import (
    StoredState,
    UpsertResult,
    get_states,
    upsert_many,
)


def make_book(**overrides: Any) -> Audiobook:
    fields: dict[str, Any] = {
        "id": 1,
        "title": "Dune",
        "author": "Frank Herbert",
        "description": "A desert planet.",
        "genre": "Sci-Fi & Fantasy",
        "release_date": date(1965, 8, 1),
        "cover_url": "https://x.test/dune.jpg",
        "content_hash": "hash-1",
        "palette": None,
    }
    fields.update(overrides)
    return Audiobook(**fields)


# Each helper uses its own connection, so every call is its own committed
# transaction, like separate ingestion runs. That matters for updated_at:
# Postgres' now() is the start time of the current transaction.


async def upsert(pool: AsyncConnectionPool, *books: Audiobook) -> UpsertResult:
    async with pool.connection() as conn:
        return await upsert_many(conn, books)


async def fetch_row(pool: AsyncConnectionPool, id_: int) -> dict[str, Any]:
    async with pool.connection() as conn:
        cursor = await conn.execute("SELECT * FROM audiobooks WHERE id = %s", (id_,))
        row = await cursor.fetchone()
        assert row is not None, f"audiobook {id_} not stored"
        assert cursor.description is not None
        return dict(
            zip([column.name for column in cursor.description], row, strict=True)
        )


async def updated_at(pool: AsyncConnectionPool, id_: int) -> datetime:
    value = (await fetch_row(pool, id_))["updated_at"]
    assert isinstance(value, datetime)
    return value


# --- get_states -----------------------------------------------------------------


async def test_get_states_of_no_ids(db_pool: AsyncConnectionPool) -> None:
    async with db_pool.connection() as conn:
        assert await get_states(conn, []) == {}


async def test_get_states_reports_hash_and_palette_status(
    db_pool: AsyncConnectionPool,
) -> None:
    await upsert(
        db_pool,
        make_book(id=1, content_hash="a", palette=("#112233",)),
        make_book(id=2, content_hash="b", palette=None),
    )

    async with db_pool.connection() as conn:
        states = await get_states(conn, [1, 2, 3])

    assert states == {
        1: StoredState(content_hash="a", missing_palette=False),
        2: StoredState(content_hash="b", missing_palette=True),
    }


# --- upsert_many: inserts -------------------------------------------------------


async def test_upsert_of_nothing(db_pool: AsyncConnectionPool) -> None:
    assert await upsert(db_pool) == UpsertResult(created=0, updated=0)


async def test_upsert_inserts_new_books_with_every_field(
    db_pool: AsyncConnectionPool,
) -> None:
    book = make_book(palette=("#112233", "#aabbcc"))

    assert await upsert(db_pool, book) == UpsertResult(created=1, updated=0)

    row = await fetch_row(db_pool, book.id)
    assert row["title"] == "Dune"
    assert row["author"] == "Frank Herbert"
    assert row["description"] == "A desert planet."
    assert row["genre"] == "Sci-Fi & Fantasy"
    assert row["release_date"] == date(1965, 8, 1)
    assert row["cover_url"] == "https://x.test/dune.jpg"
    assert row["palette"] == ["#112233", "#aabbcc"]  # a JSON array in Postgres
    assert row["content_hash"] == "hash-1"
    assert row["updated_at"] is not None


async def test_upsert_stores_missing_optional_fields_as_null(
    db_pool: AsyncConnectionPool,
) -> None:
    book = make_book(
        author=None, description=None, genre=None, release_date=None, cover_url=None
    )

    await upsert(db_pool, book)

    row = await fetch_row(db_pool, book.id)
    assert [row[c] for c in ("author", "description", "genre", "release_date")] == [
        None,
        None,
        None,
        None,
    ]
    assert row["cover_url"] is None
    assert row["palette"] is None


# --- upsert_many: updates and no-ops --------------------------------------------


async def test_identical_upsert_is_a_no_op(db_pool: AsyncConnectionPool) -> None:
    book = make_book(palette=("#112233",))
    await upsert(db_pool, book)
    first_updated_at = await updated_at(db_pool, book.id)

    assert await upsert(db_pool, book) == UpsertResult(created=0, updated=0)
    assert await updated_at(db_pool, book.id) == first_updated_at


async def test_changed_hash_updates_every_field(db_pool: AsyncConnectionPool) -> None:
    await upsert(db_pool, make_book(palette=("#112233",)))
    first_updated_at = await updated_at(db_pool, 1)
    changed = make_book(
        title="Dune Messiah",
        author="F. Herbert",
        description=None,
        genre="Classics",
        release_date=date(1969, 1, 1),
        cover_url="https://x.test/messiah.jpg",
        content_hash="hash-2",
        palette=None,
    )

    assert await upsert(db_pool, changed) == UpsertResult(created=0, updated=1)

    row = await fetch_row(db_pool, 1)
    assert row["title"] == "Dune Messiah"
    assert row["author"] == "F. Herbert"
    assert row["description"] is None
    assert row["genre"] == "Classics"
    assert row["release_date"] == date(1969, 1, 1)
    assert row["cover_url"] == "https://x.test/messiah.jpg"
    assert row["content_hash"] == "hash-2"
    assert (
        row["palette"] is None
    )  # a changed record's palette is replaced, even by NULL
    assert row["updated_at"] > first_updated_at


async def test_new_palette_for_same_hash_is_an_update(
    db_pool: AsyncConnectionPool,
) -> None:
    # The palette retry: same content, but this time the palette was computed.
    await upsert(db_pool, make_book(palette=None))

    result = await upsert(db_pool, make_book(palette=("#112233",)))

    assert result == UpsertResult(created=0, updated=1)
    assert (await fetch_row(db_pool, 1))["palette"] == ["#112233"]


async def test_failed_palette_retry_is_a_no_op(db_pool: AsyncConnectionPool) -> None:
    await upsert(db_pool, make_book(palette=None))
    first_updated_at = await updated_at(db_pool, 1)

    assert await upsert(db_pool, make_book(palette=None)) == UpsertResult(0, 0)
    assert await updated_at(db_pool, 1) == first_updated_at


async def test_mixed_batch_counts(db_pool: AsyncConnectionPool) -> None:
    await upsert(
        db_pool,
        make_book(id=1, content_hash="a"),
        make_book(id=2, content_hash="b"),
    )

    result = await upsert(
        db_pool,
        make_book(id=1, content_hash="a"),  # unchanged
        make_book(id=2, content_hash="b2"),  # changed
        make_book(id=3, content_hash="c"),  # new
        make_book(id=4, content_hash="d"),  # new
    )

    assert result == UpsertResult(created=2, updated=1)


async def test_upsert_runs_in_the_callers_transaction(
    db_pool: AsyncConnectionPool,
) -> None:
    async with db_pool.connection() as conn:
        await upsert_many(conn, [make_book()])
        await conn.rollback()

    async with db_pool.connection() as conn:
        assert await get_states(conn, [1]) == {}


@pytest.mark.parametrize("palette", [("#112233",), None])
async def test_states_reflect_upserted_palette(
    db_pool: AsyncConnectionPool, palette: tuple[str, ...] | None
) -> None:
    await upsert(db_pool, make_book(palette=palette))

    async with db_pool.connection() as conn:
        states = await get_states(conn, [1])

    assert states[1].missing_palette is (palette is None)
