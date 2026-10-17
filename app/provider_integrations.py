"""A Workspace's provider accounts, projected into TurnCall (ADR-0023, #55-#58).

The Builder mints one TurnCall project per agent, so a Workspace credential is
one write per agent: a **projection**. Every write is recorded per agent, can be
re-run to completion after a partial failure, and skips agents that already hold
the current version — re-running a finished projection changes nothing.

Precedence is the engine's (turncall ADR-0020), never redefined here: an agent's
own inline key wins over the Workspace's, which wins over the platform's. An
agent carrying one is **pinned**: nothing moves its key without the explicit
adoption action. It still receives the Workspace credential — its inline key
covers only its LLM, so its transcription, voice or realtime model on the same
account would otherwise fall to the platform key — and is recorded `pinned` so
the card can say which agents override the Workspace for their model.

A credential change never regenerates an Agent Backend: the regeneration
condition is tools and Automations, and a credential is neither.
"""

from __future__ import annotations

import logging
from typing import Any

from . import agent_update, integration_store, provider_catalog
from .backends import phones, registry
from .turncall_client import failure_reason

logger = logging.getLogger("turncall_builder")

AWS = "aws"
TELNYX = "telnyx"
ASTERISK = "asterisk"
# Role providers billed to an account spelled differently; `ollama` and
# `custom_openai` have none — their key belongs to the endpoint they name.
_ACCOUNT = {"openai_live": "openai", "bedrock": AWS}
_NO_ACCOUNT = {"ollama", "custom_openai"}


class AdoptRefused(ValueError):
    """Adoption would overwrite a Workspace key, or the provider rejects the key."""


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
    secret = (
        document.get("api_key")
        or document.get("access_key_id")
        or document.get("role_arn")
        or document.get("password")  # asterisk
    )
    return (secret or "")[-4:]


async def project(pool: Any, client: Any, workspace_id: str, provider: str) -> list[dict[str, Any]]:
    """Write the Workspace's credential into every agent's TurnCall project.

    Returns per-agent state: `applied`, `pinned` (written too, but its own LLM
    key wins for the model), or `failed` with the reason. Safe to call again:
    agents already holding this version are not written to. Does nothing while a
    disconnect is unfinished — re-applying would put the key back into agents
    already cleared.
    """
    integration = await integration_store.get(pool, workspace_id, provider)
    if integration is None or integration["disconnecting"]:
        return []
    version = integration["version"]
    document = await integration_store.get_document(pool, workspace_id, provider)
    done = {
        p["agent_id"]
        for p in await integration_store.list_projections(pool, workspace_id, provider)
        if p["state"] == "applied" and p["version"] == version
    }
    results = []
    for backend in await _targets(pool, workspace_id):
        agent_id = backend["agent_id"]
        error = None if agent_id in done else await _write(client, provider, document, backend)
        state = _state(backend, provider, error)
        await integration_store.record_projection(
            pool, workspace_id, provider, agent_id, state=state, version=version, error=error
        )
        results.append({"agent_id": agent_id, "state": state, "error": error})
    return results


async def _targets(pool: Any, workspace_id: str) -> list[dict[str, Any]]:
    """Every TurnCall project the Workspace's keys go into: each agent's, and
    the Workspace's webhook project once it has one (#65)."""
    targets = list(await registry.list_backends(pool, workspace_id))
    webhook = await phones.workspace_webhook_project(pool, workspace_id)
    if webhook:
        targets.append(phones.as_projection_target(workspace_id, webhook))
    return targets


async def _write(client: Any, provider: str, document: dict[str, Any], backend: dict[str, Any]) -> str | None:
    """One agent's write; the reason it failed, or None."""
    try:
        await client.put_credential(provider, document, backend["api_key"])
    except Exception as exc:  # noqa: BLE001 — recorded, and retryable
        logger.warning("projection of %s to %s failed: %s", provider, backend["agent_id"], exc)
        return failure_reason(exc)
    return None


def _state(backend: dict[str, Any], provider: str, error: str | None) -> str:
    if error:
        return "failed"
    return "pinned" if pinned_account(backend.get("config")) == provider else "applied"


async def project_into_new_target(
    pool: Any, client: Any, workspace_id: str, target: dict[str, Any]
) -> None:
    """A new projection target gets every Workspace credential: an agent's
    project when the agent is created, or the Workspace's webhook project when
    its first webhook number is bound (#65).

    Best-effort: the agent exists either way, and a failure is recorded so the
    card shows it and the next apply retries it.
    """
    for integration in await integration_store.list_for_workspace(pool, workspace_id):
        if integration["disconnecting"]:
            continue
        provider = integration["provider"]
        document = await integration_store.get_document(pool, workspace_id, provider)
        error = await _write(client, provider, document, target)
        await integration_store.record_projection(
            pool, workspace_id, provider, target["agent_id"],
            state=_state(target, provider, error), version=integration["version"], error=error,
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
    for backend in await _targets(pool, workspace_id):
        agent_id = backend["agent_id"]
        state, error = "removed", None
        if agent_id not in removed:
            try:
                # Idempotent at TurnCall. A pinned agent's own key lives in its
                # config, which this never touches.
                await client.delete_credential(provider, backend["api_key"])
            except Exception as exc:  # noqa: BLE001
                state, error = "remove_failed", failure_reason(exc)
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

    Stores it as the Workspace Integration, takes it out of the agent's config,
    then projects it everywhere. Returns the account adopted. Refuses when the
    Workspace already has a key for this account (replacing one is a deliberate
    Replace on the card, not a side effect of adopting) and when the provider
    rejects the key, exactly as a typed one would be.
    """
    config = backend.get("config") or {}
    provider = pinned_account(config)
    if provider is None:
        raise ValueError("this agent carries no key of its own to adopt")
    if await integration_store.get(pool, workspace_id, provider) is not None:
        raise AdoptRefused(f"this workspace already has a {provider} key — replace or disconnect it first")
    document = {"api_key": config["llm"]["api_key"]}
    validation = await provider_catalog.probe(provider, document)
    if validation["status"] == "rejected":
        raise AdoptRefused(f"{provider} rejected this agent's key ({validation['detail']}); it was not adopted")
    await integration_store.save(
        pool, workspace_id, provider,
        document=document, last4=last4(document), validation=validation,
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


def _webhook_numbers(
    projections: list[dict[str, Any]], provider: str, target: str | None, shared: int
) -> dict[str, Any]:
    """Which key the Workspace's webhook-routed numbers run on, for one card."""
    p = next(
        (p for p in projections if p["provider"] == provider and p["agent_id"] == target),
        None,
    ) if target else None
    return {
        "state": p["state"] if p else None,
        "error": p["error"] if p else None,
        "on_platform_keys": shared,
    }


async def cards(pool: Any, client: Any, workspace_id: str) -> dict[str, Any]:
    """One card per provider account: what is connected and which key runs calls."""
    engine = await engine_providers(pool, client)
    integrations = {i["provider"]: i for i in await integration_store.list_for_workspace(pool, workspace_id)}
    projections = await integration_store.list_projections(pool, workspace_id)
    backends = await registry.list_backends(pool, workspace_id)
    names = {b["agent_id"]: (b.get("config") or {}).get("name") or b.get("slug") for b in backends}
    live = set(names)
    # Webhook-routed numbers (#65): their own project's projection, and how many
    # still sit in the shared project on the platform's keys until rebound.
    webhook = await phones.workspace_webhook_project(pool, workspace_id)
    webhook_target = phones.as_projection_target(workspace_id, webhook)["agent_id"] if webhook else None
    shared_numbers = await phones.shared_webhook_numbers(pool, workspace_id)

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
            "webhook_numbers": _webhook_numbers(projections, provider, webhook_target, shared_numbers),
        })
    return {"providers": out, "roles": (engine or {}).get("roles") or {}, "engine_reachable": engine is not None}
