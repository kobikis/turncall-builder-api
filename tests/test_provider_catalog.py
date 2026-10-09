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
