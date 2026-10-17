"""The audit trail a route records a person's changes on (ADR-0028).

A route that changes one of ADR-0028's resources (agents, numbers, integrations
and connected apps, members and roles, workspace skills, linked repos) takes
`audit: Audit` and records each change with `await audit.record(ctx, ...)`.
Three things hold the rest:

- **Every such route takes one.** `tests/test_audit_wiring.py` lists every
  mutating route the app serves, and fails on one that neither takes a trail
  nor is named, with its reason, as out of scope.
- **A successful request recorded.** The trail is a dependency whose teardown
  runs as the route returns, before the response is sent: a route that
  returned normally with nothing recorded is a 500, not a change saved
  unrecorded. `audit.unchanged()` is the one way out, for a branch that
  provably changed nothing; it is explicit so it can be grepped for.
- **No secret is recorded.** `diff()` walks before/after as JSON Pointers and
  masks every secret-named key: a secret that changed reads `***` on both
  sides, so the entry says the Deepgram key changed and never what it is.

Where the change is builder-side only, the entry is written in the change's
own transaction (`db.transaction`), so the two commit or roll back together.
Where it reached TurnCall, the entry is written once TurnCall accepted it: the
two systems share no transaction, and a request that fails writes nothing.

**It links to what TurnCall recorded** (#102). While the trail is open, the
TurnCall client collects the engine audit id of every call TurnCall audited
(`X-TurnCall-Audit-Id`). Each entry takes those made since the request's
previous entry as its `engine_links`, one `{project_id, engine_audit_id}` per
engine request, so a change that reached many projects links to every one. A
change TurnCall does not audit, or that never reached it, has none.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute

from . import audit_store, runtime
from .backends import registry
from .deps import AuthContext
from .github import SECRET_KEYS
from .turncall_client import AuditedCalls, collecting

logger = logging.getLogger("turncall_builder")

MASK = "***"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# The closed vocabulary of what an entry is about. The Console renders these.
RESOURCE_TYPES = frozenset(
    {
        "agent",
        "phone_number",
        "integration",  # a provider account (Deepgram key, AWS set), ADR-0023
        "connected_app",  # ADR-0018
        "pipedream_project",
        "twilio_account",
        "mcp_server",
        "member",
        "invite",
        "skill",
        "linked_repo",
        # Redaction and retention (#103): the Workspace's ("workspace") or one
        # Agent's override (its agent id).
        "compliance",
    }
)

# Beyond SECRET_KEYS (the names the GitHub push already redacts): whatever is
# named like a secret. Over-masking costs a value nobody needed in the log;
# under-masking puts a credential in an append-only table nothing may delete.
_SECRET_SUFFIXES = ("_secret", "_token", "_password", "_encrypted", "_key")


class UnrecordedChange(RuntimeError):
    """An audited route returned without recording anything."""


def is_secret(key: str) -> bool:
    name = key.lower()
    return name in SECRET_KEYS or name.endswith(_SECRET_SUFFIXES)


class _Secret:
    """A secret's raw value, held only to compare before with after."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Secret) and self.value == other.value

    def shown(self) -> str | None:
        return MASK if self.value not in (None, "", {}, []) else None


def _escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _flatten(value: Any, pointer: str, out: dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict) and value:
        for key, child in value.items():
            path = f"{pointer}/{_escape(str(key))}"
            if is_secret(str(key)):
                out[path] = _Secret(child)
            else:
                _flatten(child, path, out)
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            _flatten(child, f"{pointer}/{index}", out)
    elif pointer and value not in ({}, []):
        # An empty object or list reads as unset, as an absent field does: its
        # first item is then the change, not the container appearing.
        out[pointer] = value
    return out


def diff(before: Any, after: Any) -> dict[str, dict[str, Any]]:
    """Every field that differs, by JSON Pointer, as `{before, after}`.

    None on a side means the resource did not exist on it: a creation lists
    every field it was created with, `before: null`, a deletion every field it
    had, `after: null`. A secret-named key is compared on its raw value and
    shown as `***` (or null when unset), at any depth and whatever it holds."""
    old = _flatten(before, "", {}) if before is not None else {}
    new = _flatten(after, "", {}) if after is not None else {}
    changes: dict[str, dict[str, Any]] = {}
    for path in dict.fromkeys([*old, *new]):
        was, now = old.get(path), new.get(path)
        if was == now:
            continue
        if isinstance(was, _Secret) or isinstance(now, _Secret):
            # One side may be absent (None) while the other is a secret.
            was = was if isinstance(was, _Secret) else _Secret(was)
            now = now if isinstance(now, _Secret) else _Secret(now)
            if was == now or was.shown() is now.shown() is None:
                continue  # unchanged, or unset either way ("" and null)
            changes[path] = {"before": was.shown(), "after": now.shown()}
        else:
            changes[path] = {"before": was, "after": now}
    return changes


def secret_changed(existed_before: bool, exists_after: bool) -> dict[str, dict[str, Any]]:
    """The whole of a credential's entry: that it was set, replaced or removed."""
    return {
        "/value": {
            "before": MASK if existed_before else None,
            "after": MASK if exists_after else None,
        }
    }


class AuditTrail:
    """One request's entries. Every entry it records shares its request_id."""

    def __init__(self) -> None:
        self.request_id = str(uuid.uuid4())
        self.entries: list[dict[str, Any]] = []
        self._unchanged = False
        # The audited TurnCall calls this request makes, collected by the
        # client while _trail holds the trail open.
        self.engine_calls = AuditedCalls()

    async def record(
        self,
        ctx: AuthContext,
        resource_type: str,
        resource_id: str,
        action: str,
        *,
        before: Any = None,
        after: Any = None,
        changes: dict[str, Any] | None = None,
        resource_name: str | None = None,
        agent_ids: Iterable[str | None] = (),
        db: Any = None,
    ) -> dict[str, Any]:
        """Append an entry for one change `ctx`'s member made.

        What changed is `changes`, or else the diff of `before` and `after`.
        `db` is the change's own transaction when it has one (db.transaction);
        otherwise the entry is written on its own, after the change. It links
        to every TurnCall call TurnCall audited since the previous entry, so
        record a change once its TurnCall calls are made."""
        if resource_type not in RESOURCE_TYPES:
            raise ValueError(f"unknown audit resource_type {resource_type!r}")
        db = db if db is not None else runtime.get_pool()
        entry = {
            "id": str(uuid.uuid4()),
            "workspace_id": str(ctx.workspace_id),
            "user_id": str(ctx.user["id"]),
            "user_email": str(ctx.user.get("email") or ""),
            "role": ctx.role,
            "resource_type": resource_type,
            "resource_id": str(resource_id),
            "resource_name": resource_name,
            "action": action,
            "agent_ids": sorted({str(a) for a in agent_ids if a}),
            "changes": changes if changes is not None else diff(before, after),
            "request_id": self.request_id,
            "engine_links": await self._engine_links(db),
        }
        await audit_store.insert(db, entry)
        self.entries.append(entry)
        return entry

    async def _engine_links(self, db: Any) -> list[dict[str, str | None]]:
        """The engine requests behind this entry, in the order made. A call made
        with an Agent's or a webhook project's key is in that key's project."""
        calls = self.engine_calls.take()
        keys = [c.api_key for c in calls if c.project_id is None and c.api_key]
        projects = await registry.projects_for_keys(db, keys) if keys else {}
        return [
            {
                "project_id": c.project_id or projects.get(c.api_key or ""),
                "engine_audit_id": c.audit_id,
            }
            for c in calls
        ]

    def unchanged(self) -> None:
        """Say this request changed nothing worth an entry. Only for a branch
        that provably changed nothing an admin would look for."""
        self._unchanged = True

    @property
    def satisfied(self) -> bool:
        return bool(self.entries) or self._unchanged


async def _trail() -> AsyncIterator[AuditTrail]:
    trail = AuditTrail()
    with collecting(trail.engine_calls):
        yield trail
    # Reached only when the route returned normally: an exception (a 4xx, a
    # TurnCall failure) is thrown in at the yield and propagates past this.
    if not trail.satisfied:
        logger.error("audited route returned without recording an entry")
        raise UnrecordedChange("an audited route returned without recording an entry")


# scope="function": the teardown runs as the route returns, before the
# response is sent, so its refusal is the response.
Audit = Annotated[AuditTrail, Depends(_trail, scope="function")]


def api_routes(app: FastAPI) -> Iterator[tuple[str, APIRoute]]:
    """Every API route the app serves, with its full path."""
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # FastAPI before included routers were kept as such
        for route in app.routes:
            if isinstance(route, APIRoute):
                yield route.path, route
        return
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if isinstance(route, APIRoute) and context.path:
            yield context.path, route


def uses_audit(route: APIRoute) -> bool:
    """Whether the route takes an audit trail, at any depth."""
    pending = [route.dependant]
    while pending:
        dependant = pending.pop()
        if dependant.call is _trail:
            return True
        pending.extend(dependant.dependencies)
    return False


# --- retention -----------------------------------------------------------------

SWEEP_SECONDS = 3600


async def retention_loop(pool: Any, days: int) -> None:
    """Delete entries older than `days`, hourly, for every Workspace. An
    operator's setting (AUDIT_LOG_RETENTION_DAYS), never a Workspace's: an
    admin must not be able to shorten the trail that records them."""
    while True:
        try:
            removed = await audit_store.purge_older_than(pool, days)
            if removed:
                logger.info("audit retention: removed %d entries older than %d days", removed, days)
        except Exception:  # noqa: BLE001 — the next sweep retries
            logger.exception("audit retention sweep failed")
        await asyncio.sleep(SWEEP_SECONDS)
