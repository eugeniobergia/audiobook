from typing import Annotated

import httpx
from fastapi import APIRouter, Depends
from psycopg_pool import AsyncConnectionPool

from audiobook.api.dependencies import get_http, get_itunes_client, get_pool
from audiobook.clients.itunes import ITunesClient, SearchParams
from audiobook.config import Settings, get_settings
from audiobook.services.ingestion import IngestionSummary, ingest

router = APIRouter(tags=["ingestion"])


@router.post("/ingestions", summary="Ingest audiobooks from iTunes")
async def create_ingestion(
    params: SearchParams,
    pool: Annotated[AsyncConnectionPool, Depends(get_pool)],
    itunes: Annotated[ITunesClient, Depends(get_itunes_client)],
    http: Annotated[httpx.AsyncClient, Depends(get_http)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> IngestionSummary:
    """Search iTunes for audiobooks, clean the results and store them.

    Safe to repeat: new records are created, changed ones updated, and unchanged
    ones skipped (so `fetched == created + updated + skipped`). Records that can't
    be used are skipped too and listed in `invalid` with the reason. If iTunes is
    unavailable, a saved sample is ingested instead and `source` is `"fallback"`.
    """
    return await ingest(
        params,
        pool=pool,
        itunes=itunes,
        http=http,
        palette_concurrency=settings.palette_concurrency,
    )
