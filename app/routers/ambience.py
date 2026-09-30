"""Per-agent ambience console endpoints (proxy TurnCall's ambience sounds).

Ambience (turncall ADR-0021) is a project's uploaded sound mixed quietly under
everything the agent says. One agent per project here, so the project's sounds
are the agent's sounds. Choosing one is not here: `ambience` is an ordinary
config field, saved by the Config tab's PUT /agents/{id} like any other, and the
mapper carries it to both the stored agent and call-init's inline agent.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import Response

from .. import runtime
from .._helpers import _backend_or_404, _turncall_http_error, ranged_audio
from ..deps import AuthContext, require_editor, require_member

router = APIRouter()

# What a phone caller hears: TurnCall's Twilio rate. The preview defaults to it
# because forced mono at 8 kHz is the change an uploader most needs to hear.
PHONE_RATE = 8000


@router.get("/agents/{agent_id}/ambience/sounds")
async def list_ambience_sounds(
    agent_id: str, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    """Every sound the agent could choose."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    sounds = await runtime.get_client().list_ambience_sounds(b["api_key"])
    return {"success": True, "data": {"sounds": sounds}}


@router.post("/agents/{agent_id}/ambience/sounds")
async def upload_ambience_sound(
    agent_id: str, file: UploadFile, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Upload a sound. TurnCall's 409 (too large/long) and 415 (unreadable, with
    the .m4a fix) come back verbatim, and so do its advisory `warnings`."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        sound = await runtime.get_client().upload_ambience_sound(
            file.filename or "ambience",
            await file.read(),
            file.content_type or "application/octet-stream",
            b["api_key"],
        )
    except httpx.HTTPStatusError as exc:
        raise _turncall_http_error(exc) from exc
    return {"success": True, "data": sound}


@router.delete("/agents/{agent_id}/ambience/sounds/{sound_id}")
async def delete_ambience_sound(
    agent_id: str, sound_id: str, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """TurnCall refuses (409, naming the agent) while the agent still uses it."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        await runtime.get_client().delete_ambience_sound(sound_id, b["api_key"])
    except httpx.HTTPStatusError as exc:
        raise _turncall_http_error(exc) from exc
    return {"success": True, "data": {"deleted": sound_id}}


@router.get("/agents/{agent_id}/ambience/sounds/{sound_id}/audio")
async def ambience_sound_audio(
    agent_id: str,
    sound_id: str,
    request: Request,
    rate: int = PHONE_RATE,
    ctx: AuthContext = Depends(require_member),
) -> Response:
    """A rendition's MP3, streamed through so the browser never needs the key."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        audio = await runtime.get_client().get_ambience_audio(
            sound_id, rate, b["api_key"]
        )
    except httpx.HTTPStatusError as exc:
        raise _turncall_http_error(exc) from exc
    return ranged_audio(audio, request, "audio/mpeg")
