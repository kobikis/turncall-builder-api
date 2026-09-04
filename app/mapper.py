"""Map the builder's trimmed config (ADR-0003) to a TurnCall CreateAgentRequest."""

from __future__ import annotations

from typing import Any

from .backends.scaffold import validate_tool_names

_BUILTIN_DESCRIPTIONS = {
    "end_call": "End the call when the conversation is complete.",
    "transfer_call": "Transfer the call to a human or another phone number.",
    "send_dtmf": "Send keypad tones (DTMF) during the call — e.g. to navigate an "
    "IVR menu or enter a code.",
}

# Per-provider TTS model. Without an explicit model, TurnCall fills its schema
# default (a Deepgram model) and passes it to whatever provider is selected —
# ElevenLabs/Cartesia/OpenAI then reject it (e.g. ElevenLabs 403 invalid model).
# Deepgram is omitted: there the *voice* carries the model (aura-2-*).
_TTS_DEFAULT_MODEL = {
    "cartesia": "sonic-3",
    "elevenlabs": "eleven_turbo_v2_5",
    "openai": "gpt-4o-mini-tts",
}


def _tts_block(tts: dict[str, Any]) -> dict[str, Any]:
    """TTS config with a provider-appropriate model so TurnCall doesn't leak a
    Deepgram model default into ElevenLabs/Cartesia/OpenAI."""
    provider = tts.get("provider", "deepgram")
    block: dict[str, Any] = {"provider": provider, "voice": tts.get("voice", "aura-2-helena-en")}
    model = tts.get("model") or _TTS_DEFAULT_MODEL.get(provider)
    if model:
        block["model"] = model
    return block


def resolved_tool_url(
    tool: dict[str, Any], cfg: dict[str, Any], tools_base_url: str | None
) -> str | None:
    """Where this tool's calls actually go: tool server_url (verbatim) →
    agent server_url (base) → generated backend (tools_base_url)."""
    if tool.get("server_url"):
        return tool["server_url"]
    base = (cfg.get("server_url") or tools_base_url or "").rstrip("/")
    return f"{base}/tools/{tool['name']}" if base else None


def external_tool_names(cfg: dict[str, Any], tools_base_url: str | None) -> set[str]:
    """Tools whose calls resolve OFF the generated backend (ADR-0010) — no
    generated handler. Configs may carry the backend's own URL explicitly
    (normalization fills empty fields with the effective URL), so externality
    is a resolved-destination comparison, not a field-presence check."""
    if not tools_base_url:
        return set()
    return {
        t["name"]
        for t in cfg.get("custom_tools") or []
        if not (resolved_tool_url(t, cfg, tools_base_url) or "").startswith(
            tools_base_url
        )
    }


def to_create_agent_request(
    cfg: dict[str, Any],
    tools_base_url: str | None = None,
    tools_secret: str | None = None,
) -> dict[str, Any]:
    """Trimmed agent_config -> the body for POST /v1/agents.

    Custom tools get their webhook_url wired to the agent's generated backend
    (tools_base_url) and, when tools_secret is set, TurnCall HMAC-signs each
    tool call so the backend can verify origin. Tool names are validated here —
    they are interpolated into generated Python source. Fields outside the
    trimmed surface take TurnCall's defaults by omission.
    """
    if not cfg.get("name"):
        raise ValueError("agent_config.name is required")

    # `tools` is meant to hold builtin tool *names* (strings), but the chat step
    # sometimes emits full tool-definition dicts here instead of in custom_tools.
    # Accept both: strings map to builtins, dicts are treated as custom tools.
    raw_tools = cfg.get("tools") or []
    builtin_names = [t for t in raw_tools if isinstance(t, str)]
    custom_tools = [
        *(cfg.get("custom_tools") or []),
        *(t for t in raw_tools if isinstance(t, dict)),
    ]
    validate_tool_names(custom_tools)

    llm = cfg.get("llm") or {}
    tts = cfg.get("tts") or {}
    tools = [
        {
            "name": name,
            "description": _BUILTIN_DESCRIPTIONS.get(name, name),
            "parameters_schema": {"type": "object", "properties": {}},
        }
        for name in builtin_names
    ]
    # Tool routing (ADR-0010): per-tool server_url (verbatim) → agent
    # server_url (base + /tools/{name}) → generated backend (tools_base_url).
    for t in custom_tools:
        tool: dict[str, Any] = {
            "name": t["name"],
            "description": t.get("description", t["name"]),
            "parameters_schema": t.get("parameters_schema")
            or {"type": "object", "properties": {}},
        }
        url = resolved_tool_url(t, cfg, tools_base_url)
        if url:
            tool["webhook_url"] = url
        if tools_secret:
            tool["webhook_secret"] = tools_secret
        tools.append(tool)

    config: dict[str, Any] = {
        "system_prompt": cfg.get("system_prompt", ""),
        "first_message": cfg.get("first_message"),
        "llm": {
            "provider": llm.get("provider", "openai"),
            "model": llm.get("model", "gpt-4o-mini"),
        },
        "tts": _tts_block(tts),
        "tools": tools,
    }
    # Speech-to-speech pipeline: a native audio model replaces STT→LLM→TTS. The
    # tts block above is left in place but ignored by TurnCall in s2s mode; the
    # voice comes from the s2s block.
    if cfg.get("pipeline_mode") == "s2s":
        s2s = cfg.get("s2s") or {}
        config["pipeline_mode"] = "s2s"
        config["s2s"] = {
            "provider": s2s.get("provider", "openai"),
            "model": s2s.get("model") or "gpt-4o-realtime-preview",
            "voice": s2s.get("voice") or "alloy",
        }

    # Voicemail detection (cascade only): leave a message when an outbound call
    # reaches voicemail. TurnCall rejects it alongside s2s, so drop it in s2s mode
    # even if set — s2s wins.
    vm = cfg.get("voicemail_detection") or {}
    if vm.get("enabled") and config.get("pipeline_mode") != "s2s":
        config["voicemail_detection"] = {
            "enabled": True,
            **({"voicemail_message": vm["voicemail_message"]} if vm.get("voicemail_message") else {}),
        }

    # Guardrails: prohibited topics the platform injects as a refusal rule. Only
    # emit prohibited_topics (max_tool_calls_per_turn isn't enforced yet).
    topics = [
        t.strip()
        for t in (cfg.get("guardrails") or {}).get("prohibited_topics") or []
        if isinstance(t, str) and t.strip()
    ]
    if topics:
        config["guardrails"] = {"prohibited_topics": topics}

    # Analysis config (incl. takeaway_ids) passes through untrimmed — the
    # console manages takeaway attachments and they must survive updates.
    if cfg.get("analysis"):
        config["analysis"] = cfg["analysis"]

    return {
        "name": cfg["name"],
        "environment": "development",
        "config": config,
    }
