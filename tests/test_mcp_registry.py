"""Workspace MCP server registry (ADR-0023, #59) on the TestClient seam."""

from __future__ import annotations

from app.mapper import to_create_agent_request
from app.routers.auth import COOKIE_NAME as COOKIE
from tests.conftest import TEST_WORKSPACE_ID, backend_row

WS = {"X-Workspace-Id": TEST_WORKSPACE_ID}
SID = "5e1f0000-0000-0000-0000-000000000001"
TOKEN = "Bearer mcp-secret-token"


def _server(**over):
    row = {
        "id": SID,
        "name": "crm",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "header_names": ["Authorization"],
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
    }
    row.update(over)
    return row


def test_stdio_is_refused_with_the_reason(client, mocks):
    r = client.post(
        "/integrations/mcp-servers",
        json={"name": "local", "transport": "stdio", "url": "http://x"},
        headers=WS,
    )
    assert r.status_code == 422
    assert "local process" in r.text
    mocks.mcp_registry_store.create_server.assert_not_called()


def test_register_stores_headers_and_never_returns_them(client, mocks):
    mocks.mcp_registry_store.create_server.return_value = _server()
    r = client.post(
        "/integrations/mcp-servers",
        json={"name": "crm", "transport": "http", "url": "https://mcp.example.com/mcp",
              "headers": {"Authorization": TOKEN}},
        headers=WS,
    )
    assert r.status_code == 201, r.text
    assert TOKEN not in r.text
    assert mocks.mcp_registry_store.create_server.await_args.kwargs["headers"] == {"Authorization": TOKEN}


def test_list_names_opted_in_agents(client, mocks):
    mocks.mcp_registry_store.list_servers.return_value = [_server()]
    mocks.registry.list_backends.return_value = [
        backend_row(agent_id="in", config={"name": "In", "workspace_mcp_servers": [SID]}),
        backend_row(agent_id="out"),
    ]
    r = client.get("/integrations/mcp-servers", headers=WS)
    assert r.json()["data"][0]["agents"] == ["in"]


def test_an_edit_reaches_only_the_agents_opted_in(client, mocks):
    mocks.mcp_registry_store.update_server.return_value = _server()
    mocks.registry.list_backends.return_value = [
        backend_row(agent_id="in", api_key="tc_in", config={"name": "In", "workspace_mcp_servers": [SID]}),
        backend_row(agent_id="out", api_key="tc_out"),
    ]
    rotated = {"name": "crm", "url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer new"}}
    mocks.mcp_registry_store.resolve.return_value = [rotated]

    r = client.put(f"/integrations/mcp-servers/{SID}", json=rotated, headers=WS)

    assert r.status_code == 200, r.text
    pushed = mocks.client.update_agent.await_args_list
    assert [c.args[0] for c in pushed] == ["in"]
    assert pushed[0].args[1]["config"]["mcp_servers"] == [rotated]


def test_removing_strips_it_from_agents_first(client, mocks):
    mocks.mcp_registry_store.get_server.return_value = _server()
    mocks.registry.list_backends.return_value = [
        backend_row(agent_id="in", config={"name": "In", "workspace_mcp_servers": [SID, "other"]}),
    ]
    order = []
    mocks.client.update_agent.side_effect = lambda *a: order.append("push")
    mocks.mcp_registry_store.delete_server.side_effect = lambda *a: order.append("delete")

    r = client.delete(f"/integrations/mcp-servers/{SID}", headers=WS)

    assert r.status_code == 200
    assert order == ["push", "delete"]
    stored = mocks.registry.update_config.await_args.args[2]
    assert stored["workspace_mcp_servers"] == ["other"]


def test_editors_cannot_register(gate_client, mocks):
    mocks.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}
    mocks.auth_store.resolve_membership.return_value = "editor"
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.post(
        "/integrations/mcp-servers",
        json={"name": "crm", "url": "https://mcp.example.com/mcp"},
        headers=WS,
    )
    assert r.status_code == 403


def test_inline_servers_are_ordered_ahead_of_registered_ones():
    """TurnCall resolves a tool-name collision by server order — the agent's own
    server is the one its author just typed, so it goes first."""
    cfg = {
        "name": "A",
        "mcp_servers": [{"name": "mine", "transport": "http", "url": "https://mine.test"}],
    }
    registered = [{"name": "crm", "transport": "http", "url": "https://crm.test", "headers": {"A": "b"}}]
    servers = to_create_agent_request(cfg, registered_mcp_servers=registered)["config"]["mcp_servers"]
    assert [s["name"] for s in servers] == ["mine", "crm"]


def test_an_agent_that_did_not_opt_in_gets_no_registered_server():
    cfg = {"name": "A"}
    assert "mcp_servers" not in to_create_agent_request(cfg, registered_mcp_servers=[])["config"]


def test_an_agents_own_llm_key_reaches_turncall():
    """The pin (ADR-0023): dropped by the mapper before, so it pinned nothing."""
    cfg = {"name": "A", "llm": {"provider": "anthropic", "model": "m", "api_key": "sk-own"}}
    assert to_create_agent_request(cfg)["config"]["llm"]["api_key"] == "sk-own"
    assert "api_key" not in to_create_agent_request({"name": "A"})["config"]["llm"]
