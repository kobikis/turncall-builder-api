"""Validating a key on save (#56): what was checked, never "it works"."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app import provider_catalog


def _answering(monkeypatch, status=None, raise_exc=None):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if raise_exc:
            raise raise_exc
        return httpx.Response(status)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        provider_catalog.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    return seen


@pytest.mark.parametrize(
    ("status", "expected"), [(200, "valid"), (401, "rejected"), (403, "rejected"), (500, "unverified")]
)
def test_outcome_follows_the_list_endpoint(monkeypatch, status, expected):
    seen = _answering(monkeypatch, status)
    result = asyncio.run(provider_catalog.probe("deepgram", {"api_key": "k"}))
    assert result["status"] == expected
    assert result["checked"] == "GET https://api.deepgram.com/v1/projects"
    assert "works" not in result["detail"]
    assert seen[0].headers["authorization"] == "Token k"


def test_unreachable_is_unverified_not_rejected(monkeypatch):
    _answering(monkeypatch, raise_exc=httpx.ConnectError("down"))
    result = asyncio.run(provider_catalog.probe("openai", {"api_key": "k"}))
    assert result["status"] == "unverified"


def test_a_provider_with_nothing_to_ask_is_unchecked():
    assert asyncio.run(provider_catalog.probe("aws", {"access_key_id": "A"}))["status"] == "unchecked"
    assert asyncio.run(provider_catalog.probe("heygen", {"api_key": "k"}))["status"] == "unchecked"


def test_assemblyai_key_is_checked_against_its_transcript_list(monkeypatch):
    seen = _answering(monkeypatch, 200)
    result = asyncio.run(provider_catalog.probe("assemblyai", {"api_key": "k"}))
    assert result["status"] == "valid"
    assert seen[0].url.host == "api.assemblyai.com"
    assert seen[0].headers["authorization"] == "k"


def test_twilio_numbers_are_listed_with_basic_auth(monkeypatch):
    from app import twilio_api

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"incoming_phone_numbers": [
            {"sid": "PN1", "phone_number": "+15550001111", "friendly_name": "Main"}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(twilio_api.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    numbers = asyncio.run(twilio_api.list_numbers("AC1", "tok"))
    assert numbers == [{"sid": "PN1", "phone_number": "+15550001111", "friendly_name": "Main"}]
    assert seen[0].url.path.endswith("/Accounts/AC1/IncomingPhoneNumbers.json")
    assert seen[0].headers["authorization"].startswith("Basic ")


def test_twilio_rejection_is_named(monkeypatch):
    from app import twilio_api

    real = httpx.AsyncClient
    monkeypatch.setattr(twilio_api.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(401)), **kw))
    with pytest.raises(twilio_api.TwilioRejected):
        asyncio.run(twilio_api.list_numbers("AC1", "bad"))


def _xai_answering(monkeypatch, response):
    seen = []
    real = httpx.AsyncClient
    monkeypatch.setattr(
        provider_catalog.httpx,
        "AsyncClient",
        lambda **kw: real(
            transport=httpx.MockTransport(lambda r: seen.append(r) or response), **kw
        ),
    )
    return seen


def test_xai_key_is_checked_against_its_key_endpoint(monkeypatch):
    seen = _xai_answering(monkeypatch, httpx.Response(200, json={"api_key_blocked": False}))
    result = asyncio.run(provider_catalog.probe("xai", {"api_key": "k"}))
    assert result["status"] == "valid"
    assert result["checked"] == "GET https://api.x.ai/v1/api-key"
    assert seen[0].headers["authorization"] == "Bearer k"


def test_xai_reads_its_400_for_a_wrong_key_as_a_rejection(monkeypatch):
    """xAI answers a wrong key with 400 and this body, not a 401 (measured)."""
    _xai_answering(monkeypatch, httpx.Response(400, json={
        "code": "invalid-argument",
        "error": "Incorrect API key provided. You can obtain an API key from https://console.x.ai.",
    }))
    result = asyncio.run(provider_catalog.probe("xai", {"api_key": "bad"}))
    assert result["status"] == "rejected"
    assert result["detail"] == "xai answered 400: the key is incorrect"


def test_another_400_from_xai_stays_unverified(monkeypatch):
    _xai_answering(monkeypatch, httpx.Response(400, json={"error": "something else"}))
    assert asyncio.run(provider_catalog.probe("xai", {"api_key": "k"}))["status"] == "unverified"


def test_a_400_elsewhere_is_not_read_as_xais(monkeypatch):
    _xai_answering(monkeypatch, httpx.Response(400, json={"error": "Incorrect API key provided."}))
    assert asyncio.run(provider_catalog.probe("openai", {"api_key": "k"}))["status"] == "unverified"
