import hashlib
import html
import json
import logging
import warnings
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
from pydantic import BaseModel, ConfigDict, ValidationError

from audiobook.models import Audiobook

logger = logging.getLogger(__name__)

# Tags that start a new line in rendered HTML. Inline tags (<b>, <i>, <a>...) don't,
# so "<b>Dune</b> rocks" stays "Dune rocks" on one line.
_BLOCK_TAGS = ["p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"]


class InvalidRecord(BaseModel):
    """A source record we could not turn into an Audiobook, and why."""

    model_config = ConfigDict(frozen=True)

    source_id: int | None  # None when the record had no usable collectionId
    reason: str


def normalize_batch(
    records: Iterable[Any],
) -> tuple[list[Audiobook], list[InvalidRecord]]:
    """Normalize raw iTunes records, dropping duplicates (the first one wins).

    Order is preserved. Every dropped record is returned with its reason and logged.
    """
    audiobooks: list[Audiobook] = []
    invalid: list[InvalidRecord] = []
    seen_ids: set[int] = set()
    for raw in records:
        result = normalize_record(raw)
        if isinstance(result, Audiobook) and result.id in seen_ids:
            result = InvalidRecord(
                source_id=result.id, reason="duplicate collectionId in batch"
            )
        if isinstance(result, InvalidRecord):
            logger.warning("Skipping record %s: %s", result.source_id, result.reason)
            invalid.append(result)
            continue
        seen_ids.add(result.id)
        audiobooks.append(result)
    return audiobooks, invalid


def normalize_record(raw: Any) -> Audiobook | InvalidRecord:
    """Clean one raw iTunes record, or explain why it can't be used.

    Required: a positive integer collectionId, wrapperType "audiobook", and a
    non-empty title. Every other field is optional and becomes None when missing
    or unusable, without rejecting the record.
    """
    if not isinstance(raw, dict):
        return InvalidRecord(source_id=None, reason="not an object")

    source_id = raw.get("collectionId")
    # bool is a subclass of int in Python, so True would otherwise pass as id 1.
    if isinstance(source_id, bool) or not isinstance(source_id, int) or source_id <= 0:
        return InvalidRecord(source_id=None, reason="missing or invalid collectionId")

    if raw.get("wrapperType") != "audiobook":
        return InvalidRecord(
            source_id=source_id,
            reason=f"not an audiobook (wrapperType={raw.get('wrapperType')!r})",
        )

    title = _clean_text(raw.get("collectionName"))
    if title is None:
        return InvalidRecord(source_id=source_id, reason="missing title")

    description = raw.get("description")
    if not isinstance(description, str):
        description = None
    fields: dict[str, Any] = {
        "id": source_id,
        "title": title,
        "author": _clean_text(raw.get("artistName")),
        "description": html_to_text(description),
        "genre": _clean_text(raw.get("primaryGenreName")),
        "release_date": _parse_date(raw.get("releaseDate")),
        "cover_url": _parse_url(raw.get("artworkUrl100")),
    }
    try:
        return Audiobook(**fields, content_hash=content_hash(fields))
    except ValidationError as exc:
        # Safety net: the cleaning above should make this impossible. If it ever
        # happens, drop this record instead of failing the whole ingestion.
        error = exc.errors()[0]
        location = ".".join(str(part) for part in error["loc"])
        return InvalidRecord(
            source_id=source_id, reason=f"invalid {location}: {error['msg']}"
        )


def _clean_text(value: Any) -> str | None:
    """Single-line text: entities decoded, whitespace collapsed; None if empty."""
    if not isinstance(value, str):
        return None
    text = " ".join(html.unescape(value).split())
    return text or None


def _parse_date(value: Any) -> date | None:
    """ "2018-04-26T07:00:00Z" or "2018-04-26" -> date(2018, 4, 26); else None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip()).date()
    except ValueError:
        return None


def _parse_url(value: Any) -> str | None:
    """The URL as it comes, if it is an absolute http(s) URL; else None."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return url


def html_to_text(raw: str | None) -> str | None:
    """Turn an HTML fragment into plain text, keeping line breaks.

    Entities are decoded (&amp; -> &), <script>/<style> content is dropped, spaces
    are collapsed and blank lines removed. Returns None if no text is left.
    """
    if not raw:
        return None
    with warnings.catch_warnings():
        # BeautifulSoup warns when the input looks like a URL or file name (it
        # guesses we meant to fetch it). A description that is just a URL is still
        # text, so silence that one warning here.
        warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
        soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for br in soup("br"):
        br.replace_with("\n")
    for block in soup(_BLOCK_TAGS):
        block.insert_after("\n")

    lines = (" ".join(line.split()) for line in soup.get_text().splitlines())
    text = "\n".join(line for line in lines if line)
    return text or None


def content_hash(fields: Mapping[str, Any]) -> str:
    """SHA-256 fingerprint of an audiobook's cleaned fields.

    Pass only the fields we store from the source (not palette or updated_at): two
    records with equal cleaned fields get the same hash, whatever their key order,
    so an unchanged upstream record is detected as unchanged.
    """
    canonical = json.dumps(
        fields, sort_keys=True, separators=(",", ":"), default=_json_default
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _json_default(value: Any) -> str:
    # Only dates need help; anything else unexpected is a bug, so fail loudly
    # instead of hashing some arbitrary str(value).
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Can't hash value of type {type(value).__name__}")
