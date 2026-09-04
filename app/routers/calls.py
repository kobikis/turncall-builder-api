"""Per-agent call console endpoints (proxy the agent's TurnCall project)."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from .. import runtime
from .._helpers import _backend_or_404
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
    total = len(audio)
    range_header = request.headers.get("range", "")
    if range_header.startswith("bytes="):
        start_s, _, end_s = range_header[len("bytes=") :].partition("-")
        try:
            start = int(start_s) if start_s else 0
            end = min(int(end_s), total - 1) if end_s else total - 1
        except ValueError:
            start, end = 0, total - 1
        if start > end or start >= total:
            return Response(
                status_code=416, headers={"Content-Range": f"bytes */{total}"}
            )
        return Response(
            content=audio[start : end + 1],
            status_code=206,
            media_type="audio/wav",
            headers={
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Accept-Ranges": "bytes",
            },
        )
    return Response(
        content=audio, media_type="audio/wav", headers={"Accept-Ranges": "bytes"}
    )
