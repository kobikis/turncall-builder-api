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


def test_openai_offers_no_model_it_has_deprecated(monkeypatch):
    # OpenAI's list carries a shutdown_date on each model it has deprecated
    # (measured 2026-10-30): o4-mini's has passed and answers 404.
    listing = {"data": [
        {"id": "gpt-5.4-mini", "shutdown_date": None},
        {"id": "gpt-4o-mini", "shutdown_date": "2027-01-14"},
        {"id": "o4-mini", "shutdown_date": "2026-10-23"},
        {"id": "gpt-realtime-2", "shutdown_date": None},
        {"id": "gpt-4o-realtime-preview", "shutdown_date": "2026-10-23"},
    ]}
    _serving(monkeypatch, lambda r: httpx.Response(200, json=listing))
    assert asyncio.run(provider_catalog.llm_models("openai", {"openai": "k"})) == ["gpt-5.4-mini"]
    assert asyncio.run(provider_catalog.s2s_models("openai", {"openai": "k"})) == ["gpt-realtime-2"]


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


def test_xai_models_are_the_flagship_alone():
    """Curated: xAI's model list is not what the realtime endpoint takes, and
    TurnCall pins grok-voice-3.0 rather than the moving `grok-voice-latest`.
    The deprecated grok-voice-2.0 is not offered."""
    provider_catalog._CACHE.clear()
    assert asyncio.run(provider_catalog.s2s_models("xai")) == ["grok-voice-3.0"]


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


# --- xai in the cascade: LLM, STT, TTS (#122, turncall#385) -------------------

# xAI's GET /v1/models lists every model on the account, not only chat ones.
_XAI_MODELS = {"object": "list", "data": [
    {"id": "grok-4.20-non-reasoning", "object": "model"},
    {"id": "grok-4.20-reasoning", "object": "model"},
    {"id": "grok-code-fast-1", "object": "model"},
    {"id": "grok-imagine-image", "object": "model"},
    {"id": "grok-imagine-video", "object": "model"},
    {"id": "grok-2-image-1212", "object": "model"},
    {"id": "grok-voice-3.0", "object": "model"},
    {"id": "grok-voice-transcribe-2.0", "object": "model"},
]}


def test_xai_llm_models_are_the_accounts_chat_models(monkeypatch):
    seen = []
    _serving(monkeypatch, lambda r: seen.append(r) or httpx.Response(200, json=_XAI_MODELS))
    models = asyncio.run(provider_catalog.llm_models("xai", {"xai": "k"}))
    assert models == ["grok-4.20-non-reasoning", "grok-4.20-reasoning", "grok-code-fast-1"]
    assert str(seen[0].url) == "https://api.x.ai/v1/models"
    assert seen[0].headers["authorization"] == "Bearer k"


def test_xai_llm_models_without_a_key_are_turncalls_default(monkeypatch):
    seen = []
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    _serving(monkeypatch, lambda r: seen.append(r) or httpx.Response(200, json=_XAI_MODELS))
    assert asyncio.run(provider_catalog.llm_models("xai", {})) == ["grok-4.20-non-reasoning"]
    assert seen == []


def test_xai_llm_models_fall_back_when_xai_refuses_the_key_for_chat(monkeypatch):
    # A voice-only key is refused on the chat endpoints.
    _serving(monkeypatch, lambda r: httpx.Response(403, json={"error": "no access"}))
    assert asyncio.run(provider_catalog.llm_models("xai", {"xai": "voice-only"})) == ["grok-4.20-non-reasoning"]


def test_xai_llm_models_fall_back_when_none_is_a_chat_model(monkeypatch):
    _serving(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "grok-voice-3.0"}]}))
    assert asyncio.run(provider_catalog.llm_models("xai", {"xai": "k"})) == ["grok-4.20-non-reasoning"]


def test_xai_stt_model_is_turncalls_default_and_asks_nothing(monkeypatch):
    seen = []
    _serving(monkeypatch, lambda r: seen.append(r) or httpx.Response(200, json={}))
    assert asyncio.run(provider_catalog.stt_models("xai", {"xai": "k"})) == ["grok-voice-transcribe-2.0"]
    assert seen == []


def test_xai_tts_voices_are_the_s2s_voice_list(monkeypatch):
    seen = []
    _serving(monkeypatch, lambda r: seen.append(r) or httpx.Response(200, json=_XAI_VOICES))
    voices = asyncio.run(provider_catalog.tts_voices("xai", {"xai": "k"}))
    assert voices == asyncio.run(provider_catalog.s2s_voices("xai", {"xai": "k"}))
    assert voices[0] == {"value": "ara", "label": "Ara"}
    assert str(seen[0].url) == "https://api.x.ai/v1/tts/voices"
    assert seen[0].headers["authorization"] == "Bearer k"


def test_xai_tts_voices_without_a_key_ask_nothing(monkeypatch):
    seen = []
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    _serving(monkeypatch, lambda r: seen.append(r) or httpx.Response(200, json=_XAI_VOICES))
    assert asyncio.run(provider_catalog.tts_voices("xai", {})) == []
    assert seen == []


def test_xai_tts_has_no_model_field():
    # TurnCall's xai TTS is chosen by its voice alone.
    assert provider_catalog.tts_models("xai") == []
