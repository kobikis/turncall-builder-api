"""Phone-number binding endpoints + routing resolution."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from .. import provider_integrations, runtime, twilio_store
from ..integration_store import BOX, IntegrationError
from ..backends import generator, phones, registry
from ..deps import AuthContext, require_editor, require_member

router = APIRouter()


class PhoneBind(BaseModel):
    e164: str
    # The number's Twilio SID. Optional when the number brings its own Twilio
    # account: TurnCall looks it up there by E.164.
    sid: str = ""
    # 'agent' | 'agent_call_init' (managed call-init, ADR-0008) | 'webhook' | 'none'
    routing_type: str
    agent_id: str | None = None
    server_url: str | None = None
    sms_enabled: bool = False
    # The number's own Twilio account (both or neither). Write-only: the token
    # is sealed here so a rebind can re-send it, and never returned. Omitted on
    # an edit = keep the stored pair.
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    # Bind with the Workspace's connected Twilio account (Integrations page)
    # instead of entering a pair here.
    use_workspace_twilio: bool = False
    # A name for the number in the console ("Support line"). Builder-side only.
    label: str | None = Field(default=None, max_length=100)

    @field_validator("label")
    @classmethod
    def _label(cls, v: str | None) -> str | None:
        return (v or "").strip() or None

    @model_validator(mode="after")
    def _twilio_pair(self) -> "PhoneBind":
        if bool(self.twilio_account_sid) != bool(self.twilio_auth_token):
            raise ValueError("twilio_account_sid and twilio_auth_token go together")
        return self

    def own_twilio(self) -> tuple[str, str] | None:
        if self.twilio_account_sid and self.twilio_auth_token:
            return self.twilio_account_sid.strip(), self.twilio_auth_token.strip()
        return None


async def _unbind_in_turncall(pool: Any, number: dict[str, Any], workspace_id: str) -> None:
    """Unbind in TurnCall with a clean failure mode: an unreachable TurnCall
    becomes a 502 (retryable), never a raw 500 with a stack trace."""
    old_key = await _key_for_number(pool, number, workspace_id)
    try:
        await runtime.get_client().unbind_phone_number(number["id"], old_key)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return  # already gone in TurnCall — treat as unbound
        raise HTTPException(
            status_code=502, detail=f"TurnCall rejected the unbind: {exc.response.text[:200]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="TurnCall is unreachable — nothing was changed; retry in a moment",
        ) from exc


async def _key_for_number(pool: Any, number: dict[str, Any], workspace_id: str) -> str:
    """The TurnCall project key that owns this number's binding."""
    if number["routing_type"] in ("agent", "agent_call_init"):
        b = await registry.get_backend(pool, number["agent_id"], workspace_id)
        return b["api_key"]
    # The project the number was bound in: its Workspace's, or the shared one
    # every webhook number went to before #65.
    key = await phones.key_for_project(pool, number["project_id"])
    if key:
        return key
    wp = await phones.ensure_webhook_project(pool, runtime.get_client())
    return wp["api_key"]


async def _resolve_routing(pool: Any, body: PhoneBind, workspace_id: str) -> dict[str, Any]:
    """Resolve a routing choice to its TurnCall fields + owning project/key.

    'agent_call_init' (managed call-init, ADR-0008) is TurnCall webhook routing
    pointed at the agent's own backend, bound in the agent's project so events
    keep flowing to that backend."""
    if body.routing_type in ("agent", "agent_call_init"):
        if not body.agent_id:
            raise HTTPException(status_code=400, detail="agent_id required for agent routing")
        b = await registry.get_backend(pool, body.agent_id, workspace_id)
        if not b:
            raise HTTPException(status_code=404, detail="agent not found")
        if body.routing_type == "agent":
            fields: dict[str, Any] = {
                "routing_target_type": "agent",
                "routing_target_id": body.agent_id,
            }
            server_url = None
        else:
            server_url = f"{registry.backend_url(b['port'])}/call-init"
            fields = {"routing_target_type": "webhook", "server_url": server_url}
        return {
            "fields": fields,
            "project_id": b["project_id"],
            "api_key": b["api_key"],
            "backend": b,
            "agent_id": body.agent_id,
            "server_url": server_url,
        }
    if body.routing_type == "webhook":
        if not body.server_url:
            raise HTTPException(status_code=400, detail="server_url required for webhook routing")
        client = runtime.get_client()
        wp, created = await phones.ensure_workspace_webhook_project(pool, client, workspace_id)
        if created:
            # Its calls run on the Workspace's keys, like any agent's (#65).
            await provider_integrations.project_into_new_agent(
                pool, client, workspace_id, phones.as_projection_target(workspace_id, wp)
            )
        return {
            "fields": {"routing_target_type": "webhook", "server_url": body.server_url},
            "project_id": wp["project_id"],
            "api_key": wp["api_key"],
            "backend": None,
            "agent_id": None,
            "server_url": body.server_url,
        }
    raise HTTPException(
        status_code=400, detail="routing_type must be agent|agent_call_init|webhook|none"
    )


async def _sync_call_init_secret(backend: dict[str, Any] | None, secret: str | None) -> None:
    """Write the number's call-init secret into the backend's .env and recreate
    the container so /call-init verifies against it (managed call-init only).

    The registry follows. An agent is created with a builder-minted secret so a
    browser test call can reach call-init at all; TurnCall mints its own at bind
    and this one wins, so the stored copy has to move with it or the WebRTC path
    keeps signing with a secret the backend has forgotten.
    """
    if backend and secret:
        generator.set_env_value(backend["service_dir"], "CALL_INIT_SECRET", secret)
        await registry.set_call_init_secret(runtime.get_pool(), backend["agent_id"], secret)
        await generator.restart(backend["service_dir"])


async def _bind_in_turncall(
    payload: dict[str, Any], api_key: str, e164: str
) -> dict[str, Any]:
    """Bind in TurnCall with a clean failure mode, as the unbind path has.

    A conflict is the one worth naming: TurnCall's uniqueness is global on the
    number, not scoped to a project, so a number bound outside the builder —
    by an example script, or another workspace — is invisible to our mirror
    until this fires. Without the mapping it surfaced as an ASGI stack trace.
    """
    try:
        return await runtime.get_client().bind_phone_number(payload, api_key)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            raise HTTPException(status_code=409, detail=_conflict_detail(exc, e164)) from exc
        raise HTTPException(
            status_code=502,
            detail=f"TurnCall rejected the bind: {exc.response.text[:200]}",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="TurnCall is unreachable — nothing was changed; retry in a moment",
        ) from exc


def _conflict_detail(exc: httpx.HTTPStatusError, e164: str) -> str:
    """Say which project holds the number, so the fix is obvious."""
    owner = None
    try:
        owner = (exc.response.json().get("details") or {}).get("existing_project_id")
    except ValueError:
        pass
    where = f" by TurnCall project {owner}" if owner else " in TurnCall"
    return (
        f"{e164} is already bound{where}. Unbind it there before binding it "
        "here — a number can only be bound once, across every project."
    )


def _seal(twilio: tuple[str, str] | None, source: str | None = None) -> dict[str, Any]:
    """The mirror's copy of a number's Twilio account. A number on the
    Workspace's account keeps no token of its own — the account row is the copy,
    which is what lets a rotation reach it."""
    if twilio is None:
        return {}
    if source == "workspace":
        return {"twilio_account_sid": twilio[0], "twilio_source": "workspace"}
    try:
        return {
            "twilio_account_sid": twilio[0],
            "twilio_auth_token_encrypted": BOX.encrypt(twilio[1]),
            "twilio_source": "number",
        }
    except IntegrationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _public(number: dict[str, Any]) -> dict[str, Any]:
    """What the console may see: never the sealed token."""
    out = {k: v for k, v in number.items() if k != "twilio_auth_token_encrypted"}
    if "twilio_credentials" not in out:
        out["twilio_credentials"] = (
            "workspace" if number.get("twilio_source") == "workspace"
            else "number" if number.get("twilio_auth_token_encrypted")
            else "platform"
        )
    out.pop("twilio_source", None)
    if out["twilio_credentials"] == "platform":
        out["twilio_account_sid"] = None
    return out


async def _twilio_for(
    pool: Any,
    body: PhoneBind,
    workspace_id: str,
    carried: tuple[str, str] | None,
    carried_source: str | None,
) -> tuple[tuple[str, str] | None, str | None]:
    """Which Twilio account this bind sends, and where it came from: the
    Workspace's (asked for), a pair entered on the form, else what a rebind
    carried over."""
    if body.use_workspace_twilio:
        try:
            creds = await twilio_store.credentials(pool, workspace_id)
        except IntegrationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if creds is None:
            raise HTTPException(
                status_code=400,
                detail="This workspace has no Twilio account connected — connect it on Integrations",
            )
        return creds, "workspace"
    if body.own_twilio():
        return body.own_twilio(), "number"
    return carried, carried_source if carried else None


async def _bind(
    pool: Any,
    body: PhoneBind,
    workspace_id: str,
    twilio: tuple[str, str] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Bind a number in the right project and mirror it. Returns the mirror row.
    An unassigned number ('none') is only mirrored locally — TurnCall's bind needs
    a routing target, so it isn't bound there until assigned an agent/webhook.

    `twilio` is the number's own Twilio account: what the body brings, else what
    a rebind carries over from the mirror."""
    twilio, source = await _twilio_for(pool, body, workspace_id, twilio, source)
    if body.routing_type == "none":
        number = {
            "id": str(uuid.uuid4()),  # local id — no TurnCall binding yet
            "e164": body.e164,
            "sid": body.sid,
            "routing_type": "none",
            "agent_id": None,
            "project_id": None,
            "server_url": None,
            "sms_enabled": body.sms_enabled,
            "label": body.label,
            "workspace_id": workspace_id,
            **_seal(twilio, source),
        }
        await phones.record_number(pool, number)
        return _public(number)

    routing = await _resolve_routing(pool, body, workspace_id)
    payload: dict[str, Any] = {
        "e164_number": body.e164,
        "sms_enabled": body.sms_enabled,
        **({"external_number_sid": body.sid} if body.sid else {}),
        **(
            {"twilio_account_sid": twilio[0], "twilio_auth_token": twilio[1]}
            if twilio
            else {}
        ),
        **routing["fields"],
    }
    data = await _bind_in_turncall(payload, routing["api_key"], body.e164)
    secret = data.get("server_url_secret")
    if body.routing_type == "agent_call_init":
        await _sync_call_init_secret(routing["backend"], secret)
    number = {
        "id": data["id"],
        "e164": body.e164,
        # TurnCall's, which it looked up on the number's own account if needed.
        "sid": data.get("external_number_sid") or body.sid,
        "routing_type": body.routing_type,
        "agent_id": routing["agent_id"],
        "project_id": routing["project_id"],
        "server_url": routing["server_url"],
        # per-number call-init signing secret (webhook + agent_call_init routing)
        "server_url_secret": secret,
        "sms_enabled": body.sms_enabled,
        "label": body.label,
        "workspace_id": workspace_id,
        **_seal(twilio, source),
    }
    await phones.record_number(pool, number)
    # Response-only (not mirrored): whether TurnCall actually pointed the
    # Twilio number at itself — False means calls won't arrive yet.
    number["twilio_webhooks_configured"] = data.get("twilio_webhooks_configured")
    return _public(number)


@router.get("/phone-numbers")
async def list_phone_numbers(
    ctx: AuthContext = Depends(require_member),
) -> dict[str, Any]:
    numbers = await phones.list_numbers(runtime.get_pool(), ctx.workspace_id)
    return {"success": True, "data": {"phone_numbers": numbers}}


@router.get("/phone-numbers/{phone_id}")
async def get_phone_number(
    phone_id: str, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    n = await phones.get_number(runtime.get_pool(), phone_id, ctx.workspace_id)
    if not n:
        raise HTTPException(status_code=404, detail="phone number not found")
    return {"success": True, "data": n}


@router.post("/phone-numbers")
async def add_phone_number(
    body: PhoneBind, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    return {"success": True, "data": await _bind(runtime.get_pool(), body, ctx.workspace_id)}


@router.put("/phone-numbers/{phone_id}")
async def update_phone_number(
    phone_id: str, body: PhoneBind, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Edit a binding. Same-project edits go through TurnCall's PUT — the phone
    id and its call-init secret stay stable. Cross-project moves (changing which
    project owns the number) unbind + rebind, which rotates both; the response
    flags that with `rebound` so the console can warn."""
    pool = runtime.get_pool()
    workspace_id = ctx.workspace_id
    existing = await phones.get_number(pool, phone_id, workspace_id)
    if not existing:
        raise HTTPException(status_code=404, detail="phone number not found")

    if body.routing_type != "none" and existing["routing_type"] != "none":
        routing = await _resolve_routing(pool, body, workspace_id)
        if routing["project_id"] == existing["project_id"]:
            new_twilio, new_source = await _twilio_for(pool, body, workspace_id, None, None)
            data = await runtime.get_client().update_phone_number(
                phone_id,
                {
                    "sms_enabled": body.sms_enabled,
                    **routing["fields"],
                    # Only a newly entered pair is sent; omitted keeps TurnCall's.
                    **(
                        {"twilio_account_sid": new_twilio[0], "twilio_auth_token": new_twilio[1]}
                        if new_twilio
                        else {}
                    ),
                },
                routing["api_key"],
            )
            secret = data.get("server_url_secret") or existing.get("server_url_secret")
            if body.routing_type == "agent_call_init":
                await _sync_call_init_secret(routing["backend"], secret)
            number = {
                "id": phone_id,
                "e164": existing["e164"],
                "sid": existing["sid"],
                "routing_type": body.routing_type,
                "agent_id": routing["agent_id"],
                "project_id": routing["project_id"],
                "server_url": routing["server_url"],
                "server_url_secret": secret,
                "sms_enabled": body.sms_enabled,
                "label": body.label,
                "workspace_id": workspace_id,
                **_seal(new_twilio, new_source),
            }
            await phones.record_number(pool, number)
            stored = await phones.get_number(pool, phone_id, workspace_id) or number
            return {
                "success": True,
                "data": {
                    **_public(stored),
                    "rebound": False,
                    "twilio_webhooks_configured": data.get(
                        "twilio_webhooks_configured"
                    ),
                },
            }

    # Cross-project move (or to/from unassigned): unbind (TurnCall rejects
    # binding an already-bound number, so this must precede the rebind), then
    # bind, and only delete the old mirror once the new bind succeeds — a bind
    # failure otherwise leaves the number cleared in Twilio AND gone from the
    # console (with its SID), unrecoverable.
    # The stored Twilio pair travels with the number: TurnCall never returns
    # the token, so a rebind can only re-send the mirror's copy.
    try:
        carried = await phones.twilio_account(pool, phone_id)
    except IntegrationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if existing["routing_type"] != "none":
        await _unbind_in_turncall(pool, existing, workspace_id)
    number = await _bind(
        pool, body, workspace_id, twilio=carried, source=existing.get("twilio_credentials")
    )
    if number["id"] != phone_id:
        await phones.delete_number(pool, phone_id)
    return {"success": True, "data": {**number, "rebound": True}}


@router.delete("/phone-numbers/{phone_id}")
async def delete_phone_number(
    phone_id: str, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Unbind: TurnCall removes the binding AND clears the Twilio number's
    webhooks, so the number stops pointing at TurnCall entirely."""
    pool = runtime.get_pool()
    existing = await phones.get_number(pool, phone_id, ctx.workspace_id)
    if not existing:
        raise HTTPException(status_code=404, detail="phone number not found")
    if existing["routing_type"] != "none":
        await _unbind_in_turncall(pool, existing, ctx.workspace_id)
    await phones.delete_number(pool, phone_id)
    return {"success": True, "data": {"unbound": phone_id}}
