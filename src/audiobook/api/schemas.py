from datetime import date, datetime

from pydantic import BaseModel, Field

from audiobook.models import Audiobook


class AudiobookResponse(BaseModel):
    """An audiobook as clients see it (internal fields like content_hash left out)."""

    id: int = Field(description="The iTunes collectionId.", examples=[1441611591])
    title: str = Field(examples=["Heretics of Dune"])
    author: str | None = Field(examples=["Frank Herbert"])
    description: str | None = Field(description="Plain text, HTML removed.")
    genre: str | None = Field(examples=["Sci-Fi & Fantasy"])
    release_date: date | None
    cover_url: str | None
    palette: list[str] | None = Field(
        description="Up to 5 cover colors, most common first. Null when there is "
        "no cover or its colors couldn't be computed.",
        examples=[["#22110d", "#5a2f1c", "#9b5934"]],
    )
    updated_at: datetime = Field(description="When this audiobook last changed.")

    @classmethod
    def from_audiobook(cls, book: Audiobook) -> "AudiobookResponse":
        return cls.model_validate(book.model_dump(exclude={"content_hash"}))


class AudiobookPage(BaseModel):
    items: list[AudiobookResponse]
    total: int = Field(description="Audiobooks matching the filters, on all pages.")
    limit: int
    offset: int


class GenreCount(BaseModel):
    genre: str | None = Field(description="Null counts audiobooks without a genre.")
    count: int


class YearCount(BaseModel):
    year: int | None = Field(
        description="Release year. Null counts audiobooks without a release date."
    )
    count: int


class StatsResponse(BaseModel):
    by_genre: list[GenreCount] = Field(
        description="Most common genre first; ties alphabetical; null last."
    )
    by_release_year: list[YearCount] = Field(
        description="Oldest year first; null last."
    )
