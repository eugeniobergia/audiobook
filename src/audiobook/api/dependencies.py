from collections.abc import AsyncIterator

import httpx
from fastapi import Request
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from audiobook.clients.itunes import ITunesClient


def get_pool(request: Request) -> AsyncConnectionPool:
    """FastAPI dependency: the app's connection pool, for work that should borrow
    connections only briefly (e.g. ingestion, which downloads covers in between)."""
    pool: AsyncConnectionPool = request.app.state.pool
    return pool


def get_http(request: Request) -> httpx.AsyncClient:
    """FastAPI dependency: the app's shared HTTP client (e.g. for cover images)."""
    http: httpx.AsyncClient = request.app.state.http
    return http


async def get_conn(request: Request) -> AsyncIterator[AsyncConnection]:
    """FastAPI dependency: borrow a pooled connection for the duration of a request.

    The transaction commits when the request handler finishes without an error and
    rolls back otherwise.
    """
    pool: AsyncConnectionPool = request.app.state.pool
    async with pool.connection() as conn:
        yield conn


def get_itunes_client(request: Request) -> ITunesClient:
    """FastAPI dependency: an iTunes client over the app's shared HTTP client."""
    return ITunesClient(request.app.state.http)
