"""Tests for Agent Backend generation — pure pieces (no docker/network)."""

import ast

from app.backends import scaffold, toolgen
from app.backends.diff import tools_changed
from app.mapper import to_create_agent_request


def test_slugify():
    assert scaffold.slugify("My Hotel Receptionist!") == "my-hotel-receptionist"
    assert scaffold.slugify("") == "agent"


def test_rendered_app_is_valid_python_with_tools():
    tools = [
        {"name": "cancel_reservation", "description": "Cancel a booking"},
        {"name": "lookup_order", "description": "Find an order"},
    ]
    bodies = {"cancel_reservation": '    return {"status": "cancelled", "args": args}'}
    files = scaffold.render(slug="hotel-abc123", port=9007, tools=tools, tool_bodies=bodies)

    # every generated file exists
    for f in ("app.py", "Dockerfile", "docker-compose.yml", "requirements.txt"):
        assert f in files
    # the generated app.py must be syntactically valid Python
    ast.parse(files["app.py"])
    # both tools registered; the one without an LLM body falls back to a stub
    assert "'cancel_reservation': tool_cancel_reservation" in files["app.py"]
    assert "tool_lookup_order" in files["app.py"]
    assert "9007:8000" in files["docker-compose.yml"]


def test_rendered_app_valid_with_no_tools():
    files = scaffold.render(slug="x-1", port=9001, tools=[])
    ast.parse(files["app.py"])


def test_mapper_wires_custom_tool_webhook_url():
    cfg = {
        "name": "Hotel",
        "system_prompt": "hi",
        "custom_tools": [{"name": "cancel_reservation", "description": "cancel"}],
    }
    req = to_create_agent_request(cfg, tools_base_url="http://host.docker.internal:9007")
    tool = next(t for t in req["config"]["tools"] if t["name"] == "cancel_reservation")
    assert tool["webhook_url"] == "http://host.docker.internal:9007/tools/cancel_reservation"


def test_mapper_omits_webhook_url_without_base():
    cfg = {"name": "Hotel", "custom_tools": [{"name": "cancel_reservation", "description": "c"}]}
    req = to_create_agent_request(cfg)  # no base url (e.g. display path)
    tool = next(t for t in req["config"]["tools"] if t["name"] == "cancel_reservation")
    assert "webhook_url" not in tool


def test_tools_changed_detects_tool_edits_only():
    a = {"name": "X", "system_prompt": "hi", "custom_tools": [{"name": "cancel", "description": "c"}]}
    # prompt/voice-only edit -> no regen
    assert tools_changed(a, {**a, "system_prompt": "bye"}) is False
    # reordering same tools -> no regen
    two = [{"name": "b", "description": "1"}, {"name": "a", "description": "2"}]
    assert tools_changed({"custom_tools": two}, {"custom_tools": list(reversed(two))}) is False
    # adding a tool -> regen
    assert tools_changed(a, {**a, "custom_tools": a["custom_tools"] + [{"name": "book", "description": "b"}]}) is True
    # changing a tool's params -> regen
    assert tools_changed(
        {"custom_tools": [{"name": "cancel", "description": "c"}]},
        {"custom_tools": [{"name": "cancel", "description": "c", "parameters_schema": {"x": 1}}]},
    ) is True


def test_toolgen_validates_and_normalizes_bodies():
    assert toolgen._valid('    return {"ok": True}')
    assert not toolgen._valid("return (")  # broken syntax
    # dedents then re-indents to 4 spaces so it slots into the function
    assert toolgen._normalize('return {"ok": True}') == '    return {"ok": True}'


def test_tool_name_validation_rejects_injection():
    import pytest

    bad = [{"name": "x(a): pass\nimport os", "description": "evil"}]
    with pytest.raises(ValueError, match="invalid tool name"):
        scaffold.render(slug="x-1", port=9001, tools=bad)
    with pytest.raises(ValueError, match="invalid tool name"):
        to_create_agent_request({"name": "X", "custom_tools": bad})


def test_secrets_live_in_env_not_compose():
    files = scaffold.render(
        slug="x-1", port=9001, tools=[], agent_id="a-1",
        webhook_secret="whsec", call_init_secret="cisec",
    )
    assert "whsec" not in files["docker-compose.yml"]
    assert "env_file: .env" in files["docker-compose.yml"]
    assert "WEBHOOK_SECRET=whsec" in files[".env"]
    assert "CALL_INIT_SECRET=cisec" in files[".env"]
    assert "AGENT_ID=a-1" in files[".env"]
    assert ".env" in files[".gitignore"]


def test_generated_app_fails_closed_and_serves_call_init():
    import ast as ast_mod

    files = scaffold.render(slug="x-1", port=9001, tools=[], agent_id="a-1")
    app_py = files["app.py"]
    ast_mod.parse(app_py)
    # fail closed: empty secret rejects instead of accepting
    assert "return False" in app_py.split("def _verify", 1)[1].split("expected", 1)[0]
    # managed call-init endpoint + pinned CORS + verified tools
    assert '@app.post("/call-init")' in app_py
    assert "allow_origins=_ORIGINS" in app_py
    assert 'allow_origins=["*"]' not in app_py
    assert "knowledge_context" in app_py


def test_mapper_signs_tools_when_secret_given():
    cfg = {"name": "X", "custom_tools": [{"name": "cancel", "description": "c"}]}
    req = to_create_agent_request(cfg, tools_base_url="http://b:9007", tools_secret="s" * 32)
    tool = next(t for t in req["config"]["tools"] if t["name"] == "cancel")
    assert tool["webhook_secret"] == "s" * 32
    # builtins never get a webhook secret
    req2 = to_create_agent_request({"name": "X", "tools": ["end_call"]}, tools_secret="s" * 32)
    builtin = next(t for t in req2["config"]["tools"] if t["name"] == "end_call")
    assert "webhook_secret" not in builtin


def test_set_env_value_replaces_in_place(tmp_path):
    from app.backends.generator import set_env_value

    d = str(tmp_path)
    set_env_value(d, "CALL_INIT_SECRET", "one")
    set_env_value(d, "WEBHOOK_SECRET", "w")
    set_env_value(d, "CALL_INIT_SECRET", "two")
    content = (tmp_path / ".env").read_text()
    assert "CALL_INIT_SECRET=two" in content
    assert "CALL_INIT_SECRET=one" not in content
    assert "WEBHOOK_SECRET=w" in content


def test_generated_backend_verifies_call_init_end_to_end(tmp_path, monkeypatch):
    """Execute the generated app.py: unsigned requests 401, signed ones answer."""
    import hashlib
    import hmac
    import json as json_mod
    import time

    from fastapi.testclient import TestClient

    monkeypatch.setenv("AGENT_ID", "agent-123")
    monkeypatch.setenv("WEBHOOK_SECRET", "evsecret")
    monkeypatch.setenv("CALL_INIT_SECRET", "cisecret")
    monkeypatch.chdir(tmp_path)  # events.db lands in tmp

    files = scaffold.render(slug="x-1", port=9001, tools=[], agent_id="agent-123")
    ns: dict = {}
    exec(compile(files["app.py"], "app.py", "exec"), ns)  # noqa: S102 — our own scaffold
    client = TestClient(ns["app"])

    body = json_mod.dumps({"payload": {"customer": {"number": "+14445556666"}}})
    assert client.post("/call-init", content=body).status_code == 401  # unsigned

    ts = str(int(time.time()))
    sig = "v1=" + hmac.new(b"cisecret", f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    resp = client.post(
        "/call-init",
        content=body,
        headers={"X-TurnCall-Signature": sig, "X-TurnCall-Timestamp": ts},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_id"] == "agent-123"
    assert "+14445556666" in data["dynamic_data"]["knowledge_context"]

    # events endpoint fails closed with the OTHER secret missing
    monkeypatch.setenv("WEBHOOK_SECRET", "")
    ns2: dict = {}
    exec(compile(files["app.py"], "app.py", "exec"), ns2)  # noqa: S102
    assert TestClient(ns2["app"]).post("/events", content="{}").status_code == 401


def test_toolgen_skips_malformed_body_items():
    """The model sometimes emits bare strings in `bodies` — those tools stub
    out; a malformed item must never raise (it killed agent creation once)."""
    from types import SimpleNamespace

    resp = SimpleNamespace(content=[
        SimpleNamespace(type="tool_use", name="tool_bodies", input={"bodies": [
            "return {'oops': 'bare string'}",                     # malformed
            {"name": "good_tool", "body": 'return {"ok": True}'}, # valid
            {"body": "return {}"},                                # missing name
            {"name": "bad_syntax", "body": "return ("},           # invalid python
        ]}),
    ])
    out = toolgen._parse_bodies(resp)
    assert list(out) == ["good_tool"]


def test_explicit_null_server_url_is_not_a_tool_change():
    """Config normalization adds server_url: null — legacy configs without the
    key must not regenerate on their next save."""
    old = {"custom_tools": [{"name": "book", "description": "b"}]}
    new = {"custom_tools": [{"name": "book", "description": "b", "server_url": None}]}
    assert tools_changed(old, new) is False
    assert tools_changed(old, {"custom_tools": [{"name": "book", "description": "b", "server_url": "https://x.io"}]}) is True


def test_stub_tool_echoes_its_own_name():
    """The stub body references the `name` parameter (no substring .replace),
    so a generated stub returns its own tool name."""
    import asyncio

    fn_src = scaffold._tool_function({"name": "lookup_order"}, None)
    ast.parse(fn_src)  # valid python
    ns: dict = {}
    exec(fn_src, ns)  # noqa: S102 — generated code under test
    result = asyncio.run(ns["tool_lookup_order"]({"q": 1}))
    assert result["tool"] == "lookup_order"
    assert result["arguments"] == {"q": 1}
