"""Eleven Realtime, the elevenlabs S2S provider (#139, turncall#465)."""

import asyncio
import pathlib

from app import provider_catalog
from app.mapper import to_create_agent_request


def _s2s(block):
    return to_create_agent_request(
        {"name": "Bot", "system_prompt": "hi", "pipeline_mode": "s2s", "s2s": block}
    )["config"]["s2s"]


def test_defaults_are_elevenlabs_own():
    assert _s2s({"provider": "elevenlabs"}) == {
        "provider": "elevenlabs",
        "model": "eleven_realtime_v1_mini",
        "voice": "brian",
    }


def test_language_eagerness_and_temperature_reach_turncall():
    """TurnCall applies them, or answers the 422 naming the field (a bad
    eagerness, a base_url), which a save passes through."""
    block = {
        "provider": "elevenlabs",
        "model": "eleven_realtime_v1_max",
        "temperature": 0.4,
        "extra": {"language": "es", "eagerness": "low"},
        "base_url": "wss://gateway.example/v1/realtime",
    }
    s2s = _s2s(block)
    assert s2s["extra"] == {"language": "es", "eagerness": "low"}
    assert (s2s["model"], s2s["temperature"], s2s["base_url"]) == (
        "eleven_realtime_v1_max", 0.4, "wss://gateway.example/v1/realtime",
    )


def test_models_and_voices_are_offered():
    assert asyncio.run(provider_catalog._fetch_s2s_models(None, "elevenlabs", "")) == [
        "eleven_realtime_v1_mini",
        "eleven_realtime_v1_max",
    ]
    voices = asyncio.run(provider_catalog._fetch_s2s_voices(None, "elevenlabs", ""))
    assert {"value": "brian", "label": "brian"} in voices


def test_the_account_voices_follow_the_named_ones(monkeypatch):
    async def account(client, provider, key):
        assert (provider, key) == ("elevenlabs", "k")
        return [{"value": "JBFqnCBsd6RMkjVDRZzb", "label": "George (mine)"}]

    monkeypatch.setattr(provider_catalog, "_fetch_tts_voices", account)
    voices = asyncio.run(provider_catalog._fetch_s2s_voices(None, "elevenlabs", "k"))
    assert voices[0]["value"] == "brian"
    assert voices[-1] == {"value": "JBFqnCBsd6RMkjVDRZzb", "label": "George (mine)"}


def test_the_compose_schema_and_skill_know_it():
    from app.builder import _AGENT_CONFIG_SCHEMA, SKILLS_DIR

    s2s = _AGENT_CONFIG_SCHEMA["properties"]["s2s"]["properties"]
    assert "elevenlabs" in s2s["provider"]["enum"]
    assert "eleven_realtime_v1_mini" in s2s["model"]["description"]
    assert "brian" in s2s["voice"]["description"]
    skill = (pathlib.Path(SKILLS_DIR) / "speech-to-speech" / "SKILL.md").read_text()
    assert "eleven_realtime_v1_max" in skill


def test_it_runs_on_the_elevenlabs_account():
    from app.provider_integrations import accounts_used

    assert accounts_used({"pipeline_mode": "s2s", "s2s": {"provider": "elevenlabs"}}) == {"elevenlabs"}
