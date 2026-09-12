"""Connecting a Workspace to Slack or Google Calendar (ADR-0018).

Connecting is an admin action: the credential is workspace-wide and what it
authorizes is spending money and posting in someone's Slack. Seeing what is
connected is not — a viewer building an agent needs to know what is available.

The builder never touches the third party's consent screen or its tokens. It
sends the admin to Pipedream's hosted Connect Link and, when they come back,
asks Pipedream what exists. Pipedream stays the system of record; there is no
local mirror to drift.

The callback is unauthenticated by construction — it is a redirect arriving from
Pipedream — so it trusts nothing in the query string except our own signed state,
which is what names the Workspace and the app.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, field_validator

from .. import agent_update, pipedream, pipedream_store, runtime
from .._helpers import _backend_or_404
from ..backends import registry
from ..deps import AuthContext, require_admin, require_editor, require_member
from ..settings import load_settings

router = APIRouter(prefix="/integrations")


async def _credential() -> pipedream.Credential:
    """The deployment credential, or a 503 that says what is missing."""
    try:
        cred = await pipedream_store.get_credential(runtime.get_pool())
    except pipedream.PipedreamError as exc:
        # A rotated encryption key, most likely. Say which key and what to do —
        # this must not read as a server bug.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if cred is None:
        raise HTTPException(
            status_code=503,
            detail="Integrations are not configured — set PIPEDREAM_CLIENT_ID, "
            "PIPEDREAM_CLIENT_SECRET and PIPEDREAM_PROJECT_ID and restart.",
        )
    return cred


def _fail(exc: pipedream.PipedreamError) -> HTTPException:
    """Pipedream errors are written to be shown — they say what to do next."""
    return HTTPException(status_code=400, detail=str(exc))


class ConnectBody(BaseModel):
    app: str

    @field_validator("app")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in pipedream.CATALOG:
            raise ValueError(f"unknown app — choose one of {sorted(pipedream.CATALOG)}")  # noqa: E501
        return v


@router.get("/catalog")
async def catalog(ctx: AuthContext = Depends(require_member)) -> dict[str, Any]:
    """The apps this build can connect. Small on purpose (ADR-0018) — anything
    else goes through the raw MCP server form."""
    return {
        "success": True,
        "data": [
            {"app": app.slug, "name": app.name, "tools": list(app.tools)}
            for app in pipedream.CATALOG.values()
        ],
    }


@router.get("")
async def list_connected(ctx: AuthContext = Depends(require_member)) -> dict[str, Any]:
    """What this Workspace has connected. Scoped by `external_user_id`, so
    another Workspace's Connected apps are not reachable from here."""
    cred = await _credential()
    try:
        accounts = await pipedream.list_accounts(cred, workspace_id=ctx.workspace_id)
    except pipedream.PipedreamError as exc:
        raise _fail(exc) from exc
    return {"success": True, "data": accounts}


@router.post("/connect")
async def connect(
    body: ConnectBody, ctx: AuthContext = Depends(require_admin)
) -> dict[str, Any]:
    """Start an authorization. Returns the hosted page to send the admin to —
    the console navigates there; nothing is recorded until they come back."""
    cred = await _credential()
    settings = load_settings()
    if not settings.pipedream_redirect_uri:
        raise HTTPException(
            status_code=503,
            detail="PIPEDREAM_REDIRECT_URI is not set — Pipedream has nowhere to "
            "send the admin back to. Set it to this API's public "
            "/integrations/callback URL.",
        )
    try:
        url = await pipedream.connect_link(
            cred,
            workspace_id=ctx.workspace_id,
            app=body.app,
            callback_url=settings.pipedream_redirect_uri,
        )
    except pipedream.PipedreamError as exc:
        raise _fail(exc) from exc
    return {"success": True, "data": {"connect_link_url": url}}


def _app_name(slug: str) -> str:
    known = pipedream.CATALOG.get(slug)
    return known.name if known else slug


def _back(**params: str) -> RedirectResponse:
    """Land the browser back on the console's integrations page."""
    base = load_settings().public_base_url or ""
    return RedirectResponse(url=f"{base}/integrations?{urlencode(params)}", status_code=303)


@router.get("/callback")
async def callback(
    state: str = Query(...), error: str | None = Query(default=None)
) -> RedirectResponse:
    """Where Pipedream sends the admin back, success or failure.

    A success redirect is not proof of anything, so we ask Pipedream whether the
    account actually exists before saying so. Reporting an abandoned flow as
    connected is the half-made record this endpoint exists to avoid.
    """
    try:
        workspace_id, app = pipedream.load_state(state)
    except pipedream.PipedreamError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if error:
        return _back(error=f"{_app_name(app)} was not connected.")

    cred = await _credential()
    try:
        accounts = await pipedream.list_accounts(cred, workspace_id=workspace_id, app=app)
    except pipedream.PipedreamError as exc:
        return _back(error=str(exc))
    if not accounts:
        return _back(error=f"{_app_name(app)} was not connected.")
    return _back(connected=app)


@router.delete("/{account_id}", status_code=204)
async def disconnect(
    account_id: str, ctx: AuthContext = Depends(require_admin)
) -> None:
    """Revoke a Connected app at Pipedream.

    The id is checked against this Workspace's own list first: Pipedream's
    account ids are global, and an admin here must not be able to delete one by
    guessing at another tenant's.
    """
    cred = await _credential()
    try:
        mine = await pipedream.list_accounts(cred, workspace_id=ctx.workspace_id)
        if not any(a["id"] == account_id for a in mine):
            raise HTTPException(status_code=404, detail="No such connected app.")
        await pipedream.delete_account(cred, account_id)
    except pipedream.PipedreamError as exc:
        raise _fail(exc) from exc


# --- attaching an app to an Agent (ADR-0018) ---------------------------------


class AttachBody(BaseModel):
    apps: list[str]

    @field_validator("apps")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        unknown = [a for a in v if a not in pipedream.CATALOG]
        if unknown:
            raise ValueError(
                f"unknown app(s) {unknown} — choose from {sorted(pipedream.CATALOG)}"
            )
        # Order is not meaningful and a repeat would produce two identically
        # named servers, which TurnCall resolves by dropping one.
        return sorted(set(v))


@router.get("/agents/{agent_id}")
async def get_attached(
    agent_id: str, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    """Which apps this Agent has attached."""
    backend = await _backend_or_404(agent_id, ctx.workspace_id)
    apps = (backend.get("config") or {}).get("integrations") or []
    return {"success": True, "data": {"apps": apps}}


@router.put("/agents/{agent_id}")
async def set_attached(
    agent_id: str, body: AttachBody, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Attach (or detach) Connected apps on this Agent.

    The whole list is the unit: an empty list detaches everything. Writing it
    goes down the ordinary apply path, so the agent's Config reaches TurnCall the
    same way a prompt edit does — one code path, one set of rules.
    """
    backend = await _backend_or_404(agent_id, ctx.workspace_id)
    config = {**(backend.get("config") or {}), "integrations": body.apps}
    await agent_update.apply_update(
        runtime.get_pool(), runtime.get_client(), backend, config
    )
    return {"success": True, "data": {"apps": body.apps}}


# --- minting for an Agent Backend (ADR-0019) ---------------------------------


async def _calling_backend(authorization: str | None = Header(default=None)) -> dict:
    """Resolve which Agent Backend is calling, from its own bearer token.

    Not the session cookie: the caller is a container, not a person. Not the
    webhook_secret either — TurnCall holds that one, and reusing it would let
    TurnCall mint Pipedream tokens for every Workspace it serves.
    """
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="agent backend token required")
    backend = await registry.get_backend_by_builder_token(
        runtime.get_pool(), token.strip()
    )
    if backend is None:
        raise HTTPException(status_code=401, detail="unknown agent backend")
    return backend


@router.post("/token")
async def mint_token(backend: dict = Depends(_calling_backend)) -> dict[str, Any]:
    """A short-lived Pipedream access token for the calling Agent Backend.

    The token is Pipedream-project-wide; what scopes it to one tenant is
    `external_user_id`, returned alongside and fixed to the Workspace that owns
    this Agent. A backend cannot ask for another Workspace's — it never names
    one.

    Reused until it expires rather than minted per call: Pipedream tokens live
    an hour and a call is not the unit of anything here.
    """
    cred = await _credential()
    if not backend.get("workspace_id"):
        raise HTTPException(
            status_code=409,
            detail="This agent predates Workspace scoping and cannot be given a "
            "tenant-scoped token. Recreate it.",
        )
    try:
        minted = await pipedream.access_token(cred)
    except pipedream.PipedreamError as exc:
        raise _fail(exc) from exc
    return {
        "success": True,
        "data": {
            "access_token": minted.token,
            "expires_at": minted.expires_at,
            # What the backend puts in x-pd-* headers. None of it is a secret,
            # and the client secret is never any part of this response.
            "external_user_id": backend["workspace_id"],
            "project_id": cred.project_id,
            "environment": cred.environment,
        },
    }
