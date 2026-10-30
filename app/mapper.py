"""Map the builder's trimmed config (ADR-0003) to a TurnCall CreateAgentRequest."""

from __future__ import annotations

from typing import Any

from . import pipedream
from .backends.scaffold import validate_tool_names

_BUILTIN_DESCRIPTIONS = {
    # The call ends after the agent's reply to this tool's result, so the
    # goodbye comes after the call (turncall#240). Asked to say it first,
    # gpt-4o-mini said goodbye and only called the tool on the caller's next turn.
    "end_call": "Call this as soon as the caller is done or says goodbye. You "
    "will then say a short goodbye, and the call ends after it.",
    "transfer_call": "Transfer the call to a human or another phone number.",
    # Same order as end_call — the tool first, the goodbye after (turncall#240).
    "do_not_call": "Call this when the caller asks not to be called again. "
    "Their number goes on the do-not-call list; then confirm they won't be "
    "called again and say a short goodbye — the call ends after it.",
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
    # Not gpt-4o-mini-tts: on 2026-10-12 most of its requests hung with no
    # answer, while tts-1 answered every one.
    "openai": "tts-1",
}


# Top-level agent settings TurnCall defines and the builder has no opinion on:
# sent as set, so TurnCall validates them and its defaults apply when unset.
_PASSTHROUGH = (
    "language",
    "smart_turn_detection",
    "smart_turn_stop_secs",
    "silence_timeout_ms",
    "interruption_enabled",
    "greeting_interruptible",
    "user_idle_timeout_ms",
    "idle_message",
    "max_call_duration_seconds",
    "avatar",
    # The Skills an agent may load ([{skill, version_id?}]) and how many
    # at once (#133). Sent as written: TurnCall checks each against the
    # Workspace's Skill library, and its 422 names the entry.
    "skills",
    "max_loaded_skills",
    # Guided procedures (turncall#417, #137): graphs of steps the agent
    # follows. Sent as written: TurnCall checks the graph, and its 422 names
    # the procedure and the step (`field_errors`).
    "procedures",
)


def _set(block: dict[str, Any] | None) -> dict[str, Any]:
    """The keys actually set. A null or an empty string means "unset", and
    the way to say that to TurnCall is to leave the key out — its schema
    reads absence as the default and rejects null. False and 0 are values."""
    return {k: v for k, v in (block or {}).items() if v is not None and v != ""}


def _tts_block(tts: dict[str, Any]) -> dict[str, Any]:
    """TTS config with a provider-appropriate model so TurnCall doesn't leak a
    Deepgram model default into ElevenLabs/Cartesia/OpenAI. Everything else
    set on it (pronunciations, failover, speed) passes through."""
    provider = tts.get("provider", "deepgram")
    block: dict[str, Any] = {
        **_set(tts),
        "provider": provider,
        "voice": tts.get("voice", "aura-2-helena-en"),
    }
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


# Per-provider S2S fallbacks — the OpenAI defaults are wrong for the others
# (Nova Sonic would reject "alloy", Gemini would reject a realtime model id).
_S2S_DEFAULTS = {
    # gpt-4o-realtime-preview is retired; TurnCall's own default is gpt-realtime-2.1.
    "openai": ("gpt-realtime-2.1", "alloy"),
    "openai_live": ("gpt-live-1", "alloy"),
    "google": ("models/gemini-3.1-flash-live-preview", "Charon"),
    "aws": ("amazon.nova-2-sonic-v1:0", "matthew"),
    # Pinned, never xAI's moving `grok-voice-latest` (turncall#384).
    "xai": ("grok-voice-3.0", "eve"),
}
# Providers whose whole s2s block is sent, as the llm block is. Grok Voice
# takes its session settings in s2s.extra (reasoning, replace, resumption), and
# TurnCall refuses base_url, temperature and max_tokens on it with a 422 naming
# the field (turncall#387), which a save passes through. Dropping them here
# would run the agent without what its author set, saying nothing. The older
# providers keep the trimmed block: a field the builder never sent must not
# start failing their stored agents' saves.
_S2S_SENT_WHOLE = {"xai"}


def to_create_agent_request(
    cfg: dict[str, Any],
    tools_base_url: str | None = None,
    tools_secret: str | None = None,
    pipedream_target: dict[str, str] | None = None,
    registered_mcp_servers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Trimmed agent_config -> the body for POST /v1/agents.

    `registered_mcp_servers` are the Workspace MCP servers this agent opted
    into (`workspace_mcp_servers` ids, resolved and decrypted by the caller).

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
        # Everything set on the block passes through — sampling, failover,
        # base_url, and an agent's own api_key, which pins the agent to that
        # account over the Workspace Integration (ADR-0023). TurnCall
        # validates it; only provider and model get the builder's defaults.
        "llm": {
            **_set(llm),
            "provider": llm.get("provider", "openai"),
            "model": llm.get("model", "gpt-5.4-mini"),
        },
        "tts": _tts_block(tts),
        "tools": tools,
    }
    # STT as the console set it — provider, model, and anything from the
    # Advanced JSON (keyterms, failover); TurnCall validates it. It was never
    # sent, so every agent heard callers through Deepgram whatever the console
    # showed. An empty model is TurnCall's "the provider's own default".
    stt = _set(cfg.get("stt"))
    if stt:
        config["stt"] = stt
    config.update(_set({k: cfg.get(k) for k in _PASSTHROUGH}))
    # Speech-to-speech pipeline: a native audio model replaces STT→LLM→TTS. The
    # tts block above is left in place but ignored by TurnCall in s2s mode; the
    # voice comes from the s2s block.
    if cfg.get("pipeline_mode") == "s2s":
        s2s = cfg.get("s2s") or {}
        provider = s2s.get("provider", "openai")
        default_model, default_voice = _S2S_DEFAULTS.get(provider, _S2S_DEFAULTS["openai"])
        config["pipeline_mode"] = "s2s"
        config["s2s"] = {
            **(_set(s2s) if provider in _S2S_SENT_WHOLE else {}),
            "provider": provider,
            "model": s2s.get("model") or default_model,
            "voice": s2s.get("voice") or default_voice,
            # openai_live's delegate (turncall#243), set in the Advanced JSON.
            # Passed through untouched: TurnCall validates it and answers a 422.
            **({"delegation": s2s["delegation"]} if s2s.get("delegation") else {}),
        }

    # AWS region for the bedrock LLM / aws S2S providers. Region only — the
    # builder must never emit AWS credentials into an agent config: they belong
    # to the operator (server environment, or an assumed role), and the config is
    # not encrypted at rest. Omitted entirely when unset, so TurnCall falls back
    # to its own AWS_REGION.
    region = (cfg.get("aws") or {}).get("region")
    if region and (config.get("llm", {}).get("provider") == "bedrock"
                   or config.get("s2s", {}).get("provider") == "aws"):
        config["aws"] = {"region": region}

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

    # Ambience (turncall ADR-0021) passes through the same way: the console
    # sets it, TurnCall validates the sound against the project, and an absent
    # block means off. Without this an inline agent (call-init, ADR-0020) and
    # every update silently dropped it.
    if cfg.get("ambience"):
        config["ambience"] = cfg["ambience"]

    # MCP servers pass through the same way, and for the same reason: the
    # console sets them, not the interview (ADR-0014 left that open; this is
    # the answer). They are deliberately absent from the compose schema — an
    # LLM composing an agent from a conversation must not be inventing
    # outbound URLs, and a bearer token has no business being typed into a
    # chat transcript that is then stored and pushed to GitHub.
    #
    # stdio is refused: it runs a subprocess on the TurnCall host. TurnCall
    # gates it behind MCP_STDIO_ENABLED too, but a builder-authored config
    # should not be the thing asking.
    servers = [
        server
        for server in (cfg.get("mcp_servers") or [])
        if isinstance(server, dict) and server.get("transport") in ("http", "sse")
    ]
    # Attached catalog apps (ADR-0018) become further entries, after the raw
    # ones so a hand-written server always wins a name collision. They carry a
    # pinned tool_filter and no Authorization header — the access token lives an
    # hour and is minted per call through call-init.
    #
    # Dropped rather than half-emitted when there is no target: integrations
    # unconfigured, or the credential unreadable. A server the agent cannot
    # authenticate to is worse than no server — it costs a discovery timeout at
    # the start of every call.
    # Mid-call only (ADR-0021): an end-of-call-only attachment must not put a
    # tool in front of the model. Before that ADR every attachment did.
    # Registered Workspace servers come after the agent's own, for the same
    # reason: what the agent author typed wins a tool-name collision (#59).
    servers += list(registered_mcp_servers or [])
    apps = pipedream.mid_call_apps(cfg.get("integrations"))
    if apps and pipedream_target:
        servers += pipedream.mcp_servers_for(apps, **pipedream_target)
    if servers:
        config["mcp_servers"] = servers

    return {
        "name": cfg["name"],
        "environment": "development",
        "config": config,
    }
