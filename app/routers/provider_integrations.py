"""Provider Integrations: a Workspace's own provider accounts (ADR-0023, #55-#58).

Writing, re-applying, disconnecting and adopting are admin-only — the account is
workspace-wide and billable. Reading presence is open to every member, because
the agent form needs to know what is connected. No read carries a value: a card
shows presence, a last-four and what the key was checked against.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .. import integration_store, provider_catalog, provider_integrations, runtime
from .._helpers import Refusal, _readable
from ..audit import Audit, secret_changed
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


class _Telnyx(_ApiKey):
    """A Telnyx account (#84): its key, and the public key that verifies its
    webhooks — without it TurnCall refuses every webhook the account sends."""

    public_key: str = Field(min_length=1, max_length=256)

    @field_validator("public_key")
    @classmethod
    def _ed25519(cls, value: str) -> str:
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raw = b""
        if len(raw) != 32:
            raise ValueError(
                "public_key must be the account's base64 Ed25519 public key, "
                "as Mission Control shows it (Keys & Credentials > Public Key)"
            )
        return value


_NAME = r"^[A-Za-z0-9_.-]{1,64}$"


class _Asterisk(BaseModel):
    """A PBX reached over ARI (#84), in the shape TurnCall validates. One for
    every agent's project: TurnCall shares one ARI connection per account
    (kobikis/turncall#274). Never dialled from here — TurnCall applies its own
    URL allowlist when the credential is projected."""

    model_config = ConfigDict(extra="forbid")
    ari_url: str = Field(min_length=1, max_length=2048)
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=4096)
    app: str | None = Field(default=None, pattern=_NAME)
    media_connection: str | None = Field(default=None, pattern=_NAME)
    transfer_context: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,80}$")

    @field_validator("ari_url")
    @classmethod
    def _http_base(cls, value: str) -> str:
        value = value.rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("ari_url must be the http(s) base of Asterisk's ARI, e.g. https://pbx:8088")
        return value


_SCHEMAS: dict[str, type[BaseModel]] = {
    provider_integrations.AWS: _AWSSet,
    provider_integrations.TELNYX: _Telnyx,
    provider_integrations.ASTERISK: _Asterisk,
}


def _document(provider: str, body: dict[str, Any]) -> dict[str, Any]:
    # A blank optional field means TurnCall's default, which is its absence.
    stripped = {
        k: (v.strip() or None) if isinstance(v, str) else v for k, v in body.items()
    }
    schema = _SCHEMAS.get(provider, _ApiKey)
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


class _Acknowledge(BaseModel):
    """Telnyx numbers the admin accepts will stop answering calls (#114): the
    `numbers` of a `numbers_not_on_account` refusal, sent back."""

    model_config = ConfigDict(extra="forbid")
    acknowledge_numbers: list[str] = Field(default_factory=list, max_length=10_000)

    @field_validator("acknowledge_numbers")
    @classmethod
    def _stripped(cls, value: list[str]) -> list[str]:
        return [n.strip() for n in value]


def _acknowledged(raw: Any) -> list[str]:
    if raw is None:
        return []
    try:
        return _Acknowledge.model_validate({"acknowledge_numbers": raw}).acknowledge_numbers
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_readable(exc)) from exc


def _not_on_account(numbers: list[str], when: str) -> str:
    named = provider_integrations.name_numbers(numbers)
    one = len(numbers) == 1
    return (
        f"{named} {'is' if one else 'are'} not on this Telnyx account. "
        f"{'It is' if one else 'They are'} on another Telnyx account (likely TurnCall's "
        f"platform account) and will stop answering calls once {when}. Move "
        f"{'it' if one else 'them'} to this account first, or acknowledge "
        f"{'it' if one else 'them'} to go ahead."
    )


async def _check_telnyx_numbers(
    api_key: str,
    numbers: list[str],
    acknowledged: list[str],
    *,
    when: str,
    new_key: bool = False,
) -> dict[str, Any]:
    """Refuse a Telnyx key whose account doesn't hold the numbers bound in the
    projects it will be written into (`telnyx_write_targets`), unless every one
    of them is acknowledged (#114, #119). Returns the audit entry's record of
    the ones acknowledged, if any."""
    try:
        missing = await provider_integrations.check_telnyx_numbers(
            api_key, numbers, acknowledged, new_key=new_key
        )
    except provider_integrations.NumbersNotOnAccount as exc:
        raise Refusal(
            409, _not_on_account(exc.numbers, when), "numbers_not_on_account", numbers=exc.numbers
        ) from exc
    except provider_integrations.NumberLookupFailed as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"/acknowledged_numbers": {"before": None, "after": missing}} if missing else {}


@router.get("")
async def list_providers(ctx: AuthContext = Depends(require_member)) -> dict[str, Any]:
    data = await provider_integrations.cards(runtime.get_pool(), runtime.get_client(), ctx.workspace_id)
    return {"success": True, "data": data}


@router.put("/{provider}")
async def connect(
    provider: str,
    audit: Audit,
    body: dict[str, Any] = Body(...),  # noqa: B008
    ctx: AuthContext = Depends(require_admin),
) -> dict[str, Any]:
    """Validate, store and project a credential. A key the provider rejects is
    not stored; one that could not be checked is stored and says so.

    A Telnyx key is refused (409, `numbers_not_on_account`) while its account
    doesn't hold every Telnyx number bound in the projects it will be written
    into (each agent's, and the Workspace's webhook project; never the shared
    one), unless the request acknowledges each of them in
    `acknowledge_numbers` (#114, #119)."""
    acknowledged = _acknowledged(body.pop("acknowledge_numbers", None))
    await _known(provider)
    document = _document(provider, body)
    validation = await provider_catalog.probe(provider, document)
    if validation["status"] == "rejected":
        raise HTTPException(
            status_code=400,
            detail=f"{provider} rejected this key ({validation['detail']}; checked "
            f"{validation['checked']}). It was not saved.",
        )
    noted: dict[str, Any] = {}
    # The Telnyx numbers checked here (held, or acknowledged): the projection's
    # own check on each write skips them, and catches any bound since.
    cleared: list[str] = []
    if provider == provider_integrations.TELNYX:
        # A save writes into every target.
        targets = await provider_integrations.telnyx_write_targets(
            runtime.get_pool(), ctx.workspace_id
        )
        noted = await _check_telnyx_numbers(
            document["api_key"], targets.numbers, acknowledged,
            when="this account is connected", new_key=True,
        )
        cleared = targets.numbers
    pool, client = runtime.get_pool(), runtime.get_client()
    existed = await integration_store.get(pool, ctx.workspace_id, provider) is not None
    # That the key changed, never the key (ADR-0028). Landed once stored: a
    # projection that fails after it is recorded as partial.
    change = audit.begin(
        ctx, "integration", provider, "updated" if existed else "created", landed=False,
        resource_name=provider, changes={**secret_changed(existed, True), **noted},
    )
    try:
        saved = await integration_store.save(
            pool, ctx.workspace_id, provider,
            document=document, last4=provider_integrations.last4(document), validation=validation,
        )
        change.landed()
        agents = await provider_integrations.project(
            pool, client, ctx.workspace_id, provider, cleared=cleared
        )
    except integration_store.IntegrationError as exc:
        raise _store_error(exc) from exc
    await change.record()
    return {"success": True, "data": {**saved, "agents": agents}}


@router.post("/{provider}/apply")
async def reapply(
    provider: str,
    audit: Audit,
    body: dict[str, Any] | None = Body(None),  # noqa: B008
    ctx: AuthContext = Depends(require_admin),
) -> dict[str, Any]:
    """Finish a projection that failed partway; agents already done are skipped.

    For Telnyx, the projects it would write into are checked as connecting
    checks them (#114, #119): only their bound numbers, and none when every
    project holds the current key already."""
    pool = runtime.get_pool()
    integration = await integration_store.get(pool, ctx.workspace_id, provider)
    if integration is None:
        raise HTTPException(status_code=404, detail=f"{provider} is not connected")
    if integration["disconnecting"]:
        raise HTTPException(status_code=409, detail=f"{provider} is being disconnected — finish that first")
    noted: dict[str, Any] = {}
    cleared: list[str] = []  # as connect's
    if provider == provider_integrations.TELNYX:
        # Apply writes only into targets not holding the current key.
        targets = await provider_integrations.telnyx_write_targets(
            pool, ctx.workspace_id, skip_holding=integration
        )
        cleared = targets.numbers
        acknowledged = _acknowledged((body or {}).get("acknowledge_numbers"))
        if targets.numbers:
            try:
                document = await integration_store.get_document(pool, ctx.workspace_id, provider)
            except integration_store.IntegrationError as exc:
                raise _store_error(exc) from exc
            noted = await _check_telnyx_numbers(
                document["api_key"], targets.numbers, acknowledged,
                when="this account is applied to their projects",
            )
    try:
        agents = await provider_integrations.project(
            pool, runtime.get_client(), ctx.workspace_id, provider, cleared=cleared
        )
    except integration_store.IntegrationError as exc:
        raise _store_error(exc) from exc
    await audit.record(ctx, "integration", provider, "reapplied", resource_name=provider, changes=noted)
    return {"success": True, "data": {"agents": agents}}


@router.delete("/{provider}")
async def disconnect(
    provider: str, audit: Audit, ctx: AuthContext = Depends(require_admin)
) -> dict[str, Any]:
    """Remove from every agent first, the stored record last. `complete: false`
    leaves the card showing what remains; calling again finishes it."""
    pool = runtime.get_pool()
    if await integration_store.get(pool, ctx.workspace_id, provider) is None:
        raise HTTPException(status_code=404, detail=f"{provider} is not connected")
    # Raising partway (not the per-agent failures it reports) leaves some
    # agents without the key: recorded as partial once TurnCall accepted any
    # removal.
    change = audit.begin(
        ctx, "integration", provider, "disconnect_started", landed=False,
        resource_name=provider, changes={},
    )
    data = await provider_integrations.disconnect(pool, runtime.get_client(), ctx.workspace_id, provider)
    # Partway (`complete: false`) the key is still stored: the entry says the
    # disconnect began, and the call that finishes it records the deletion.
    if data.get("complete"):
        await change.record(action="deleted", changes=secret_changed(True, False))
    else:
        await change.record()
    return {"success": True, "data": data}


class AdoptBody(BaseModel):
    agent_id: str


@router.post("/{provider}/adopt")
async def adopt(
    provider: str, body: AdoptBody, audit: Audit, ctx: AuthContext = Depends(require_admin)
) -> dict[str, Any]:
    """Make a pinned agent's own key the Workspace's. Explicit by design: nothing
    moves a secret between scopes unless someone asks for it here."""
    pool = runtime.get_pool()
    backend = await registry.get_backend(pool, body.agent_id, ctx.workspace_id)
    if backend is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if provider_integrations.pinned_account(backend.get("config")) != provider:
        raise HTTPException(status_code=409, detail=f"this agent carries no {provider} key of its own")
    # adopt() refuses when the Workspace already holds a key for it. Several
    # steps (store, project, rewrite the agent): landed once TurnCall
    # accepted any of them.
    change = audit.begin(
        ctx, "integration", provider, "adopted", landed=False,
        resource_name=provider, agent_ids=[body.agent_id], changes=secret_changed(False, True),
    )
    try:
        await provider_integrations.adopt(pool, runtime.get_client(), ctx.workspace_id, backend)
    except provider_integrations.AdoptRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except integration_store.IntegrationError as exc:
        raise _store_error(exc) from exc
    await change.record()
    data = await provider_integrations.cards(pool, runtime.get_client(), ctx.workspace_id)
    return {"success": True, "data": data}
