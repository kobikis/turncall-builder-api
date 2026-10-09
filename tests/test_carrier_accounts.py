"""A workspace's own Telnyx and Asterisk accounts (#84, turncall ADR-0020/0022)."""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest

from app import provider_catalog
from tests.test_provider_integrations import ENGINE, WS, _integration

PUBLIC_KEY = base64.b64encode(b"k" * 32).decode()
CARRIER_ENGINE = {
    **ENGINE,
    "accounts": {
        **ENGINE["accounts"],
        "telnyx": {"serves": {"carrier": ["telnyx"]}, "platform_key": False, "project_credential": False},
        "asterisk": {"serves": {"carrier": ["asterisk"]}, "platform_key": False, "project_credential": False},
    },
}
ARI = {"ari_url": "https://pbx.example.com:8088", "username": "turncall", "password": "s3cret-pass"}


@pytest.fixture
def engine(mocks, monkeypatch):
    mocks.client.list_providers.return_value = CARRIER_ENGINE
    mocks.registry.list_backends.return_value = []

    async def unchecked(provider, document):
        return {"status": "unchecked", "checked": None, "detail": "test"}

    monkeypatch.setattr(provider_catalog, "probe", unchecked)
    return mocks


def test_a_telnyx_account_takes_its_key_and_public_key(client, engine):
    engine.integration_store.save.return_value = _integration("telnyx")
    r = client.put(
        "/integrations/providers/telnyx",
        json={"api_key": " KEYabc123 ", "public_key": f" {PUBLIC_KEY} "},
        headers=WS,
    )
    assert r.status_code == 200, r.text
    kw = engine.integration_store.save.await_args.kwargs
    assert kw["document"] == {"api_key": "KEYabc123", "public_key": PUBLIC_KEY}
    assert kw["last4"] == "c123"


@pytest.mark.parametrize("body", [
    {"api_key": "KEYabc123"},  # webhooks could never be verified
    {"api_key": "KEYabc123", "public_key": "not-base64!"},
    {"api_key": "KEYabc123", "public_key": base64.b64encode(b"short").decode()},
])
def test_a_telnyx_account_without_a_valid_public_key_is_refused(client, engine, body):
    r = client.put("/integrations/providers/telnyx", json=body, headers=WS)
    assert r.status_code == 422
    assert "public_key" in r.json()["detail"]
    engine.integration_store.save.assert_not_called()


def test_a_workspace_asterisk_takes_its_ari_login(client, engine):
    # One PBX for every agent's project: turncall#274 keys its ARI edge by account.
    engine.integration_store.save.return_value = _integration("asterisk")
    r = client.put(
        "/integrations/providers/asterisk",
        json={**ARI, "ari_url": f" {ARI['ari_url']}/ ", "app": "", "transfer_context": "to-ops"},
        headers=WS,
    )
    assert r.status_code == 200, r.text
    kw = engine.integration_store.save.await_args.kwargs
    assert kw["document"] == {**ARI, "transfer_context": "to-ops"}
    assert kw["last4"] == "pass"


@pytest.mark.parametrize("body", [
    {"ari_url": "https://pbx.example.com:8088", "username": "turncall"},
    {**ARI, "ari_url": "pbx.example.com:8088"},
    {**ARI, "app": "turn call"},
    {**ARI, "secret": "x"},
])
def test_a_malformed_asterisk_login_is_refused(client, engine, body):
    r = client.put("/integrations/providers/asterisk", json=body, headers=WS)
    assert r.status_code == 422
    engine.integration_store.save.assert_not_called()


def test_an_asterisk_login_is_not_checked_by_dialling_the_url_it_names():
    result = asyncio.run(provider_catalog.probe("asterisk", ARI))
    assert result["status"] == "unchecked"


def test_a_telnyx_key_is_checked_against_an_endpoint_that_authenticates(monkeypatch):
    seen = []
    real = httpx.AsyncClient
    monkeypatch.setattr(
        provider_catalog.httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(401)), **kw),
    )
    result = asyncio.run(provider_catalog.probe("telnyx", {"api_key": "k", "public_key": PUBLIC_KEY}))
    assert result["status"] == "rejected"
    assert seen[0].url.host == "api.telnyx.com"
    assert seen[0].headers["authorization"] == "Bearer k"
