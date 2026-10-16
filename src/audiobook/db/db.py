from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Request
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def create_pool(database_url: str) -> AsyncConnectionPool:
    # Not opened here: the app lifespan opens it (opening in the constructor is
    # deprecated for async pools).
    return AsyncConnectionPool(database_url, open=False)


async def apply_schema(pool: AsyncConnectionPool) -> None:
    """Run schema.sql. It only uses IF NOT EXISTS, so it is safe on every startup."""
    async with pool.connection() as conn:
        await conn.execute(SCHEMA_PATH.read_bytes())


async def get_conn(request: Request) -> AsyncIterator[AsyncConnection]:
    """FastAPI dependency: borrow a pooled connection for the duration of a request.

    The transaction commits when the request handler finishes without an error and
    rolls back otherwise.
    """
    pool: AsyncConnectionPool = request.app.state.pool
    async with pool.connection() as conn:
        yield conn
