"""Per-agent knowledge-base console endpoints (proxy TurnCall KB API)."""

from __future__ import annotations

import logging
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .. import runtime
from .._helpers import _agent_kb, _backend_or_404, _turncall_http_error
from ..deps import AuthContext, require_editor, require_member

logger = logging.getLogger(__name__)
router = APIRouter()

# Said to the model when it may look things up itself (`tool` mode) and the
# console gave no description of its own.
DEFAULT_TOOL_DESCRIPTION = (
    "Look up facts in this business's documents — menu, prices, hours, "
    "policies — before answering a question about them."
)


async def _retrieval(agent_id: str, kb_id: str, api_key: str) -> dict[str, Any] | None:
    """How the agent reads its knowledge: the TurnCall link's mode and
    settings. The builder's call-init sends the same link (#69)."""
    links = await runtime.get_client().list_agent_knowledge_bases(agent_id, api_key)
    link = next((x for x in links if x.get("knowledge_base_id") == kb_id), None)
    if link is None:
        return None
    return {k: link.get(k) for k in ("mode", "top_k", "tool_description")}


@router.get("/agents/{agent_id}/knowledge/documents")
async def list_agent_documents(
    agent_id: str, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    kb = await _agent_kb(b["agent_id"], b["api_key"])
    if not kb:
        return {"success": True, "data": {"documents": [], "retrieval": None}}
    client = runtime.get_client()
    docs = await client.list_documents(kb["id"], b["api_key"])
    retrieval = await _retrieval(b["agent_id"], kb["id"], b["api_key"])
    return {"success": True, "data": {"documents": docs, "retrieval": retrieval}}


class Retrieval(BaseModel):
    """How the agent reads its documents (TurnCall's link modes):
    `prompt` puts all of them in every turn's prompt, `auto` searches them
    each turn and adds the best passages, `tool` lets the model search when
    it decides to."""

    mode: Literal["prompt", "auto", "tool"]
    top_k: int = Field(default=5, ge=1, le=50)
    tool_description: str | None = Field(default=None, max_length=1024)


@router.put("/agents/{agent_id}/knowledge/retrieval")
async def set_agent_retrieval(
    agent_id: str, body: Retrieval, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Change how the agent reads its knowledge. TurnCall has no update for a
    link, so this unlinks and links again; if the new link is refused, the
    old one is put back rather than leave the agent with no knowledge."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    kb = await _agent_kb(b["agent_id"], b["api_key"])
    if not kb:
        raise HTTPException(status_code=409, detail="Upload a document first.")
    client = runtime.get_client()
    current = await _retrieval(b["agent_id"], kb["id"], b["api_key"])
    wanted = {
        "mode": body.mode,
        "top_k": body.top_k,
        "tool_description": (
            (body.tool_description or DEFAULT_TOOL_DESCRIPTION)
            if body.mode == "tool"
            else None
        ),
    }
    if current is not None and all(current.get(k) == v for k, v in wanted.items()):
        return {"success": True, "data": current}
    try:
        if current is not None:
            await client.unlink_knowledge_base(b["agent_id"], kb["id"], b["api_key"])
        try:
            await client.link_knowledge_base(
                b["agent_id"],
                kb["id"],
                b["api_key"],
                wanted["mode"],
                top_k=wanted["top_k"],
                tool_description=wanted["tool_description"],
            )
        except httpx.HTTPError:
            if current is not None:
                await client.link_knowledge_base(
                    b["agent_id"],
                    kb["id"],
                    b["api_key"],
                    current["mode"],
                    top_k=current.get("top_k"),
                    tool_description=current.get("tool_description"),
                )
            raise
    except httpx.HTTPStatusError as exc:
        raise _turncall_http_error(exc) from exc
    except httpx.HTTPError as exc:
        logger.warning("knowledge retrieval change failed agent=%s: %s", agent_id, exc)
        raise HTTPException(status_code=502, detail="TurnCall is unreachable") from exc
    return {"success": True, "data": wanted}


@router.post("/agents/{agent_id}/knowledge/documents")
async def upload_agent_document(
    agent_id: str, file: UploadFile, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    kb = await _agent_kb(b["agent_id"], b["api_key"], create=True)
    try:
        doc = await runtime.get_client().upload_document(
            kb["id"],  # type: ignore[index]
            file.filename or "document",
            await file.read(),
            file.content_type or "application/octet-stream",
            b["api_key"],
        )
    except httpx.HTTPStatusError as exc:
        raise _turncall_http_error(exc) from exc
    return {"success": True, "data": doc}


@router.delete("/agents/{agent_id}/knowledge/documents/{doc_id}")
async def delete_agent_document(
    agent_id: str, doc_id: str, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    kb = await _agent_kb(b["agent_id"], b["api_key"])
    if not kb:
        raise HTTPException(status_code=404, detail="no knowledge for this agent")
    await runtime.get_client().delete_document(kb["id"], doc_id, b["api_key"])
    return {"success": True, "data": {"deleted": doc_id}}


class KnowledgeSearch(BaseModel):
    query: str


@router.post("/agents/{agent_id}/knowledge/search")
async def search_agent_knowledge(
    agent_id: str, body: KnowledgeSearch, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    """Test what the agent retrieves for a query (Console's search box). Read-only
    (retrieves, changes nothing), so a viewer may run it (#31)."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    kb = await _agent_kb(b["agent_id"], b["api_key"])
    if not kb:
        return {"success": True, "data": {"results": []}}
    data = await runtime.get_client().search_knowledge_base(kb["id"], body.query, b["api_key"])
    return {"success": True, "data": data}
