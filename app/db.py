"""Postgres pool. Schema is owned by Alembic — run `make migrate` after pulling
schema changes (alembic/versions/); the app never issues DDL at startup."""

from __future__ import annotations

import os

import asyncpg


async def create_pool() -> asyncpg.Pool:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not configured")
    return await asyncpg.create_pool(dsn)
