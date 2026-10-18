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
from collections.abc import Collection
from typing import Any, NamedTuple

from . import agent_update, integration_store, provider_catalog, telnyx_api
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


class NumbersNotOnAccount(Exception):
    """Telnyx numbers bound in the Workspace that the account about to be
    projected doesn't hold, not all of them acknowledged (#114)."""

    def __init__(self, numbers: list[str]) -> None:
        super().__init__(", ".join(numbers))
        self.numbers = numbers


class NumberLookupFailed(Exception):
    """The numbers could not be looked up on the account: Telnyx refused the
    key, or didn't answer. The message is the sentence to show."""


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


# A target holds the Workspace's credential when its last write of the current
# version succeeded: `applied`, or `pinned` (written all the same; only the
# agent's own LLM key wins, for its model). One definition, for re-applying and
# for a call that must run on the Workspace's account.
_HOLDS = ("applied", "pinned")


async def project(
    pool: Any, client: Any, workspace_id: str, provider: str, *, cleared: Collection[str] = ()
) -> list[dict[str, Any]]:
    """Write the Workspace's credential into every agent's TurnCall project.

    Returns per-agent state: `applied`, `pinned` (written too, but its own LLM
    key wins for the model), or `failed` with the reason. Safe to call again:
    agents already holding this version are not written to. Does nothing while a
    disconnect is unfinished — re-applying would put the key back into agents
    already cleared.

    `cleared`: Telnyx numbers the caller has checked on this account already
    (held, or acknowledged); see `_telnyx_write_allowed`.
    """
    integration = await integration_store.get(pool, workspace_id, provider)
    if integration is None or integration["disconnecting"]:
        return []
    document = await integration_store.get_document(pool, workspace_id, provider)
    holding = await _holding(pool, workspace_id, integration)
    return [
        await _project_one(
            pool, client, workspace_id, integration, document, target,
            write=target["agent_id"] not in holding, holds=target["agent_id"] in holding,
            cleared=cleared, refused_is_failed=True,
        )
        for target in await _targets(pool, workspace_id)
    ]


async def _holding(pool: Any, workspace_id: str, integration: dict[str, Any]) -> set[str]:
    """The targets the log says hold this version of the credential."""
    return {
        p["agent_id"]
        for p in await integration_store.list_projections(
            pool, workspace_id, integration["provider"]
        )
        if p["state"] in _HOLDS and p["version"] == integration["version"]
    }


async def _project_one(
    pool: Any,
    client: Any,
    workspace_id: str,
    integration: dict[str, Any],
    document: dict[str, Any],
    target: dict[str, Any],
    *,
    write: bool = True,
    holds: bool | None = None,
    cleared: Collection[str] = (),
    refused_is_failed: bool = False,
) -> dict[str, Any]:
    """Write the credential into one target (unless it holds it already) and
    log the outcome: its state, and the reason when it failed.

    The one write every projection goes through, so a Telnyx key is checked
    here against the numbers bound in the target (`_telnyx_write_allowed`):
    NumbersNotOnAccount or NumberLookupFailed, and nothing is written or
    logged. With `refused_is_failed` the refusal is logged as the target's
    failure instead, for a projection over many targets. `holds`: whether the
    log says the target holds this version (None: not known, and looked up
    only if needed)."""
    provider = integration["provider"]
    if write and provider == TELNYX:
        try:
            await _telnyx_write_allowed(
                pool, workspace_id, integration, document, target, holds=holds, cleared=cleared
            )
        except (NumbersNotOnAccount, NumberLookupFailed) as exc:
            if not refused_is_failed:
                raise
            error = _refused_write(exc)
            await integration_store.record_projection(
                pool, workspace_id, provider, target["agent_id"],
                state="failed", version=integration["version"], error=error,
            )
            return {"agent_id": target["agent_id"], "state": "failed", "error": error}
    error = await _write(client, provider, document, target) if write else None
    state = _state(target, provider, error)
    await integration_store.record_projection(
        pool, workspace_id, provider, target["agent_id"],
        state=state, version=integration["version"], error=error,
    )
    return {"agent_id": target["agent_id"], "state": state, "error": error}


async def _targets(pool: Any, workspace_id: str) -> list[dict[str, Any]]:
    """Every TurnCall project the Workspace's keys go into: each agent's, and
    the Workspace's webhook project once it has one (#65)."""
    targets = list(await registry.list_backends(pool, workspace_id))
    webhook = await phones.workspace_webhook_project(pool, workspace_id)
    if webhook:
        targets.append(phones.as_projection_target(workspace_id, webhook))
    return targets


# --- Telnyx numbers on the account being connected (#114) ----------------------
#
# A Telnyx number's webhooks are signed by the account that holds it, and
# TurnCall verifies them with the key of whatever account the number's project
# holds now. Projecting a Telnyx account that doesn't hold a bound number makes
# that number stop answering calls (403 on every webhook). So before a Telnyx
# key is saved or projected, every number bound in a project it will be written
# into is looked up on it (#119): only those projects' numbers change accounts.

class TelnyxWriteTargets(NamedTuple):
    """Where a Telnyx save or apply would write the key, and what it would put
    at risk there."""

    projects: set[str]  # the TurnCall projects it would write the key into
    numbers: list[str]  # the Workspace's Telnyx numbers bound in them, E.164


async def telnyx_write_targets(
    pool: Any, workspace_id: str, *, skip_holding: dict[str, Any] | None = None
) -> TelnyxWriteTargets:
    """The projects a Telnyx save or apply writes the key into, by the same
    `_targets` the projection writes into, and the bound numbers in them (#119).

    A save writes into every target. With `skip_holding` (the stored
    Integration), targets the log says hold its current version are left out,
    as `project()` skips them on apply. The shared webhook project is never a
    target, so a number still bound there (on the platform's keys) is never
    among `numbers`, nor is one bound in any other project. An unassigned row
    is in none."""
    holding = await _holding(pool, workspace_id, skip_holding) if skip_holding else set()
    projects = {
        t["project_id"]
        for t in await _targets(pool, workspace_id)
        if t["agent_id"] not in holding and t.get("project_id")
    }
    return TelnyxWriteTargets(projects, await _bound_telnyx(pool, workspace_id, projects))


async def _bound_telnyx(pool: Any, workspace_id: str, projects: set[str]) -> list[str]:
    """The Workspace's Telnyx numbers bound in `projects`, E.164, sorted. An
    unassigned row is in no project."""
    if not projects:
        return []
    return sorted({
        n["e164"]
        for n in await phones.list_numbers(pool, workspace_id)
        if n.get("carrier") == TELNYX and n.get("project_id") in projects
    })


_NUMBERS = "the workspace's Telnyx numbers"
_NAMED = 10  # numbers a refusal's sentence names; its `numbers` carries them all


def name_numbers(numbers: list[str]) -> str:
    """Numbers as a refusal's sentence names them: at most ten, then "and N
    more"."""
    named = ", ".join(numbers[:_NAMED])
    if len(numbers) > _NAMED:
        named += f" and {len(numbers) - _NAMED} more"
    return named


async def _missing_on_account(api_key: str, numbers: list[str], *, new_key: bool) -> list[str]:
    """The numbers the Telnyx account doesn't hold. The account is listed once
    and compared here: a request per 250 numbers it holds, not one per number
    asked about (#116)."""
    try:
        held = await telnyx_api.list_numbers(api_key)
    except telnyx_api.LOOKUP_ERRORS as exc:
        raise NumberLookupFailed(
            telnyx_api.lookup_failure(exc, _NUMBERS, new_key=new_key)
        ) from exc
    return [e164 for e164 in numbers if e164 not in held]


async def check_telnyx_numbers(
    api_key: str, numbers: list[str], acknowledged: list[str], *, new_key: bool = False
) -> list[str]:
    """Look up `numbers` (a `telnyx_write_targets(...).numbers`) on the account
    `api_key` belongs to.

    Returns the numbers it doesn't hold that were acknowledged, for the audit
    entry. Raises NumbersNotOnAccount when any it doesn't hold aren't all in
    `acknowledged`, and NumberLookupFailed when they couldn't be looked up.
    `new_key`: `api_key` is being connected, not yet saved."""
    if not numbers:
        return []
    missing = await _missing_on_account(api_key, numbers, new_key=new_key)
    if not set(missing) <= set(acknowledged):
        raise NumbersNotOnAccount(missing)
    return missing


async def _telnyx_write_allowed(
    pool: Any,
    workspace_id: str,
    integration: dict[str, Any],
    document: dict[str, Any],
    target: dict[str, Any],
    *,
    holds: bool | None,
    cleared: Collection[str],
) -> None:
    """Refuse writing this Telnyx key into `target` while numbers bound in its
    project aren't on the key's account (#109 review): they would stop
    answering calls. Raises NumbersNotOnAccount with them, or
    NumberLookupFailed.

    Nothing is looked up when no number is bound there (a new project, or an
    agent's with none), for numbers in `cleared` (checked by the caller,
    connect or apply, which take acknowledgements), or when the log says the
    target holds this version already: rewriting it moves no number to another
    account, and its numbers were checked (or acknowledged) when it was
    written."""
    project_id = target.get("project_id")
    if not project_id:
        return
    numbers = [
        e164 for e164 in await _bound_telnyx(pool, workspace_id, {project_id})
        if e164 not in cleared
    ]
    if not numbers:
        return
    if holds is None:
        holds = target["agent_id"] in await _holding(pool, workspace_id, integration)
    if holds:
        return
    missing = await _missing_on_account(document["api_key"], numbers, new_key=False)
    if missing:
        raise NumbersNotOnAccount(missing)


def _refused_write(exc: Exception) -> str:
    """A refused Telnyx write, as the projection log keeps it."""
    if isinstance(exc, NumbersNotOnAccount):
        return (
            f"not written: {name_numbers(exc.numbers)} bound here, not on this Telnyx "
            "account; re-apply and acknowledge them to go ahead"
        )
    return f"not written: {exc}"


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
    card shows it and the next apply retries it. A Telnyx key the numbers
    bound in the target's project aren't on is recorded failed too (a new
    project has none bound).
    """
    for integration in await integration_store.list_for_workspace(pool, workspace_id):
        if integration["disconnecting"]:
            continue
        document = await integration_store.get_document(
            pool, workspace_id, integration["provider"]
        )
        await _project_one(
            pool, client, workspace_id, integration, document, target,
            holds=False, refused_is_failed=True,
        )


class Ensured(NamedTuple):
    """What `ensure_in_target` did: whether it wrote the credential, and why
    the target doesn't hold it (None when it does)."""

    written: bool
    error: str | None


async def ensure_in_target(
    pool: Any,
    client: Any,
    workspace_id: str,
    integration: dict[str, Any],
    target: dict[str, Any],
    *,
    rewrite: bool = False,
    document: dict[str, Any] | None = None,
) -> Ensured:
    """Make sure one target holds the Workspace's current credential for
    `integration` (a connected one, as `integration_store.get` read it), and
    say whether it was written and, when the target doesn't hold it, why not.
    `document` is the credential when the caller has read it.

    For a call that must run on the Workspace's account and never fall back
    to the platform's key, which TurnCall would use if the copy were missing:
    searching, buying and releasing Telnyx numbers (#108). By default the log
    is trusted, as `project()` trusts it. `rewrite` writes it regardless: the
    log only says the last write succeeded, not that TurnCall still holds it,
    and buying or releasing on the wrong account cannot be undone (#109).

    A Telnyx key is checked against the numbers bound in the target first, with
    no acknowledgement: NumbersNotOnAccount or NumberLookupFailed propagate,
    and nothing is written (#109 review)."""
    holds = target["agent_id"] in await _holding(pool, workspace_id, integration)
    if holds and not rewrite:
        return Ensured(False, None)
    if document is None:
        document = await integration_store.get_document(
            pool, workspace_id, integration["provider"]
        )
    outcome = await _project_one(
        pool, client, workspace_id, integration, document, target, holds=holds
    )
    return Ensured(outcome["error"] is None, outcome["error"])


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

    Never a Telnyx key: an agent's own key is only its LLM's, and no LLM
    provider bills to Telnyx, so adopting never needs the Telnyx numbers check
    (#114).
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
