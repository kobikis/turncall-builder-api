"""The Pipedream client credential at rest (ADR-0018, #24).

Same shape as test_github.py::TestTokenStorage, for the same reason: this is a
credential the builder must read back rather than hash, so a database dump alone
must not yield the ability to mint tokens against our Pipedream project.

Below the HTTP seam on purpose — "encrypted at rest" is not observable over HTTP.
The HTTP-visible guarantee (the secret never appears in a response) is a
different one, asserted in test_integrations.py.
"""

import asyncio
import os

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("PIPEDREAM_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app import pipedream  # noqa: E402


@pytest.mark.unit
class TestCredentialStorage:
    def test_roundtrip_and_ciphertext_hides_the_secret(self):
        c = pipedream.encrypt_secret("pd_client_secret_value")
        assert "pd_client_secret_value" not in c
        assert pipedream.decrypt_secret(c) == "pd_client_secret_value"

    def test_same_secret_encrypts_differently_each_time(self):
        """Fernet includes a nonce; identical secrets must not collide in the DB."""
        assert pipedream.encrypt_secret("s") != pipedream.encrypt_secret("s")

    def test_a_changed_key_fails_loudly_rather_than_returning_junk(self, monkeypatch):
        c = pipedream.encrypt_secret("sec")
        monkeypatch.setenv("PIPEDREAM_ENCRYPTION_KEY", Fernet.generate_key().decode())
        with pytest.raises(pipedream.PipedreamError, match="has changed"):
            pipedream.decrypt_secret(c)

    def test_missing_key_explains_how_to_make_one(self, monkeypatch):
        monkeypatch.delenv("PIPEDREAM_ENCRYPTION_KEY", raising=False)
        with pytest.raises(pipedream.PipedreamError, match="Fernet.generate_key"):
            pipedream.encrypt_secret("sec")


@pytest.mark.unit
class TestAccountShape:
    """Some Pipedream endpoints return the connected account's credentials. The
    public shape is an allowlist, which is what keeps them out of a response."""

    def test_credentials_are_dropped(self):
        out = pipedream._account(
            {
                "id": "apn_1",
                "name": "acme.slack.com",
                "app": {"name_slug": "slack", "name": "Slack"},
                "credentials": {"oauth_access_token": "xoxb-secret"},
            }
        )
        assert "xoxb-secret" not in str(out)
        assert out == {
            "id": "apn_1",
            "app": "slack",
            "name": "Slack",
            "account_name": "acme.slack.com",
            "healthy": True,
            "created_at": None,
            "updated_at": None,
        }

    def test_an_unnamed_app_falls_back_to_the_catalog(self):
        out = pipedream._account({"id": "a", "app": {"name_slug": "google_calendar"}})
        assert out["name"] == "Google Calendar"


@pytest.mark.unit
class TestConnectState:
    """The callback trusts nothing Pipedream hands back except our own signed
    state — which app and which Workspace are ours, not theirs."""

    def test_roundtrip(self):
        nonce = pipedream.new_flow_nonce()
        assert pipedream.load_state(pipedream.sign_state(nonce)) == nonce

    def test_the_state_carries_nothing_but_the_nonce(self):
        """Signed is not secret: whatever rides in the state is readable by
        Pipedream, a proxy log and the browser history."""
        import base64
        import json

        state = pipedream.sign_state("n0nce")
        payload = state.split(".")[0]
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        assert decoded == {"n": "n0nce"}

    def test_nonces_are_unguessable_and_distinct(self):
        nonces = {pipedream.new_flow_nonce() for _ in range(50)}
        assert len(nonces) == 50 and all(len(n) >= 40 for n in nonces)

    def test_tampering_is_rejected(self):
        state = pipedream.sign_state("n0nce")
        with pytest.raises(pipedream.PipedreamError):
            pipedream.load_state(state[:-2] + ("aa" if state[-2:] != "aa" else "bb"))

    def test_garbage_is_rejected(self):
        with pytest.raises(pipedream.PipedreamError):
            pipedream.load_state("not-a-state")

    def test_an_old_state_without_a_nonce_is_rejected(self):
        old = pipedream._serializer().dumps({"w": "w1", "a": "slack"})
        with pytest.raises(pipedream.PipedreamError):
            pipedream.load_state(old)


@pytest.mark.unit
class TestAccountFromFlow:
    """Pipedream's redirect names no account: the one the flow made is the
    one the Workspace did not have when it started."""

    def _a(self, id, created, updated=None):
        return {"id": id, "created_at": created, "updated_at": updated or created}

    def test_the_account_that_is_new_since_the_flow_began(self):
        accounts = [self._a("old", "2026-10-02"), self._a("mine", "2026-10-01")]
        # Not the newest by date: the one that was not there before.
        assert pipedream.account_from_flow(accounts, ["old"])["id"] == "mine"

    def test_two_new_accounts_take_the_newest(self):
        accounts = [self._a("a", "2026-10-01"), self._a("b", "2026-10-02")]
        assert pipedream.account_from_flow(accounts, [])["id"] == "b"

    def test_a_reauthorized_account_is_the_one_updated_last(self):
        accounts = [self._a("x", "2026-01-01", "2026-10-05"), self._a("y", "2026-06-01")]
        assert pipedream.account_from_flow(accounts, ["x", "y"])["id"] == "x"

    def test_no_accounts_is_none(self):
        assert pipedream.account_from_flow([], []) is None


@pytest.mark.unit
class TestAccessTokenCache:
    """Pipedream tokens live an hour, so a burst of calls must cost one grant —
    and the hour ending must cost exactly one more (ADR-0019, #25)."""

    @pytest.fixture(autouse=True)
    def _clear(self):
        pipedream._token_cache.clear()
        yield
        pipedream._token_cache.clear()

    @staticmethod
    def _cred(secret="s"):
        return pipedream.Credential(
            client_id="cid", client_secret=secret, project_id="p", environment="development"
        )

    @staticmethod
    def _grants(monkeypatch, expires_in=3600):
        """Count grants without a network: stand in for httpx.AsyncClient."""
        calls = []

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"access_token": f"tok{len(calls)}", "expires_in": expires_in}

        class _Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, **kw):
                calls.append(url)
                return _Response()

        monkeypatch.setattr(pipedream.httpx, "AsyncClient", _Client)
        return calls

    def test_a_burst_of_calls_costs_one_grant(self, monkeypatch):
        calls = self._grants(monkeypatch)
        cred = self._cred()
        first = asyncio.run(pipedream.access_token(cred))
        second = asyncio.run(pipedream.access_token(cred))
        assert first.token == second.token
        assert len(calls) == 1

    def test_an_expired_token_is_re_minted(self, monkeypatch):
        calls = self._grants(monkeypatch, expires_in=0)
        cred = self._cred()
        first = asyncio.run(pipedream.access_token(cred))
        second = asyncio.run(pipedream.access_token(cred))
        assert first.token != second.token
        assert len(calls) == 2

    def test_a_rotated_secret_is_not_served_the_old_token(self, monkeypatch):
        calls = self._grants(monkeypatch)
        asyncio.run(pipedream.access_token(self._cred("old")))
        asyncio.run(pipedream.access_token(self._cred("new")))
        assert len(calls) == 2
