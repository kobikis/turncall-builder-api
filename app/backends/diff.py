"""Detect whether an agent edit needs the backend regenerated.

Two things live in generated code: the custom tool handlers, and the end-of-call
Automations (ADR-0021). An edit to either has to reach the container, and an edit
to neither must not pay for a rebuild.
"""

from __future__ import annotations

from typing import Any

from ..pipedream import parse_integrations


def _custom_tools(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    tools = (config or {}).get("custom_tools") or []
    # Normalize optional fields so an absent key vs an explicit null (added by
    # config normalization, ADR-0010) doesn't read as a tool change.
    normalized = [{**t, "server_url": t.get("server_url")} for t in tools]
    return sorted(normalized, key=lambda t: t.get("name", ""))


def tools_changed(old: dict[str, Any] | None, new: dict[str, Any] | None) -> bool:
    """True if the custom tools differ (name/params), so the Agent Backend must be
    re-rendered. Order-insensitive; prompt/voice-only edits return False."""
    return _custom_tools(old) != _custom_tools(new)


def _automations(config: dict[str, Any] | None) -> list[tuple[str, str, str]]:
    """End-of-call Automations, normalized for comparison. Args are compared by
    their sorted items so key order in the stored config is not a change."""
    out = []
    for i in parse_integrations((config or {}).get("integrations")):
        if i.call_ended:
            args = repr(sorted(i.call_ended.args.items(), key=lambda kv: kv[0]))
            out.append((i.app, i.call_ended.action, args))
    return sorted(out)


def automations_changed(old: dict[str, Any] | None, new: dict[str, Any] | None) -> bool:
    """True if the end-of-call Automations differ, so the backend must be
    re-rendered. Without this a channel edit is accepted, stored, and never
    reaches the container — the one failure in ADR-0021 that looks like success.
    """
    return _automations(old) != _automations(new)
