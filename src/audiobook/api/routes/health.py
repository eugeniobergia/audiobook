import asyncio
import logging
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Depends
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from audiobook.api.dependencies import get_pool
from audiobook.api.errors import AppError, ErrorResponse

logger = logging.getLogger(__name__)

# Public on purpose: monitoring and load balancers call it without a key.
router = APIRouter(tags=["health"])

# How long the database gets to answer before the service counts as unhealthy.
# Kept below the 2s timeout of the Docker healthcheck, so that check gets a
# clear 503 instead of timing out itself.
HEALTH_DB_TIMEOUT_SECONDS = 1.0


class HealthResponse(BaseModel):
    status: Literal["ok"]


@router.get(
    "/health",
    summary="Health check",
    responses={
        503: {"model": ErrorResponse, "description": "The database is unreachable"}
    },
)
async def health(
    pool: Annotated[AsyncConnectionPool, Depends(get_pool)],
) -> HealthResponse:
    """Whether the service can handle requests: the app is up **and** the database
    answers a query (`SELECT 1`) within a second.

    Returns 200 `{"status": "ok"}`, or 503 when the database is unreachable or too
    slow. Public (no API key), for load balancers, orchestrators and monitoring.
    """
    try:
        # One deadline for everything: getting a connection from the pool (which
        # waits while Postgres is down) and running the query.
        async with asyncio.timeout(HEALTH_DB_TIMEOUT_SECONDS):
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
    except (psycopg.Error, TimeoutError) as exc:
        logger.warning("Health check failed, database unreachable: %r", exc)
        raise AppError(
            503, "database_unavailable", "The database is unreachable."
        ) from exc
    return HealthResponse(status="ok")
