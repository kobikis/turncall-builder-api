"""Workspace compliance settings: redaction and retention (#103, ADR-0028).

An admin sets them once for the Workspace; builder-api projects them into every
Agent's TurnCall project (turncall ADR-0024: `PUT /v1/projects/{id}/redaction`
and `/retention`, admin key). The master here is authoritative and the copies
disposable, as for any [[Projection]]: one change is N writes, each recorded per
target, skipped when the target already holds that exact setting, and retried
by running the projection again.

**A per-agent override lands in the Agent's own project setting, not in its
config.** Every Agent is its own TurnCall project, so the project's setting
already is the Agent's, exactly as with its limits (#76). Writing the merged
value there keeps one mechanism for both halves — redaction has no agent-level
field in the engine at all — and keeps `retention` out of the agent config,
where every Apply would have to carry it over unchanged or meet the engine's
admin-only refusal. The engine's agent `config.retention` is left unset.

Defaults are the engine's: unset is no redaction and keep everything, so a
Workspace that never set anything writes nothing.

Validation mirrors the engine's (entity names, 1–36500 days), so a bad value is
refused here, before any project is written, with a message naming the field.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

from . import compliance_store
from .backends import phones, registry
from .provider_integrations import _why

logger = logging.getLogger("turncall_builder")

# TurnCall's `services/redaction.py`, copied rather than fetched: the engine
# validates against a static list too, and a builder that accepted a name the
# engine refuses would fail N projects at once instead of one form.
DEFAULT_ENTITIES: tuple[str, ...] = (
    "PHONE_NUMBER",
    "EMAIL_ADDRESS",
    "CREDIT_CARD",
    "US_SSN",
    "IBAN_CODE",
)
SUPPORTED_ENTITIES: frozenset[str] = frozenset({
    "AGE", "CREDIT_CARD", "CRYPTO", "DATE_TIME", "EMAIL_ADDRESS", "IBAN_CODE",
    "IP_ADDRESS", "LOCATION", "MAC_ADDRESS", "MEDICAL_LICENSE", "NRP",
    "ORGANIZATION", "PERSON", "PHONE_NUMBER", "UK_NHS", "URL", "US_BANK_NUMBER",
    "US_DRIVER_LICENSE", "US_ITIN", "US_PASSPORT", "US_SSN",
})
MAX_RETENTION_DAYS = 36500
KINDS = ("recordings_days", "transcripts_days", "events_days")

WEBHOOK_PREFIX = "webhook-numbers:"


class Redaction(BaseModel):
    """TurnCall's `ProjectRedaction`. `enabled` is required: a setting that
    names only `entities` must not turn redaction off by omission."""

    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    entities: list[str] | None = None

    @field_validator("entities")
    @classmethod
    def _known(cls, entities: list[str] | None) -> list[str] | None:
        if entities is None:
            return None
        if not entities:
            raise ValueError("name at least one entity type, or null for the default")
        unknown = sorted(set(entities) - SUPPORTED_ENTITIES)
        if unknown:
            raise ValueError(
                f"unknown entity types {unknown}; supported: " + ", ".join(sorted(SUPPORTED_ENTITIES))
            )
        return list(dict.fromkeys(entities))


def _days() -> Any:
    return Field(default=None, ge=1, le=MAX_RETENTION_DAYS)


class Retention(BaseModel):
    """TurnCall's `RetentionConfig`: whole days from the call's end. On the
    Workspace null keeps that kind for good; on an Agent null takes the
    Workspace's (so an Agent keeps a kind for good with 36500)."""

    model_config = ConfigDict(extra="forbid")
    recordings_days: StrictInt | None = _days()
    transcripts_days: StrictInt | None = _days()
    events_days: StrictInt | None = _days()


class WorkspaceSettings(BaseModel):
    """Both blocks required: the PUT replaces the whole setting, and a body that
    forgot one must not silently switch redaction off or retention to forever."""

    model_config = ConfigDict(extra="forbid")
    redaction: Redaction
    retention: Retention


class AgentOverride(BaseModel):
    """Both keys required, either may be null (= the Workspace's)."""

    model_config = ConfigDict(extra="forbid")
    redaction: Redaction | None
    retention: Retention | None


DEFAULT: dict[str, Any] = {
    "redaction": {"enabled": False, "entities": None},
    "retention": dict.fromkeys(KINDS),
}


def override_document(body: AgentOverride) -> dict[str, Any]:
    """What is stored for an override: a retention with every field null is no
    retention override at all."""
    retention = body.retention.model_dump() if body.retention else None
    if retention is not None and all(v is None for v in retention.values()):
        retention = None
    return {
        "redaction": body.redaction.model_dump() if body.redaction else None,
        "retention": retention,
    }


def effective(
    workspace: dict[str, Any] | None, override: dict[str, Any] | None
) -> dict[str, Any]:
    """What one project should hold: the Workspace's (or the engine's defaults),
    the Agent's redaction instead when it has one, and its retention field by
    field."""
    base = workspace or DEFAULT
    override = override or {}
    redaction = override.get("redaction") or base["redaction"]
    retention = dict(base["retention"])
    for kind, days in (override.get("retention") or {}).items():
        if days is not None:
            retention[kind] = days
    return {"redaction": dict(redaction), "retention": retention}


def fingerprint(setting: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(setting, sort_keys=True).encode()).hexdigest()[:16]


def webhook_target(workspace_id: str, project: dict[str, str]) -> dict[str, Any]:
    """The Workspace's webhook project (#65): custom call-init numbers' calls are
    stored there, so they are redacted and expire like any Agent's."""
    return {
        **phones.as_projection_target(workspace_id, project),
        "project_id": project["project_id"],
        "webhook": True,
    }


async def _targets(pool: Any, workspace_id: str) -> list[dict[str, Any]]:
    targets = list(await registry.list_backends(pool, workspace_id))
    webhook = await phones.workspace_webhook_project(pool, workspace_id)
    if webhook:
        targets.append(webhook_target(workspace_id, webhook))
    return targets


def _holds(record: dict[str, Any] | None, setting: dict[str, Any], fp: str) -> bool:
    """The target already has this setting: it was written, or nothing was ever
    written and it is the engine's default a fresh project starts with."""
    if record is None:
        return setting == DEFAULT
    return record["state"] == "applied" and record["fingerprint"] == fp


async def _write(client: Any, target: dict[str, Any], setting: dict[str, Any]) -> str | None:
    """Both halves into one project; the reason it failed, or None. Each PUT
    replaces its whole setting, so a retry after a partial write is safe."""
    try:
        await client.set_project_redaction(target["project_id"], setting["redaction"], target["api_key"])
        await client.set_project_retention(target["project_id"], setting["retention"], target["api_key"])
    except Exception as exc:  # noqa: BLE001 — recorded, and retryable
        logger.warning("compliance projection to %s failed: %s", target["agent_id"], exc)
        return _why(exc)
    return None


async def _project_one(
    pool: Any,
    client: Any,
    workspace_id: str,
    target: dict[str, Any],
    setting: dict[str, Any],
    record: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Write when the target does not hold `setting` yet; the record as it now
    stands (None: nothing ever written, and nothing needed)."""
    fp = fingerprint(setting)
    if _holds(record, setting, fp):
        return record
    error = await _write(client, target, setting)
    record = {"state": "failed" if error else "applied", "fingerprint": fp, "error": error}
    await compliance_store.record_projection(pool, workspace_id, target["agent_id"], **record)
    return record


def _state(record: dict[str, Any] | None, setting: dict[str, Any]) -> tuple[str, str | None]:
    """`applied`, `failed` (with why), or `pending`: the master changed since
    this target was last written, and no projection has reached it yet."""
    if _holds(record, setting, fingerprint(setting)):
        return "applied", None
    if record and record["state"] == "failed":
        return "failed", record.get("error")
    return "pending", None


def _settings_view(workspace: dict[str, Any] | None) -> dict[str, Any]:
    base = workspace or DEFAULT
    return {
        "set": workspace is not None,
        "redaction": base["redaction"],
        "retention": base["retention"],
        "updated_at": (workspace or {}).get("updated_at"),
    }


async def report(pool: Any, client: Any, workspace_id: str, *, write: bool) -> dict[str, Any]:
    """The Workspace setting and where each target stands. With `write`, first
    project into every target not already holding its setting — the PUT and the
    retry are the same call, and a finished projection changes nothing."""
    workspace = await compliance_store.get_workspace(pool, workspace_id)
    overrides = await compliance_store.list_overrides(pool, workspace_id)
    records = await compliance_store.list_projections(pool, workspace_id)
    agents, webhook_numbers = [], None
    for target in await _targets(pool, workspace_id):
        agent_id = target["agent_id"]
        override = None if target.get("webhook") else overrides.get(agent_id)
        setting = effective(workspace, override)
        record = records.get(agent_id)
        if write:
            record = await _project_one(pool, client, workspace_id, target, setting, record)
        state, error = _state(record, setting)
        if target.get("webhook"):
            webhook_numbers = {"state": state, "error": error}
            continue
        agents.append({
            "agent_id": agent_id,
            "name": (target.get("config") or {}).get("name") or target.get("slug"),
            "state": state,
            "error": error,
            "overridden": override is not None,
            "effective": setting,
        })
    states = [a["state"] for a in agents] + ([webhook_numbers["state"]] if webhook_numbers else [])
    return {
        **_settings_view(workspace),
        "agents": agents,
        "webhook_numbers": webhook_numbers,
        "complete": all(s == "applied" for s in states),
        "entity_types": {"supported": sorted(SUPPORTED_ENTITIES), "default": list(DEFAULT_ENTITIES)},
    }


async def agent_report(
    pool: Any, client: Any, workspace_id: str, backend: dict[str, Any], *, write: bool
) -> dict[str, Any]:
    """One Agent's view: the Workspace's setting, its override, what applies,
    and whether its project holds it. With `write`, project it first."""
    agent_id = backend["agent_id"]
    workspace = await compliance_store.get_workspace(pool, workspace_id)
    override = await compliance_store.get_override(pool, workspace_id, agent_id)
    setting = effective(workspace, override)
    record = (await compliance_store.list_projections(pool, workspace_id)).get(agent_id)
    if write:
        record = await _project_one(pool, client, workspace_id, backend, setting, record)
    state, error = _state(record, setting)
    return {
        "workspace": _settings_view(workspace),
        "override": override or {"redaction": None, "retention": None},
        "effective": setting,
        "projection": {"state": state, "error": error},
    }


async def project_into_new_target(
    pool: Any, client: Any, workspace_id: str, target: dict[str, Any]
) -> None:
    """A new project gets the Workspace's setting: an Agent's at Create, or the
    Workspace's webhook project at its first custom call-init bind. Nothing is
    written while the Workspace has none — a fresh project already holds the
    defaults. Best-effort: a failure is recorded, and the next apply retries it.
    """
    workspace = await compliance_store.get_workspace(pool, workspace_id)
    if workspace is None:
        return
    await _project_one(pool, client, workspace_id, target, effective(workspace, None), None)
