"""Per-agent knowledge-base console endpoints (proxy TurnCall KB API)."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel

from .. import runtime
from .._helpers import _agent_kb, _backend_or_404, _turncall_http_error
from ..deps import AuthContext, require_editor, require_member

router = APIRouter()


@router.get("/agents/{agent_id}/knowledge/documents")
async def list_agent_documents(
    agent_id: str, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    kb = await _agent_kb(b["agent_id"], b["api_key"])
    docs = await runtime.get_client().list_documents(kb["id"], b["api_key"]) if kb else []
    return {"success": True, "data": {"documents": docs}}


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
