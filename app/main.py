"""FastAPI app: builder loop + Create (per-agent project + agent + backend).

Control-plane only. On Create the builder creates a dedicated TurnCall project +
key for the agent, provisions the agent, registers a webhook pointing at the
agent's generated backend, and bakes the signing secret into it. Events flow
TurnCall -> backend directly; the builder is never in the event path (ADR-0005,
revised). The UI polls each backend directly at localhost:<port>.

Endpoints live in app/routers/*; this module is just the app factory + lifespan.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import logging

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from . import builder, runtime, schema_check
from .backends import generator, registry
from .db import create_pool
from .routers import (
    agents,
    auth,
    calls,
    github,
    knowledge,
    members,
    phones,
    sessions,
    takeaways,
    workspaces,
)
from .settings import load_settings
from .turncall_client import TurnCallClient

logger = logging.getLogger("turncall_builder")
# uvicorn only configures its own loggers, so our app logger's INFO/ERROR lines
# (startup reconcile, schema-drift guard) would be swallowed. Give it a handler.
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


async def _reap_orphan_backends(pool) -> None:
    """A stack restart can drop registry rows while the old agent containers keep
    running and squatting on their host ports, so the next generation collides
    ('port already allocated'). Reconcile at startup: remove any agent container
    with no live registry row. Best-effort — never blocks startup."""
    try:
        live_slugs = {b["slug"] for b in await registry.list_backends(pool) if b.get("slug")}
        removed = await generator.reap_orphan_agent_containers(live_slugs)
        if removed:
            logger.info("startup: reaped %d orphan agent container(s)", len(removed))
    except Exception:
        logger.exception("startup orphan reap failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    pool = await create_pool()
    # Per-agent projects/keys are created on demand; the platform key gates that
    # bootstrap against TurnCall (turncall#102).
    client = TurnCallClient(settings.turncall_base_url, platform_key=settings.platform_api_key)
    runtime.set_runtime(pool, client)
    # Fail loud at boot, not at the user's first message (turncall#builder-keys).
    availability = builder.provider_availability()
    if not any(availability.values()):
        logger.warning(
            "no builder API key configured (%s) — the builder cannot run",
            " / ".join(builder._KEY_ENV.values()),
        )
    await _reap_orphan_backends(pool)
    await schema_check.check_drift(client)
    try:
        yield
    finally:
        await pool.close()
        await client.close()
        runtime.clear_runtime()


app = FastAPI(title="TurnCall Builder API", lifespan=lifespan)
# Authlib stores the OAuth state/nonce here across the Google redirect+callback.
# https_only so this CSRF cookie is Secure (localhost is still a secure context).
app.add_middleware(
    SessionMiddleware, secret_key=load_settings().session_secret, https_only=True
)

for _router in (
    sessions, agents, phones, calls, knowledge, takeaways, auth, workspaces,
    members, github,
):
    app.include_router(_router.router)
