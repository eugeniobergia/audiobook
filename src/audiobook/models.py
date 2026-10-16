from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

HexColor = Annotated[str, Field(pattern=r"^#[0-9a-f]{6}$")]


class Audiobook(BaseModel):
    """A cleaned audiobook, shared by normalization, ingestion, storage and the API."""

    # frozen: instances can't be changed after creation (use model_copy).
    # strict: no silent coercion, e.g. id="123" or id=True is rejected, not
    # converted to an int.
    model_config = ConfigDict(frozen=True, strict=True)

    id: int = Field(gt=0)  # iTunes collectionId
    title: str = Field(min_length=1)
    author: str | None
    description: str | None  # plain text, HTML already stripped
    genre: str | None
    release_date: date | None
    cover_url: str | None
    content_hash: str  # fingerprint of the fields above; palette/updated_at excluded
    # Up to 5 colors, most dominant first. Attached during ingestion; None when
    # there is no cover or the palette could not be computed.
    palette: tuple[HexColor, ...] | None = Field(default=None, max_length=5)
    # Set by the database: always None on writes (the upsert ignores it), always
    # set on reads.
    updated_at: datetime | None = None
