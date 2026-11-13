"""Mapper tests — trimmed config -> CreateAgentRequest."""

import pytest

from app.mapper import to_create_agent_request


def test_defaults_applied_when_omitted():
    req = to_create_agent_request({"name": "Bot", "system_prompt": "hi"})
    cfg = req["config"]
    assert req["name"] == "Bot"
    assert req["environment"] == "development"
    assert cfg["llm"] == {"provider": "openai", "model": "gpt-5.4-mini"}
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
    oa = to_create_agent_request(
        {"name": "B", "system_prompt": "h", "tts": {"provider": "openai", "voice": "alloy"}}
    )["config"]["tts"]
    assert oa["model"] == "tts-1"
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


def test_do_not_call_is_a_builtin_with_its_own_description():
    req = to_create_agent_request(
        {"name": "Bot", "system_prompt": "hi", "tools": ["do_not_call"]}
    )
    tool = req["config"]["tools"][0]
    assert tool["name"] == "do_not_call"
    # Not the name echoed back: the model has to know when to use it.
    assert tool["description"] != "do_not_call"
    assert "webhook_url" not in tool


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
        "model": "gpt-realtime-2.1",
        "voice": "alloy",
    }


def test_s2s_openai_live_defaults():
    """gpt-live-1 is full duplex and speaks its own protocol, so it is its own
    provider rather than another model under `openai`."""
    cfg = to_create_agent_request(
        {
            "name": "Bot",
            "system_prompt": "hi",
            "pipeline_mode": "s2s",
            "s2s": {"provider": "openai_live"},
        }
    )["config"]
    assert cfg["s2s"] == {
        "provider": "openai_live",
        "model": "gpt-live-1",
        "voice": "alloy",
    }


def test_s2s_openai_live_respects_explicit_model():
    cfg = to_create_agent_request(
        {
            "name": "Bot",
            "system_prompt": "hi",
            "pipeline_mode": "s2s",
            "s2s": {"provider": "openai_live", "model": "gpt-live-1-mini", "voice": "sage"},
        }
    )["config"]
    assert cfg["s2s"]["model"] == "gpt-live-1-mini"
    assert cfg["s2s"]["voice"] == "sage"


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


def test_compose_schema_allows_openai_live():
    """The compose tool's enum is the hard gate: a provider missing from it
    cannot be emitted by the model at all, however well the skill describes it."""
    from app.builder import _AGENT_CONFIG_SCHEMA

    enum = _AGENT_CONFIG_SCHEMA["properties"]["s2s"]["properties"]["provider"]["enum"]
    assert "openai_live" in enum
    assert enum == ["openai", "openai_live", "google", "aws", "xai", "elevenlabs"]


def test_providers_endpoint_offers_openai_live():
    """The config form's S2S dropdown is fed by this static list, so a provider
    missing here is invisible in the UI even when everything else knows it."""
    from app.routers.sessions import _S2S_PROVIDERS

    assert "openai_live" in _S2S_PROVIDERS
    assert "xai" in _S2S_PROVIDERS


def _with_servers(servers):
    return to_create_agent_request(
        {"name": "Bot", "system_prompt": "hi", "mcp_servers": servers}
    )["config"]


class TestMCPServers:
    """ADR-0014 left "exposing mcp_servers in the Console" open. This is the
    answer: it reaches TurnCall, but only from the console — never from the
    interview."""

    def test_http_and_sse_servers_reach_turncall(self):
        servers = [
            {"name": "crm", "transport": "http", "url": "https://mcp.acme.com/mcp"},
            {"name": "docs", "transport": "sse", "url": "https://mcp.acme.com/sse"},
        ]

        assert _with_servers(servers)["mcp_servers"] == servers

    def test_headers_survive_because_that_is_how_the_feature_authenticates(self):
        servers = [
            {
                "name": "crm",
                "transport": "http",
                "url": "https://mcp.acme.com/mcp",
                "headers": {"Authorization": "Bearer t"},
            }
        ]

        assert _with_servers(servers)[
            "mcp_servers"
        ][0]["headers"] == {"Authorization": "Bearer t"}

    def test_stdio_is_refused(self):
        """It runs a subprocess on the TurnCall host. TurnCall gates that behind
        MCP_STDIO_ENABLED, but a builder-authored config should not be the thing
        asking for it."""
        config = _with_servers(
            [{"name": "local", "transport": "stdio", "command": "python"}]
        )

        assert "mcp_servers" not in config

    def test_a_stdio_server_does_not_take_the_others_with_it(self):
        config = _with_servers(
            [
                {"name": "local", "transport": "stdio", "command": "python"},
                {"name": "crm", "transport": "http", "url": "https://mcp.acme.com/mcp"},
            ]
        )

        assert [s["name"] for s in config["mcp_servers"]] == ["crm"]

    def test_nothing_configured_means_no_key_at_all(self):
        """An empty list must not overwrite an agent's existing servers on
        update — the same reason analysis is only emitted when present."""
        assert "mcp_servers" not in _with_servers([])
        assert "mcp_servers" not in to_create_agent_request(
            {"name": "Bot", "system_prompt": "hi"}
        )["config"]

    def test_junk_is_dropped_rather_than_forwarded(self):
        assert "mcp_servers" not in _with_servers(["not-a-server", None, 42])


class TestAmbience:
    def test_passes_through_untouched(self):
        ambience = {"sound": "290679e3-6630-49c7-90b1-b9186bf177e0", "volume": 0.2}

        config = to_create_agent_request(
            {"name": "Bot", "system_prompt": "hi", "ambience": ambience}
        )["config"]

        assert config["ambience"] == ambience

    def test_absent_means_no_key_at_all(self):
        """Absence is TurnCall's "off"; an emitted null would be a 422."""
        assert "ambience" not in to_create_agent_request(
            {"name": "Bot", "system_prompt": "hi"}
        )["config"]
        assert "ambience" not in to_create_agent_request(
            {"name": "Bot", "system_prompt": "hi", "ambience": None}
        )["config"]


def test_the_interview_cannot_compose_an_mcp_server():
    """Not in the compose schema, deliberately: an LLM building an agent from a
    conversation must not invent outbound URLs, and a bearer token has no
    business in a chat transcript that gets stored and pushed to GitHub."""
    import json

    from app.builder import COMPOSE_TOOL

    assert "mcp_servers" not in json.dumps(COMPOSE_TOOL)


class TestAttachedApps:
    """Catalog apps attached to an Agent become mcp_servers entries (ADR-0018).

    Pipedream serves every app from one MCP URL and routes by header, so two
    attached apps are two entries sharing a URL and differing by app slug.
    """

    TARGET = {
        "project_id": "proj_1",
        "environment": "development",
        "external_user_id": "ws-1",
    }

    def _config(self, apps, **over):
        cfg = {"name": "Bot", "system_prompt": "hi", "integrations": apps, **over}
        return to_create_agent_request(cfg, pipedream_target=self.TARGET)["config"]

    def test_two_apps_share_a_url_and_differ_by_app_slug(self):
        servers = self._config(["slack_v2", "google_calendar"])["mcp_servers"]
        assert len({s["url"] for s in servers}) == 1
        assert [s["headers"]["x-pd-app-slug"] for s in servers] == [
            "slack_v2",
            "google_calendar",
        ]

    def test_the_tenant_and_project_ride_in_the_headers(self):
        headers = self._config(["slack_v2"])["mcp_servers"][0]["headers"]
        assert headers["x-pd-project-id"] == "proj_1"
        assert headers["x-pd-environment"] == "development"
        assert headers["x-pd-external-user-id"] == "ws-1"

    def test_no_credential_rides_in_the_config(self):
        """The access token lives an hour — it is minted per call (#27), never
        stored in an agent config."""
        headers = self._config(["slack_v2"])["mcp_servers"][0]["headers"]
        assert not any(h.lower() == "authorization" for h in headers)

    def test_every_entry_carries_a_pinned_tool_filter(self):
        for server in self._config(["slack_v2", "google_calendar"])["mcp_servers"]:
            assert server["tool_filter"], f"{server['name']} has no tool_filter"
            # Pipedream names a tool for its app — a filter naming another app's
            # tools would silently match nothing.
            slug = server["headers"]["x-pd-app-slug"]
            assert all(t.startswith(f"{slug}-") for t in server["tool_filter"])

    def test_an_app_with_no_pinned_filter_is_not_sent(self, monkeypatch):
        """Unfiltered, Slack alone lands on TurnCall's 50-tools-per-server cap."""
        from app import pipedream

        monkeypatch.setitem(
            pipedream.CATALOG,
            "slack_v2",
            pipedream.CatalogApp(slug="slack_v2", name="Slack", tools=()),
        )
        assert "mcp_servers" not in self._config(["slack_v2"])

    def test_without_a_target_they_are_dropped_not_half_emitted(self):
        """A server the agent cannot authenticate to costs a discovery timeout at
        the start of every call — worse than no server."""
        config = to_create_agent_request(
            {"name": "Bot", "system_prompt": "hi", "integrations": ["slack_v2"]}
        )["config"]
        assert "mcp_servers" not in config

    def test_hand_written_servers_are_unaffected_and_come_first(self):
        raw = {"name": "crm", "transport": "http", "url": "https://mcp.acme.com/mcp"}
        servers = self._config(["slack_v2"], mcp_servers=[raw])["mcp_servers"]
        assert servers[0] == raw
        assert servers[1]["name"] == "pipedream_slack_v2"

    def test_stdio_is_still_refused_alongside_attached_apps(self):
        servers = self._config(
            ["slack_v2"],
            mcp_servers=[{"name": "local", "transport": "stdio", "command": "python"}],
        )["mcp_servers"]
        assert [s["name"] for s in servers] == ["pipedream_slack_v2"]

    def test_an_agent_with_no_attached_apps_is_untouched(self):
        assert to_create_agent_request(
            {"name": "Bot", "system_prompt": "hi"}, pipedream_target=self.TARGET
        ) == to_create_agent_request({"name": "Bot", "system_prompt": "hi"})

    def test_an_unknown_app_does_not_take_the_rest_of_the_list_with_it(self):
        servers = self._config(["slack_v2", "dropbox"])["mcp_servers"]
        assert [s["headers"]["x-pd-app-slug"] for s in servers] == ["slack_v2"]


def test_s2s_delegation_reaches_turncall():
    """gpt-live-1 calls no tools itself; its delegate does (turncall#243). The
    block is set in the Advanced JSON editor and was dropped here, so an
    openai_live agent built in the console could never use its tools."""
    delegation = {"mode": "responses", "model": "gpt-6-astra", "timeout_seconds": 45}
    cfg = to_create_agent_request(
        {
            "name": "Bot",
            "system_prompt": "hi",
            "pipeline_mode": "s2s",
            "s2s": {"provider": "openai_live", "delegation": delegation},
        }
    )["config"]
    assert cfg["s2s"]["delegation"] == delegation


def test_s2s_without_delegation_emits_none():
    cfg = to_create_agent_request(
        {"name": "Bot", "system_prompt": "hi", "pipeline_mode": "s2s"}
    )["config"]
    assert "delegation" not in cfg["s2s"]


class TestStt:
    def test_the_consoles_choice_reaches_turncall(self):
        """It was never sent: every agent heard callers through Deepgram."""
        stt = {"provider": "cartesia", "model": "ink-whisper", "keyterms": ["Acme"]}

        config = to_create_agent_request(
            {"name": "Bot", "system_prompt": "hi", "stt": stt}
        )["config"]

        assert config["stt"] == stt

    def test_an_empty_model_is_the_providers_default(self):
        config = to_create_agent_request(
            {"name": "Bot", "system_prompt": "hi", "stt": {"provider": "openai", "model": ""}}
        )["config"]

        assert config["stt"] == {"provider": "openai"}

    def test_absent_means_no_key_at_all(self):
        assert "stt" not in to_create_agent_request(
            {"name": "Bot", "system_prompt": "hi"}
        )["config"]


def _config(**fields):
    return to_create_agent_request({"name": "Bot", "system_prompt": "hi", **fields})["config"]


class TestAgentSettingsReachTurnCall:
    """#71: the mapper is an allowlist, so everything off it was dropped —
    even when set in the Advanced JSON."""

    def test_llm_sampling_failover_and_endpoint_pass_through(self):
        llm = {
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "temperature": 0,
            "max_tokens": 300,
            "reasoning_effort": "low",
            "base_url": "https://gateway.example.com/v1",
            "failover": [{"provider": "openai", "model": "gpt-4o-mini"}],
        }

        assert _config(llm=llm)["llm"] == llm

    def test_llm_keeps_its_defaults(self):
        assert _config(llm={"temperature": 0.2})["llm"] == {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "temperature": 0.2,
        }

    def test_tts_pronunciations_and_failover_pass_through(self):
        tts = {
            "provider": "cartesia",
            "voice": "uuid",
            "pronunciations": {"Metformin": "mɛtˈfɔɹmɪn"},
            "failover": [{"provider": "deepgram", "voice": "aura-2-helena-en"}],
        }

        assert _config(tts=tts)["tts"] == {**tts, "model": "sonic-3"}

    def test_turn_taking_and_call_guards_pass_through(self):
        settings = {
            "language": "es",
            "smart_turn_detection": False,
            "smart_turn_stop_secs": 0.5,
            "silence_timeout_ms": 600,
            "interruption_enabled": False,
            "greeting_interruptible": False,
            "user_idle_timeout_ms": 0,
            "idle_message": "¿Sigue ahí?",
            "max_call_duration_seconds": 300,
            "avatar": {"enabled": True, "provider": "tavus", "replica_id": "r1"},
        }

        config = _config(**settings)

        assert {k: config[k] for k in settings} == settings

    def test_unset_means_no_key_at_all(self):
        """An absent key is TurnCall's default; a null or an empty string is not."""
        config = _config(
            language="", idle_message=None, smart_turn_detection=None, avatar=None
        )

        for key in ("language", "idle_message", "smart_turn_detection", "avatar"):
            assert key not in config
        assert "temperature" not in _config(llm={"temperature": None})["llm"]


class TestGrokVoice:
    """The xai S2S provider (#120, turncall#384)."""

    @staticmethod
    def _s2s(block):
        return to_create_agent_request(
            {"name": "Bot", "system_prompt": "hi", "pipeline_mode": "s2s", "s2s": block}
        )["config"]["s2s"]

    def test_defaults_are_groks_not_openais(self):
        """TurnCall swaps OpenAI's sentinels too, but the builder names the
        model it runs: grok-voice-3.0, pinned rather than xAI's moving alias."""
        assert self._s2s({"provider": "xai"}) == {
            "provider": "xai",
            "model": "grok-voice-3.0",
            "voice": "eve",
        }

    def test_an_explicit_model_and_voice_are_kept(self):
        s2s = self._s2s({"provider": "xai", "model": "grok-voice-2.0", "voice": "my-cloned-voice"})
        assert (s2s["model"], s2s["voice"]) == ("grok-voice-2.0", "my-cloned-voice")

    def test_its_session_extras_reach_turncall(self):
        """reasoning, replace and resumption are Grok's own session settings,
        set in the Advanced JSON; TurnCall applies them or refuses a bad value."""
        extra = {
            "reasoning": {"effort": "none"},
            "replace": {"turn call": "TurnCall"},
            "resumption": {"enabled": True},
        }
        assert self._s2s({"provider": "xai", "extra": extra})["extra"] == extra

    @pytest.mark.parametrize(
        ("field", "value"),
        [("base_url", "wss://gateway.example/v1/realtime"), ("temperature", 0.3), ("max_tokens", 400)],
    )
    def test_what_grok_lacks_reaches_turncall_to_be_refused(self, field, value):
        """Dropping it would run the agent without what its author asked for.
        Sent, TurnCall answers the 422 naming the field, and the save passes
        that status and reason through."""
        assert self._s2s({"provider": "xai", field: value})[field] == value

    def test_unset_fields_are_left_out(self):
        s2s = self._s2s({"provider": "xai", "temperature": None, "base_url": "", "extra": None})
        assert set(s2s) == {"provider", "model", "voice"}

    def test_other_providers_keep_their_trimmed_block(self):
        """Unchanged for the providers stored agents already use: a field the
        builder never sent must not start failing their saves."""
        assert "temperature" not in self._s2s({"provider": "google", "temperature": 0.3})

    def test_the_compose_schema_and_skill_know_grok(self):
        import pathlib

        from app.builder import _AGENT_CONFIG_SCHEMA, SKILLS_DIR

        s2s = _AGENT_CONFIG_SCHEMA["properties"]["s2s"]["properties"]
        assert "grok-voice-3.0" in s2s["model"]["description"]
        assert "eve" in s2s["voice"]["description"]
        skill = (pathlib.Path(SKILLS_DIR) / "speech-to-speech" / "SKILL.md").read_text()
        assert "grok-voice-3.0" in skill
        assert "xai" in skill
