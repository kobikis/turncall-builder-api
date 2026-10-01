"""Provider Integrations: a Workspace's own provider accounts (ADR-0023, #55-#58).

Writing, re-applying, disconnecting and adopting are admin-only — the account is
workspace-wide and billable. Reading presence is open to every member, because
the agent form needs to know what is connected. No read carries a value: a card
shows presence, a last-four and what the key was checked against.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .. import integration_store, provider_catalog, provider_integrations, runtime
from .._helpers import _readable
from ..backends import registry
from ..deps import AuthContext, require_admin, require_member

router = APIRouter(prefix="/integrations/providers")


class _ApiKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str = Field(min_length=1, max_length=4096)


class _AWSSet(BaseModel):
    """The shape TurnCall validates for an `aws` project credential (ADR-0020)."""

    model_config = ConfigDict(extra="forbid")
    region: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    role_arn: str | None = None
    external_id: str | None = None
    profile: str | None = None

    @model_validator(mode="after")
    def _source(self) -> "_AWSSet":
        if bool(self.access_key_id) != bool(self.secret_access_key):
            raise ValueError("access_key_id and secret_access_key go together")
        if not (self.access_key_id or self.role_arn or self.profile):
            raise ValueError("give access_key_id + secret_access_key, role_arn, or profile")
        return self


def _document(provider: str, body: dict[str, Any]) -> dict[str, Any]:
    stripped = {k: v.strip() if isinstance(v, str) else v for k, v in body.items()}
    schema = _AWSSet if provider == provider_integrations.AWS else _ApiKey
    try:
        return schema.model_validate(stripped).model_dump(exclude_none=True)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_readable(exc)) from exc


async def _known(provider: str) -> None:
    """The account namespace is TurnCall's: ask it rather than keep a copy."""
    engine = await provider_integrations.engine_providers(runtime.get_pool(), runtime.get_client())
    if engine is None:
        raise HTTPException(status_code=503, detail="TurnCall is unreachable — cannot check the provider")
    if provider not in engine.get("accounts", {}):
        known = ", ".join(sorted(engine.get("accounts", {})))
        raise HTTPException(status_code=422, detail=f"unknown provider {provider!r} — one of {known}")


def _store_error(exc: integration_store.IntegrationError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


@router.get("")
async def list_providers(ctx: AuthContext = Depends(require_member)) -> dict[str, Any]:
    data = await provider_integrations.cards(runtime.get_pool(), runtime.get_client(), ctx.workspace_id)
    return {"success": True, "data": data}


@router.put("/{provider}")
async def connect(
    provider: str,
    body: dict[str, Any] = Body(...),  # noqa: B008
    ctx: AuthContext = Depends(require_admin),
) -> dict[str, Any]:
    """Validate, store and project a credential. A key the provider rejects is
    not stored; one that could not be checked is stored and says so."""
    await _known(provider)
    document = _document(provider, body)
    validation = await provider_catalog.probe(provider, document)
    if validation["status"] == "rejected":
        raise HTTPException(
            status_code=400,
            detail=f"{provider} rejected this key ({validation['detail']}; checked "
            f"{validation['checked']}). It was not saved.",
        )
    pool, client = runtime.get_pool(), runtime.get_client()
    try:
        saved = await integration_store.save(
            pool, ctx.workspace_id, provider,
            document=document, last4=provider_integrations.last4(document), validation=validation,
        )
        agents = await provider_integrations.project(pool, client, ctx.workspace_id, provider)
    except integration_store.IntegrationError as exc:
        raise _store_error(exc) from exc
    return {"success": True, "data": {**saved, "agents": agents}}


@router.post("/{provider}/apply")
async def reapply(provider: str, ctx: AuthContext = Depends(require_admin)) -> dict[str, Any]:
    """Finish a projection that failed partway; agents already done are skipped."""
    pool = runtime.get_pool()
    integration = await integration_store.get(pool, ctx.workspace_id, provider)
    if integration is None:
        raise HTTPException(status_code=404, detail=f"{provider} is not connected")
    if integration["disconnecting"]:
        raise HTTPException(status_code=409, detail=f"{provider} is being disconnected — finish that first")
    try:
        agents = await provider_integrations.project(pool, runtime.get_client(), ctx.workspace_id, provider)
    except integration_store.IntegrationError as exc:
        raise _store_error(exc) from exc
    return {"success": True, "data": {"agents": agents}}


@router.delete("/{provider}")
async def disconnect(provider: str, ctx: AuthContext = Depends(require_admin)) -> dict[str, Any]:
    """Remove from every agent first, the stored record last. `complete: false`
    leaves the card showing what remains; calling again finishes it."""
    pool = runtime.get_pool()
    if await integration_store.get(pool, ctx.workspace_id, provider) is None:
        raise HTTPException(status_code=404, detail=f"{provider} is not connected")
    data = await provider_integrations.disconnect(pool, runtime.get_client(), ctx.workspace_id, provider)
    return {"success": True, "data": data}


class AdoptBody(BaseModel):
    agent_id: str


@router.post("/{provider}/adopt")
async def adopt(provider: str, body: AdoptBody, ctx: AuthContext = Depends(require_admin)) -> dict[str, Any]:
    """Make a pinned agent's own key the Workspace's. Explicit by design: nothing
    moves a secret between scopes unless someone asks for it here."""
    pool = runtime.get_pool()
    backend = await registry.get_backend(pool, body.agent_id, ctx.workspace_id)
    if backend is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if provider_integrations.pinned_account(backend.get("config")) != provider:
        raise HTTPException(status_code=409, detail=f"this agent carries no {provider} key of its own")
    try:
        await provider_integrations.adopt(pool, runtime.get_client(), ctx.workspace_id, backend)
    except provider_integrations.AdoptRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except integration_store.IntegrationError as exc:
        raise _store_error(exc) from exc
    data = await provider_integrations.cards(pool, runtime.get_client(), ctx.workspace_id)
    return {"success": True, "data": data}
