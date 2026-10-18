"""Phone-number binding endpoints + routing resolution, and buying and
releasing numbers on the Workspace's carrier accounts (#108)."""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from .. import compliance, integration_store, provider_integrations, runtime, twilio_store
from .._helpers import _turncall_http_error
from ..audit import Audit, Change, diff, masked
from ..integration_store import BOX, IntegrationError
from ..provider_integrations import TELNYX
from ..backends import generator, phones, registry
from ..deps import AuthContext, require_admin, require_editor, require_member

router = APIRouter()

TWILIO = "twilio"
Carrier = Literal["twilio", "telnyx", "asterisk"]
_CARRIER_NAMES = {TWILIO: "Twilio", TELNYX: "Telnyx"}


class PhoneBind(BaseModel):
    e164: str
    # Who the number belongs to (turncall ADR-0022). Fixed at bind: null on an
    # edit keeps the number's own; null on a bind is Twilio.
    carrier: Carrier | None = None
    # The number's Twilio SID. Optional when the number brings its own Twilio
    # account, and on Telnyx: TurnCall looks it up by E.164. An Asterisk
    # number has none.
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


def _check_carrier(body: PhoneBind, carrier: str) -> None:
    """Refuse Twilio's features on another carrier with a sentence, before
    TurnCall would: a number's own Twilio account and SMS are Twilio-only."""
    if carrier == TWILIO:
        return
    twilio_only = [
        name
        for name, used in (
            ("SMS", body.sms_enabled),
            ("the workspace's Twilio account", body.use_workspace_twilio),
            ("a Twilio account", body.own_twilio() is not None),
        )
        if used
    ]
    if twilio_only:
        raise HTTPException(
            status_code=422,
            detail=f"{twilio_only[0]} applies to Twilio numbers only, not {carrier}.",
        )


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


async def _workspace_project(pool: Any, workspace_id: str) -> tuple[dict[str, str], bool]:
    """The Workspace's webhook project, created on first use with the
    Workspace's keys and compliance settings in it. True when it was created
    (and its keys written) here."""
    client = runtime.get_client()
    wp, created = await phones.ensure_workspace_webhook_project(pool, client, workspace_id)
    if created:
        # Its calls run on the Workspace's keys, like any agent's (#65), and
        # under its compliance settings (#103).
        await provider_integrations.project_into_new_target(
            pool, client, workspace_id, phones.as_projection_target(workspace_id, wp)
        )
        await compliance.project_into_new_target(
            pool, client, workspace_id, compliance.webhook_target(workspace_id, wp)
        )
    return wp, created


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
        wp, _ = await _workspace_project(pool, workspace_id)
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
        await generator.restart(backend["service_dir"], backend["port"])


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
        # TurnCall's sentence, not its JSON (#73). A number the carrier
        # account cannot see (422) or no carrier account at all (503) is the
        # user's to fix, so it keeps its status; anything else is upstream.
        status = exc.response.status_code
        reason = str(_turncall_http_error(exc).detail)[:300]
        raise HTTPException(
            status_code=status if status in (422, 503) else 502,
            detail=f"TurnCall rejected the bind: {reason}",
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
    # A bought number's purchase state (#108): "pending" until its Telnyx
    # order lands, else null. Always present, so the console can rely on it.
    out.setdefault("purchase_status", None)
    out.setdefault("telnyx_order_id", None)
    if out["twilio_credentials"] == "platform":
        out["twilio_account_sid"] = None
    return out


def _pair(twilio: tuple[str, str] | None) -> dict[str, str]:
    """A Twilio account as TurnCall's bodies take it: the pair, or nothing."""
    return {"twilio_account_sid": twilio[0], "twilio_auth_token": twilio[1]} if twilio else {}


def _not_connected(carrier: str) -> HTTPException:
    name = _CARRIER_NAMES[carrier]
    return HTTPException(
        status_code=400,
        detail=f"This workspace has no {name} account connected — connect it on Integrations",
    )


async def _workspace_twilio(pool: Any, workspace_id: str) -> tuple[str, str]:
    """The Workspace Twilio account's pair; refused when it has none."""
    try:
        creds = await twilio_store.credentials(pool, workspace_id)
    except IntegrationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if creds is None:
        raise _not_connected(TWILIO)
    return creds


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
        return await _workspace_twilio(pool, workspace_id), "workspace"
    if body.own_twilio():
        return body.own_twilio(), "number"
    return carried, carried_source if carried else None


async def _bind(
    pool: Any,
    body: PhoneBind,
    workspace_id: str,
    twilio: tuple[str, str] | None = None,
    source: str | None = None,
    carrier: str | None = None,
    purchase: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind a number in the right project and mirror it. Returns the mirror row.
    An unassigned number ('none') is only mirrored locally — TurnCall's bind needs
    a routing target, so it isn't bound there until assigned an agent/webhook.

    `twilio` is the number's own Twilio account: what the body brings, else what
    a rebind carries over from the mirror. `purchase` is a bought number's
    purchase state, kept while it stays unassigned; a bind ends it."""
    carrier = carrier or body.carrier or TWILIO
    _check_carrier(body, carrier)
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
            "carrier": carrier,
            "workspace_id": workspace_id,
            **(purchase or {}),
            **_seal(twilio, source),
        }
        await phones.record_number(pool, number)
        return _public(number)

    routing = await _resolve_routing(pool, body, workspace_id)
    payload: dict[str, Any] = {
        "carrier": carrier,
        "e164_number": body.e164,
        "sms_enabled": body.sms_enabled,
        **({"external_number_sid": body.sid} if body.sid else {}),
        **_pair(twilio),
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
        "carrier": carrier,
        "workspace_id": workspace_id,
        **_seal(twilio, source),
    }
    await phones.record_number(pool, number)
    # Response-only (not mirrored): whether TurnCall actually pointed the
    # number at itself — False means calls won't arrive yet. The twilio_ one
    # is null off Twilio; webhooks_configured is every carrier's (ADR-0022).
    number["twilio_webhooks_configured"] = data.get("twilio_webhooks_configured")
    number["webhooks_configured"] = data.get("webhooks_configured")
    return _public(number)


# --- search, buy and release (#108, turncall ADR-0025) -----------------------
#
# Admin-only: buying spends the Workspace's money and releasing is
# irreversible. They run only on the Workspace's own carrier accounts — its
# Twilio account (twilio_store), sent as the pair, and its Telnyx Integration,
# projected into the TurnCall project as the `telnyx` credential — and never
# fall through to the platform's. They are TurnCall calls made with the
# Workspace's webhook project key, which, like every key the builder mints,
# has TurnCall's `admin` role (TurnCallClient.create_api_key).
#
# Searching uses that project too, so the first search creates it (once per
# Workspace, with its keys written in): on Telnyx it must, since TurnCall
# reads the Telnyx account from the key's project; on Twilio it keeps a
# Workspace's searches (and any limits TurnCall counts per project) on its
# own project, not the builder's shared one. A search writes the Telnyx key
# only while the projection log says the project lacks it, so searching again
# writes nothing. Buying and releasing write it every time (#109).
#
# Every refusal's `detail` is a sentence: the console shows it as it is. So the
# bodies and query take plain strings and are checked here, not by a schema
# that would answer with FastAPI's array.

PURCHASE_CARRIERS = (TWILIO, TELNYX)
_E164 = re.compile(r"\+[1-9][0-9]{1,14}")
# Past tense and gerund of what a buy or release does, for its sentences.
_BOUGHT, _RELEASED = ("bought", "buying"), ("released", "releasing")


class PhonePurchase(BaseModel):
    carrier: str | None = None
    e164: str | None = None


class PhoneRelease(BaseModel):
    # The number being released, repeated. Checked in the handler.
    confirm: str | None = None


def _purchase_carrier(carrier: str | None) -> str:
    if carrier in PURCHASE_CARRIERS:
        return carrier  # type: ignore[return-value]
    if carrier == "asterisk":
        raise HTTPException(
            status_code=422,
            detail="Asterisk numbers belong to the PBX's dialplan; "
            "numbers are bought and released on Twilio or Telnyx only.",
        )
    raise HTTPException(
        status_code=422,
        detail="Choose a carrier to buy on: twilio or telnyx."
        if not carrier
        else f"{carrier} is not a carrier numbers can be bought on; choose twilio or telnyx.",
    )


async def _telnyx_integration(pool: Any, workspace_id: str) -> dict[str, Any] | None:
    """The Workspace's Telnyx Integration, unless it has none or is
    disconnecting it."""
    telnyx = await integration_store.get(pool, workspace_id, TELNYX)
    return telnyx if telnyx and not telnyx["disconnecting"] else None


async def _carrier_project(
    pool: Any,
    workspace_id: str,
    carrier: str,
    doing: str,
    *,
    telnyx: dict[str, Any] | None = None,
    rewrite: bool = False,
) -> dict[str, str]:
    """The Workspace's webhook project, ready to act on `carrier`: on Telnyx,
    holding the Workspace's current `telnyx` credential. Without it TurnCall
    would use the platform's Telnyx account, so a copy that cannot be written
    refuses the call. A Workspace with no Telnyx Integration is refused before
    anything reaches TurnCall; `telnyx` is the row when the caller has read it.

    `rewrite` writes the copy even when the log says the project holds it, as
    buying and releasing must; a project created here was just written."""
    if carrier == TELNYX:
        telnyx = telnyx or await _telnyx_integration(pool, workspace_id)
        if telnyx is None:
            raise _not_connected(TELNYX)
    wp, created = await _workspace_project(pool, workspace_id)
    if carrier != TELNYX:
        return wp
    try:
        error = await provider_integrations.ensure_in_target(
            pool, runtime.get_client(), workspace_id, telnyx,
            phones.as_projection_target(workspace_id, wp),
            rewrite=rewrite and not created,
        )
    except IntegrationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if error:
        raise HTTPException(
            status_code=502,
            detail=f"The workspace's Telnyx account could not be given to TurnCall, "
            f"so nothing was {doing}: {error}",
        )
    return wp


def _turncall_refused(exc: httpx.HTTPStatusError, doing: str) -> HTTPException:
    """TurnCall's refusal with its own status and sentence (403 shared
    account, 409 bound, 422, 502 carrier, 503 no account)."""
    err = _turncall_http_error(exc)
    # TurnCall refusing the builder's own key is not the console's session
    # expiring: that is what a 401 from here would say.
    return HTTPException(
        status_code=502 if err.status_code == 401 else err.status_code,
        detail=f"TurnCall refused to {doing}: {err.detail}",
        headers=err.headers,
    )


def _body(exc: httpx.HTTPStatusError) -> dict[str, Any] | None:
    try:
        body = exc.response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _done_anyway(exc: httpx.HTTPStatusError, done: str) -> dict[str, Any] | None:
    """What the carrier did, when TurnCall answers 500 "the carrier <done> it,
    but TurnCall could not record it": the number, from `details.<done>`.
    None for any other failure."""
    if exc.response.status_code != 500:
        return None
    details = (_body(exc) or {}).get("details")
    number = details.get(done) if isinstance(details, dict) else None
    return number if isinstance(number, dict) and number.get("e164_number") else None


def _may_have_happened(exc: httpx.HTTPStatusError) -> str | None:
    """Why a failed buy or release may have reached the carrier anyway, or
    None for a refusal that says nothing happened: any other 500 (TurnCall
    failed partway), or a gateway's 502/503/504 in front of TurnCall, which is
    not TurnCall's own answer (that one says what the carrier did)."""
    status = exc.response.status_code
    if status == 500:
        return "TurnCall failed"
    if status in (502, 503, 504) and "error" not in (_body(exc) or {}):
        return "TurnCall did not answer"
    return None


def _uncertain(why: str, e164: str, carrier: str, verb: tuple[str, str]) -> HTTPException:
    done, doing = verb
    return HTTPException(
        status_code=502,
        detail=f"{why}, so {e164} may have been {done}: check the "
        f"{_CARRIER_NAMES[carrier]} account before {doing} it again",
    )


def _unfinished_order(number: dict[str, Any], exc: HTTPException) -> HTTPException | None:
    """TurnCall refusing to bind a pending number because it is not on the
    Telnyx account yet: the order has not finished, so say that."""
    if number.get("purchase_status") != "pending" or exc.status_code != 422:
        return None
    if "is not a phone number on" not in str(exc.detail):
        return None
    return HTTPException(
        status_code=422,
        detail=f"Telnyx hasn't finished the order for {number['e164']}{_order(number)} "
        "yet, so it can't be bound: try again in a few minutes.",
    )


def _order(number: dict[str, Any]) -> str:
    order = number.get("telnyx_order_id")
    return f" (order {order})" if order else ""


def _bought_number(
    workspace_id: str,
    carrier: str,
    e164: str,
    twilio: tuple[str, str] | None,
    bought: dict[str, Any],
) -> dict[str, Any]:
    """The registry row for a number the carrier sold: unassigned, on the
    Workspace's account, and pending while its Telnyx order has not finished."""
    return {
        "id": str(uuid.uuid4()),  # local id — no TurnCall binding yet
        "e164": bought.get("e164_number") or e164,
        # Not null: the column holds "" for a number without one (an Asterisk
        # number, a Telnyx one bound by E.164), and so for a pending order.
        "sid": bought.get("external_number_sid") or "",
        "routing_type": "none",
        "agent_id": None,
        "project_id": None,
        "server_url": None,
        "sms_enabled": False,
        "label": None,
        "carrier": carrier,
        "workspace_id": workspace_id,
        "purchase_status": "pending" if bought.get("status") == "pending" else None,
        "telnyx_order_id": bought.get("telnyx_order_id"),
        **_seal(twilio, "workspace"),
    }


@router.get("/phone-numbers/available")
async def search_available_numbers(
    carrier: str | None = None,
    country: str | None = None,
    number_type: str | None = None,
    area_code: str | None = None,
    contains: str | None = None,
    limit: str | None = None,
    ctx: AuthContext = Depends(require_admin),
) -> dict[str, Any]:
    """Numbers the Workspace can buy on one of its carrier accounts, and the
    carriers it can buy on. Without `carrier`, only the carriers: nothing is
    searched. Filters are TurnCall's, and checked there."""
    pool = runtime.get_pool()
    telnyx = await _telnyx_integration(pool, ctx.workspace_id)
    carriers = [
        c for c, connected in (
            (TWILIO, await twilio_store.get(pool, ctx.workspace_id) is not None),
            (TELNYX, telnyx is not None),
        )
        if connected
    ]
    if carrier is None:
        return {"success": True, "data": {"carriers": carriers, "numbers": []}}
    carrier = _purchase_carrier(carrier)
    twilio = await _workspace_twilio(pool, ctx.workspace_id) if carrier == TWILIO else None
    if carrier == TELNYX and telnyx is None:
        raise _not_connected(TELNYX)
    wp = await _carrier_project(pool, ctx.workspace_id, carrier, "searched", telnyx=telnyx)
    params = {
        "carrier": carrier, "country": country, "number_type": number_type,
        "area_code": area_code, "contains": contains, "limit": limit,
    }
    try:
        numbers = await runtime.get_client().search_available_numbers(
            params, wp["api_key"], twilio
        )
    except httpx.HTTPStatusError as exc:
        raise _turncall_refused(exc, "search for numbers") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail="TurnCall is unreachable — retry in a moment"
        ) from exc
    return {"success": True, "data": {"carriers": carriers, "numbers": numbers}}


@router.post("/phone-numbers/purchase")
async def purchase_phone_number(
    body: PhonePurchase, audit: Audit, ctx: AuthContext = Depends(require_admin)
) -> dict[str, Any]:
    """Buy a number on the Workspace's carrier account and record it in the
    Number registry, unassigned: bind it with `PUT /phone-numbers/{id}`. A
    Telnyx order TurnCall could not see finish is `purchase_status: pending`,
    with no SID; it binds once Telnyx has put it on the account.

    Where it is unknown whether the carrier sold the number (TurnCall failed
    partway, or did not answer), it says so and records nothing: the number
    may be on the account. Where TurnCall says the carrier sold it but it
    could not record that, the number is recorded here all the same, with a
    `warning`."""
    pool = runtime.get_pool()
    workspace_id = ctx.workspace_id
    carrier = _purchase_carrier(body.carrier)
    e164 = (body.e164 or "").strip()
    if not _E164.fullmatch(e164):
        raise HTTPException(
            status_code=422,
            detail="Choose a number to buy, in E.164 form (like +13125550100).",
        )
    twilio = await _workspace_twilio(pool, workspace_id) if carrier == TWILIO else None
    wp = await _carrier_project(pool, workspace_id, carrier, "bought", rewrite=True)

    # Nothing has landed until TurnCall says the carrier sold it. Where that
    # is unknown, it is marked landed so the entry is written (partial, with
    # the "may have been bought" reason): the money may be spent.
    change = audit.begin(
        ctx, "phone_number", e164, "purchased", landed=False, resource_name=e164,
        after={"e164": e164, "carrier": carrier},
    )
    name = _CARRIER_NAMES[carrier]
    warning = None
    try:
        bought = await runtime.get_client().purchase_phone_number(
            {"carrier": carrier, "e164_number": e164, **_pair(twilio)}, wp["api_key"]
        )
    except httpx.HTTPStatusError as exc:
        bought = _done_anyway(exc, "bought")
        if bought is None:
            if why := _may_have_happened(exc):
                change.landed()
                raise _uncertain(why, e164, carrier, _BOUGHT) from exc
            raise _turncall_refused(exc, f"buy {e164}") from exc
        warning = (
            f"{name} sold {bought['e164_number']} to this workspace and it is on Phone "
            "Numbers, but TurnCall could not record the purchase in its own log."
        )
    except httpx.HTTPError as exc:
        change.landed()
        raise _uncertain("TurnCall did not answer", e164, carrier, _BOUGHT) from exc
    change.landed()
    status = bought.get("status") or "active"
    number = _bought_number(workspace_id, carrier, e164, twilio, bought)
    try:
        await phones.record_number(pool, number)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"{name} sold {number['e164']} to this workspace, but "
            "the builder could not record it: add it on Phone Numbers to bind it",
        ) from exc
    await change.record(**_entry(None, number, diff(None, {"status": status})))
    return {
        "success": True,
        "data": {**_public(number), "status": status, "warning": warning},
    }


@router.post("/phone-numbers/{phone_id}/release")
async def release_phone_number(
    phone_id: str, body: PhoneRelease, audit: Audit, ctx: AuthContext = Depends(require_admin)
) -> dict[str, Any]:
    """Give an unassigned number back to its carrier, and drop it from the
    Number registry. **Irreversible**: the carrier may sell it to someone
    else. `confirm` must repeat the number. A pending Telnyx purchase is not
    on the account yet, so it is refused until the order lands.

    Not `DELETE /phone-numbers/{id}`: that only removes the registry row (and
    unbinds a routed number), leaving the number on the carrier account, still
    billed. Release is the one that ends the number's billing.

    On Twilio it runs on the number's own stored account (the Workspace's, for
    a number bought or bound with it), else the Workspace's; on Telnyx, the
    Workspace's Telnyx Integration. Never the platform's: with neither, it is
    refused."""
    pool = runtime.get_pool()
    workspace_id = ctx.workspace_id
    existing = await phones.get_number(pool, phone_id, workspace_id)
    if not existing:
        raise HTTPException(status_code=404, detail="phone number not found")
    e164 = existing["e164"]
    if existing["routing_type"] != "none":
        raise HTTPException(
            status_code=409,
            detail=f"{e164} is still routed. Set it to unassigned before releasing it.",
        )
    if existing.get("purchase_status") == "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Telnyx hasn't finished the order for {e164}{_order(existing)} yet; "
            "it can be released once the order lands.",
        )
    if (body.confirm or "").strip() != e164:
        raise HTTPException(
            status_code=422,
            detail=f"Releasing is irreversible: confirm by typing the number being "
            f"released ({e164}).",
        )
    carrier = existing.get("carrier") or TWILIO
    if carrier not in PURCHASE_CARRIERS:
        raise HTTPException(
            status_code=422,
            detail=f"{e164} is on {carrier}; only Twilio and Telnyx numbers can be released.",
        )
    twilio = None
    if carrier == TWILIO:
        try:
            twilio = await phones.twilio_account(pool, phone_id)
        except IntegrationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # No stored pair: the number was recorded without its account. The
        # Workspace's is the only other one it can be released on; TurnCall
        # refuses (422) a number that account does not hold.
        twilio = twilio or await _workspace_twilio(pool, workspace_id)
    wp = await _carrier_project(pool, workspace_id, carrier, "released", rewrite=True)

    change = _begin(audit, ctx, "released", existing, None)
    warning = None
    try:
        released = await runtime.get_client().release_phone_number(
            {"carrier": carrier, "e164_number": e164, "confirm": e164, **_pair(twilio)},
            wp["api_key"],
        )
    except httpx.HTTPStatusError as exc:
        released = _done_anyway(exc, "released")
        if released is None:
            if why := _may_have_happened(exc):
                change.landed()
                raise _uncertain(why, e164, carrier, _RELEASED) from exc
            raise _turncall_refused(exc, f"release {e164}") from exc
        warning = (
            f"{_CARRIER_NAMES[carrier]} took {e164} back and it is gone from Phone "
            "Numbers, but TurnCall could not record the release in its own log."
        )
    except httpx.HTTPError as exc:
        change.landed()
        raise _uncertain("TurnCall did not answer", e164, carrier, _RELEASED) from exc
    change.landed()
    await phones.delete_number(pool, phone_id)
    await change.record()
    return {
        "success": True,
        "data": {
            "released": e164,
            "carrier": carrier,
            "external_number_sid": released.get("external_number_sid"),
            "warning": warning,
        },
    }


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


# What an audit entry compares for a number: its binding as the console sees it.
# `server_url_secret` is masked by name; the number's own Twilio token is never
# in the mirror's public view, so a new one is noted separately (_token_change).
_AUDITED_FIELDS = (
    "id", "e164", "carrier", "sid", "routing_type", "agent_id", "project_id",
    "server_url", "server_url_secret", "sms_enabled", "label",
    "twilio_credentials", "twilio_account_sid", "purchase_status",
)


def _audited(number: dict[str, Any] | None) -> dict[str, Any] | None:
    if number is None:
        return None
    public = _public(number)
    return {k: public.get(k) for k in _AUDITED_FIELDS}


def _token_change(body: PhoneBind, existing: dict[str, Any] | None) -> dict[str, Any]:
    """A Twilio auth token entered on the form is a credential change: say it
    changed, never what it is."""
    if body.own_twilio() is None:
        return {}
    had = existing is not None and _public(existing)["twilio_credentials"] == "number"
    return {"/twilio_auth_token": masked(had, True)}


def _entry(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """An entry's fields for a number going from `before` to `after`."""
    number = after or before or {}
    return {
        "resource_id": number["id"],
        "resource_name": number.get("e164"),
        # A re-pointed number is about the agent it left and the one it reached.
        "agent_ids": [(before or {}).get("agent_id"), (after or {}).get("agent_id")],
        "changes": {**diff(_audited(before), _audited(after)), **(extra or {})},
    }


def _begin(
    audit: Audit,
    ctx: AuthContext,
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    extra: dict[str, Any] | None = None,
    *,
    landed: bool = False,
) -> Change:
    """Begin a number's change as it would read if it stopped at `after`.
    Not landed until TurnCall accepts its first write: a refused bind or
    unbind changed nothing."""
    fields = _entry(before, after, extra)
    return audit.begin(
        ctx, "phone_number", fields.pop("resource_id"), action, landed=landed, **fields
    )


def _unbound(number: dict[str, Any]) -> dict[str, Any]:
    """A number as it stands once unbound in TurnCall and not yet bound again."""
    return {
        **number, "routing_type": "none", "agent_id": None, "project_id": None,
        "server_url": None, "server_url_secret": None,
    }


@router.post("/phone-numbers")
async def add_phone_number(
    body: PhoneBind, audit: Audit, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    # TurnCall's bind is the first write; a failure after it (the mirror)
    # still leaves the binding, and so an entry.
    change = audit.begin(
        ctx, "phone_number", body.e164, "created", landed=False,
        resource_name=body.e164, agent_ids=[body.agent_id],
        after={"e164": body.e164, "routing_type": body.routing_type, "agent_id": body.agent_id},
    )
    number = await _bind(runtime.get_pool(), body, ctx.workspace_id)
    await change.record(**_entry(None, number, _token_change(body, None)))
    return {"success": True, "data": number}


@router.put("/phone-numbers/{phone_id}")
async def update_phone_number(
    phone_id: str, body: PhoneBind, audit: Audit, ctx: AuthContext = Depends(require_editor)
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
    carrier = existing.get("carrier") or TWILIO
    if body.carrier and body.carrier != carrier:
        raise HTTPException(
            status_code=422,
            detail=f"This number is on {carrier}; a number's carrier is fixed when it is bound.",
        )
    _check_carrier(body, carrier)

    if body.routing_type != "none" and existing["routing_type"] != "none":
        routing = await _resolve_routing(pool, body, workspace_id)
        if routing["project_id"] == existing["project_id"]:
            new_twilio, new_source = await _twilio_for(pool, body, workspace_id, None, None)
            requested = {
                **existing, "routing_type": body.routing_type, "agent_id": routing["agent_id"],
                "server_url": routing["server_url"], "sms_enabled": body.sms_enabled,
                "label": body.label,
            }
            change = _begin(audit, ctx, "updated", existing, requested, _token_change(body, existing))
            data = await runtime.get_client().update_phone_number(
                phone_id,
                {
                    "sms_enabled": body.sms_enabled,
                    **routing["fields"],
                    # Only a newly entered pair is sent; omitted keeps TurnCall's.
                    **_pair(new_twilio),
                },
                routing["api_key"],
            )
            change.landed()
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
                "carrier": carrier,
                "workspace_id": workspace_id,
                **_seal(new_twilio, new_source),
            }
            await phones.record_number(pool, number)
            stored = await phones.get_number(pool, phone_id, workspace_id) or number
            await change.record(**_entry(existing, stored, _token_change(body, existing)))
            return {
                "success": True,
                "data": {
                    **_public(stored),
                    "rebound": False,
                    "twilio_webhooks_configured": data.get(
                        "twilio_webhooks_configured"
                    ),
                    "webhooks_configured": data.get("webhooks_configured"),
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
    # Recorded as it stands after each step: a bind that fails once the
    # unbind went through leaves the number unbound, and the entry says so.
    change = _begin(audit, ctx, "updated", existing, _unbound(existing))
    if existing["routing_type"] != "none":
        await _unbind_in_turncall(pool, existing, workspace_id)
        change.landed()
    try:
        number = await _bind(
            pool,
            body,
            workspace_id,
            twilio=carried,
            source=existing.get("twilio_credentials"),
            carrier=carrier,
            purchase={k: existing.get(k) for k in ("purchase_status", "telnyx_order_id")},
        )
    except HTTPException as exc:
        unfinished = _unfinished_order(existing, exc)
        if unfinished is None:
            raise
        raise unfinished from exc
    if number["id"] != phone_id:
        await phones.delete_number(pool, phone_id)
    await change.record(**_entry(existing, number, _token_change(body, existing)))
    return {"success": True, "data": {**number, "rebound": True}}


@router.delete("/phone-numbers/{phone_id}")
async def delete_phone_number(
    phone_id: str, audit: Audit, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Unbind: TurnCall removes the binding AND clears the Twilio number's
    webhooks, so the number stops pointing at TurnCall entirely. An
    unassigned number is only dropped from the registry. Neither gives the
    number back to its carrier: it stays on the account, billed, until
    released (`POST /phone-numbers/{id}/release`)."""
    pool = runtime.get_pool()
    existing = await phones.get_number(pool, phone_id, ctx.workspace_id)
    if not existing:
        raise HTTPException(status_code=404, detail="phone number not found")
    change = _begin(audit, ctx, "deleted", existing, None)
    if existing["routing_type"] != "none":
        await _unbind_in_turncall(pool, existing, ctx.workspace_id)
        change.landed()
    await phones.delete_number(pool, phone_id)
    change.landed()
    await change.record()
    return {"success": True, "data": {"unbound": phone_id}}
