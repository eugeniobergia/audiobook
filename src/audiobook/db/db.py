from pathlib import Path

from psycopg_pool import AsyncConnectionPool

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def create_pool(database_url: str, max_size: int) -> AsyncConnectionPool:
    # Not opened here: the app lifespan opens it (opening in the constructor is
    # deprecated for async pools).
    # Grows from 2 to max_size connections under load. A running export holds
    # one connection for its whole duration, so psycopg's default (a fixed 4)
    # would let a few exports block every other request.
    return AsyncConnectionPool(database_url, min_size=2, max_size=max_size, open=False)


async def apply_schema(pool: AsyncConnectionPool) -> None:
    """Run schema.sql. It only uses IF NOT EXISTS, so it is safe on every startup."""
    async with pool.connection() as conn:
        await conn.execute(SCHEMA_PATH.read_bytes())
