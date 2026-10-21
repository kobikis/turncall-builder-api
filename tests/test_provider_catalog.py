"""Model lists for the providers TurnCall added after the catalog (#72)."""

from __future__ import annotations

import asyncio

import httpx

from app import provider_catalog


def _serving(monkeypatch, handler):
    provider_catalog._CACHE.clear()
    real = httpx.AsyncClient
    monkeypatch.setattr(
        provider_catalog.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )


def test_assemblyai_streaming_models_are_a_fixed_list():
    models = asyncio.run(provider_catalog.stt_models("assemblyai"))
    assert "universal-streaming-english" in models
    assert "universal-streaming-multilingual" in models


def test_deepgram_lists_flux_beside_its_fetched_models(monkeypatch):
    # Deepgram's /v1/models does not list Flux (measured 2026-10-09).
    _serving(monkeypatch, lambda r: httpx.Response(200, json={"stt": [{"canonical_name": "nova-3-general"}]}))
    models = asyncio.run(provider_catalog.stt_models("deepgram", {"deepgram": "k"}))
    assert {"nova-3-general", "flux-general-en", "flux-general-multi"} <= set(models)


def test_google_llm_models_are_the_ones_that_generate_content(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"models": [
            {"name": "models/gemini-3.6-flash", "supportedGenerationMethods": ["generateContent", "countTokens"]},
            {"name": "models/gemini-3.8-live", "supportedGenerationMethods": ["bidiGenerateContent"]},
            {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
            {"name": "models/gemini-3.6-flash-preview-tts", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.6-flash-image", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemma-3-27b-it", "supportedGenerationMethods": ["generateContent"]},
        ]})

    _serving(monkeypatch, handler)
    models = asyncio.run(provider_catalog.llm_models("google", {"google": "k"}))
    assert models == ["gemini-3.6-flash"]
    assert seen[0].headers["x-goog-api-key"] == "k"


def test_flux_survives_a_failed_deepgram_fetch(monkeypatch):
    _serving(monkeypatch, lambda r: httpx.Response(401))
    models = asyncio.run(provider_catalog.stt_models("deepgram", {"deepgram": "bad"}))
    assert models == ["flux-general-en", "flux-general-multi"]


def test_google_without_a_key_lists_nothing(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    provider_catalog._CACHE.clear()
    assert asyncio.run(provider_catalog.llm_models("google", {})) == []


# --- Grok Voice, the xai S2S provider (#120, turncall#384) --------------------

# xAI's GET /v1/tts/voices, as it answered on 2026-10-21 (trimmed).
_XAI_VOICES = {"voices": [
    {"voice_id": "eve", "name": "Eve", "language": "multilingual", "gender": "female"},
    {"voice_id": "ara", "name": "Ara", "language": "multilingual", "gender": "female"},
    {"voice_id": "rex", "name": "Rex", "language": "multilingual", "gender": "male"},
]}


def test_xai_models_are_the_flagship_then_the_deprecated_one():
    """Curated: xAI's model list is not what the realtime endpoint takes, and
    TurnCall pins grok-voice-3.0 rather than the moving `grok-voice-latest`."""
    provider_catalog._CACHE.clear()
    assert asyncio.run(provider_catalog.s2s_models("xai")) == ["grok-voice-3.0", "grok-voice-2.0"]
    assert provider_catalog.deprecated_s2s_models("xai") == ["grok-voice-2.0"]
    assert provider_catalog.deprecated_s2s_models("openai") == []


def test_xai_voices_are_the_accounts_own_listed_with_its_key(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_XAI_VOICES)

    _serving(monkeypatch, handler)
    voices = asyncio.run(provider_catalog.s2s_voices("xai", {"xai": "k"}))
    assert voices == [
        {"value": "ara", "label": "Ara"},
        {"value": "eve", "label": "Eve"},
        {"value": "rex", "label": "Rex"},
    ]
    assert str(seen[0].url) == "https://api.x.ai/v1/tts/voices"
    assert seen[0].headers["authorization"] == "Bearer k"


def test_xai_voices_fall_back_to_the_builders_own_key(monkeypatch):
    seen = []
    monkeypatch.setenv("XAI_API_KEY", "env-key")
    _serving(monkeypatch, lambda r: seen.append(r) or httpx.Response(200, json=_XAI_VOICES))
    assert asyncio.run(provider_catalog.s2s_voices("xai", {}))
    assert seen[0].headers["authorization"] == "Bearer env-key"


def test_xai_voices_without_a_key_ask_nothing(monkeypatch):
    seen = []
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    _serving(monkeypatch, lambda r: seen.append(r) or httpx.Response(200, json=_XAI_VOICES))
    assert asyncio.run(provider_catalog.s2s_voices("xai", {})) == []
    assert seen == []


def test_xai_voices_are_empty_when_xai_refuses(monkeypatch):
    # A wrong key is a 400 on xAI, not a 401 (measured).
    _serving(monkeypatch, lambda r: httpx.Response(400, json={"error": "Incorrect API key provided."}))
    assert asyncio.run(provider_catalog.s2s_voices("xai", {"xai": "bad"})) == []


def test_xai_voices_are_cached_per_key(monkeypatch):
    seen = []
    _serving(monkeypatch, lambda r: seen.append(r) or httpx.Response(200, json=_XAI_VOICES))
    asyncio.run(provider_catalog.s2s_voices("xai", {"xai": "k"}))
    asyncio.run(provider_catalog.s2s_voices("xai", {"xai": "k"}))
    assert len(seen) == 1
    asyncio.run(provider_catalog.s2s_voices("xai", {"xai": "another"}))
    assert len(seen) == 2
