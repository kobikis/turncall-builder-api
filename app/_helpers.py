"""Helpers shared across router modules (no routes, no `app` reference)."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from . import runtime
from .backends import registry


class _ToolShape(BaseModel):
    """A custom tool must at least have a string name — the builder indexes
    tools by name in _normalize_config / the mapper. Extra fields pass through."""

    model_config = ConfigDict(extra="allow")
    name: str


class _AgentConfigShape(BaseModel):
    """Validates only the structural invariants the BUILDER's own code relies on
    (tool names, object-vs-scalar shapes). Everything else passes through to
    TurnCall, which is the authority on the full agent schema — so this stays
    permissive (extra='allow') and never duplicates that schema."""

    model_config = ConfigDict(extra="allow")
    name: str | None = None
    system_prompt: str | None = None
    first_message: str | None = None
    server_url: str | None = None
    llm: dict[str, Any] | None = None
    tts: dict[str, Any] | None = None
    stt: dict[str, Any] | None = None
    analysis: dict[str, Any] | None = None
    custom_tools: list[_ToolShape] | None = None


def _validate_agent_config(config: dict[str, Any]) -> None:
    """Reject a structurally-broken config with a clean 422 before it reaches
    the fragile helpers (_normalize_config, the mapper), which would otherwise
    raise a raw 500 deep in the stack. Does NOT reshape — the original dict is
    what gets forwarded to TurnCall."""
    try:
        _AgentConfigShape.model_validate(config)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid agent config: {exc}") from exc


def _turncall_http_error(exc: httpx.HTTPStatusError) -> HTTPException:
    """Translate a TurnCall HTTPStatusError into an HTTPException carrying its
    message, so every proxy endpoint surfaces TurnCall failures identically."""
    try:
        detail = exc.response.json().get("error") or exc.response.text
    except Exception:
        detail = exc.response.text
    return HTTPException(status_code=exc.response.status_code, detail=detail)


def _normalize_config(
    config: dict[str, Any], port: int | None = None
) -> dict[str, Any]:
    """Return a NEW config with routing fields visible AND concrete for the
    console's JSON editor: empty fields are filled with the URL actually in
    effect (the generated backend's, once a port exists). Externality is decided
    by where a URL resolves, not by whether the field is set (ADR-0010). Pure —
    never mutates the caller's config."""
    backend_base = registry.backend_url(port) if port else None
    server_url = config.get("server_url") or backend_base
    base = (server_url or "").rstrip("/")
    out = {**config, "server_url": server_url}
    if config.get("custom_tools") is not None:
        out["custom_tools"] = [
            tool
            if tool.get("server_url")
            else {
                **tool,
                "server_url": f"{base}/tools/{tool['name']}" if base else None,
            }
            for tool in config["custom_tools"]
        ]
    # Only the active pipeline's services apply — drop the inert block so the
    # console JSON shows just what's in effect (TurnCall re-defaults either way).
    unused = ("stt", "llm", "tts") if config.get("pipeline_mode") == "s2s" else ("s2s",)
    return {k: v for k, v in out.items() if k not in unused}


def _backend_view(port: int, status: str) -> dict[str, Any]:
    # browser_url is what the UI polls (host), backend_url is container-to-container.
    return {"port": port, "browser_url": f"http://localhost:{port}", "status": status}


async def _backend_or_404(
    agent_id: str, workspace_id: str | None = None
) -> dict[str, Any]:
    """The agent's backend, scoped to its Workspace (#31). An agent in another
    Workspace reads as 404 — the caller can't tell it apart from a missing one."""
    b = await registry.get_backend(runtime.get_pool(), agent_id, workspace_id)
    if not b:
        raise HTTPException(status_code=404, detail="agent not found")
    return b
