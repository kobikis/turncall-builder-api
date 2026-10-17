"""Postgres pool. Schema is owned by Alembic — run `make migrate` after pulling
schema changes (alembic/versions/); the app never issues DDL at startup."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg


async def create_pool() -> asyncpg.Pool:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not configured")
    return await asyncpg.create_pool(dsn)


def jsonb(value: Any) -> Any:
    """A jsonb column's value as Python: asyncpg hands jsonb back as text
    unless a codec is set, which these pools do not."""
    return json.loads(value) if isinstance(value, str) else value


class _Pinned:
    """A pool-shaped handle on one connection, for store functions written
    against a pool. `acquire()` hands back the same connection, and a store's
    own `conn.transaction()` nests as a savepoint, so the store runs unchanged
    inside the caller's transaction."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        yield self._conn


@asynccontextmanager
async def transaction(pool: Any) -> AsyncIterator[Any]:
    """One transaction, as something a store takes in place of the pool.

    What lets a builder-side change and its audit entry commit together: a
    change that rolls back leaves no entry, and an entry that cannot be
    written takes its change back with it (ADR-0028)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            yield _Pinned(conn)
