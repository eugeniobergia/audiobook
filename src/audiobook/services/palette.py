import asyncio
import io
import logging
from collections.abc import Sequence

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

PALETTE_SIZE = 5
# Covers are shrunk to at most this many pixels per side before quantizing: the
# main colors barely change, and it keeps the work small for big images.
_MAX_SIDE = 200


async def compute_palettes(
    http: httpx.AsyncClient, urls: Sequence[str], concurrency: int
) -> list[tuple[str, ...] | None]:
    """Palettes for many covers, in the same order as `urls`.

    At most `concurrency` covers are downloaded at once, so a big batch doesn't
    open hundreds of connections to the image host.
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def limited(url: str) -> tuple[str, ...] | None:
        async with semaphore:
            return await compute_palette(http, url)

    return await asyncio.gather(*(limited(url) for url in urls))


async def compute_palette(http: httpx.AsyncClient, url: str) -> tuple[str, ...] | None:
    """Download a cover and return its palette, or None if anything goes wrong.

    A missing palette must never fail the record or the ingestion, so every
    failure (network, HTTP status, not an image, unreadable image) is logged and
    turned into None.
    """
    try:
        response = await http.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            raise ValueError(f"not an image (content-type {content_type!r})")
        # Decoding and quantizing are CPU work: keep them off the event loop.
        return await asyncio.to_thread(extract_palette, response.content)
    except Exception as exc:  # deliberately broad, see docstring
        logger.warning("No palette for %s: %r", url, exc)
        return None


def extract_palette(image_data: bytes) -> tuple[str, ...]:
    """Up to PALETTE_SIZE "#rrggbb" colors, the most common first.

    The image is reduced to PALETTE_SIZE colors (median cut), then those colors
    are sorted by how many pixels use them. Raises if the bytes aren't an image.
    """
    with Image.open(io.BytesIO(image_data)) as image:
        rgb = image.convert("RGB")
    rgb.thumbnail((_MAX_SIDE, _MAX_SIDE))
    quantized = rgb.quantize(colors=PALETTE_SIZE, method=Image.Quantize.MEDIANCUT)

    flat_palette = quantized.getpalette() or []
    # getcolors() gives (pixel_count, palette_index) for every color in use, so
    # colors that ended up unused (e.g. in a one-color image) are left out.
    by_count = sorted(quantized.getcolors() or [], reverse=True)
    colors: list[str] = []
    for _count, index in by_count:
        # In a palette ("P" mode) image each color is a palette index.
        assert isinstance(index, int)
        red, green, blue = flat_palette[index * 3 : index * 3 + 3]
        colors.append(f"#{red:02x}{green:02x}{blue:02x}")
    return tuple(colors)
