import asyncio
import io
import logging
import re

import httpx
import pytest
from PIL import Image

from audiobook.services.palette import (
    PALETTE_SIZE,
    compute_palette,
    compute_palettes,
    extract_palette,
)

RED = (200, 0, 0)
BLUE = (0, 0, 200)
GREEN = (10, 200, 10)

Color = tuple[int, int, int]


def striped_image(
    stripes: list[tuple[Color, float]], image_format: str = "PNG", mode: str = "RGB"
) -> bytes:
    """A 100x100 image made of vertical stripes, each covering a share of the width."""
    image = Image.new("RGB", (100, 100))
    x = 0
    for color, share in stripes:
        width = round(100 * share)
        image.paste(color, (x, 0, x + width, 100))
        x += width
    buffer = io.BytesIO()
    image.convert(mode).save(buffer, image_format)
    return buffer.getvalue()


def cover_client(
    content: bytes = b"", status: int = 200, content_type: str = "image/png"
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, content=content, headers={"content-type": content_type}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- extract_palette ------------------------------------------------------------


def test_most_common_color_comes_first() -> None:
    # Blue is listed first in the image but covers less of it.
    image = striped_image([(BLUE, 0.3), (RED, 0.7)])

    assert extract_palette(image) == ("#c80000", "#0000c8")


def test_colors_are_ordered_by_share() -> None:
    image = striped_image([(GREEN, 0.2), (RED, 0.5), (BLUE, 0.3)])

    assert extract_palette(image) == ("#c80000", "#0000c8", "#0ac80a")


def test_single_color_image_has_one_color_without_duplicates() -> None:
    assert extract_palette(striped_image([((50, 60, 70), 1.0)])) == ("#323c46",)


def test_palette_is_capped_and_lowercase_hex() -> None:
    many_colors = [((i * 30, 255 - i * 30, i * 10), 0.125) for i in range(8)]

    palette = extract_palette(striped_image(many_colors))

    assert len(palette) == PALETTE_SIZE
    assert all(re.fullmatch(r"#[0-9a-f]{6}", color) for color in palette)
    assert len(set(palette)) == len(palette)


@pytest.mark.parametrize(
    ("image_format", "mode"),
    [("JPEG", "RGB"), ("PNG", "RGBA"), ("PNG", "L"), ("GIF", "P")],
)
def test_common_image_formats_and_modes(image_format: str, mode: str) -> None:
    image = striped_image([(RED, 0.7), (BLUE, 0.3)], image_format, mode)

    palette = extract_palette(image)

    assert 1 <= len(palette) <= PALETTE_SIZE


def test_non_image_bytes_raise() -> None:
    with pytest.raises(Exception):  # noqa: B017 (Pillow raises UnidentifiedImageError)
        extract_palette(b"definitely not an image")


# --- compute_palette ------------------------------------------------------------


async def test_compute_palette_downloads_and_extracts() -> None:
    async with cover_client(striped_image([(RED, 0.7), (BLUE, 0.3)])) as http:
        palette = await compute_palette(http, "https://covers.test/a.png")

    assert palette == ("#c80000", "#0000c8")


@pytest.mark.parametrize(
    ("status", "content_type", "content"),
    [
        pytest.param(404, "image/png", b"", id="not-found"),
        pytest.param(500, "image/png", b"", id="server-error"),
        pytest.param(200, "text/html", b"<html></html>", id="not-an-image"),
        pytest.param(200, "image/png", b"corrupt", id="unreadable-image"),
    ],
)
async def test_compute_palette_returns_none_on_failure(
    status: int, content_type: str, content: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    async with cover_client(content, status, content_type) as http:
        with caplog.at_level(logging.WARNING):
            palette = await compute_palette(http, "https://covers.test/a.png")

    assert palette is None
    assert "No palette for https://covers.test/a.png" in caplog.text


async def test_compute_palette_returns_none_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await compute_palette(http, "https://covers.test/a.png") is None


# --- compute_palettes -----------------------------------------------------------


async def test_compute_palettes_keeps_order_and_isolates_failures() -> None:
    images = {
        "/red.png": striped_image([(RED, 1.0)]),
        "/blue.png": striped_image([(BLUE, 1.0)]),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path not in images:
            return httpx.Response(404)
        return httpx.Response(
            200, content=images[request.url.path], headers={"content-type": "image/png"}
        )

    urls = [
        "https://covers.test/blue.png",
        "https://covers.test/missing.png",
        "https://covers.test/red.png",
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        palettes = await compute_palettes(http, urls, concurrency=2)

    assert palettes == [("#0000c8",), None, ("#c80000",)]


async def test_compute_palettes_limits_concurrent_downloads() -> None:
    image = striped_image([(RED, 1.0)])
    in_flight = 0
    max_in_flight = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)  # let other downloads start, if they're allowed
        in_flight -= 1
        return httpx.Response(200, content=image, headers={"content-type": "image/png"})

    urls = [f"https://covers.test/{i}.png" for i in range(10)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        palettes = await compute_palettes(http, urls, concurrency=3)

    assert max_in_flight == 3
    assert palettes == [("#c80000",)] * 10


async def test_compute_palettes_of_nothing() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500))
    ) as http:
        assert await compute_palettes(http, [], concurrency=3) == []
