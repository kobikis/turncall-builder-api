"""Request-pipeline gate: resolve (user, workspace, role) and enforce RBAC (#31).

Every Workspace-owned endpoint depends on `require_member` (any role, read) or
`require_editor` (editor/admin, write). The dependency resolves the caller from
the session cookie and the active Workspace from the `X-Workspace-Id` header,
looks up the Membership fresh each request (so a kick/re-role lands on the next
call), and injects an AuthContext. Handlers then scope their queries to
`ctx.workspace_id`. TurnCall stays identity-free — this all lives in builder-api.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import Cookie, Header, HTTPException

from . import auth_store, runtime
from .routers.auth import COOKIE_NAME  # single source of truth for the cookie name

# Role ordering: a dependency admits any role at or above its threshold.
_RANK = {"viewer": 0, "editor": 1, "admin": 2}


@dataclass(frozen=True)
class AuthContext:
    """The resolved caller for one request. Handlers scope by workspace_id."""

    user: dict[str, Any]
    workspace_id: str
    role: str


def require_role(min_role: str) -> Callable:
    """Build a dependency that admits callers whose role is `min_role` or higher.

    401 no/invalid session · 400 missing X-Workspace-Id · 403 not a member or
    role too low."""

    async def dependency(
        # Cookie param name must match the cookie key, hence the underscored alias.
        tc_builder_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
        x_workspace_id: str | None = Header(default=None),
    ) -> AuthContext:
        pool = runtime.get_pool()
        if not tc_builder_session:
            raise HTTPException(status_code=401, detail="not authenticated")
        user = await auth_store.resolve_session(pool, tc_builder_session)
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        if not x_workspace_id:
            raise HTTPException(status_code=400, detail="X-Workspace-Id header required")
        role = await auth_store.resolve_membership(pool, user["id"], x_workspace_id)
        if role is None:
            raise HTTPException(status_code=403, detail="not a member of this workspace")
        # role comes from the DB — an unrecognized value is a 403, never a KeyError 500
        # that would leak the gate's internals. min_role is always a trusted literal.
        rank = _RANK.get(role)
        if rank is None or rank < _RANK[min_role]:
            raise HTTPException(status_code=403, detail=f"requires {min_role} role")
        return AuthContext(user=user, workspace_id=x_workspace_id, role=role)

    return dependency


# Stable singletons so tests can target them in app.dependency_overrides.
require_member = require_role("viewer")
require_editor = require_role("editor")
require_admin = require_role("admin")
