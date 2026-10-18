"""Provider Integrations on the TestClient seam (ADR-0023, #55-#58).

Projection is observed as calls on the mocked TurnCall client: what a Workspace
admin's write causes the engine to be asked. The stores are autospecced; their
SQL is covered by test_integration_store_db.py.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from app import provider_catalog, telnyx_api
from app.routers.auth import COOKIE_NAME as COOKIE
from tests.conftest import TEST_WORKSPACE_ID, audit_entries, backend_row

WS = {"X-Workspace-Id": TEST_WORKSPACE_ID}
KEY = "dg-secret-key-1234"
V1 = "11111111-1111-1111-1111-111111111111"

ENGINE = {
    "roles": {"stt": ["deepgram", "openai"], "llm": ["openai", "ollama"], "tts": ["deepgram"]},
    "accounts": {
        "deepgram": {"serves": {"stt": ["deepgram"], "tts": ["deepgram"]}, "platform_key": True, "project_credential": False},
        "openai": {"serves": {"stt": ["openai"], "llm": ["openai"]}, "platform_key": False, "project_credential": False},
        "cartesia": {"serves": {"tts": ["cartesia"]}, "platform_key": False, "project_credential": False},
    },
}


def _integration(provider="deepgram", **over):
    row = {
        "provider": provider,
        "last4": "1234",
        "validation": {"status": "valid", "checked": "GET https://api.deepgram.com/v1/projects"},
        "version": V1,
        "disconnecting": False,
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
    }
    row.update(over)
    return row


@pytest.fixture
def engine(mocks):
    mocks.phones.ensure_webhook_project.return_value = {"project_id": "wp", "api_key": "tc_wp"}
    mocks.client.list_providers.return_value = ENGINE
    return mocks


@pytest.fixture
def probe(monkeypatch):
    result = {"status": "valid", "checked": "GET https://api.deepgram.com/v1/projects", "detail": "ok"}

    async def fake(provider, document):
        fake.calls.append((provider, document))
        return result

    fake.calls = []
    fake.result = result
    monkeypatch.setattr(provider_catalog, "probe", fake)
    return fake


def _two_agents(mocks, **second):
    mocks.registry.list_backends.return_value = [
        backend_row(agent_id="a1", api_key="tc_a1"),
        backend_row(agent_id="a2", api_key="tc_a2", **second),
    ]


# --- connecting -----------------------------------------------------------------


def test_connect_stores_and_projects_into_every_agent(client, engine, probe):
    _two_agents(engine)
    engine.integration_store.save.return_value = _integration()
    engine.integration_store.get.return_value = _integration()
    engine.integration_store.get_document.return_value = {"api_key": KEY}

    r = client.put("/integrations/providers/deepgram", json={"api_key": f"  {KEY} "}, headers=WS)

    assert r.status_code == 200, r.text
    assert KEY not in r.text
    kw = engine.integration_store.save.await_args.kwargs
    assert kw["document"] == {"api_key": KEY}
    assert kw["last4"] == "1234"
    calls = engine.client.put_credential.await_args_list
    assert [c.args for c in calls] == [
        ("deepgram", {"api_key": KEY}, "tc_a1"),
        ("deepgram", {"api_key": KEY}, "tc_a2"),
    ]
    assert [a["state"] for a in r.json()["data"]["agents"]] == ["applied", "applied"]


def test_a_rejected_key_is_not_stored(client, engine, probe):
    probe.result.update(status="rejected", detail="deepgram answered 401")
    r = client.put("/integrations/providers/deepgram", json={"api_key": "bad"}, headers=WS)
    assert r.status_code == 400
    assert "not saved" in r.json()["detail"]
    engine.integration_store.save.assert_not_called()
    engine.client.put_credential.assert_not_called()


def test_an_unreachable_provider_is_stored_and_says_so(client, engine, probe):
    probe.result.update(status="unverified", detail="unreachable: ConnectError")
    engine.registry.list_backends.return_value = []
    engine.integration_store.save.return_value = _integration(validation=probe.result)
    r = client.put("/integrations/providers/deepgram", json={"api_key": KEY}, headers=WS)
    assert r.status_code == 200
    assert engine.integration_store.save.await_args.kwargs["validation"]["status"] == "unverified"


def test_unknown_provider_is_named(client, engine, probe):
    r = client.put("/integrations/providers/ollama", json={"api_key": "k"}, headers=WS)
    assert r.status_code == 422
    assert "'ollama'" in r.json()["detail"]


def test_malformed_body_is_422(client, engine, probe):
    r = client.put("/integrations/providers/deepgram", json={"key": "k"}, headers=WS)
    assert r.status_code == 422
    engine.integration_store.save.assert_not_called()


def test_a_pinned_agent_still_receives_the_workspace_key(client, engine, probe):
    """Its own key covers only its LLM; its voice on the same account must not
    fall to the platform key. It is recorded `pinned` so the card can say so."""
    engine.registry.list_backends.return_value = [
        backend_row(agent_id="a1", api_key="tc_a1"),
        backend_row(
            agent_id="pinned",
            api_key="tc_p",
            config={"name": "P", "llm": {"provider": "openai", "api_key": "sk-own"}},
        ),
    ]
    engine.integration_store.save.return_value = _integration("openai")
    engine.integration_store.get.return_value = _integration("openai")
    engine.integration_store.get_document.return_value = {"api_key": "sk-ws"}

    r = client.put("/integrations/providers/openai", json={"api_key": "sk-ws"}, headers=WS)

    assert [c.args[2] for c in engine.client.put_credential.await_args_list] == ["tc_a1", "tc_p"]
    states = {a["agent_id"]: a["state"] for a in r.json()["data"]["agents"]}
    assert states == {"a1": "applied", "pinned": "pinned"}


def test_a_credential_change_never_regenerates_a_backend(client, engine, probe):
    _two_agents(engine)
    engine.integration_store.save.return_value = _integration()
    engine.integration_store.get.return_value = _integration()
    engine.integration_store.get_document.return_value = {"api_key": KEY}
    client.put("/integrations/providers/deepgram", json={"api_key": KEY}, headers=WS)
    engine.generator.materialize_and_run.assert_not_called()
    engine.registry.set_status.assert_not_called()


# --- retrying ---------------------------------------------------------------------


def test_partial_failure_is_recorded_and_the_retry_finishes_it(client, engine, probe):
    _two_agents(engine)
    engine.integration_store.save.return_value = _integration()
    engine.integration_store.get.return_value = _integration()
    engine.integration_store.get_document.return_value = {"api_key": KEY}
    engine.client.put_credential.side_effect = [None, RuntimeError("turncall down")]

    r = client.put("/integrations/providers/deepgram", json={"api_key": KEY}, headers=WS)
    states = [(a["agent_id"], a["state"]) for a in r.json()["data"]["agents"]]
    assert states == [("a1", "applied"), ("a2", "failed")]

    # The log now says a1 holds this version; only a2 is written again.
    engine.integration_store.list_projections.return_value = [
        {"agent_id": "a1", "state": "applied", "version": V1},
        {"agent_id": "a2", "state": "failed", "version": V1},
    ]
    engine.client.put_credential.reset_mock(side_effect=True)
    r = client.post("/integrations/providers/deepgram/apply", headers=WS)
    assert r.status_code == 200
    assert [c.args[2] for c in engine.client.put_credential.await_args_list] == ["tc_a2"]


def test_a_pinned_agent_holding_the_current_version_is_not_written_again(client, engine):
    """`pinned` holds the credential as `applied` does (its own LLM key only
    wins for the model), so re-applying skips it like any agent holding it."""
    _two_agents(engine, config={"name": "P", "llm": {"provider": "openai", "api_key": "sk-own"}})
    engine.integration_store.get.return_value = _integration("openai")
    engine.integration_store.list_projections.return_value = [
        {"agent_id": "a1", "state": "applied", "version": V1},
        {"agent_id": "a2", "state": "pinned", "version": V1},
    ]
    r = client.post("/integrations/providers/openai/apply", headers=WS)
    assert r.status_code == 200
    engine.client.put_credential.assert_not_called()
    states = {a["agent_id"]: a["state"] for a in r.json()["data"]["agents"]}
    assert states == {"a1": "applied", "a2": "pinned"}


def test_rerunning_a_complete_projection_writes_nothing(client, engine):
    _two_agents(engine)
    engine.integration_store.get.return_value = _integration()
    engine.integration_store.list_projections.return_value = [
        {"agent_id": "a1", "state": "applied", "version": V1},
        {"agent_id": "a2", "state": "applied", "version": V1},
    ]
    r = client.post("/integrations/providers/deepgram/apply", headers=WS)
    assert r.status_code == 200
    engine.client.put_credential.assert_not_called()


# --- disconnecting ------------------------------------------------------------------


def test_disconnect_removes_from_every_agent_before_the_record(client, engine):
    _two_agents(engine)
    engine.integration_store.get.return_value = _integration()
    order = []
    engine.client.delete_credential.side_effect = lambda p, key: order.append(("engine", key))
    engine.integration_store.delete.side_effect = lambda *a: order.append(("record",))

    r = client.delete("/integrations/providers/deepgram", headers=WS)

    assert r.json()["data"]["complete"] is True
    assert order == [("engine", "tc_a1"), ("engine", "tc_a2"), ("record",)]


def test_an_interrupted_disconnect_keeps_the_record_and_finishes_on_retry(client, engine):
    _two_agents(engine)
    engine.integration_store.get.return_value = _integration()
    engine.client.delete_credential.side_effect = [None, RuntimeError("timeout")]

    r = client.delete("/integrations/providers/deepgram", headers=WS)

    data = r.json()["data"]
    assert data["complete"] is False
    assert [a["state"] for a in data["agents"]] == ["removed", "remove_failed"]
    engine.integration_store.set_disconnecting.assert_awaited()
    engine.integration_store.delete.assert_not_called()

    engine.integration_store.list_projections.return_value = [
        {"agent_id": "a1", "state": "removed", "version": None},
    ]
    engine.client.delete_credential.reset_mock(side_effect=True)
    r = client.delete("/integrations/providers/deepgram", headers=WS)
    assert r.json()["data"]["complete"] is True
    assert [c.args[1] for c in engine.client.delete_credential.await_args_list] == ["tc_a2"]
    engine.integration_store.delete.assert_awaited_once()


def test_disconnect_never_touches_a_pinned_agents_config(client, engine):
    pinned = backend_row(
        agent_id="p", api_key="tc_p", config={"name": "P", "llm": {"provider": "openai", "api_key": "sk-own"}}
    )
    engine.registry.list_backends.return_value = [pinned]
    engine.integration_store.get.return_value = _integration("openai")
    client.delete("/integrations/providers/openai", headers=WS)
    engine.client.update_agent.assert_not_called()


# --- cards: which key runs the call --------------------------------------------------


def test_cards_say_which_key_runs_the_call(client, engine):
    engine.registry.list_backends.return_value = [
        backend_row(agent_id="a1", config={"name": "One", "tts": {"provider": "deepgram"}}),
        backend_row(agent_id="p", config={"name": "Pin", "llm": {"provider": "openai", "api_key": "sk-own"}}),
    ]
    engine.integration_store.list_for_workspace.return_value = [_integration("openai", last4="wxyz")]

    r = client.get("/integrations/providers", headers=WS)

    assert r.status_code == 200
    assert "sk-own" not in r.text
    cards = {c["provider"]: c for c in r.json()["data"]["providers"]}
    assert cards["openai"]["runs_on"] == "workspace"
    assert cards["openai"]["last4"] == "wxyz"
    assert cards["openai"]["pinned_agents"] == [{"agent_id": "p", "name": "Pin"}]
    assert cards["deepgram"]["runs_on"] == "platform"  # no Integration, platform key
    assert cards["deepgram"]["agents_using"] == 2  # default STT/TTS on both
    assert cards["cartesia"]["runs_on"] == "none"  # neither: calls will fail
    assert cards["deepgram"]["serves"] == {"stt": ["deepgram"], "tts": ["deepgram"]}


def test_cards_say_which_key_webhook_numbers_run_on(client, engine):
    """#65: the Workspace's webhook project shows its projection, failures
    included, and numbers still in the shared project are counted — they run on
    the platform's keys until rebound."""
    engine.registry.list_backends.return_value = []
    engine.integration_store.list_for_workspace.return_value = [_integration("deepgram")]
    engine.phones.workspace_webhook_project.return_value = {"project_id": "wp-ws", "api_key": "tc_wp_ws"}
    engine.phones.shared_webhook_numbers.return_value = 2
    target = f"webhook-numbers:{TEST_WORKSPACE_ID}"
    engine.integration_store.list_projections.return_value = [
        {"provider": "deepgram", "agent_id": target, "state": "failed", "version": V1, "error": "401"},
    ]

    r = client.get("/integrations/providers", headers=WS)

    cards = {c["provider"]: c for c in r.json()["data"]["providers"]}
    assert cards["deepgram"]["webhook_numbers"] == {
        "state": "failed", "error": "401", "on_platform_keys": 2,
    }
    assert cards["deepgram"]["agents"] == []  # never listed as an agent
    assert cards["openai"]["webhook_numbers"]["state"] is None


# --- adoption -------------------------------------------------------------------------


def test_adopt_moves_the_agents_key_into_the_workspace(client, engine, probe):
    pinned = backend_row(
        agent_id="p", api_key="tc_p",
        config={"name": "P", "llm": {"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-own"}},
    )
    engine.registry.get_backend.return_value = pinned
    engine.registry.list_backends.return_value = [backend_row(agent_id="p", api_key="tc_p")]
    # None before adoption, the new Integration once saved.
    engine.integration_store.get.side_effect = [None, _integration("openai")]
    engine.integration_store.get_document.return_value = {"api_key": "sk-own"}

    r = client.post("/integrations/providers/openai/adopt", json={"agent_id": "p"}, headers=WS)

    assert r.status_code == 200, r.text
    assert engine.integration_store.save.await_args.kwargs["document"] == {"api_key": "sk-own"}
    pushed = engine.client.update_agent.await_args.args[1]["config"]["llm"]
    assert "api_key" not in pushed
    engine.client.put_credential.assert_awaited_with("openai", {"api_key": "sk-own"}, "tc_p")


def test_adopt_refuses_an_agent_without_its_own_key(client, engine):
    engine.registry.get_backend.return_value = backend_row(agent_id="a1")
    r = client.post("/integrations/providers/openai/adopt", json={"agent_id": "a1"}, headers=WS)
    assert r.status_code == 409
    engine.integration_store.save.assert_not_called()


# --- roles ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("put", "/integrations/providers/deepgram", {"api_key": "k"}),
        ("delete", "/integrations/providers/deepgram", None),
        ("post", "/integrations/providers/deepgram/apply", None),
        ("post", "/integrations/providers/openai/adopt", {"agent_id": "a1"}),
    ],
)
def test_editors_cannot_change_a_credential(gate_client, engine, method, path, body):
    engine.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}
    engine.auth_store.resolve_membership.return_value = "editor"
    gate_client.cookies.set(COOKIE, "live")
    kwargs = {"json": body} if body is not None else {}
    r = getattr(gate_client, method)(path, headers=WS, **kwargs)
    assert r.status_code == 403
    engine.integration_store.save.assert_not_called()


def test_a_viewer_can_read_presence(gate_client, engine):
    engine.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}
    engine.auth_store.resolve_membership.return_value = "viewer"
    engine.registry.list_backends.return_value = []
    gate_client.cookies.set(COOKIE, "live")
    assert gate_client.get("/integrations/providers", headers=WS).status_code == 200


# --- new agents and the pickers ---------------------------------------------------------


def test_a_new_agent_gets_every_connected_credential(engine):
    import asyncio

    from app import provider_integrations

    engine.integration_store.list_for_workspace.return_value = [
        _integration("deepgram"),
        _integration("openai", disconnecting=True),
    ]
    engine.integration_store.get_document.return_value = {"api_key": KEY}
    asyncio.run(
        provider_integrations.project_into_new_target(
            object(), engine.client, TEST_WORKSPACE_ID, {"agent_id": "new", "api_key": "tc_new", "config": {}}
        )
    )
    engine.client.put_credential.assert_awaited_once_with("deepgram", {"api_key": KEY}, "tc_new")


def test_pickers_list_with_the_workspace_key(client, mocks, monkeypatch):
    mocks.integration_store.api_keys.return_value = {"openai": "sk-ws"}
    seen = []

    async def fetch(c, provider, key):
        seen.append(key)
        return ["gpt-x"]

    monkeypatch.setattr(provider_catalog, "_fetch_llm_models", fetch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-builder-env")
    r = client.get("/providers/llm/openai/models", headers=WS)
    assert r.json()["data"]["models"] == ["gpt-x"]
    assert seen == ["sk-ws"]

    # Removed: the next read falls back to the Builder's env under a new cache key.
    mocks.integration_store.api_keys.return_value = {}
    client.get("/providers/llm/openai/models", headers=WS)
    assert seen == ["sk-ws", "sk-builder-env"]


def test_adopt_never_overwrites_a_workspace_key(client, engine, probe):
    engine.registry.get_backend.return_value = backend_row(
        agent_id="p", config={"name": "P", "llm": {"provider": "openai", "api_key": "sk-own"}}
    )
    engine.integration_store.get.return_value = _integration("openai")
    r = client.post("/integrations/providers/openai/adopt", json={"agent_id": "p"}, headers=WS)
    assert r.status_code == 409
    engine.integration_store.save.assert_not_called()


def test_reapply_is_refused_while_a_disconnect_is_unfinished(client, engine):
    engine.integration_store.get.return_value = _integration(disconnecting=True)
    r = client.post("/integrations/providers/deepgram/apply", headers=WS)
    assert r.status_code == 409
    engine.client.put_credential.assert_not_called()


# --- the Workspace's webhook project (#65) --------------------------------------


def test_connect_also_projects_into_the_workspace_webhook_project(client, engine, probe):
    """Numbers routed to a customer's own call-init server run in the Workspace's
    webhook project; without this they ran on the platform's keys."""
    _two_agents(engine)
    engine.phones.workspace_webhook_project.return_value = {"project_id": "wp-ws", "api_key": "tc_wp_ws"}
    engine.integration_store.save.return_value = _integration()
    engine.integration_store.get.return_value = _integration()
    engine.integration_store.get_document.return_value = {"api_key": KEY}

    r = client.put("/integrations/providers/deepgram", json={"api_key": KEY}, headers=WS)

    assert r.status_code == 200, r.text
    keys = [c.args[2] for c in engine.client.put_credential.await_args_list]
    assert keys == ["tc_a1", "tc_a2", "tc_wp_ws"]


def test_disconnect_also_removes_it_from_the_workspace_webhook_project(client, engine):
    _two_agents(engine)
    engine.phones.workspace_webhook_project.return_value = {"project_id": "wp-ws", "api_key": "tc_wp_ws"}
    engine.integration_store.get.return_value = _integration()

    r = client.delete("/integrations/providers/deepgram", headers=WS)

    assert r.json()["data"]["complete"] is True
    keys = [c.args[1] for c in engine.client.delete_credential.await_args_list]
    assert keys == ["tc_a1", "tc_a2", "tc_wp_ws"]


# --- Telnyx numbers on the account being connected (#114) -----------------------

TELNYX_KEY = "KEYtelnyx-new-5678"
PUBLIC_KEY = base64.b64encode(b"k" * 32).decode()
TELNYX_BODY = {"api_key": TELNYX_KEY, "public_key": PUBLIC_KEY}
N1, N2, N3 = "+13125550100", "+13125550101", "+13125550102"
TELNYX_ENGINE = {
    **ENGINE,
    "accounts": {
        **ENGINE["accounts"],
        "telnyx": {"serves": {}, "platform_key": True, "project_credential": True},
    },
}


def _number(e164, project_id="p1", carrier="telnyx"):
    return {"id": e164, "e164": e164, "carrier": carrier, "project_id": project_id}


@pytest.fixture
def telnyx(engine, probe, monkeypatch):
    """A Workspace with one agent (project p1) and Telnyx numbers to look up;
    `telnyx.held` is what the new account holds."""
    engine.client.list_providers.return_value = TELNYX_ENGINE
    engine.registry.list_backends.return_value = [backend_row(agent_id="a1", api_key="tc_a1")]
    engine.integration_store.get.return_value = _integration("telnyx")
    engine.integration_store.save.return_value = _integration("telnyx")
    engine.integration_store.get_document.return_value = TELNYX_BODY
    engine.phones.list_numbers.return_value = []

    async def list_numbers(api_key, **kw):
        list_numbers.calls.append(api_key)
        if list_numbers.error:
            raise list_numbers.error
        return set(list_numbers.held)

    list_numbers.calls, list_numbers.held, list_numbers.error = [], set(), None
    monkeypatch.setattr(telnyx_api, "list_numbers", list_numbers)

    async def find_number(*a, **kw):  # the release check's, never this check's
        raise AssertionError("the connect check lists the account once")

    monkeypatch.setattr(telnyx_api, "find_number", find_number)
    engine.listing = list_numbers
    return engine


def _connect(client, **extra):
    return client.put("/integrations/providers/telnyx", json={**TELNYX_BODY, **extra}, headers=WS)


def _nothing_saved(mocks):
    mocks.integration_store.save.assert_not_called()
    mocks.client.put_credential.assert_not_called()
    assert audit_entries(mocks) == []


def test_connecting_telnyx_with_no_bound_numbers_looks_nothing_up(client, telnyx):
    # An unassigned Telnyx row and a bound Twilio number don't count.
    telnyx.phones.list_numbers.return_value = [
        _number(N1, project_id=None), _number(N2, carrier="twilio"),
    ]
    r = _connect(client)
    assert r.status_code == 200, r.text
    assert telnyx.listing.calls == []
    telnyx.integration_store.save.assert_awaited_once()


def test_connecting_telnyx_holding_every_bound_number_goes_ahead(client, telnyx):
    telnyx.phones.workspace_webhook_project.return_value = {"project_id": "wp-ws", "api_key": "tc_wp_ws"}
    telnyx.phones.list_numbers.return_value = [_number(N1), _number(N2, project_id="wp-ws")]
    telnyx.listing.held = {N1, N2}
    r = _connect(client)
    assert r.status_code == 200, r.text
    assert telnyx.listing.calls == [TELNYX_KEY]  # the new key's account, once
    telnyx.client.put_credential.assert_awaited()
    assert "/acknowledged_numbers" not in audit_entries(telnyx)[0]["changes"]


def test_connecting_telnyx_missing_bound_numbers_is_refused_with_them(client, telnyx):
    telnyx.phones.list_numbers.return_value = [_number(N1), _number(N2), _number(N3)]
    telnyx.listing.held = {N2}
    r = _connect(client)
    assert r.status_code == 409
    body = r.json()
    assert body["reason"] == "numbers_not_on_account"
    assert body["numbers"] == [N1, N3]
    assert isinstance(body["detail"], str)
    assert N1 in body["detail"] and N3 in body["detail"] and N2 not in body["detail"]
    assert "stop answering calls" in body["detail"]
    _nothing_saved(telnyx)


SHARED = "wp-shared"  # the shared webhook project: `webhook_project` id=1, no Workspace


def test_connecting_telnyx_flags_only_numbers_in_the_projects_it_writes_into(client, telnyx):
    """A number still in the shared webhook project, or in a project that isn't
    a target, keeps its account: only an Agent's project number is flagged (#119)."""
    telnyx.phones.list_numbers.return_value = [
        _number(N1, "p1"), _number(N2, SHARED), _number(N3, "p-not-a-target"),
    ]
    r = _connect(client)
    assert r.status_code == 409
    assert r.json()["numbers"] == [N1]
    assert N2 not in r.json()["detail"] and N3 not in r.json()["detail"]
    _nothing_saved(telnyx)
    # Acknowledging the one flagged number is enough.
    r = _connect(client, acknowledge_numbers=[N1])
    assert r.status_code == 200, r.text
    assert audit_entries(telnyx)[0]["changes"]["/acknowledged_numbers"]["after"] == [N1]


def test_connecting_telnyx_with_only_shared_project_numbers_looks_nothing_up(client, telnyx):
    telnyx.phones.list_numbers.return_value = [_number(N1, SHARED), _number(N2, SHARED)]
    r = _connect(client)
    assert r.status_code == 200, r.text
    assert telnyx.listing.calls == []


def test_an_acknowledged_resend_connects_and_audits_the_numbers(client, telnyx):
    telnyx.phones.list_numbers.return_value = [_number(N1), _number(N2), _number(N3)]
    telnyx.listing.held = {N2}
    r = _connect(client, acknowledge_numbers=[N1, f" {N3} "])
    assert r.status_code == 200, r.text
    telnyx.integration_store.save.assert_awaited_once()
    # Not a field of the stored credential.
    assert "acknowledge_numbers" not in telnyx.integration_store.save.await_args.kwargs["document"]
    telnyx.client.put_credential.assert_awaited()
    changes = audit_entries(telnyx)[0]["changes"]
    assert changes["/acknowledged_numbers"] == {"before": None, "after": [N1, N3]}
    assert changes["/value"]["secret"] is True


def test_a_partial_acknowledgement_is_still_refused(client, telnyx):
    telnyx.phones.list_numbers.return_value = [_number(N1), _number(N3)]
    r = _connect(client, acknowledge_numbers=[N1])
    assert r.status_code == 409
    assert r.json()["numbers"] == [N1, N3]
    _nothing_saved(telnyx)


def test_acknowledge_numbers_must_be_a_list_of_numbers(client, telnyx):
    r = _connect(client, acknowledge_numbers=N1)
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)
    _nothing_saved(telnyx)


def _answered(status, body="{}"):
    request = httpx.Request("GET", "https://api.telnyx.com/v2/phone_numbers")
    response = httpx.Response(status, text=body, request=request)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


_RETRY = "retry in a moment."
_NOT_A_RETRY = "this needs a fix in TurnCall's builder, not a retry."


NUMBERS = "the workspace's Telnyx numbers"


@pytest.mark.parametrize("error, says", [
    (
        telnyx_api.TelnyxRejected("Telnyx answered 401"),
        f"Telnyx rejected this API key while checking {NUMBERS} on its account, so nothing "
        "was saved: check the key and try again.",
    ),
    (httpx.ConnectError("boom"), f"Telnyx didn't answer when looking up {NUMBERS} on this Telnyx account; {_RETRY}"),
    (httpx.ReadTimeout("slow"), f"Telnyx didn't answer when looking up {NUMBERS} on this Telnyx account; {_RETRY}"),
    (_answered(429), f"Telnyx answered 429 when looking up {NUMBERS}; {_RETRY}"),
    (_answered(500), f"Telnyx answered 500 when looking up {NUMBERS}; {_RETRY}"),
    (_answered(404), f"Telnyx answered 404 when looking up {NUMBERS}; {_NOT_A_RETRY}"),
    (
        telnyx_api.TelnyxMalformed(200, "<html>"),
        f"Telnyx answered 200 when looking up {NUMBERS}; {_NOT_A_RETRY}",
    ),
    (
        telnyx_api.TelnyxRetryLater(429, 30),
        "Telnyx is rate-limiting this Telnyx account; try again in about 30 seconds.",
    ),
    (
        telnyx_api.TelnyxRetryLater(503, 2),
        f"Telnyx answered 503 when looking up {NUMBERS}; try again in about 2 seconds.",
    ),
    (
        telnyx_api.TelnyxRetryLater(None, None),
        f"Telnyx is answering too slowly when looking up {NUMBERS} on this Telnyx account; "
        f"{_RETRY}",
    ),
])
def test_a_lookup_that_fails_is_a_502_and_saves_nothing(client, telnyx, error, says):
    telnyx.phones.list_numbers.return_value = [_number(N1)]
    telnyx.listing.error = error
    r = _connect(client)
    assert (r.status_code, r.json()) == (502, {"detail": says})
    _nothing_saved(telnyx)


def test_apply_says_the_stored_key_was_rejected(client, telnyx):
    _applying(telnyx, holding=["a1"])
    telnyx.listing.error = telnyx_api.TelnyxRejected("Telnyx answered 403")
    r = client.post("/integrations/providers/telnyx/apply", headers=WS)
    assert (r.status_code, r.json()) == (502, {
        "detail": f"Telnyx rejected the workspace's Telnyx API key while checking {NUMBERS}: "
        "update it on the Integrations page.",
    })


def test_many_numbers_are_checked_against_one_listing(client, telnyx):
    numbers = [f"+1312555{i:04d}" for i in range(400)]
    telnyx.phones.list_numbers.return_value = [_number(n) for n in numbers]
    telnyx.listing.held = set(numbers[1:])
    r = _connect(client)
    assert (r.status_code, r.json()["numbers"]) == (409, [numbers[0]])
    assert telnyx.listing.calls == [TELNYX_KEY]


def test_other_providers_are_never_looked_up_on_telnyx(client, telnyx):
    telnyx.phones.list_numbers.return_value = [_number(N1)]
    telnyx.integration_store.save.return_value = _integration()
    telnyx.integration_store.get.return_value = _integration()  # what the projection reads
    r = client.put("/integrations/providers/deepgram", json={"api_key": KEY}, headers=WS)
    assert r.status_code == 200, r.text
    assert telnyx.listing.calls == []


def _applying(telnyx, *, holding):
    """Telnyx connected; a1 (project p1) and a2 (project p2), `holding` the
    current version."""
    telnyx.registry.list_backends.return_value = [
        backend_row(agent_id="a1", api_key="tc_a1", project_id="p1"),
        backend_row(agent_id="a2", api_key="tc_a2", project_id="p2"),
    ]
    telnyx.integration_store.list_projections.return_value = [
        {"agent_id": a, "state": "applied", "version": V1} for a in holding
    ]
    telnyx.phones.list_numbers.return_value = [_number(N1, "p1"), _number(N2, "p2")]


def test_apply_with_every_project_holding_the_key_looks_nothing_up(client, telnyx):
    _applying(telnyx, holding=["a1", "a2"])
    r = client.post("/integrations/providers/telnyx/apply", headers=WS)
    assert r.status_code == 200, r.text
    assert telnyx.listing.calls == []


def test_apply_checks_only_the_projects_it_would_write_and_can_refuse(client, telnyx):
    _applying(telnyx, holding=["a1"])
    r = client.post("/integrations/providers/telnyx/apply", headers=WS)
    assert r.status_code == 409
    assert (r.json()["reason"], r.json()["numbers"]) == ("numbers_not_on_account", [N2])
    assert telnyx.listing.calls == [TELNYX_KEY]
    telnyx.client.put_credential.assert_not_called()
    assert audit_entries(telnyx) == []


def test_apply_acknowledged_goes_ahead_and_audits_the_numbers(client, telnyx):
    _applying(telnyx, holding=["a1"])
    r = client.post(
        "/integrations/providers/telnyx/apply", json={"acknowledge_numbers": [N2]}, headers=WS
    )
    assert r.status_code == 200, r.text
    assert [c.args[2] for c in telnyx.client.put_credential.await_args_list] == ["tc_a2"]
    assert audit_entries(telnyx)[0]["changes"] == {
        "/acknowledged_numbers": {"before": None, "after": [N2]},
    }


def test_apply_acknowledge_numbers_must_be_a_list_of_numbers(client, telnyx):
    _applying(telnyx, holding=["a1"])
    r = client.post(
        "/integrations/providers/telnyx/apply", json={"acknowledge_numbers": N2}, headers=WS
    )
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)
    telnyx.client.put_credential.assert_not_called()


def test_apply_into_the_webhook_project_checks_its_numbers(client, telnyx):
    _applying(telnyx, holding=["a1", "a2"])
    telnyx.phones.workspace_webhook_project.return_value = {"project_id": "wp-ws", "api_key": "tc_wp_ws"}
    telnyx.phones.list_numbers.return_value = [_number(N1, "p1"), _number(N3, "wp-ws")]
    r = client.post("/integrations/providers/telnyx/apply", headers=WS)
    assert r.status_code == 409
    assert r.json()["numbers"] == [N3]


def test_apply_flags_only_numbers_in_the_projects_it_writes_into(client, telnyx):
    """Writing into every target still leaves a shared-project number alone (#119)."""
    _applying(telnyx, holding=[])
    telnyx.phones.list_numbers.return_value = [
        _number(N1, "p1"), _number(N2, SHARED), _number(N3, "p-not-a-target"),
    ]
    r = client.post("/integrations/providers/telnyx/apply", headers=WS)
    assert r.status_code == 409
    assert r.json()["numbers"] == [N1]
    telnyx.client.put_credential.assert_not_called()
    r = client.post(
        "/integrations/providers/telnyx/apply", json={"acknowledge_numbers": [N1]}, headers=WS
    )
    assert r.status_code == 200, r.text


def test_apply_lookup_failure_is_a_502(client, telnyx):
    _applying(telnyx, holding=[])
    telnyx.listing.error = httpx.ReadTimeout("slow")
    r = client.post("/integrations/providers/telnyx/apply", headers=WS)
    assert r.status_code == 502
    telnyx.client.put_credential.assert_not_called()


def test_adopting_never_projects_a_telnyx_key(client, telnyx):
    """An agent's own key is only its LLM's, and none bills to Telnyx."""
    telnyx.registry.get_backend.return_value = backend_row(
        agent_id="p", config={"name": "P", "llm": {"provider": "openai", "api_key": "sk-own"}}
    )
    telnyx.phones.list_numbers.return_value = [_number(N1)]
    r = client.post("/integrations/providers/telnyx/adopt", json={"agent_id": "p"}, headers=WS)
    assert r.status_code == 409
    assert "reason" not in r.json()
    assert telnyx.listing.calls == []
    _nothing_saved(telnyx)


# --- the check on every write of a Telnyx key (#109 review) ---------------------
#
# Connecting and applying check (and take acknowledgements); the write itself
# checks too, so no path writes a Telnyx key into a project holding bound
# Telnyx numbers its account doesn't hold.


def test_a_new_target_holding_unheld_telnyx_numbers_is_not_written(telnyx):
    import asyncio

    from app import provider_integrations

    telnyx.integration_store.list_for_workspace.return_value = [_integration("telnyx")]
    telnyx.phones.list_numbers.return_value = [_number(N1, "p-new")]
    asyncio.run(provider_integrations.project_into_new_target(
        object(), telnyx.client, TEST_WORKSPACE_ID,
        {"agent_id": "new", "api_key": "tc_new", "config": {}, "project_id": "p-new"},
    ))
    telnyx.client.put_credential.assert_not_called()
    recorded = telnyx.integration_store.record_projection.await_args
    assert recorded.kwargs["state"] == "failed"
    assert N1 in recorded.kwargs["error"]


def test_a_number_bound_after_connect_checked_is_not_moved(client, telnyx, monkeypatch):
    """A number bound between the connect's check and its writes: that
    project's write is refused and logged failed, the others go ahead."""
    telnyx.registry.list_backends.return_value = [
        backend_row(agent_id="a1", api_key="tc_a1", project_id="p1"),
        backend_row(agent_id="a2", api_key="tc_a2", project_id="p2"),
    ]
    telnyx.listing.held = {N1}
    seen = iter([
        [_number(N1, "p1")],  # what the connect's check sees
        [_number(N1, "p1")],  # a1's write
        [_number(N1, "p1"), _number(N2, "p2")],  # a2's write: N2 bound since
    ])
    telnyx.phones.list_numbers.side_effect = lambda *a, **kw: next(seen)
    r = _connect(client)
    assert r.status_code == 200, r.text
    assert [c.args[2] for c in telnyx.client.put_credential.await_args_list] == ["tc_a1"]
    states = {a["agent_id"]: a["state"] for a in r.json()["data"]["agents"]}
    assert states == {"a1": "applied", "a2": "failed"}
    assert telnyx.listing.calls == [TELNYX_KEY, TELNYX_KEY]
