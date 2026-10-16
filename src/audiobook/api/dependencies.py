from collections.abc import AsyncIterator

from fastapi import Request
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from audiobook.clients.itunes import ITunesClient


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
