"""Per-agent eval endpoints (proxy the agent's TurnCall project).

TurnCall owns evals entirely — scenarios, runs, batches, mocks, verdicts and
the worker that executes them (turncall#68). Nothing eval-shaped is stored
here. These routes exist to put a workspace boundary and a session cookie in
front of the agent's API key, exactly as `calls.py` does for call records.

One thing this router does that the older ones do not: it passes **TurnCall's
own error text** through. A scenario rejected by pipecat's parser comes back
naming the offending field, and replacing that with "scenario failed" would
make the Console strictly worse than curl at the one moment an author needs
help.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import runtime
from .._helpers import _backend_or_404
from ..deps import AuthContext, require_editor, require_member

router = APIRouter()


# Pydantic prefixes a raised ValueError with this; it is noise to a reader who
# only wants to know which field is wrong.
_VALUE_ERROR_PREFIX = "Value error, "


def _validation_messages(body: dict[str, Any]) -> str | None:
    """The field-level messages out of TurnCall's validation envelope.

    Its errors come in two shapes, and only one of them puts the useful text
    where you would first look:

        business   {"error": "no scenarios carry the tag 'pre-publsh'", ...}
        validation {"error": "Validation error",
                    "details": {"errors": [{"msg": "Value error, ...", ...}]}}

    The second is the one a scenario author actually hits — a definition
    pipecat rejects — and reading only `error` there reports "Validation
    error", which is exactly as useless as the "scenario failed" this function
    exists to avoid. Verified against a live TurnCall; a unit test alone agreed
    with the wrong assumption.
    """
    errors = (body.get("details") or {}).get("errors")
    if not isinstance(errors, list):
        return None
    messages = []
    for err in errors:
        msg = err.get("msg") if isinstance(err, dict) else None
        if msg:
            messages.append(str(msg).removeprefix(_VALUE_ERROR_PREFIX))
    return "; ".join(messages) or None


def _turncall_error(exc: httpx.HTTPStatusError, fallback: str) -> HTTPException:
    """TurnCall's message, not ours.

    The message names the offending field — "'turns:' must be a list", "no
    scenarios carry the tag 'pre-publsh'" — and that is the whole value of the
    response to whoever is writing the scenario, so it reaches the browser
    intact.
    """
    detail = fallback
    try:
        body = exc.response.json()
        detail = (
            _validation_messages(body)
            or body.get("error")
            or body.get("detail")
            or fallback
        )
    except Exception:
        pass
    return HTTPException(status_code=exc.response.status_code, detail=detail)


class ScenarioBody(BaseModel):
    """Passed through as-is: TurnCall validates the definition by round-tripping
    it through pipecat's parser, and duplicating that here would be a second
    schema to keep in sync with a library we do not own."""

    model_config = {"extra": "allow"}

    name: str | None = None
    definition: dict[str, Any] | None = None


class RunBody(BaseModel):
    """Exactly one of `scenario_id` and `tag`, as TurnCall requires. Not
    re-validated here — its error says so better than a duplicated rule
    would."""

    model_config = {"extra": "allow"}

    scenario_id: str | None = None
    tag: str | None = None
    modality: str = "text"
    iterations: int = Field(default=1, ge=1)


class FromSessionBody(BaseModel):
    session_id: str
    name: str | None = None
    # Same default as the call path, for the same reason: a derived scenario
    # asserts whatever the agent did in that conversation.
    save: bool = False
    tags: list[str] = Field(default_factory=list)


class FromCallBody(BaseModel):
    call_id: str
    name: str | None = None
    # A derived scenario asserts whatever the agent did on that call, mistakes
    # included. It is shown for review before anything is stored, so the
    # default stays False here as it is in TurnCall.
    save: bool = False
    tags: list[str] = Field(default_factory=list)


@router.get("/agents/{agent_id}/eval-scenarios")
async def list_eval_scenarios(
    agent_id: str, tag: str | None = None, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    """The agent's saved scenarios. Readable by any member, viewers included."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    return await runtime.get_client().list_eval_scenarios(b["api_key"], tag=tag)


@router.post("/agents/{agent_id}/eval-scenarios", status_code=201)
async def create_eval_scenario(
    agent_id: str, body: ScenarioBody, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        data = await runtime.get_client().create_eval_scenario(
            b["api_key"], body.model_dump(exclude_none=True)
        )
    except httpx.HTTPStatusError as exc:
        raise _turncall_error(exc, "could not create the scenario") from exc
    return {"success": True, "data": data}


@router.put("/agents/{agent_id}/eval-scenarios/{scenario_id}")
async def update_eval_scenario(
    agent_id: str,
    scenario_id: str,
    body: ScenarioBody,
    ctx: AuthContext = Depends(require_editor),
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        data = await runtime.get_client().update_eval_scenario(
            # `exclude_unset`, not `exclude_none`: a PUT forwards what the
            # browser sent, and for `judge`/`simulator` an explicit null is a
            # real instruction — "back to the platform default" (turncall#119).
            # Dropping it would leave the Console offering a choice that
            # silently did nothing.
            scenario_id,
            b["api_key"],
            body.model_dump(exclude_unset=True),
        )
    except httpx.HTTPStatusError as exc:
        raise _turncall_error(exc, "could not update the scenario") from exc
    return {"success": True, "data": data}


@router.delete("/agents/{agent_id}/eval-scenarios/{scenario_id}")
async def delete_eval_scenario(
    agent_id: str, scenario_id: str, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        await runtime.get_client().delete_eval_scenario(scenario_id, b["api_key"])
    except httpx.HTTPStatusError as exc:
        raise _turncall_error(exc, "could not delete the scenario") from exc
    return {"success": True, "data": {"deleted": True}}


@router.post("/agents/{agent_id}/eval-runs", status_code=202)
async def create_eval_run(
    agent_id: str, body: RunBody, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Queue a run, or a batch of them when the body carries a `tag`.

    202, not 200: a separate worker executes these (TurnCall's ADR-0004 keeps
    eval load out of the process serving live calls), so the response is a
    batch id and the runs it created, never a verdict. Editor+ — starting a run
    spends model time.

    The target is the agent whose page this is. The Console never asks which
    agent to run against, because the answer is always the one you are looking
    at.
    """
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    payload = body.model_dump(exclude_none=True)
    payload["target"] = {"type": "agent", "agent_id": b["agent_id"]}
    try:
        data = await runtime.get_client().create_eval_run(b["api_key"], payload)
    except httpx.HTTPStatusError as exc:
        raise _turncall_error(exc, "could not queue the run") from exc
    return {"success": True, "data": data}


@router.get("/agents/{agent_id}/eval-runs")
async def list_eval_runs(
    agent_id: str,
    status: str | None = None,
    scenario_id: str | None = None,
    batch_id: str | None = None,
    ctx: AuthContext = Depends(require_member),
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    return await runtime.get_client().list_eval_runs(
        b["api_key"], status=status, scenario_id=scenario_id, batch_id=batch_id
    )


@router.get("/agents/{agent_id}/eval-runs/batches/{batch_id}")
async def get_eval_batch(
    agent_id: str, batch_id: str, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    """One verdict for a batch, without every run's transcripts.

    Declared before `/{run_id}` so a literal path segment is not read as a run
    id — FastAPI matches in declaration order.
    """
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        data = await runtime.get_client().get_eval_batch(batch_id, b["api_key"])
    except httpx.HTTPStatusError as exc:
        raise _turncall_error(exc, "batch not found") from exc
    return {"success": True, "data": data}


@router.get("/agents/{agent_id}/eval-runs/{run_id}")
async def get_eval_run(
    agent_id: str, run_id: str, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        data = await runtime.get_client().get_eval_run(run_id, b["api_key"])
    except httpx.HTTPStatusError as exc:
        raise _turncall_error(exc, "run not found") from exc
    return {"success": True, "data": data}


@router.post("/agents/{agent_id}/eval-scenarios/from-call")
async def scenario_from_call(
    agent_id: str, body: FromCallBody, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Derive a scenario draft from one of this agent's finished calls.

    Editor+ even with `save: false`: it reads a call's transcript and its tool
    results, which is more than a viewer's read of the call list.
    """
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        data = await runtime.get_client().scenario_from_call(
            b["api_key"], body.model_dump()
        )
    except httpx.HTTPStatusError as exc:
        # "the call has no caller speech", "convert it once it has ended" —
        # TurnCall's refusals say what to do about them.
        raise _turncall_error(exc, "could not derive a scenario from that call") from exc
    return {"success": True, "data": data}


@router.post("/agents/{agent_id}/eval-scenarios/from-session")
async def scenario_from_session(
    agent_id: str, body: FromSessionBody, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Derive a scenario draft from one of this agent's text conversations.

    The chat sibling of `from-call`. Editor+ for the same reason: it reads the
    conversation and the results its tools returned.
    """
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        data = await runtime.get_client().scenario_from_session(
            b["api_key"], body.model_dump()
        )
    except httpx.HTTPStatusError as exc:
        raise _turncall_error(
            exc, "could not derive a scenario from that conversation"
        ) from exc
    return {"success": True, "data": data}
