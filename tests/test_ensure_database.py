"""Creating the builder's database on a fresh Postgres (the Kubernetes install)."""

import os
import uuid

import asyncpg
import pytest

from app.ensure_database import ensure, split


def test_split_points_at_the_maintenance_database():
    admin, name = split("postgresql+asyncpg://u:p@db:5432/builder")
    assert admin == "postgresql://u:p@db:5432/postgres"
    assert name == "builder"


@pytest.mark.asyncio
async def test_creates_once_then_finds_it():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    admin, _ = split(url)
    name = f"ensure_{uuid.uuid4().hex[:8]}"
    target = admin.rsplit("/", 1)[0] + f"/{name}"
    try:
        assert await ensure(target) is True
        assert await ensure(target) is False
    finally:
        conn = await asyncpg.connect(admin)
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await conn.close()
