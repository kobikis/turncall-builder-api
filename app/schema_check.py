"""Drift guard: the config the builder sends vs the live TurnCall schema.

The builder + mapper emit a deliberately trimmed slice of TurnCall's
AgentConfigSchema (ADR-0003). That slice is hand-curated, so it silently rots
when the API renames or removes a field — the failure only shows up later as a
rejected agent create. This fetches the live schema at startup and turns any
divergence into a log line: an error when the builder sends a field the API no
longer defines (real drift), and an info line listing API fields the builder
doesn't set (capability headroom).

Checks structure, not values: providers/voices/tool names are plain strings in
the JSON schema (TurnCall validates them at runtime), so only field-level drift
is visible here.
"""

from __future__ import annotations

import logging
from typing import Any

from .mapper import to_create_agent_request

logger = logging.getLogger("turncall_builder")


def emitted_config_fields() -> set[str]:
    """The AgentConfigSchema fields the builder can send — derived by running the
    mapper so it never drifts from the mapper. Some fields are mutually exclusive
    (s2s vs voicemail_detection), so union a probe of each mode."""
    base = {
        "name": "probe",
        "system_prompt": "x",
        "first_message": "hi",
        "llm": {"provider": "openai", "model": "gpt-4o-mini"},
        "tts": {"provider": "deepgram", "voice": "aura-2-helena-en"},
        "tools": ["end_call"],
        "custom_tools": [{"name": "t", "description": "d"}],
        "analysis": {"enabled": True},
        "guardrails": {"prohibited_topics": ["x"]},
    }
    s2s_probe = {
        **base,
        "pipeline_mode": "s2s",
        "s2s": {"provider": "openai", "model": "gpt-4o-realtime-preview", "voice": "alloy"},
    }
    cascade_probe = {
        **base,
        "voicemail_detection": {"enabled": True, "voicemail_message": "Hi, call us back."},
    }
    fields: set[str] = set()
    for probe in (s2s_probe, cascade_probe):
        fields |= set(to_create_agent_request(probe, tools_base_url="http://probe").get("config", {}))
    return fields


def live_config_fields(openapi: dict[str, Any] | None) -> set[str]:
    """AgentConfigSchema property names from a fetched OpenAPI spec (empty if absent)."""
    schemas = (openapi or {}).get("components", {}).get("schemas", {})
    return set(schemas.get("AgentConfigSchema", {}).get("properties", {}))


def reconcile(emitted: set[str], live: set[str]) -> tuple[set[str], set[str]]:
    """(unknown, unused): fields the builder sends that the API doesn't define
    (drift — a real problem), and API fields the builder never sets (headroom)."""
    return emitted - live, live - emitted


async def check_drift(client: Any) -> None:
    """Best-effort startup guard: log builder/mapper drift vs the live API."""
    openapi = await client.get_openapi()
    if not openapi:
        logger.info("schema drift check skipped: TurnCall /openapi.json unreachable")
        return
    live = live_config_fields(openapi)
    if not live:
        logger.warning("schema drift check: AgentConfigSchema absent from live spec")
        return
    unknown, unused = reconcile(emitted_config_fields(), live)
    if unknown:
        logger.error(
            "builder config DRIFT — the builder sends fields TurnCall's "
            "AgentConfigSchema no longer defines: %s",
            sorted(unknown),
        )
    if unused:
        logger.info(
            "TurnCall AgentConfigSchema fields the builder doesn't set "
            "(capability headroom): %s",
            sorted(unused),
        )
