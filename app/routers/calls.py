"""Per-agent call console endpoints (proxy the agent's TurnCall project)."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from .. import runtime
from .._helpers import _backend_or_404, ranged_audio
from ..deps import AuthContext, require_editor, require_member

router = APIRouter()


class TestChat(BaseModel):
    message: str
    session_id: str | None = None
    customer_number: str = "+15550000000"


@router.post("/agents/{agent_id}/chat")
async def agent_test_chat(
    agent_id: str, body: TestChat, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Talk to the agent over the text Chat API — the in-console test surface.
    A test call, so editor+ only; a viewer is read-only (#31)."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        data = await runtime.get_client().send_chat(
            b["agent_id"],
            body.message,
            b["api_key"],
            session_id=body.session_id,
            customer_number=body.customer_number,
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail="chat failed") from exc
    return {"success": True, "data": data}


@router.get("/agents/{agent_id}/calls")
async def agent_calls(
    agent_id: str,
    page: int = 1,
    limit: int = 50,
    ctx: AuthContext = Depends(require_member),
) -> dict[str, Any]:
    """The agent's Call records — its project IS the agent, so no filter needed."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    return await runtime.get_client().list_calls(b["api_key"], page, limit)


@router.get("/agents/{agent_id}/calls/{call_id}/transcript")
async def agent_call_transcript(
    agent_id: str, call_id: str, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    data = await runtime.get_client().get_call_transcript(call_id, b["api_key"])
    return {"success": True, "data": data}


@router.get("/agents/{agent_id}/calls/{call_id}/events")
async def agent_call_events(
    agent_id: str,
    call_id: str,
    event_type: str | None = None,
    ctx: AuthContext = Depends(require_member),
) -> dict[str, Any]:
    """A call's events; the console asks for `metrics.turn` (per-turn latency)."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        data = await runtime.get_client().get_call_events(
            call_id, b["api_key"], event_type=event_type
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code, detail="events not available"
        ) from exc
    return {"success": True, "data": data}


@router.get("/agents/{agent_id}/calls/{call_id}/tool-invocations")
async def agent_call_tool_invocations(
    agent_id: str, call_id: str, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    """The tools a call ran — the console threads them into the transcript."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        data = await runtime.get_client().get_call_tool_invocations(
            call_id, b["api_key"]
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code, detail="tool calls not available"
        ) from exc
    return {"success": True, "data": data}


@router.get("/agents/{agent_id}/calls/{call_id}/recording")
async def agent_call_recording(
    agent_id: str, call_id: str, request: Request, ctx: AuthContext = Depends(require_member)
) -> Response:
    """The call's WAV, streamed through so the browser never needs the key.

    Honors Range requests (206) — Safari refuses to play <audio> from
    endpoints that only ever answer 200 with the full body."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        audio = await runtime.get_client().get_call_recording(call_id, b["api_key"])
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code, detail="recording not available"
        ) from exc
    return ranged_audio(audio, request, "audio/wav")
