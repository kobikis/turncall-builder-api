"""Detect whether an agent edit changed its custom tools (→ regenerate backend)."""

from __future__ import annotations

from typing import Any


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
