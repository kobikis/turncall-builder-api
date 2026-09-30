"""Helpers shared across router modules (no routes, no `app` reference)."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, ValidationError

from . import runtime
from .backends import registry
from .pipedream import CATALOG


class _ToolShape(BaseModel):
    """A custom tool must at least have a string name — the builder indexes
    tools by name in _normalize_config / the mapper. Extra fields pass through."""

    model_config = ConfigDict(extra="allow")
    name: str


def _check_needs_integrations(value: Any) -> None:
    """`needs_integrations` is a request, not an attachment.

    Validated so a typo surfaces as a 422 instead of a console badge naming an
    app nobody can connect. It grants nothing: the mapper never forwards it, and
    only `integrations` puts a tool in front of a model.
    """
    if value is None:
        return
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("The apps an agent needs must be a list of app names.")
    unknown = [v for v in value if v not in CATALOG]
    if unknown:
        known = ", ".join(a.name for a in CATALOG.values())
        raise ValueError(
            f"{unknown[0]} is not an app this build can connect. Choose one of: {known}."
        )


def _check_custom_tools_do_not_shadow(config: dict[str, Any]) -> None:
    """Refuse a custom tool that does a Connected app's job.

    A generated handler holds no third-party credential, so one written for work
    an integration already does cannot succeed — and it competes with the real
    tool for the model's attention at call time, usually winning on name. The
    builder is told to use `needs_integrations` instead; this is what happens
    when it does it anyway.
    """
    for tool in config.get("custom_tools") or []:
        if not isinstance(tool, dict):
            continue
        text = f"{tool.get('name', '')} {tool.get('description', '')}".lower()
        for app in CATALOG.values():
            if app.name.lower() not in text:
                continue
            raise ValueError(
                f"The tool {tool.get('name')!r} says it does {app.name}'s work, but a "
                f"generated tool has no {app.name} credentials and cannot. Connect "
                f"{app.name} on the Integrations page and attach it to this agent "
                f"instead."
            )


def _check_integrations(value: Any) -> None:
    """Validate `config.integrations`, in sentences a person can act on.

    Deliberately hand-written rather than a pydantic shape. The field accepts a
    bare slug or an object, and expressing that as `list[str | Model]` made every
    mistake report twice — once for the branch that was never going to match —
    inside a message naming private classes and linking to pydantic's docs. What
    a Workspace needs to read is which app is wrong and what to do about it.
    """
    if value is None:
        return
    if not isinstance(value, list):
        raise ValueError("Connected apps must be a list.")

    for position, entry in enumerate(value, start=1):
        # A bare slug is the legacy shape and is left unchecked: validating it
        # now would refuse an edit to an agent that has been working, over a
        # field the edit did not touch.
        if isinstance(entry, str):
            continue
        if not isinstance(entry, dict):
            raise ValueError(f"Connected app #{position} is not an app.")

        slug = entry.get("app")
        app = CATALOG.get(slug) if isinstance(slug, str) else None
        if app is None:
            known = ", ".join(a.name for a in CATALOG.values())
            raise ValueError(
                f"{slug or f'Connected app #{position}'} is not an app this build "
                f"can connect. Choose one of: {known}."
            )

        raw = entry.get("call_ended")
        if not entry.get("mid_call") and raw is None:
            raise ValueError(
                f"{app.name} is attached but set to do nothing. Turn on "
                f'"available during calls", pick something for when a call ends, '
                f"or remove it."
            )
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"{app.name}: the end-of-call setting is malformed.")

        action = next((a for a in app.actions if a.id == raw.get("action")), None)
        if action is None:
            offered = ", ".join(a.label for a in app.actions)
            raise ValueError(
                f"{app.name} cannot do that when a call ends. "
                + (f"Choose one of: {offered}." if offered else "It offers nothing to run.")
            )
        # Unlike the rest, this one is not a typo a Workspace can see: the
        # Console fills it from the catalog, so an empty one means the form sent
        # something it should not have.
        if not str(raw.get("summary_arg") or "").strip():
            raise ValueError(
                f'{app.name}: "{action.label}" has nowhere to put the call summary.'
            )
        args = raw.get("args") or {}
        missing = [
            i.label
            for i in action.inputs
            if not str((args or {}).get(i.name) or "").strip()
        ]
        if missing:
            raise ValueError(
                f'"{action.label}" needs a {missing[0]} before it can run.'
                if len(missing) == 1
                else f'"{action.label}" still needs: {", ".join(missing)}.'
            )


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
    integrations: list[Any] | None = None


def _validate_agent_config(config: dict[str, Any]) -> None:
    """Reject a structurally-broken config with a clean 422 before it reaches
    the fragile helpers (_normalize_config, the mapper), which would otherwise
    raise a raw 500 deep in the stack. Does NOT reshape — the original dict is
    what gets forwarded to TurnCall."""
    try:
        _AgentConfigShape.model_validate(config)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_readable(exc)) from exc
    try:
        _check_integrations(config.get("integrations"))
        _check_needs_integrations(config.get("needs_integrations"))
        _check_custom_tools_do_not_shadow(config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _readable(exc: ValidationError) -> str:
    """A pydantic ValidationError as one line a person can act on.

    The default rendering names private model classes, prints the offending
    value back, and links to pydantic's documentation — three things a Workspace
    cannot use and should not have to read.
    """
    seen: dict[str, None] = {}
    for err in exc.errors():
        # Drop pydantic's internal path segments (union tags, `function-after[…]`)
        # and keep the field names a reader would recognise from the form.
        path = ".".join(
            str(part)
            for part in err["loc"]
            if not (isinstance(part, str) and ("[" in part or part in {"str", "dict"}))
        )
        seen[f"{path}: {err['msg']}" if path else err["msg"]] = None
    return "; ".join(seen) or "The configuration is not valid."


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


KNOWLEDGE_KB_NAME = "builder-knowledge"


async def _agent_kb(
    agent_id: str, api_key: str, create: bool = False
) -> dict[str, Any] | None:
    """Resolve the agent's implicit Agent Knowledge KB (ADR-0009). The agent's
    project holds at most this one KB; created + linked on first upload.

    Shared rather than private to the knowledge router because Create also has
    to reach it, to forward the documents staged on the Session.
    """
    client = runtime.get_client()
    for kb in await client.list_knowledge_bases(api_key):
        if kb["name"] == KNOWLEDGE_KB_NAME:
            return kb
    if not create:
        return None
    kb = await client.create_knowledge_base(KNOWLEDGE_KB_NAME, api_key)
    # prompt mode: builder KBs are small (menus, FAQs), so inject the full text
    # into the agent's prompt — always present, never missed by a retrieval query.
    await client.link_knowledge_base(agent_id, kb["id"], api_key, mode="prompt")
    return kb


def ranged_audio(audio: bytes, request: Request, media_type: str) -> Response:
    """Audio bytes as a response that honors Range requests (206).

    Safari refuses to play <audio> from endpoints that only ever answer 200
    with the full body."""
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
            media_type=media_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Accept-Ranges": "bytes",
            },
        )
    return Response(
        content=audio, media_type=media_type, headers={"Accept-Ranges": "bytes"}
    )
