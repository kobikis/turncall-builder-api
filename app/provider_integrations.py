"""A Workspace's provider accounts, projected into TurnCall (ADR-0023, #55-#58).

The Builder mints one TurnCall project per agent, so a Workspace credential is
one write per agent: a **projection**. Every write is recorded per agent, can be
re-run to completion after a partial failure, and skips agents that already hold
the current version — re-running a finished projection changes nothing.

Precedence is the engine's (turncall ADR-0020), never redefined here: an agent's
own inline key wins over the Workspace's, which wins over the platform's. An
agent carrying one is **pinned** — projection skips it and nothing moves its key
without the explicit adoption action.

A credential change never regenerates an Agent Backend: the regeneration
condition is tools and Automations, and a credential is neither.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import agent_update, integration_store, provider_catalog
from .backends import phones, registry

logger = logging.getLogger("turncall_builder")

AWS = "aws"
# Role providers billed to an account spelled differently; `ollama` and
# `custom_openai` have none — their key belongs to the endpoint they name.
_ACCOUNT = {"openai_live": "openai", "bedrock": AWS}
_NO_ACCOUNT = {"ollama", "custom_openai"}


def account_of(provider: str | None) -> str | None:
    if not provider or provider in _NO_ACCOUNT:
        return None
    return _ACCOUNT.get(provider, provider)


def pinned_account(config: dict[str, Any] | None) -> str | None:
    """The account an agent pins with its own inline key, if any.

    Only `llm.api_key` can pin: the Builder never emits AWS credentials into an
    agent config, and no other block carries a key.
    """
    llm = (config or {}).get("llm") or {}
    return account_of(llm.get("provider") or "openai") if llm.get("api_key") else None


def accounts_used(config: dict[str, Any] | None) -> set[str]:
    """Accounts this agent's calls run on, with TurnCall's defaults filled in."""
    config = config or {}
    if config.get("pipeline_mode") == "s2s":
        providers = [(config.get("s2s") or {}).get("provider") or "openai"]
    else:
        providers = [
            (config.get("stt") or {}).get("provider") or "deepgram",
            (config.get("llm") or {}).get("provider") or "openai",
            (config.get("tts") or {}).get("provider") or "deepgram",
        ]
    return {a for p in providers if (a := account_of(p))}


def last4(document: dict[str, Any]) -> str:
    """The one fragment ever shown: enough to tell two keys apart, not to use one."""
    secret = document.get("api_key") or document.get("access_key_id") or document.get("role_arn")
    return (secret or "")[-4:]


def _why(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            return str(exc.response.json().get("error") or exc.response.text)[:300]
        except ValueError:
            return exc.response.text[:300]
    return f"{type(exc).__name__}: {exc}"[:300]


async def project(pool: Any, client: Any, workspace_id: str, provider: str) -> list[dict[str, Any]]:
    """Write the Workspace's credential into every agent's TurnCall project.

    Returns per-agent state: `applied`, `pinned` (skipped — its own key wins),
    or `failed` with the reason. Safe to call again: agents already holding this
    version are not written to.
    """
    integration = await integration_store.get(pool, workspace_id, provider)
    if integration is None:
        return []
    version = integration["version"]
    document = await integration_store.get_document(pool, workspace_id, provider)
    done = {
        p["agent_id"]
        for p in await integration_store.list_projections(pool, workspace_id, provider)
        if p["state"] == "applied" and p["version"] == version
    }
    results = []
    for backend in await registry.list_backends(pool, workspace_id):
        agent_id = backend["agent_id"]
        state, error = "applied", None
        if pinned_account(backend.get("config")) == provider:
            state = "pinned"
        elif agent_id not in done:
            try:
                await client.put_credential(provider, document, backend["api_key"])
            except Exception as exc:  # noqa: BLE001 — recorded, and retryable
                state, error = "failed", _why(exc)
                logger.warning("projection of %s to %s failed: %s", provider, agent_id, error)
        await integration_store.record_projection(
            pool, workspace_id, provider, agent_id, state=state, version=version, error=error
        )
        results.append({"agent_id": agent_id, "state": state, "error": error})
    return results


async def project_into_new_agent(
    pool: Any, client: Any, workspace_id: str, backend: dict[str, Any]
) -> None:
    """A newly created agent gets every Workspace credential its project lacks.

    Best-effort: the agent exists either way, and a failure is recorded so the
    card shows it and the next apply retries it.
    """
    for integration in await integration_store.list_for_workspace(pool, workspace_id):
        if integration["disconnecting"]:
            continue
        provider = integration["provider"]
        state, error = "applied", None
        if pinned_account(backend.get("config")) == provider:
            state = "pinned"
        else:
            try:
                document = await integration_store.get_document(pool, workspace_id, provider)
                await client.put_credential(provider, document, backend["api_key"])
            except Exception as exc:  # noqa: BLE001
                state, error = "failed", _why(exc)
        await integration_store.record_projection(
            pool, workspace_id, provider, backend["agent_id"],
            state=state, version=integration["version"], error=error,
        )


async def disconnect(pool: Any, client: Any, workspace_id: str, provider: str) -> dict[str, Any]:
    """Remove the credential from every agent, then — only then — the record.

    The reverse order strands agents holding a credential nothing points at.
    Interrupted halfway, the record stays (flagged `disconnecting`) with each
    agent's state, and calling this again finishes the job. Permitted however
    many agents use it: revoking a leaked key must not wait on editing them.
    """
    await integration_store.set_disconnecting(pool, workspace_id, provider)
    removed = {
        p["agent_id"]
        for p in await integration_store.list_projections(pool, workspace_id, provider)
        if p["state"] == "removed"
    }
    results = []
    for backend in await registry.list_backends(pool, workspace_id):
        agent_id = backend["agent_id"]
        state, error = "removed", None
        if agent_id not in removed:
            try:
                # Idempotent at TurnCall. A pinned agent's own key lives in its
                # config, which this never touches.
                await client.delete_credential(provider, backend["api_key"])
            except Exception as exc:  # noqa: BLE001
                state, error = "remove_failed", _why(exc)
        await integration_store.record_projection(
            pool, workspace_id, provider, agent_id, state=state, version=None, error=error
        )
        results.append({"agent_id": agent_id, "state": state, "error": error})
    complete = all(r["state"] == "removed" for r in results)
    if complete:
        await integration_store.delete(pool, workspace_id, provider)
    return {"complete": complete, "agents": results}


async def adopt(
    pool: Any, client: Any, workspace_id: str, backend: dict[str, Any]
) -> str:
    """Move a pinned agent's own key into the Workspace, explicitly (#58).

    Stores it as the Workspace Integration (replacing any), takes it out of the
    agent's config, then projects it everywhere. Returns the account adopted.
    Validation is the caller's: the key is the one already running calls.
    """
    config = backend.get("config") or {}
    provider = pinned_account(config)
    if provider is None:
        raise ValueError("this agent carries no key of its own to adopt")
    key = config["llm"]["api_key"]
    document = {"api_key": key}
    await integration_store.save(
        pool, workspace_id, provider,
        document=document, last4=last4(document),
        validation=await provider_catalog.probe(provider, document),
    )
    llm = {k: v for k, v in config["llm"].items() if k != "api_key"}
    await agent_update.apply_update(pool, client, backend, {**config, "llm": llm})
    await project(pool, client, workspace_id, provider)
    return provider


async def engine_providers(pool: Any, client: Any) -> dict[str, Any] | None:
    """TurnCall's `GET /v1/providers`, asked as the Builder's own project.

    Only the platform-wide half is read from it (roles, accounts, `platform_key`);
    whether an agent holds the Workspace's credential is what the projection log
    records. None when TurnCall cannot answer — the page still renders.
    """
    try:
        wp = await phones.ensure_webhook_project(pool, client)
        return await client.list_providers(wp["api_key"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("turncall /v1/providers unavailable: %s", exc)
        return None


async def cards(pool: Any, client: Any, workspace_id: str) -> dict[str, Any]:
    """One card per provider account: what is connected and which key runs calls."""
    engine = await engine_providers(pool, client)
    integrations = {i["provider"]: i for i in await integration_store.list_for_workspace(pool, workspace_id)}
    projections = await integration_store.list_projections(pool, workspace_id)
    backends = await registry.list_backends(pool, workspace_id)
    names = {b["agent_id"]: (b.get("config") or {}).get("name") or b.get("slug") for b in backends}
    live = set(names)

    accounts = (engine or {}).get("accounts") or {
        p: {"serves": {}, "platform_key": None} for p in integrations
    }
    out = []
    for provider, info in accounts.items():
        integration = integrations.get(provider)
        platform = info.get("platform_key")
        connected = bool(integration) and not integration["disconnecting"]
        runs_on = (
            "workspace" if connected
            else "platform" if platform
            else "none" if platform is False
            else "unknown"
        )
        agents = [
            {**p, "name": names.get(p["agent_id"])}
            for p in projections
            if p["provider"] == provider and p["agent_id"] in live
        ]
        out.append({
            "provider": provider,
            "serves": info.get("serves") or {},
            "connected": connected,
            "last4": integration["last4"] if integration else None,
            "validation": integration["validation"] if integration else None,
            "updated_at": integration["updated_at"] if integration else None,
            "disconnecting": bool(integration and integration["disconnecting"]),
            "platform_key": platform,
            "runs_on": runs_on,
            "agents_using": sum(provider in accounts_used(b.get("config")) for b in backends),
            "pinned_agents": [
                {"agent_id": b["agent_id"], "name": names[b["agent_id"]]}
                for b in backends
                if pinned_account(b.get("config")) == provider
            ],
            "agents": agents,
        })
    return {"providers": out, "roles": (engine or {}).get("roles") or {}, "engine_reachable": engine is not None}
