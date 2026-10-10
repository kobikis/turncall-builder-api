"""Per-agent knowledge console endpoints (proxy TurnCall's KB API).

Each document lives in a knowledge base of its own, so each has its own
retrieval mode: TurnCall sets the mode on an agent's link to a knowledge
base, not on a document (ADR-0024).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .. import runtime
from .._helpers import (
    _agent_kbs,
    _backend_or_404,
    _drop_kb,
    _turncall_http_error,
    _upload_document,
)
from ..deps import AuthContext, require_editor, require_member

logger = logging.getLogger(__name__)
router = APIRouter()

Mode = Literal["prompt", "auto", "tool"]

# Said to the model when it may look a document up itself (`tool` mode) and
# the console gave no description of its own.
DEFAULT_TOOL_DESCRIPTION = (
    "Look up facts in this business's documents — menu, prices, hours, "
    "policies — before answering a question about them."
)

# Results shown in the console's search box, across every document.
SEARCH_RESULTS = 10


def _retrieval_of(link: dict[str, Any] | None) -> dict[str, Any] | None:
    if link is None:
        return None
    return {k: link.get(k) for k in ("mode", "top_k", "tool_description")}


async def _documents(agent_id: str, api_key: str) -> list[dict[str, Any]]:
    """Every document the agent answers from, each with how it is read: its
    knowledge base's link to the agent."""
    client = runtime.get_client()
    links = {
        link.get("knowledge_base_id"): link
        for link in await client.list_agent_knowledge_bases(agent_id, api_key)
    }
    out: list[dict[str, Any]] = []
    for kb in await _agent_kbs(api_key):
        for doc in await client.list_documents(kb["id"], api_key):
            out.append({**doc, "retrieval": _retrieval_of(links.get(kb["id"]))})
    return out


async def _document(
    agent_id: str, api_key: str, doc_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(the document, every document) or 404."""
    docs = await _documents(agent_id, api_key)
    doc = next((d for d in docs if d.get("id") == doc_id), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    return doc, docs


@router.get("/agents/{agent_id}/knowledge/documents")
async def list_agent_documents(
    agent_id: str, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    return {
        "success": True,
        "data": {"documents": await _documents(b["agent_id"], b["api_key"])},
    }


@router.post("/agents/{agent_id}/knowledge/documents")
async def upload_agent_document(
    agent_id: str,
    file: UploadFile,
    mode: Mode = Form("prompt"),
    ctx: AuthContext = Depends(require_editor),
) -> dict[str, Any]:
    """Add a document, read in `mode` — whole in every reply unless chosen
    otherwise, which is what every document got before modes existed."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        doc = await _upload_document(
            b["agent_id"],
            b["api_key"],
            file.filename or "document",
            await file.read(),
            file.content_type or "application/octet-stream",
            mode=mode,
            tool_description=DEFAULT_TOOL_DESCRIPTION if mode == "tool" else None,
        )
    except httpx.HTTPStatusError as exc:
        raise _turncall_http_error(exc) from exc
    return {"success": True, "data": doc}


@router.delete("/agents/{agent_id}/knowledge/documents/{doc_id}")
async def delete_agent_document(
    agent_id: str, doc_id: str, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Delete a document, and its knowledge base once nothing else is in it."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    doc, docs = await _document(b["agent_id"], b["api_key"], doc_id)
    kb_id = doc["knowledge_base_id"]
    await runtime.get_client().delete_document(kb_id, doc_id, b["api_key"])
    if not any(d["knowledge_base_id"] == kb_id and d["id"] != doc_id for d in docs):
        await _drop_kb(b["agent_id"], kb_id, b["api_key"])
    return {"success": True, "data": {"deleted": doc_id}}


class Retrieval(BaseModel):
    """How the agent reads one document (TurnCall's link modes): `prompt`
    puts it whole in every reply, `auto` searches it each turn and adds the
    best passages, `tool` lets the model search it when it decides to."""

    mode: Mode
    top_k: int = Field(default=5, ge=1, le=50)
    tool_description: str | None = Field(default=None, max_length=1024)


@router.put("/agents/{agent_id}/knowledge/documents/{doc_id}/retrieval")
async def set_document_retrieval(
    agent_id: str,
    doc_id: str,
    body: Retrieval,
    ctx: AuthContext = Depends(require_editor),
) -> dict[str, Any]:
    """Change how the agent reads one document. TurnCall has no update for a
    link, so this unlinks and links again; if the new link is refused, the
    old one is put back rather than leave the document unread. A knowledge
    base made before ADR-0024 may hold several documents, which then change
    together."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    doc, _ = await _document(b["agent_id"], b["api_key"], doc_id)
    kb_id, current = doc["knowledge_base_id"], doc["retrieval"]
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
    client = runtime.get_client()
    try:
        if current is not None:
            await client.unlink_knowledge_base(b["agent_id"], kb_id, b["api_key"])
        try:
            await client.link_knowledge_base(
                b["agent_id"],
                kb_id,
                b["api_key"],
                wanted["mode"],
                top_k=wanted["top_k"],
                tool_description=wanted["tool_description"],
            )
        except httpx.HTTPError:
            if current is not None:
                await client.link_knowledge_base(
                    b["agent_id"],
                    kb_id,
                    b["api_key"],
                    current["mode"],
                    top_k=current.get("top_k"),
                    tool_description=current.get("tool_description"),
                )
            raise
    except httpx.HTTPStatusError as exc:
        raise _turncall_http_error(exc) from exc
    except httpx.HTTPError as exc:
        logger.warning(
            "retrieval change failed agent=%s doc=%s: %s", agent_id, doc_id, exc
        )
        raise HTTPException(status_code=502, detail="TurnCall is unreachable") from exc
    return {"success": True, "data": wanted}


class KnowledgeSearch(BaseModel):
    query: str


@router.post("/agents/{agent_id}/knowledge/search")
async def search_agent_knowledge(
    agent_id: str, body: KnowledgeSearch, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    """Test what the agent retrieves for a query, across every document
    (the console's search box). Read-only, so a viewer may run it (#31)."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    client = runtime.get_client()
    results: list[dict[str, Any]] = []
    for kb in await _agent_kbs(b["api_key"]):
        found = await client.search_knowledge_base(kb["id"], body.query, b["api_key"])
        results.extend(found.get("results", []) if isinstance(found, dict) else [])
    results.sort(key=lambda r: r.get("similarity") or 0, reverse=True)
    return {"success": True, "data": {"results": results[:SEARCH_RESULTS]}}
