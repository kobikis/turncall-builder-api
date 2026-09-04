"""Phone-number binding endpoints + routing resolution."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import runtime
from ..backends import generator, phones, registry
from ..deps import AuthContext, require_editor, require_member

router = APIRouter()


class PhoneBind(BaseModel):
    e164: str
    sid: str
    # 'agent' | 'agent_call_init' (managed call-init, ADR-0008) | 'webhook' | 'none'
    routing_type: str
    agent_id: str | None = None
    server_url: str | None = None
    sms_enabled: bool = False


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
        wp = await phones.ensure_webhook_project(pool, runtime.get_client())
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
    the container so /call-init verifies against it (managed call-init only)."""
    if backend and secret:
        generator.set_env_value(backend["service_dir"], "CALL_INIT_SECRET", secret)
        await generator.restart(backend["service_dir"])


async def _bind(pool: Any, body: PhoneBind, workspace_id: str) -> dict[str, Any]:
    """Bind a number in the right project and mirror it. Returns the mirror row.
    An unassigned number ('none') is only mirrored locally — TurnCall's bind needs
    a routing target, so it isn't bound there until assigned an agent/webhook."""
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
            "workspace_id": workspace_id,
        }
        await phones.record_number(pool, number)
        return number

    routing = await _resolve_routing(pool, body, workspace_id)
    payload: dict[str, Any] = {
        "external_number_sid": body.sid,
        "e164_number": body.e164,
        "sms_enabled": body.sms_enabled,
        **routing["fields"],
    }
    data = await runtime.get_client().bind_phone_number(payload, routing["api_key"])
    secret = data.get("server_url_secret")
    if body.routing_type == "agent_call_init":
        await _sync_call_init_secret(routing["backend"], secret)
    number = {
        "id": data["id"],
        "e164": body.e164,
        "sid": body.sid,
        "routing_type": body.routing_type,
        "agent_id": routing["agent_id"],
        "project_id": routing["project_id"],
        "server_url": routing["server_url"],
        # per-number call-init signing secret (webhook + agent_call_init routing)
        "server_url_secret": secret,
        "sms_enabled": body.sms_enabled,
        "workspace_id": workspace_id,
    }
    await phones.record_number(pool, number)
    # Response-only (not mirrored): whether TurnCall actually pointed the
    # Twilio number at itself — False means calls won't arrive yet.
    number["twilio_webhooks_configured"] = data.get("twilio_webhooks_configured")
    return number


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
            data = await runtime.get_client().update_phone_number(
                phone_id,
                {"sms_enabled": body.sms_enabled, **routing["fields"]},
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
                "workspace_id": workspace_id,
            }
            await phones.record_number(pool, number)
            return {
                "success": True,
                "data": {
                    **number,
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
    if existing["routing_type"] != "none":
        await _unbind_in_turncall(pool, existing, workspace_id)
    number = await _bind(pool, body, workspace_id)
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
