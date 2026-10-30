"""The audit trail a route records a person's changes on (ADR-0028).

A route that changes one of ADR-0028's resources (agents, numbers, integrations
and connected apps, members and roles, workspace (builder) skills, skills, linked
repos) takes `audit: Audit` and records each change with `await audit.record(ctx, ...)`.
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
  sides and is marked `"secret": true`, so the entry says the Deepgram key
  changed and never what it is, and a plain field that holds the text `***`
  is never mistaken for one.

Where the change is builder-side only, the entry is written in the change's
own transaction (`db.transaction`), so the two commit or roll back together.

**A change of several steps is begun, then recorded.** Where a change reaches
TurnCall (or GitHub, or the Agent Backend's container) it shares no
transaction with anything, and it is often several steps: save a master, then
write it into every opted-in agent; unbind a number, then bind it elsewhere.
Such a route calls `audit.begin(...)` as its change starts to land and
`await change.record()` once it is done. If the route raises in between, the
trail itself writes the entry as the exception passes its teardown, with
`outcome: "partial"`, the reason (`failure_reason`), what the change had
reached so far (its `agent_ids`, its engine links), and lets the exception go
on. So the trail records what happened, not what was meant to: a change that
landed halfway is in the log, and one that failed before anything landed
(`begin(..., landed=False)`, never marked landed, and no TurnCall write
accepted since) is not.

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
from .turncall_client import AuditedCalls, collecting, failure_reason

logger = logging.getLogger("turncall_builder")

MASK = "***"
# What an entry's `outcome` is: the change finished, or it failed after some of
# it had landed (and `error` says why).
COMPLETED = "completed"
PARTIAL = "partial"
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
        # A skill version (its id), and the Workspace's skill library. The names
        # are the ones these were first written under: `skill` above is a
        # Builder skill, and entries are append-only.
        # (the Workspace's id): its creation, and linking projects to it
        # (#131, #132, ADR-0029).
        "capability",
        "capability_library",
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
    shown as `***` (or null when unset), at any depth and whatever it holds,
    with `secret: true` on its change."""
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
            changes[path] = masked(was.shown() is not None, now.shown() is not None)
        else:
            changes[path] = {"before": was, "after": now}
    return changes


def before_unknown(after: Any) -> dict[str, dict[str, Any]]:
    """Every field of `after`, for a change whose previous value could not be
    read: `{"after": ..., "before_unknown": true}` with no `before`, so the
    entry never claims a field was null when nobody knows what it was."""
    return {
        path: {k: v for k, v in change.items() if k != "before"} | {"before_unknown": True}
        for path, change in diff(None, after).items()
    }


def masked(was_set: bool, is_set: bool) -> dict[str, Any]:
    """One secret's change: `***` for a side where it was set, null where it
    was not, and `secret: true` so the mask is never read as a value."""
    return {"before": MASK if was_set else None, "after": MASK if is_set else None, "secret": True}


def secret_changed(existed_before: bool, exists_after: bool) -> dict[str, dict[str, Any]]:
    """The whole of a credential's entry: that it was set, replaced or removed."""
    return {"/value": masked(existed_before, exists_after)}


class Change:
    """A change of several steps, under way (AuditTrail.begin).

    The route keeps it current as steps land: `agent_ids` grows with each
    agent reached, `landed()` says the first step took effect, and `before`,
    `after`, `changes` and `resource_name` may be replaced. `record()` writes
    it as completed; a route that raises first leaves it to the trail."""

    _FIELDS = frozenset(
        {"action", "resource_id", "resource_name", "before", "after", "changes", "agent_ids"}
    )

    def __init__(
        self,
        trail: AuditTrail,
        ctx: AuthContext,
        resource_type: str,
        resource_id: str,
        action: str,
        *,
        landed: bool,
        before: Any,
        after: Any,
        changes: dict[str, Any] | None,
        resource_name: str | None,
        agent_ids: Iterable[str | None],
    ) -> None:
        self._trail = trail
        self.ctx = ctx
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.action = action
        self.before = before
        self.after = after
        self.changes = changes
        self.resource_name = resource_name
        self.agent_ids: list[str | None] = list(agent_ids)
        self._landed = landed
        self._noted_at_begin = trail.engine_calls.noted
        self.entry: dict[str, Any] | None = None

    def landed(self) -> None:
        """Something of this change has taken effect: from here on, a failure
        is recorded as partial rather than as nothing at all."""
        self._landed = True

    @property
    def has_landed(self) -> bool:
        """Marked landed, or TurnCall has accepted a write since it began (the
        client notes every call the engine audited)."""
        return self._landed or self._trail.engine_calls.noted > self._noted_at_begin

    async def record(self, **fields: Any) -> dict[str, Any]:
        """Write it as completed, with any field given here replacing the one
        it began with."""
        for name, value in fields.items():
            if name not in self._FIELDS:
                raise TypeError(f"unknown change field {name!r}")
            setattr(self, name, value)
        return await self._trail._settle(self, COMPLETED, None)


class AuditTrail:
    """One request's entries. Every entry it records shares its request_id."""

    def __init__(self) -> None:
        self.request_id = str(uuid.uuid4())
        self.entries: list[dict[str, Any]] = []
        self._unchanged = False
        self._open: list[Change] = []
        # The audited TurnCall calls this request makes, collected by the
        # client while _trail holds the trail open.
        self.engine_calls = AuditedCalls()

    def begin(
        self,
        ctx: AuthContext,
        resource_type: str,
        resource_id: str,
        action: str,
        *,
        landed: bool = True,
        before: Any = None,
        after: Any = None,
        changes: dict[str, Any] | None = None,
        resource_name: str | None = None,
        agent_ids: Iterable[str | None] = (),
    ) -> Change:
        """Start a change of several steps; see the module docstring. By
        default it has landed already (call it once the first step took
        effect); `landed=False` begins it before, and then only `landed()` or
        a TurnCall write accepted since makes a failure worth an entry."""
        if resource_type not in RESOURCE_TYPES:
            raise ValueError(f"unknown audit resource_type {resource_type!r}")
        change = Change(
            self, ctx, resource_type, resource_id, action, landed=landed,
            before=before, after=after, changes=changes,
            resource_name=resource_name, agent_ids=agent_ids,
        )
        self._open.append(change)
        return change

    async def _settle(
        self, change: Change, outcome: str, error: str | None
    ) -> dict[str, Any]:
        self._open.remove(change)
        change.entry = await self.record(
            change.ctx, change.resource_type, change.resource_id, change.action,
            before=change.before, after=change.after, changes=change.changes,
            resource_name=change.resource_name, agent_ids=change.agent_ids,
            outcome=outcome, error=error,
        )
        return change.entry

    async def _close(self, exc: BaseException | None) -> None:
        """Settle every change still under way as the route ends: completed
        when it returned, partial when it raised and the change had landed."""
        for change in list(self._open):
            if exc is None:
                await self._settle(change, COMPLETED, None)
                continue
            if not change.has_landed:
                self._open.remove(change)
                continue
            try:
                await self._settle(change, PARTIAL, failure_reason(exc))
            except Exception:  # noqa: BLE001 — the route's own failure is what propagates
                logger.exception(
                    "could not record a partial %s %s change", change.resource_type, change.action
                )

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
        outcome: str = COMPLETED,
        error: str | None = None,
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
            "outcome": outcome,
            "error": error,
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
    try:
        with collecting(trail.engine_calls):
            yield trail
    except Exception as exc:
        # The route raised (a 4xx, a TurnCall failure), thrown in at the
        # yield: a change that had landed is recorded as partial, and the
        # exception goes on to be the response.
        await trail._close(exc)
        raise
    await trail._close(None)
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
