"""The Pipedream client credential at rest (ADR-0018, #24).

Same shape as test_github.py::TestTokenStorage, for the same reason: this is a
credential the builder must read back rather than hash, so a database dump alone
must not yield the ability to mint tokens against our Pipedream project.

Below the HTTP seam on purpose — "encrypted at rest" is not observable over HTTP.
The HTTP-visible guarantee (the secret never appears in a response) is a
different one, asserted in test_integrations.py.
"""

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
        }

    def test_an_unnamed_app_falls_back_to_the_catalog(self):
        out = pipedream._account({"id": "a", "app": {"name_slug": "google_calendar"}})
        assert out["name"] == "Google Calendar"


@pytest.mark.unit
class TestConnectState:
    """The callback trusts nothing Pipedream hands back except our own signed
    state — which app and which Workspace are ours, not theirs."""

    def test_roundtrip(self):
        state = pipedream.sign_state(workspace_id="w1", app="slack")
        assert pipedream.load_state(state) == ("w1", "slack")

    def test_tampering_is_rejected(self):
        state = pipedream.sign_state(workspace_id="w1", app="slack")
        with pytest.raises(pipedream.PipedreamError):
            pipedream.load_state(state[:-2] + ("aa" if state[-2:] != "aa" else "bb"))

    def test_garbage_is_rejected(self):
        with pytest.raises(pipedream.PipedreamError):
            pipedream.load_state("not-a-state")
