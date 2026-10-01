"""The Workspace's Twilio account — a card on the Integrations page.

Connected once by an admin, then chosen on the add-number form instead of
pasting a token per number. TurnCall still holds Twilio credentials per number,
so this is not projected into agent projects: a bind sends this account's pair,
and changing it here re-sends the new pair to every number bound with it.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from .. import runtime, twilio_api, twilio_store
from ..backends import phones
from ..deps import AuthContext, require_admin, require_editor, require_member
from ..integration_store import IntegrationError
from .phones import PhoneBind, _resolve_routing

router = APIRouter(prefix="/integrations/twilio")

_AC = re.compile(r"^AC[0-9a-fA-F]{32}$")


class TwilioBody(BaseModel):
    account_sid: str
    auth_token: str

    @field_validator("account_sid")
    @classmethod
    def _sid(cls, v: str) -> str:
        v = v.strip()
        if not _AC.match(v):
            raise ValueError("account_sid should look like AC followed by 32 hex characters")
        return v

    @field_validator("auth_token")
    @classmethod
    def _token(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("auth_token must not be blank")
        return v


def _unavailable(exc: IntegrationError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


@router.get("")
async def status(ctx: AuthContext = Depends(require_member)) -> dict[str, Any]:
    """Presence, the account SID and a last-four — never the token."""
    pool = runtime.get_pool()
    account = await twilio_store.get(pool, ctx.workspace_id)
    numbers = await phones.on_workspace_twilio(pool, ctx.workspace_id)
    return {
        "success": True,
        "data": {
            "connected": account is not None,
            **(account or {}),
            "numbers": [{"id": n["id"], "e164": n["e164"]} for n in numbers],
        },
    }


@router.put("")
async def connect(body: TwilioBody, ctx: AuthContext = Depends(require_admin)) -> dict[str, Any]:
    """Validate against the account's own number list, store, then re-send the
    pair to every number bound with the Workspace's account (a rotation)."""
    checked = twilio_api.checked_url(body.account_sid)
    try:
        await twilio_api.list_numbers(body.account_sid, body.auth_token, page_size=1)
        validation = {"status": "valid", "checked": checked, "detail": "accepted by Twilio's number list"}
    except twilio_api.TwilioRejected as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Twilio rejected this Account SID / Auth Token ({exc}; checked {checked}). It was not saved.",
        ) from exc
    except httpx.HTTPError as exc:
        validation = {"status": "unverified", "checked": checked, "detail": f"unreachable: {type(exc).__name__}"}

    pool = runtime.get_pool()
    try:
        saved = await twilio_store.save(
            pool, ctx.workspace_id,
            account_sid=body.account_sid, auth_token=body.auth_token, validation=validation,
        )
    except IntegrationError as exc:
        raise _unavailable(exc) from exc
    numbers = await _resend(pool, ctx.workspace_id, (body.account_sid, body.auth_token))
    return {"success": True, "data": {"connected": True, **saved, "numbers": numbers}}


async def _resend(pool: Any, workspace_id: str, pair: tuple[str, str]) -> list[dict[str, Any]]:
    """Each number bound with the Workspace's account gets the current pair.

    Per number, recorded in the response: TurnCall re-checks that the account
    owns the number, so switching the card to a different account fails here
    — visibly, per number — for numbers that account does not hold.
    """
    client = runtime.get_client()
    results = []
    for number in await phones.on_workspace_twilio(pool, workspace_id):
        if number["routing_type"] == "none":
            results.append({"id": number["id"], "e164": number["e164"], "state": "unassigned", "error": None})
            continue
        state, error = "updated", None
        try:
            bind = PhoneBind(
                e164=number["e164"], sid=number["sid"], routing_type=number["routing_type"],
                agent_id=number.get("agent_id"), server_url=number.get("server_url"),
                sms_enabled=number["sms_enabled"],
            )
            routing = await _resolve_routing(pool, bind, workspace_id)
            await client.update_phone_number(
                number["id"],
                {
                    "sms_enabled": number["sms_enabled"],
                    **routing["fields"],
                    "twilio_account_sid": pair[0],
                    "twilio_auth_token": pair[1],
                },
                routing["api_key"],
            )
            await phones.record_number(pool, {**number, "workspace_id": workspace_id, "twilio_account_sid": pair[0]})
        except Exception as exc:  # noqa: BLE001 — reported per number, retry by saving again
            state = "failed"
            error = _why(exc)
        results.append({"id": number["id"], "e164": number["e164"], "state": state, "error": error})
    return results


def _why(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            return str(exc.response.json().get("error") or exc.response.text)[:300]
        except ValueError:
            return exc.response.text[:300]
    if isinstance(exc, HTTPException):
        return str(exc.detail)[:300]
    return f"{type(exc).__name__}: {exc}"[:300]


@router.delete("")
async def disconnect(ctx: AuthContext = Depends(require_admin)) -> dict[str, Any]:
    """Refused while numbers are bound with it: TurnCall would keep running
    them on a pair this page no longer shows, and a rotation could no longer
    reach them. Move those numbers to other credentials first."""
    pool = runtime.get_pool()
    using = await phones.on_workspace_twilio(pool, ctx.workspace_id)
    if using:
        names = ", ".join(n["e164"] for n in using)
        raise HTTPException(
            status_code=409,
            detail=f"{len(using)} number(s) use this Twilio account ({names}) — "
            "move them to other credentials or delete them first",
        )
    await twilio_store.delete(pool, ctx.workspace_id)
    return {"success": True, "data": {"connected": False}}


@router.get("/numbers")
async def numbers(ctx: AuthContext = Depends(require_editor)) -> dict[str, Any]:
    """The connected account's numbers, for the add-number picker. Each says
    whether this Workspace already has it bound."""
    pool = runtime.get_pool()
    try:
        creds = await twilio_store.credentials(pool, ctx.workspace_id)
    except IntegrationError as exc:
        raise _unavailable(exc) from exc
    if creds is None:
        raise HTTPException(status_code=404, detail="No Twilio account is connected")
    try:
        listed = await twilio_api.list_numbers(*creds)
    except twilio_api.TwilioRejected as exc:
        raise HTTPException(status_code=400, detail=f"Twilio rejected the saved credentials ({exc})") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Twilio is unreachable") from exc
    bound = {n["e164"] for n in await phones.list_numbers(pool, ctx.workspace_id)}
    return {"success": True, "data": [{**n, "bound": n["phone_number"] in bound} for n in listed]}
