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

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, field_validator

from .. import pipedream, pipedream_store, runtime
from ..deps import AuthContext, require_admin, require_member
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
            raise ValueError(f"unknown app — choose one of {sorted(pipedream.CATALOG)}")
        return v


@router.get("/catalog")
async def catalog(ctx: AuthContext = Depends(require_member)) -> dict[str, Any]:
    """The apps this build can connect. Small on purpose (ADR-0018) — anything
    else goes through the raw MCP server form."""
    return {
        "success": True,
        "data": [{"app": slug, "name": name} for slug, name in pipedream.CATALOG.items()],
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
        return _back(error=f"{pipedream.CATALOG.get(app, app)} was not connected.")

    cred = await _credential()
    try:
        accounts = await pipedream.list_accounts(cred, workspace_id=workspace_id, app=app)
    except pipedream.PipedreamError as exc:
        return _back(error=str(exc))
    if not accounts:
        return _back(error=f"{pipedream.CATALOG.get(app, app)} was not connected.")
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
