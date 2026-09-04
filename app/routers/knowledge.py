"""Per-agent knowledge-base console endpoints (proxy TurnCall KB API)."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel

from .. import runtime
from .._helpers import _backend_or_404, _turncall_http_error
from ..deps import AuthContext, require_editor, require_member

router = APIRouter()

KNOWLEDGE_KB_NAME = "builder-knowledge"


async def _agent_kb(b: dict[str, Any], create: bool = False) -> dict[str, Any] | None:
    """Resolve the agent's implicit Agent Knowledge KB (ADR-0009). The agent's
    project holds at most this one KB; created + linked on first upload."""
    client = runtime.get_client()
    kbs = await client.list_knowledge_bases(b["api_key"])
    for kb in kbs:
        if kb["name"] == KNOWLEDGE_KB_NAME:
            return kb
    if not create:
        return None
    kb = await client.create_knowledge_base(KNOWLEDGE_KB_NAME, b["api_key"])
    # prompt mode: builder KBs are small (menus, FAQs), so inject the full text
    # into the agent's prompt — always present, never missed by a retrieval query.
    await client.link_knowledge_base(b["agent_id"], kb["id"], b["api_key"], mode="prompt")
    return kb


@router.get("/agents/{agent_id}/knowledge/documents")
async def list_agent_documents(
    agent_id: str, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    kb = await _agent_kb(b)
    docs = await runtime.get_client().list_documents(kb["id"], b["api_key"]) if kb else []
    return {"success": True, "data": {"documents": docs}}


@router.post("/agents/{agent_id}/knowledge/documents")
async def upload_agent_document(
    agent_id: str, file: UploadFile, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    kb = await _agent_kb(b, create=True)
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
    kb = await _agent_kb(b)
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
    kb = await _agent_kb(b)
    if not kb:
        return {"success": True, "data": {"results": []}}
    data = await runtime.get_client().search_knowledge_base(kb["id"], body.query, b["api_key"])
    return {"success": True, "data": data}
