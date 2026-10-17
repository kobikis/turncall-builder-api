"""GitHub connections and pushing an Agent Backend to a Linked repo (ADR-0013).

Connections are per-User: `current_user`, not a workspace role, gates them. What
an agent does with a connection is a workspace action, so linking and pushing
require `editor`.

Nothing here creates a repository. The user picks an owner, repo, branch and
optional path from what their own token can already see.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from .. import github, github_store, runtime
from ..audit import Audit
from ..backends import registry
from ..deps import AuthContext, require_editor
from .auth import current_user

router = APIRouter()


def _fail(exc: github.GitHubError, status: int = 400) -> HTTPException:
    """GitHub errors are written to be shown — they say what to do next."""
    return HTTPException(status_code=status, detail=str(exc))


class ConnectBody(BaseModel):
    token: str = Field(min_length=8, max_length=255)

    @field_validator("token")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


@router.get("/github/connection")
async def get_connection(user: dict = Depends(current_user)) -> dict[str, Any]:
    """This user's connection, or null. Never returns the token."""
    conn = await github_store.get_connection(runtime.get_pool(), str(user["id"]))
    return {"success": True, "data": conn}


@router.post("/github/connection")
async def connect(
    body: ConnectBody, user: dict = Depends(current_user)
) -> dict[str, Any]:
    """Store a fine-grained token after checking GitHub accepts it.

    Verified before storing rather than on first push: a connection that looks
    fine and fails later is the failure this codebase keeps repeating.
    """
    try:
        identity = await github.identify(body.token)
    except github.GitHubError as exc:
        raise _fail(exc) from exc

    conn = await github_store.save_connection(
        runtime.get_pool(),
        user_id=str(user["id"]),
        login=identity.login,
        token=body.token,
        expires_at=identity.expires_at,
    )
    return {"success": True, "data": conn}


@router.delete("/github/connection", status_code=204)
async def disconnect(user: dict = Depends(current_user)) -> None:
    """Forget the token. Linked repos survive — pushing stops, nothing is
    deleted on GitHub, and another user can take the links over."""
    await github_store.delete_connection(runtime.get_pool(), str(user["id"]))


@router.get("/github/repos")
async def list_repos(user: dict = Depends(current_user)) -> dict[str, Any]:
    """Repositories this token can push to. Read-only ones are filtered out."""
    pool = runtime.get_pool()
    conn = await github_store.get_connection(pool, str(user["id"]))
    if conn is None:
        raise HTTPException(status_code=404, detail="No GitHub connection.")
    try:
        token = await github_store.get_token(pool, conn["id"])
        return {"success": True, "data": await github.list_repos(token or "")}
    except github.GitHubError as exc:
        raise _fail(exc) from exc


@router.get("/github/repos/{owner}/{repo}/branches")
async def list_branches(
    owner: str, repo: str, user: dict = Depends(current_user)
) -> dict[str, Any]:
    pool = runtime.get_pool()
    conn = await github_store.get_connection(pool, str(user["id"]))
    if conn is None:
        raise HTTPException(status_code=404, detail="No GitHub connection.")
    try:
        token = await github_store.get_token(pool, conn["id"])
        return {"success": True, "data": await github.list_branches(token or "", owner, repo)}
    except github.GitHubError as exc:
        raise _fail(exc) from exc


class LinkBody(BaseModel):
    owner: str = Field(min_length=1, max_length=100)
    repo: str = Field(min_length=1, max_length=100)
    branch: str = Field(min_length=1, max_length=255)
    # Empty = repo root. A path is what lets one repo hold many agents.
    path: str = Field(default="", max_length=255)

    @field_validator("path")
    @classmethod
    def _clean_path(cls, v: str) -> str:
        v = v.strip().strip("/")
        if ".." in v.split("/"):
            raise ValueError("path must not contain '..'")
        return v


@router.get("/agents/{agent_id}/github")
async def get_link(agent_id: str, ctx: AuthContext = Depends(require_editor)) -> dict:
    link = await github_store.get_link(runtime.get_pool(), agent_id)
    return {"success": True, "data": link}


@router.put("/agents/{agent_id}/github")
async def link(
    agent_id: str,
    body: LinkBody,
    audit: Audit,
    ctx: AuthContext = Depends(require_editor),
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    """Point this agent's backend at a repo the user already owns."""
    pool = runtime.get_pool()
    if await registry.get_backend(pool, agent_id) is None:
        raise HTTPException(status_code=404, detail="No backend for this agent.")

    conn = await github_store.get_connection(pool, str(user["id"]))
    if conn is None:
        raise HTTPException(
            status_code=400, detail="Connect your GitHub account before linking a repo."
        )
    try:
        token = await github_store.get_token(pool, conn["id"])
        await github.assert_writable(token or "", body.owner, body.repo)
    except github.GitHubError as exc:
        raise _fail(exc) from exc

    before = await github_store.get_link(pool, agent_id)
    await github_store.set_link(
        pool,
        agent_id,
        owner=body.owner,
        repo=body.repo,
        branch=body.branch,
        path=body.path,
        connection_id=conn["id"],
    )
    after = await github_store.get_link(pool, agent_id)
    await audit.record(
        ctx, "linked_repo", agent_id, "linked",
        resource_name=f"{body.owner}/{body.repo}", agent_ids=[agent_id],
        before=_where(before), after=_where(after),
    )
    return {"success": True, "data": after}


def _where(link: dict[str, Any] | None) -> dict[str, Any] | None:
    """What an audit entry compares for a Linked repo: where it points and whose
    connection pushes, not the last push's outcome."""
    if not link:
        return None
    return {k: link.get(k) for k in ("owner", "repo", "branch", "path", "connection_id")}


@router.delete("/agents/{agent_id}/github", status_code=204)
async def unlink(
    agent_id: str, audit: Audit, ctx: AuthContext = Depends(require_editor)
) -> None:
    """Stop pushing. The repository on GitHub is left exactly as it is."""
    pool = runtime.get_pool()
    before = await github_store.get_link(pool, agent_id)
    await github_store.clear_link(pool, agent_id)
    await audit.record(
        ctx, "linked_repo", agent_id, "unlinked",
        resource_name=f"{before['owner']}/{before['repo']}" if before else None,
        agent_ids=[agent_id], before=_where(before), after=None,
    )


@router.post("/agents/{agent_id}/github/push")
async def push(
    agent_id: str, audit: Audit, ctx: AuthContext = Depends(require_editor)
) -> dict:
    """Push this agent's current files to its Linked repo.

    409 on divergence: the remote changed since we last wrote it, so nothing is
    pushed and the user reconciles it in their own repository.
    """
    pool = runtime.get_pool()
    backend = await registry.get_backend(pool, agent_id)
    if backend is None:
        raise HTTPException(status_code=404, detail="No backend for this agent.")

    link = await github_store.get_link(pool, agent_id)
    if link is None:
        raise HTTPException(status_code=400, detail="This agent has no linked repo.")
    if link["connection_id"] is None:
        raise HTTPException(
            status_code=409,
            detail="The GitHub connection that owns this repo is gone. Connect "
            "your own GitHub and relink to take it over.",
        )

    try:
        token = await github_store.get_token(pool, link["connection_id"])
        result = await github.push_backend(
            service_dir=backend["service_dir"],
            token=token or "",
            owner=link["owner"],
            repo=link["repo"],
            branch=link["branch"],
            path=link["path"],
            last_tree_hash=await github_store.get_tree_hash(pool, agent_id),
            message=f"turncall: sync {backend['slug']}",
            # Kept current on every agent edit, not only when tools change — so
            # a prompt or model change shows up here even though it regenerates
            # nothing.
            agent_config=backend.get("config"),
        )
    except github.DivergedError as exc:
        await github_store.record_push(pool, agent_id, tree_hash=None, error=str(exc))
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except github.GitHubError as exc:
        await github_store.record_push(pool, agent_id, tree_hash=None, error=str(exc))
        raise _fail(exc) from exc

    await github_store.record_push(pool, agent_id, tree_hash=result.tree_hash, error=None)
    await audit.record(
        ctx, "linked_repo", agent_id, "pushed",
        resource_name=f"{link['owner']}/{link['repo']}", agent_ids=[agent_id],
        changes={"/commit": {"before": None, "after": result.commit_sha}},
    )
    return {
        "success": True,
        "data": {"commit": result.commit_sha, **(await github_store.get_link(pool, agent_id))},
    }
