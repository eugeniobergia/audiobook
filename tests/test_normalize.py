import logging
import re
from datetime import date
from typing import Any

import pytest

from audiobook.clients.itunes import load_sample
from audiobook.models import Audiobook
from audiobook.services.normalize import (
    InvalidRecord,
    content_hash,
    html_to_text,
    normalize_batch,
    normalize_record,
)


def raw_record(**overrides: Any) -> dict[str, Any]:
    """A valid raw iTunes audiobook record, with optional field overrides."""
    record: dict[str, Any] = {
        "wrapperType": "audiobook",
        "collectionId": 7,
        "collectionName": "Dune",
        "artistName": "Frank Herbert",
        "description": "<p>A desert planet.</p>",
        "primaryGenreName": "Sci-Fi & Fantasy",
        "releaseDate": "2007-01-01T08:00:00Z",
        "artworkUrl100": "https://is1-ssl.mzstatic.com/image/100x100bb.jpg",
        "collectionPrice": 19.99,
    }
    record.update(overrides)
    return record


def normalize_ok(raw: Any) -> Audiobook:
    result = normalize_record(raw)
    assert isinstance(result, Audiobook), result
    return result


# --- html_to_text ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, None, id="none"),
        pytest.param("", None, id="empty"),
        pytest.param("<p> </p><br>", None, id="only-tags"),
        pytest.param("Just text", "Just text", id="plain"),
        pytest.param("<b>Dune</b> is <i>great</i>", "Dune is great", id="inline-tags"),
        pytest.param("<p>One</p><p>Two</p>", "One\nTwo", id="paragraphs"),
        pytest.param("A<br>B<br/>C<br />D", "A\nB\nC\nD", id="line-breaks"),
        pytest.param("<ul><li>A</li><li><b>B</b> b</li></ul>", "A\nB b", id="list"),
        pytest.param(
            "Tom &amp; Jerry&#39;s &quot;show&quot;&nbsp;now",
            'Tom & Jerry\'s "show" now',
            id="entities",
        ),
        pytest.param("a < b and c > d", "a < b and c > d", id="literal-angle-brackets"),
        pytest.param(
            "Hi<script>alert(1)</script><style>p {}</style> there",
            "Hi there",
            id="script-and-style",
        ),
        pytest.param("  lots \t of\n\n\n  space  ", "lots of\nspace", id="whitespace"),
        pytest.param("<p>open <b>bold", "open bold", id="unclosed-tags"),
        pytest.param("https://example.com", "https://example.com", id="url-only"),
    ],
)
def test_html_to_text(raw: str | None, expected: str | None) -> None:
    assert html_to_text(raw) == expected


# --- content_hash ---------------------------------------------------------------


FIELDS: dict[str, Any] = {"id": 1, "title": "Dune", "release_date": date(1965, 8, 1)}


def test_content_hash_is_stable_and_ignores_key_order() -> None:
    reordered = dict(reversed(list(FIELDS.items())))

    assert content_hash(FIELDS) == content_hash(dict(FIELDS))
    assert content_hash(FIELDS) == content_hash(reordered)


@pytest.mark.parametrize(
    "changed",
    [
        {**FIELDS, "title": "Dune Messiah"},
        {**FIELDS, "release_date": date(1965, 8, 2)},
        {**FIELDS, "author": None},
    ],
    ids=["title", "release-date", "new-field"],
)
def test_content_hash_changes_with_any_field(changed: dict[str, Any]) -> None:
    assert content_hash(changed) != content_hash(FIELDS)


def test_content_hash_rejects_unexpected_types() -> None:
    with pytest.raises(TypeError):
        content_hash({**FIELDS, "palette": object()})


# --- normalize_record: rejected records -----------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(
            "not a dict",
            InvalidRecord(source_id=None, reason="not an object"),
            id="str",
        ),
        pytest.param(
            [raw_record()],
            InvalidRecord(source_id=None, reason="not an object"),
            id="list",
        ),
        *[
            pytest.param(
                raw_record(collectionId=bad_id),
                InvalidRecord(source_id=None, reason="missing or invalid collectionId"),
                id=f"id-{label}",
            )
            for label, bad_id in [
                ("missing", None),
                ("string", "7"),
                ("bool", True),
                ("zero", 0),
                ("negative", -7),
                ("float", 7.0),
            ]
        ],
        pytest.param(
            raw_record(wrapperType="track"),
            InvalidRecord(source_id=7, reason="not an audiobook (wrapperType='track')"),
            id="not-an-audiobook",
        ),
        *[
            pytest.param(
                raw_record(collectionName=bad_title),
                InvalidRecord(source_id=7, reason="missing title"),
                id=f"title-{label}",
            )
            for label, bad_title in [
                ("missing", None),
                ("empty", ""),
                ("blank", " \n\t "),
                ("not-a-string", 42),
            ]
        ],
    ],
)
def test_normalize_record_rejects(raw: Any, expected: InvalidRecord) -> None:
    assert normalize_record(raw) == expected


# --- normalize_record: cleaned records ------------------------------------------


def test_normalize_record_cleans_every_field() -> None:
    book = normalize_ok(
        raw_record(
            collectionName="  Tom &amp; Jerry \n",
            artistName=" Ann  Smith ",
            description="<p>Line one</p>Line <b>two</b>",
            primaryGenreName=" Fiction ",
            artworkUrl100=" https://x.test/a.jpg ",
        )
    )

    assert book.id == 7
    assert book.title == "Tom & Jerry"
    assert book.author == "Ann Smith"
    assert book.description == "Line one\nLine two"
    assert book.genre == "Fiction"
    assert book.release_date == date(2007, 1, 1)
    assert book.cover_url == "https://x.test/a.jpg"
    assert book.palette is None
    assert book.updated_at is None


def test_normalize_record_keeps_title_suffixes() -> None:
    book = normalize_ok(raw_record(collectionName="Dune (Unabridged)"))

    assert book.title == "Dune (Unabridged)"


@pytest.mark.parametrize(
    ("field", "attribute", "bad_value"),
    [
        ("artistName", "author", None),
        ("artistName", "author", "   "),
        ("description", "description", None),
        ("description", "description", 42),
        ("description", "description", "<br><p></p>"),
        ("primaryGenreName", "genre", ""),
        ("releaseDate", "release_date", None),
        ("releaseDate", "release_date", "not a date"),
        ("releaseDate", "release_date", "2018-13-45"),
        ("artworkUrl100", "cover_url", None),
        ("artworkUrl100", "cover_url", "ftp://x.test/a.jpg"),
        ("artworkUrl100", "cover_url", "/relative/a.jpg"),
        ("artworkUrl100", "cover_url", "https://"),
    ],
)
def test_bad_optional_fields_become_none(
    field: str, attribute: str, bad_value: Any
) -> None:
    book = normalize_ok(raw_record(**{field: bad_value}))

    assert getattr(book, attribute) is None


def test_missing_optional_fields_become_none() -> None:
    record = raw_record()
    for field in ("artistName", "description", "primaryGenreName", "releaseDate"):
        del record[field]

    book = normalize_ok(record)

    assert (book.author, book.description, book.genre, book.release_date) == (
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    ("raw_date", "expected"),
    [
        ("2018-04-26T07:00:00Z", date(2018, 4, 26)),
        ("2018-04-26", date(2018, 4, 26)),
        (" 2018-04-26T07:00:00Z ", date(2018, 4, 26)),
    ],
)
def test_release_date_parsing(raw_date: str, expected: date) -> None:
    assert normalize_ok(raw_record(releaseDate=raw_date)).release_date == expected


# --- normalize_record: change detection -----------------------------------------


def test_hash_ignores_fields_we_do_not_store() -> None:
    original = normalize_ok(raw_record())
    repriced = normalize_ok(raw_record(collectionPrice=4.99, artistId=123))

    assert repriced.content_hash == original.content_hash


def test_hash_ignores_differences_removed_by_cleaning() -> None:
    original = normalize_ok(raw_record(collectionName="Tom & Jerry"))
    messy = normalize_ok(raw_record(collectionName="  Tom &amp;  Jerry "))

    assert messy.content_hash == original.content_hash


@pytest.mark.parametrize(
    "change",
    [
        {"collectionName": "Dune Messiah"},
        {"artistName": "Brian Herbert"},
        {"description": "<p>An ocean planet.</p>"},
        {"primaryGenreName": "Classics"},
        {"releaseDate": "2008-01-01T08:00:00Z"},
        {"artworkUrl100": "https://is1-ssl.mzstatic.com/image/new.jpg"},
    ],
    ids=lambda change: next(iter(change)),
)
def test_hash_changes_when_a_stored_field_changes(change: dict[str, Any]) -> None:
    original = normalize_ok(raw_record())
    changed = normalize_ok(raw_record(**change))

    assert changed.content_hash != original.content_hash


# --- normalize_batch ------------------------------------------------------------


def test_normalize_batch_splits_valid_and_invalid_preserving_order() -> None:
    records = [
        raw_record(collectionId=1),
        "garbage",
        raw_record(collectionId=2, collectionName=""),
        raw_record(collectionId=3),
    ]

    audiobooks, invalid = normalize_batch(records)

    assert [book.id for book in audiobooks] == [1, 3]
    assert invalid == [
        InvalidRecord(source_id=None, reason="not an object"),
        InvalidRecord(source_id=2, reason="missing title"),
    ]


def test_normalize_batch_keeps_first_of_duplicate_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    records = [
        raw_record(collectionId=1, collectionName="First"),
        raw_record(collectionId=1, collectionName="Second"),
    ]

    with caplog.at_level(logging.WARNING):
        audiobooks, invalid = normalize_batch(records)

    assert [book.title for book in audiobooks] == ["First"]
    assert invalid == [
        InvalidRecord(source_id=1, reason="duplicate collectionId in batch")
    ]
    assert "duplicate collectionId in batch" in caplog.text


def test_invalid_duplicate_does_not_block_a_later_valid_record() -> None:
    # A record rejected for another reason must not "claim" its id.
    records = [
        raw_record(collectionId=1, collectionName=""),
        raw_record(collectionId=1, collectionName="Valid"),
    ]

    audiobooks, invalid = normalize_batch(records)

    assert [book.title for book in audiobooks] == ["Valid"]
    assert invalid == [InvalidRecord(source_id=1, reason="missing title")]


def test_normalize_batch_of_nothing() -> None:
    assert normalize_batch([]) == ([], [])


# --- the real iTunes sample -----------------------------------------------------


async def test_real_sample_normalizes_cleanly() -> None:
    audiobooks, invalid = normalize_batch(await load_sample())

    assert invalid == []
    assert audiobooks
    for book in audiobooks:
        assert book.description is None or not re.search(
            r"<[a-zA-Z/]", book.description
        )
        assert book.cover_url is None or book.cover_url.startswith("https://")
