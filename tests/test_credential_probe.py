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
