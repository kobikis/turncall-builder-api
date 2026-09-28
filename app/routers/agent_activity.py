"""What each agent has actually been doing: its number, and the last 24 hours.

The agents list answers "does this container run". This answers the question
the person came with — is the phone being answered, and has it gone quiet —
which needs two things the registry does not hold: the number routed to the
agent, and its recent calls.

Deliberately a **second** endpoint rather than more fields on `/agents`. The
list is one registry query plus a `docker ps` and renders instantly; folding a
TurnCall round-trip per agent into it would make the page's first paint wait on
a second service, and on that service being up at all. The console renders the
list, then fills the activity in when it arrives.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends

from .. import runtime
from ..backends import phones, registry
from ..deps import AuthContext, require_member

logger = logging.getLogger("turncall_builder")

router = APIRouter()

# The window the ribbon covers, one bucket per hour.
HOURS = 24

# Calls per request, and how many requests one agent is worth. TurnCall's
# /v1/calls has no date filter — it pages newest-first — so the window is
# walked until a page falls out of it. Three pages is 300 calls in a day for
# one agent; past that the count is a floor rather than a total, which the
# payload says out loud rather than quietly under-reporting.
PAGE = 100
MAX_PAGES = 3

# How many agents are queried at once. A workspace with fifty agents would
# otherwise open fifty connections to TurnCall at once for a decorative strip.
CONCURRENCY = 5


def _bucket(started_at: str | None, now: datetime) -> int | None:
    """Which hourly bucket a call's start belongs in, or None if outside it.

    Bucket 23 is the current hour, 0 is 23 hours ago — so the ribbon reads
    left-to-right as time passing, with now at the right edge.
    """
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    hours_ago = int((now - started).total_seconds() // 3600)
    if hours_ago < 0 or hours_ago >= HOURS:
        return None
    return HOURS - 1 - hours_ago


async def _activity(api_key: str, now: datetime) -> dict[str, Any]:
    """One agent's last 24 hours, from its own TurnCall project."""
    hours = [0] * HOURS
    total = 0
    truncated = False

    for page in range(1, MAX_PAGES + 1):
        body = await runtime.get_client().list_calls(api_key, page=page, limit=PAGE)
        # TurnCall's paginated envelope puts the rows directly in `data`; the
        # object form is tolerated because the two shapes are one rename apart
        # and this reads them, never writes them.
        data = body.get("data")
        calls = data if isinstance(data, list) else (data or {}).get("calls") or []
        if not calls:
            break
        for call in calls:
            slot = _bucket(call.get("started_at"), now)
            if slot is not None:
                hours[slot] += 1
                total += 1
        # The page ran out before the window did, so everything is counted.
        oldest = calls[-1].get("started_at")
        if len(calls) < PAGE or (_bucket(oldest, now) is None and oldest):
            break
        if page == MAX_PAGES:
            truncated = True

    return {"hours": hours, "calls_24h": total, "truncated": truncated, "available": True}


# What an agent gets when its calls could not be read. Zeroes would claim the
# phone was quiet, which is a different statement from "we could not ask".
UNAVAILABLE = {"hours": [0] * HOURS, "calls_24h": 0, "truncated": False, "available": False}


# `/agent-activity`, not `/agents/activity`: the latter is shadowed by
# `/agents/{agent_id}` unless this router is registered first, and it answered
# "agent not found" for an agent called "activity". Route order that has to be
# maintained in a loop over routers is a trap — a path that cannot collide is
# cheaper than a rule nobody can see.
@router.get("/agent-activity")
async def agents_activity(ctx: AuthContext = Depends(require_member)) -> dict[str, Any]:
    """Per agent: the number routed to it, and calls per hour for 24 hours."""
    pool = runtime.get_pool()
    backends = await registry.list_backends(pool, ctx.workspace_id)
    numbers = await phones.list_numbers(pool, ctx.workspace_id)

    # The mirror means the numbers cost one query rather than one request per
    # agent. First bound wins where an agent has several; the row has space for
    # one, and the list is ordered by creation.
    number_for: dict[str, str] = {}
    for n in numbers:
        agent_id = n.get("agent_id")
        if agent_id and agent_id not in number_for:
            number_for[agent_id] = n["e164"]

    limit = asyncio.Semaphore(CONCURRENCY)

    async def one(backend: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        async with limit:
            try:
                return backend["agent_id"], await _activity(backend["api_key"], datetime.now(UTC))
            except Exception:  # noqa: BLE001
                # One agent's calls being unreadable must not take the page's
                # activity down with it: TurnCall may be restarting, or this
                # agent's project may have been deleted underneath the mirror.
                logger.warning("activity unavailable for %s", backend["agent_id"])
                return backend["agent_id"], dict(UNAVAILABLE)

    results = dict(await asyncio.gather(*(one(b) for b in backends)))

    return {
        "success": True,
        "data": {
            "activity": [
                {
                    "agent_id": b["agent_id"],
                    "number": number_for.get(b["agent_id"]),
                    **results[b["agent_id"]],
                }
                for b in backends
            ]
        },
    }
