from datetime import date, datetime
from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from audiobook.models import Audiobook
from audiobook.repositories.audiobooks import (
    GenreCount,
    Stats,
    StoredState,
    UpsertResult,
    YearCount,
    get_audiobook,
    get_states,
    get_stats,
    list_audiobooks,
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


async def test_upsert_commits_on_its_own(db_pool: AsyncConnectionPool) -> None:
    async with db_pool.connection() as conn:
        await upsert_many(conn, [make_book()])
        await conn.rollback()  # too late: upsert_many already committed

    async with db_pool.connection() as conn:
        assert 1 in await get_states(conn, [1])


async def test_upsert_joins_the_callers_explicit_transaction(
    db_pool: AsyncConnectionPool,
) -> None:
    # force_rollback: undo everything in the transaction block when it ends.
    async with (
        db_pool.connection() as conn,
        conn.transaction(force_rollback=True),
    ):
        await upsert_many(conn, [make_book()])

    async with db_pool.connection() as conn:
        assert await get_states(conn, [1]) == {}


async def test_upsert_is_all_or_nothing(db_pool: AsyncConnectionPool) -> None:
    # 2**63 doesn't fit in BIGINT: Postgres rejects the third row, after the
    # first two were already sent. autocommit=True: no help from the caller.
    books = [make_book(id=1), make_book(id=2), make_book(id=2**63)]

    async with db_pool.connection() as conn:
        await conn.set_autocommit(True)
        with pytest.raises(psycopg.errors.NumericValueOutOfRange):
            await upsert_many(conn, books)

    async with db_pool.connection() as conn:
        assert await get_states(conn, [1, 2]) == {}


@pytest.mark.parametrize("palette", [("#112233",), None])
async def test_states_reflect_upserted_palette(
    db_pool: AsyncConnectionPool, palette: tuple[str, ...] | None
) -> None:
    await upsert(db_pool, make_book(palette=palette))

    async with db_pool.connection() as conn:
        states = await get_states(conn, [1])

    assert states[1].missing_palette is (palette is None)


# --- get_audiobook --------------------------------------------------------------


async def test_get_audiobook_round_trips_every_field(
    db_pool: AsyncConnectionPool,
) -> None:
    book = make_book(palette=("#112233", "#aabbcc"))
    await upsert(db_pool, book)

    async with db_pool.connection() as conn:
        found = await get_audiobook(conn, book.id)

    assert found is not None
    assert found.updated_at is not None  # set by the database
    assert found.model_copy(update={"updated_at": None}) == book
    assert found.palette == ("#112233", "#aabbcc")  # a tuple again, not a list


async def test_get_missing_audiobook(db_pool: AsyncConnectionPool) -> None:
    async with db_pool.connection() as conn:
        assert await get_audiobook(conn, 404) is None


# --- list_audiobooks ------------------------------------------------------------


async def list_page(
    pool: AsyncConnectionPool,
    *,
    q: str | None = None,
    genre: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[int], int]:
    """(ids on the page, total matching)."""
    async with pool.connection() as conn:
        books, total = await list_audiobooks(
            conn, q=q, genre=genre, limit=limit, offset=offset
        )
    return [book.id for book in books], total


@pytest.fixture
async def catalog(db_pool: AsyncConnectionPool) -> None:
    await upsert(
        db_pool,
        # id 3 is stored before id 1: without the id tie-break, the two "Dune"
        # would likely come back in storage order (3, 1).
        make_book(id=3, title="Dune", author="Brian Herbert", genre="Sci-Fi"),
        make_book(id=1, title="Dune", author="Frank Herbert", genre="Sci-Fi"),
        make_book(id=2, title="Emma", author="Jane Austen", genre="Classics"),
        make_book(id=4, title="Anathem", author="Neal Stephenson", genre="sci-fi"),
        make_book(id=5, title="100% Focus", author=None, genre=None),
        make_book(id=6, title="2000 Days", author="An Author", genre="Business"),
        make_book(id=7, title="my_book", author="X", genre="Business"),
        # Decoys: they'd match "my_book" / "100%" if _ and % stayed wildcards.
        make_book(id=8, title="myxbook", author="Y", genre="Business"),
        make_book(id=9, title="Over 1000 Tips", author="Z", genre="Business"),
    )


@pytest.mark.usefixtures("catalog")
async def test_list_sorts_by_title_then_id(db_pool: AsyncConnectionPool) -> None:
    ids, total = await list_page(db_pool)

    # "Dune" twice: id breaks the tie. Digits sort before letters. (Titles avoid
    # punctuation-only differences: how those sort depends on the collation.)
    assert ids == [5, 6, 4, 1, 3, 2, 7, 8, 9]
    assert total == 9


@pytest.mark.usefixtures("catalog")
async def test_list_pages_dont_overlap_or_skip(db_pool: AsyncConnectionPool) -> None:
    pages = [await list_page(db_pool, limit=3, offset=offset) for offset in (0, 3, 6)]

    assert [ids for ids, _ in pages] == [[5, 6, 4], [1, 3, 2], [7, 8, 9]]
    assert {total for _, total in pages} == {9}


@pytest.mark.usefixtures("catalog")
async def test_page_past_the_end_still_reports_the_total(
    db_pool: AsyncConnectionPool,
) -> None:
    assert await list_page(db_pool, offset=100) == ([], 9)


@pytest.mark.usefixtures("catalog")
@pytest.mark.parametrize(
    ("q", "expected_ids"),
    [
        pytest.param("dune", [1, 3], id="title-case-insensitive"),
        pytest.param("HERBERT", [1, 3], id="author-case-insensitive"),
        pytest.param("auste", [2], id="author-substring"),
        pytest.param("ath", [4], id="title-substring"),
        pytest.param("100%", [5], id="percent-is-literal"),
        pytest.param("my_book", [7], id="underscore-is-literal"),
        pytest.param("00 d", [6], id="spaces-kept"),
        pytest.param("nothing like it", [], id="no-match"),
    ],
)
async def test_list_search(
    db_pool: AsyncConnectionPool, q: str, expected_ids: list[int]
) -> None:
    ids, total = await list_page(db_pool, q=q)

    assert ids == expected_ids
    assert total == len(expected_ids)


@pytest.mark.usefixtures("catalog")
@pytest.mark.parametrize(
    ("genre", "expected_ids"),
    [
        pytest.param("Sci-Fi", [4, 1, 3], id="exact"),
        pytest.param("SCI-FI", [4, 1, 3], id="case-insensitive"),
        pytest.param("Sci", [], id="not-a-substring-match"),
    ],
)
async def test_list_genre_filter(
    db_pool: AsyncConnectionPool, genre: str, expected_ids: list[int]
) -> None:
    assert await list_page(db_pool, genre=genre) == (expected_ids, len(expected_ids))


@pytest.mark.usefixtures("catalog")
async def test_list_filters_combine(db_pool: AsyncConnectionPool) -> None:
    assert await list_page(db_pool, q="dune", genre="sci-fi", limit=1) == ([1], 2)


async def test_list_of_empty_catalog(db_pool: AsyncConnectionPool) -> None:
    assert await list_page(db_pool) == ([], 0)


# --- get_stats ------------------------------------------------------------------


async def test_stats_count_by_genre_and_year_with_unknown_buckets(
    db_pool: AsyncConnectionPool,
) -> None:
    await upsert(
        db_pool,
        make_book(id=1, genre="Fiction", release_date=date(2018, 4, 26)),
        make_book(id=2, genre="Fiction", release_date=date(2018, 1, 1)),
        make_book(id=3, genre="Fiction", release_date=date(2001, 1, 1)),
        make_book(id=4, genre="Biography", release_date=None),
        make_book(id=5, genre="Arts", release_date=date(2020, 6, 1)),
        make_book(id=6, genre=None, release_date=None),
        make_book(id=7, genre=None, release_date=date(2020, 1, 1)),
    )

    async with db_pool.connection() as conn:
        stats = await get_stats(conn)

    assert stats == Stats(
        by_genre=[
            GenreCount(genre="Fiction", count=3),
            GenreCount(genre="Arts", count=1),  # ties: alphabetical
            GenreCount(genre="Biography", count=1),
            GenreCount(genre=None, count=2),  # unknown is always last
        ],
        by_release_year=[
            YearCount(year=2001, count=1),
            YearCount(year=2018, count=2),
            YearCount(year=2020, count=2),
            YearCount(year=None, count=2),
        ],
    )


async def test_stats_of_empty_catalog(db_pool: AsyncConnectionPool) -> None:
    async with db_pool.connection() as conn:
        assert await get_stats(conn) == Stats(by_genre=[], by_release_year=[])
