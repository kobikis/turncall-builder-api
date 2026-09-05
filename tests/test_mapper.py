"""Mapper tests — trimmed config -> CreateAgentRequest."""

import pytest

from app.mapper import to_create_agent_request


def test_defaults_applied_when_omitted():
    req = to_create_agent_request({"name": "Bot", "system_prompt": "hi"})
    cfg = req["config"]
    assert req["name"] == "Bot"
    assert req["environment"] == "development"
    assert cfg["llm"] == {"provider": "openai", "model": "gpt-4o-mini"}
    assert cfg["tts"] == {"provider": "deepgram", "voice": "aura-2-helena-en"}
    assert cfg["tools"] == []


def test_tts_model_defaulted_per_provider():
    # ElevenLabs/Cartesia get a provider model so TurnCall doesn't leak the
    # Deepgram schema default and 403 on an invalid model.
    el = to_create_agent_request(
        {"name": "B", "system_prompt": "h", "tts": {"provider": "elevenlabs", "voice": "v1"}}
    )["config"]["tts"]
    assert el == {"provider": "elevenlabs", "voice": "v1", "model": "eleven_turbo_v2_5"}
    ca = to_create_agent_request(
        {"name": "B", "system_prompt": "h", "tts": {"provider": "cartesia", "voice": "uuid"}}
    )["config"]["tts"]
    assert ca["model"] == "sonic-3"
    # explicit model wins
    ex = to_create_agent_request(
        {"name": "B", "system_prompt": "h", "tts": {"provider": "elevenlabs", "voice": "v", "model": "eleven_v3"}}
    )["config"]["tts"]
    assert ex["model"] == "eleven_v3"


def test_builtin_tools_become_full_definitions():
    req = to_create_agent_request(
        {"name": "Bot", "system_prompt": "hi", "tools": ["end_call"]}
    )
    tool = req["config"]["tools"][0]
    assert tool["name"] == "end_call"
    assert tool["description"]  # non-empty (ToolDefinitionSchema requires it)
    assert "webhook_url" not in tool  # built-in, not a webhook tool


def test_name_required():
    with pytest.raises(ValueError):
        to_create_agent_request({"system_prompt": "hi"})


def test_cascade_has_no_s2s():
    cfg = to_create_agent_request({"name": "Bot", "system_prompt": "hi"})["config"]
    assert "pipeline_mode" not in cfg
    assert "s2s" not in cfg


def test_s2s_pipeline_mapped():
    req = to_create_agent_request(
        {
            "name": "Bot",
            "system_prompt": "hi",
            "pipeline_mode": "s2s",
            "s2s": {"provider": "google", "model": "models/gemini-3.1-flash-live-preview", "voice": "Kore"},
        }
    )
    cfg = req["config"]
    assert cfg["pipeline_mode"] == "s2s"
    assert cfg["s2s"] == {
        "provider": "google",
        "model": "models/gemini-3.1-flash-live-preview",
        "voice": "Kore",
    }


def test_s2s_defaults_to_openai_realtime():
    cfg = to_create_agent_request(
        {"name": "Bot", "system_prompt": "hi", "pipeline_mode": "s2s"}
    )["config"]
    assert cfg["s2s"] == {
        "provider": "openai",
        "model": "gpt-4o-realtime-preview",
        "voice": "alloy",
    }


def test_voicemail_mapped_when_enabled():
    cfg = to_create_agent_request(
        {
            "name": "Dialer",
            "system_prompt": "hi",
            "voicemail_detection": {"enabled": True, "voicemail_message": "Call us back."},
        }
    )["config"]
    assert cfg["voicemail_detection"] == {
        "enabled": True,
        "voicemail_message": "Call us back.",
    }


def test_voicemail_omitted_when_disabled_or_absent():
    cfg = to_create_agent_request({"name": "Bot", "system_prompt": "hi"})["config"]
    assert "voicemail_detection" not in cfg
    cfg2 = to_create_agent_request(
        {"name": "Bot", "system_prompt": "hi", "voicemail_detection": {"enabled": False}}
    )["config"]
    assert "voicemail_detection" not in cfg2


def test_guardrails_prohibited_topics_mapped():
    cfg = to_create_agent_request(
        {
            "name": "Bot",
            "system_prompt": "hi",
            "guardrails": {"prohibited_topics": ["medical advice", "  competitor pricing  "]},
        }
    )["config"]
    assert cfg["guardrails"] == {
        "prohibited_topics": ["medical advice", "competitor pricing"]
    }


def test_guardrails_omitted_when_empty():
    cfg = to_create_agent_request(
        {"name": "Bot", "system_prompt": "hi", "guardrails": {"prohibited_topics": ["", "  "]}}
    )["config"]
    assert "guardrails" not in cfg
    cfg2 = to_create_agent_request({"name": "Bot", "system_prompt": "hi"})["config"]
    assert "guardrails" not in cfg2


def test_voicemail_dropped_in_s2s_mode():
    # Mutually exclusive with s2s (TurnCall rejects the combo) — s2s wins.
    cfg = to_create_agent_request(
        {
            "name": "Bot",
            "system_prompt": "hi",
            "pipeline_mode": "s2s",
            "voicemail_detection": {"enabled": True, "voicemail_message": "x"},
        }
    )["config"]
    assert cfg["pipeline_mode"] == "s2s"
    assert "voicemail_detection" not in cfg


def test_tool_routing_precedence():
    """ADR-0010: tool server_url (verbatim) > agent server_url (base) > backend."""
    from app.mapper import external_tool_names, to_create_agent_request

    cfg = {
        "name": "X",
        "server_url": "https://api.acme.com/",
        "custom_tools": [
            {"name": "book", "description": "b"},
            {"name": "cancel", "description": "c", "server_url": "https://other.io/hooks/cancel"},
        ],
    }
    req = to_create_agent_request(cfg, tools_base_url="http://backend:9001", tools_secret="s" * 16)
    tools = {t["name"]: t for t in req["config"]["tools"]}
    assert tools["book"]["webhook_url"] == "https://api.acme.com/tools/book"
    assert tools["cancel"]["webhook_url"] == "https://other.io/hooks/cancel"
    assert tools["book"]["webhook_secret"] == "s" * 16  # external calls still signed
    assert external_tool_names(cfg, "http://backend:9001") == {"book", "cancel"}


def test_tool_routing_defaults_to_generated_backend():
    from app.mapper import external_tool_names, to_create_agent_request

    cfg = {"name": "X", "custom_tools": [{"name": "book", "description": "b"}]}
    req = to_create_agent_request(cfg, tools_base_url="http://backend:9001")
    tool = req["config"]["tools"][0]
    assert tool["webhook_url"] == "http://backend:9001/tools/book"
    assert external_tool_names(cfg, "http://backend:9001") == set()


def test_backend_own_url_is_not_external():
    """Normalization fills fields with the backend's own URL — that must not
    flip tools to external (externality = resolved destination, not presence)."""
    from app.mapper import external_tool_names

    cfg = {
        "name": "X",
        "server_url": "http://backend:9001",
        "custom_tools": [
            {"name": "book", "description": "b", "server_url": "http://backend:9001/tools/book"},
        ],
    }
    assert external_tool_names(cfg, "http://backend:9001") == set()


# --- AWS Bedrock / Nova Sonic -------------------------------------------------


def test_bedrock_llm_passes_through_with_region():
    cfg = to_create_agent_request(
        {
            "name": "Bot",
            "system_prompt": "hi",
            "llm": {"provider": "bedrock", "model": "anthropic.claude-3-5-sonnet-20241022-v2:0"},
            "aws": {"region": "eu-west-1"},
        }
    )["config"]
    assert cfg["llm"] == {
        "provider": "bedrock",
        "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    }
    assert cfg["aws"] == {"region": "eu-west-1"}


def test_nova_sonic_gets_its_own_defaults():
    # The OpenAI fallbacks are wrong here — Nova Sonic would reject "alloy".
    cfg = to_create_agent_request(
        {
            "name": "Bot",
            "system_prompt": "hi",
            "pipeline_mode": "s2s",
            "s2s": {"provider": "aws"},
            "aws": {"region": "us-west-2"},
        }
    )["config"]
    assert cfg["s2s"] == {
        "provider": "aws",
        "model": "amazon.nova-2-sonic-v1:0",
        "voice": "matthew",
    }
    assert cfg["aws"] == {"region": "us-west-2"}


def test_gemini_gets_its_own_defaults():
    # Regression: a bare google s2s block used to inherit OpenAI's realtime
    # model id and voice, which Gemini rejects.
    cfg = to_create_agent_request(
        {"name": "Bot", "system_prompt": "hi", "pipeline_mode": "s2s", "s2s": {"provider": "google"}}
    )["config"]
    assert cfg["s2s"]["model"] == "models/gemini-3.1-flash-live-preview"
    assert cfg["s2s"]["voice"] == "Charon"


def test_aws_block_dropped_when_no_aws_provider_is_used():
    # A stray region on an OpenAI agent is noise TurnCall would ignore anyway.
    cfg = to_create_agent_request(
        {"name": "Bot", "system_prompt": "hi", "aws": {"region": "us-east-1"}}
    )["config"]
    assert "aws" not in cfg


def test_aws_credentials_are_never_emitted():
    """Credentials belong to the operator; a generated config must not carry
    them, and TurnCall stores agent config unencrypted."""
    cfg = to_create_agent_request(
        {
            "name": "Bot",
            "system_prompt": "hi",
            "llm": {"provider": "bedrock", "model": "amazon.nova-pro-v1:0"},
            "aws": {
                "region": "us-east-1",
                "access_key_id": "AKIALEAK",
                "secret_access_key": "leaked",
                "role_arn": "arn:aws:iam::1:role/x",
            },
        }
    )["config"]
    assert cfg["aws"] == {"region": "us-east-1"}
