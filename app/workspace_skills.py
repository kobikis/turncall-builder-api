"""Workspace-authored Builder skills: storage, and the mount they become.

A [[Skill]] teaches the Builder how to build a kind of agent. Four ship in
`app/skills/`; these are written by a Workspace admin and mounted alongside them
(ADR-0015).

Storage is Postgres, but deepagents reads skills through a backend, so a turn
materialises the workspace's skills to disk and mounts that directory as a
second source. Materialising per turn rather than on write is deliberate: an
edited skill applies to the very next turn of any open conversation, and there
is no cache that can go stale.

A skill is guidance, not configuration. It teaches the Builder how to decide,
not what to pick — a skill only loads when the model judges it applies, so a
"default" expressed as a skill is a default that sometimes silently doesn't.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from .builder import SKILLS_DIR
from .session_docs import GENERATED_DIR, safe_name

# deepagents' own rule: lowercase alphanumeric and hyphens, and the name must
# match the directory it lives in — which is why we render the directory FROM
# the name rather than letting them drift.
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_BODY = 100_000


def builtin_names() -> set[str]:
    """The skills shipped with the builder. Reserved (ADR-0015): a workspace
    skill sharing one of these names would shadow it under last-wins, and a
    silent shadow is indistinguishable from a bug."""
    return {d for d in os.listdir(SKILLS_DIR) if os.path.isdir(os.path.join(SKILLS_DIR, d))}


def validate(name: str, description: str, body: str) -> None:
    """Raise ValueError with something worth showing. Called at the boundary."""
    if not NAME_RE.match(name) or len(name) > MAX_NAME:
        raise ValueError(
            "name must be lowercase words joined by hyphens, e.g. "
            "'restaurant-reservations'"
        )
    if name in builtin_names():
        raise ValueError(f"'{name}' is a built-in skill — choose another name")
    description = description.strip()
    if not description:
        raise ValueError(
            "describe when the builder should use this — it is the only part "
            "always in front of the model, and a skill without it is never opened"
        )
    if len(description) > MAX_DESCRIPTION:
        raise ValueError(f"the description must be under {MAX_DESCRIPTION} characters")
    if not body.strip():
        raise ValueError("the skill needs instructions")
    if len(body) > MAX_BODY:
        raise ValueError("the skill is too long")


def render(name: str, description: str, body: str) -> str:
    """A SKILL.md. The description is JSON-encoded because YAML accepts JSON
    scalars, which spares us guessing whether someone's colon or quote breaks
    the frontmatter and makes the skill disappear."""
    return f"---\nname: {name}\ndescription: {json.dumps(description.strip())}\n---\n\n{body.strip()}\n"


# --- persistence -------------------------------------------------------------


def _row(record: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": str(record["id"]),
        "name": record["name"],
        "description": record["description"],
        "body": record["body"],
        "updated_at": record["updated_at"].isoformat(),
    }


async def list_skills(pool: asyncpg.Pool, workspace_id: str) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, description, body, updated_at FROM workspace_skills "
            "WHERE workspace_id = $1 ORDER BY name",
            UUID(workspace_id),
        )
    return [_row(r) for r in rows]


async def get_skill(pool: asyncpg.Pool, workspace_id: str, name: str) -> dict[str, Any] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, description, body, updated_at FROM workspace_skills "
            "WHERE workspace_id = $1 AND name = $2",
            UUID(workspace_id), name,
        )
    return _row(row) if row else None


async def save_skill(
    pool: asyncpg.Pool, workspace_id: str, *, name: str, description: str, body: str
) -> dict[str, Any]:
    """Create or replace by name. Upsert rather than create-then-update: the
    name is the identity deepagents uses, so two rows sharing one would shadow
    each other and the unique index would reject it anyway."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO workspace_skills (id, workspace_id, name, description, body)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (workspace_id, name) DO UPDATE
                SET description = EXCLUDED.description,
                    body = EXCLUDED.body,
                    updated_at = now()
            RETURNING id, name, description, body, updated_at
            """,
            uuid4(), UUID(workspace_id), name, description.strip(), body.strip(),
        )
    return _row(row)


async def delete_skill(pool: asyncpg.Pool, workspace_id: str, name: str) -> bool:
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM workspace_skills WHERE workspace_id = $1 AND name = $2",
            UUID(workspace_id), name,
        )
    return result.endswith(" 1")


# --- the mount ---------------------------------------------------------------


def workspace_dir(workspace_id: str) -> str:
    return os.path.join(GENERATED_DIR, "workspace-skills", safe_name(workspace_id))


async def materialize(pool: asyncpg.Pool, workspace_id: str) -> str | None:
    """Write this workspace's skills to disk for the turn. None if it has none.

    The directory is rebuilt to match the database exactly — stale directories
    are removed — so a deleted skill stops applying immediately rather than
    lingering on whichever host happened to write it.
    """
    skills = await list_skills(pool, workspace_id)
    directory = workspace_dir(workspace_id)
    if not skills:
        shutil.rmtree(directory, ignore_errors=True)
        return None

    os.makedirs(directory, exist_ok=True)
    wanted = set()
    for skill in skills:
        # Names are validated on the way in, but this path is assembled from
        # database content, so it gets checked here too.
        name = safe_name(skill["name"])
        wanted.add(name)
        skill_dir = os.path.join(directory, name)
        os.makedirs(skill_dir, exist_ok=True)
        content = render(skill["name"], skill["description"], skill["body"])
        path = os.path.join(skill_dir, "SKILL.md")
        # Atomic: a concurrent turn in the same workspace must never read a
        # half-written SKILL.md and drop the skill as malformed.
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            fh.write(content)
        os.replace(tmp, path)

    for stale in set(os.listdir(directory)) - wanted:
        shutil.rmtree(os.path.join(directory, stale), ignore_errors=True)
    return directory
