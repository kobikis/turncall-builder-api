"""Create DATABASE_URL's database when it does not exist yet.

`make docker-migrate` does this with `createdb` in TurnCall's Postgres
container; a Kubernetes install has no container to exec into, so the chart's
migrate step runs `python -m app.ensure_database && alembic upgrade head`.
"""

from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit, urlunsplit

import asyncpg


def split(url: str) -> tuple[str, str]:
    """(URL of the server's `postgres` database, the database DATABASE_URL names)."""
    parts = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    return urlunsplit(parts._replace(path="/postgres")), parts.path.lstrip("/")


async def ensure(url: str) -> bool:
    """True when it had to create the database."""
    admin_url, name = split(url)
    conn = await asyncpg.connect(admin_url)
    try:
        if await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            return False
        # An identifier cannot be a bind parameter; quote it instead.
        await conn.execute(f'CREATE DATABASE "{name.replace(chr(34), chr(34) * 2)}"')
        return True
    finally:
        await conn.close()


if __name__ == "__main__":
    created = asyncio.run(ensure(os.environ["DATABASE_URL"]))
    print("created database" if created else "database exists")
