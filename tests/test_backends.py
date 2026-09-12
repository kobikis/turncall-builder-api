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

# --- a clone has to be runnable, and must not carry secrets -------------------


def _rendered():
    from app.backends.scaffold import render

    return render(
        slug="demo",
        port=9101,
        tools=[],
        tool_bodies={},
        agent_id="agent-1",
        webhook_secret="supersecret",
        call_init_secret="ci-secret",
    )


def test_env_example_is_committed_alongside_the_gitignored_env():
    files = _rendered()
    assert ".env.example" in files
    assert ".env" in files[".gitignore"].split()


def test_env_example_carries_no_secret_values():
    """It is in git — a secret's value here is a leaked credential.

    Keyed on the variable name rather than an allowlist of literals: a non-secret
    default (an origin, a URL) is useful to ship, and a SECRET/TOKEN/KEY with a
    value is the thing that must never appear.
    """
    files = _rendered()
    for line in files[".env.example"].splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if any(word in key for word in ("SECRET", "TOKEN", "KEY", "PASSWORD")):
            assert value == "", f"{key} has a value"
    assert "supersecret" not in files[".env.example"]
    assert "ci-secret" not in files[".env.example"]


def test_the_real_env_still_gets_the_secrets():
    files = _rendered()
    assert "WEBHOOK_SECRET=supersecret" in files[".env"]
    assert "CALL_INIT_SECRET=ci-secret" in files[".env"]


def test_env_example_names_every_variable_the_app_reads():
    files = _rendered()
    for var in (
        "AGENT_ID",
        "WEBHOOK_SECRET",
        "CALL_INIT_SECRET",
        "CONSOLE_ORIGIN",
        "BUILDER_URL",
        "BUILDER_TOKEN",
    ):
        assert f"{var}=" in files[".env.example"]


def test_readme_tells_you_how_to_run_a_clone():
    assert "cp .env.example .env" in _rendered()["README.md"]


def test_the_backend_token_is_not_the_webhook_secret(tmp_path):
    """ADR-0019. TurnCall holds WEBHOOK_SECRET — it signs TurnCall's requests
    *into* this backend. Reusing it outbound would let TurnCall mint Pipedream
    tokens for every Workspace it serves."""
    env = scaffold.render(
        slug="a",
        port=9001,
        tools=[],
        webhook_secret="supersecret",
        builder_token="bt-distinct",
    )[".env"]
    assert "BUILDER_TOKEN=bt-distinct" in env
    assert "WEBHOOK_SECRET=supersecret" in env


def test_the_backend_asks_the_builder_for_a_pipedream_token():
    """It holds no Pipedream credential of its own (ADR-0018)."""
    app_py = scaffold.render(slug="a", port=9001, tools=[])["app.py"]
    assert "/integrations/token" in app_py
    assert "BUILDER_TOKEN" in app_py
    # Cached: Pipedream tokens live an hour, a call is not the unit of anything.
    assert "expires_at" in app_py
    # And no Pipedream credential is ever baked in.
    assert "PIPEDREAM_CLIENT_SECRET" not in app_py


# --- call-init with attached apps (ADR-0018/0020, #27) ------------------------


def _call_init(monkeypatch, tmp_path, *, builder=None, **env):
    """Drive the generated /call-init, with `builder` standing in for builder-api.

    `builder` is called instead of the HTTP POST — None means the backend has no
    builder configured at all.
    """
    import hashlib
    import hmac
    import json as json_mod
    import time

    from fastapi.testclient import TestClient

    monkeypatch.setenv("AGENT_ID", "agent-123")
    monkeypatch.setenv("CALL_INIT_SECRET", "cisecret")
    monkeypatch.setenv("BUILDER_URL", env.get("builder_url", "http://builder:8000"))
    monkeypatch.setenv("BUILDER_TOKEN", env.get("builder_token", "bt-1"))
    monkeypatch.chdir(tmp_path)

    files = scaffold.render(slug="x-1", port=9001, tools=[], agent_id="agent-123")
    ns: dict = {}
    exec(compile(files["app.py"], "app.py", "exec"), ns)  # noqa: S102 — our own scaffold
    if builder is not None:
        monkeypatch.setitem(ns, "httpx", _FakeHttpx(builder))

    body = json_mod.dumps({"payload": {"customer": {"number": "+14445556666"}}})
    ts = str(int(time.time()))
    sig = "v1=" + hmac.new(b"cisecret", f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return TestClient(ns["app"]).post(
        "/call-init",
        content=body,
        headers={"X-TurnCall-Signature": sig, "X-TurnCall-Timestamp": ts},
    )


class _FakeHttpx:
    """Stands in for the httpx module the generated backend imports."""

    def __init__(self, respond):
        self._respond = respond

    def AsyncClient(self, **kw):  # noqa: N802 — mirrors httpx's own name
        respond = self._respond

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, **kw):
                return respond(url, **kw)

        return _Client()


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_an_agent_with_attached_apps_answers_with_an_inline_config(monkeypatch, tmp_path):
    """TurnCall never templates MCP headers and a Pipedream token lives an hour,
    so credentials can only reach a call this way (ADR-0020)."""
    agent = {"system_prompt": "hi", "mcp_servers": [{"name": "pipedream_slack_v2"}]}
    r = _call_init(
        monkeypatch, tmp_path, builder=lambda url, **kw: _Resp({"data": {"agent": agent}})
    )
    data = r.json()
    assert data["agent"] == agent
    # Exactly one: TurnCall ignores an inline agent whenever agent_id is present,
    # so sending both would silently drop the credentials.
    assert "agent_id" not in data


def test_an_agent_with_no_attached_apps_still_answers_with_its_id(monkeypatch, tmp_path):
    r = _call_init(monkeypatch, tmp_path, builder=lambda url, **kw: _Resp({"data": {"agent": None}}))
    assert r.json()["agent_id"] == "agent-123"
    assert "agent" not in r.json()


def test_a_minting_failure_costs_the_tools_and_never_the_call(monkeypatch, tmp_path):
    def _boom(url, **kw):
        raise RuntimeError("pipedream is down")

    r = _call_init(monkeypatch, tmp_path, builder=_boom)
    assert r.status_code == 200
    assert r.json()["agent_id"] == "agent-123"
    # The caller still gets a call, and still gets their own context.
    assert "+14445556666" in r.json()["dynamic_data"]["knowledge_context"]


def test_a_builder_error_response_is_also_survivable(monkeypatch, tmp_path):
    r = _call_init(monkeypatch, tmp_path, builder=lambda url, **kw: _Resp({}, status=503))
    assert r.status_code == 200
    assert r.json()["agent_id"] == "agent-123"


def test_a_backend_with_no_builder_configured_behaves_as_before(monkeypatch, tmp_path):
    r = _call_init(monkeypatch, tmp_path, builder=lambda url, **kw: _Resp({}), builder_token="")
    assert r.json()["agent_id"] == "agent-123"


def test_the_call_init_request_carries_this_backends_own_credential(monkeypatch, tmp_path):
    seen = {}

    def _capture(url, **kw):
        seen["url"] = url
        seen["headers"] = kw.get("headers", {})
        return _Resp({"data": {"agent": None}})

    _call_init(monkeypatch, tmp_path, builder=_capture)
    assert seen["url"].endswith("/integrations/call-config")
    assert seen["headers"]["Authorization"] == "Bearer bt-1"


def test_no_pipedream_credential_is_in_the_pushed_file_set():
    """ADR-0018: the builder never puts a third-party credential in a repo, and
    the one credential this backend does hold lives only in the gitignored .env."""
    files = scaffold.render(
        slug="x-1", port=9001, tools=[], builder_token="bt-secret-value"
    )
    pushed = {name: body for name, body in files.items() if name != ".env"}
    for name, body in pushed.items():
        assert "bt-secret-value" not in body, f"{name} carries the backend credential"
        assert "PIPEDREAM_CLIENT_SECRET" not in body, name
    assert ".env" in files[".gitignore"]


# --- the call.ended Automation (ADR-0018, #28) --------------------------------


def _events(monkeypatch, tmp_path, *, event="call.ended", payload=None, channel="",
            responses=None):
    """Drive the generated /events with a signed envelope.

    `responses` stands in for every outbound HTTP call the backend makes (the
    builder's mint endpoint, then Pipedream); it records them and is what the
    assertions read.
    """
    import hashlib
    import hmac
    import json as json_mod
    import time

    from fastapi.testclient import TestClient

    monkeypatch.setenv("AGENT_ID", "agent-123")
    monkeypatch.setenv("WEBHOOK_SECRET", "evsecret")
    monkeypatch.setenv("BUILDER_URL", "http://builder:8000")
    monkeypatch.setenv("BUILDER_TOKEN", "bt-1")
    monkeypatch.chdir(tmp_path)

    files = scaffold.render(slug="x-1", port=9001, tools=[], agent_id="agent-123")
    ns: dict = {}
    exec(compile(files["app.py"], "app.py", "exec"), ns)  # noqa: S102 — our own scaffold
    ns["SLACK_CHANNEL"] = channel
    calls: list = []

    def _respond(url, **kw):
        calls.append({"url": url, **kw})
        if responses is not None:
            return responses(url, **kw)
        if url.endswith("/integrations/token"):
            return _Resp({"data": {
                "access_token": "pd_tok",
                "expires_at": 9_999_999_999.0,
                "external_user_id": "ws-1",
                "project_id": "proj_1",
                "environment": "development",
                "accounts": [{"id": "apn_slack", "app": "slack_v2"}],
            }})
        return _Resp({"ok": True})

    monkeypatch.setitem(ns, "httpx", _FakeHttpx(_respond))

    body = json_mod.dumps({
        "event": event,
        "payload": payload if payload is not None else {"summary": "Booked a demo"},
    })
    ts = str(int(time.time()))
    sig = "v1=" + hmac.new(b"evsecret", f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    resp = TestClient(ns["app"]).post(
        "/events",
        content=body,
        headers={"X-TurnCall-Signature": sig, "X-TurnCall-Timestamp": ts},
    )
    return resp, calls


def test_the_scaffold_ships_a_call_ended_automation_with_the_action_filled_in():
    app_py = scaffold.render(slug="a", port=9001, tools=[])["app.py"]
    assert "async def on_call_ended" in app_py
    # The Action is pre-filled; only the target is left to the developer.
    assert "slack_v2-post-message" in app_py
    assert 'SLACK_CHANNEL = ""' in app_py
    assert "TODO" in app_py


def test_a_call_ending_posts_the_summary(monkeypatch, tmp_path):
    resp, calls = _events(
        monkeypatch,
        tmp_path,
        channel="#sales-calls",
        payload={
            "summary": "Booked a demo",
            "from_number": "+15550001111",
            "to_number": "+15552223333",
            "duration_ms": 92_000,
            "ended_reason": "customer_ended_call",
        },
    )

    assert resp.status_code == 200
    run = next(c for c in calls if "actions/run" in c["url"])
    body = run["json"]
    assert body["id"] == "slack_v2-post-message"
    assert body["external_user_id"] == "ws-1"
    # The account rides under the app slug; the action's inputs are its siblings.
    assert body["configured_props"]["slack_v2"] == {"authProvisionId": "apn_slack"}
    assert body["configured_props"]["channel"] == "#sales-calls"
    text = body["configured_props"]["text"]
    # It receives the whole call.ended payload, not just a summary string.
    assert "Booked a demo" in text
    assert "+15550001111" in text and "+15552223333" in text
    assert "92s" in text and "customer_ended_call" in text


def test_it_holds_no_credential_and_asks_the_builder_for_a_token(monkeypatch, tmp_path):
    _, calls = _events(monkeypatch, tmp_path, channel="#c")
    mint = next(c for c in calls if c["url"].endswith("/integrations/token"))
    assert mint["headers"]["Authorization"] == "Bearer bt-1"
    run = next(c for c in calls if "actions/run" in c["url"])
    assert run["headers"]["Authorization"] == "Bearer pd_tok"
    assert run["headers"]["X-PD-Environment"] == "development"


def test_an_untouched_automation_does_nothing(monkeypatch, tmp_path):
    """An agent whose handler was never edited must post nothing and fail no call."""
    resp, calls = _events(monkeypatch, tmp_path, channel="")
    assert resp.status_code == 200
    assert calls == []


def test_it_fires_on_the_event_not_on_every_event(monkeypatch, tmp_path):
    resp, calls = _events(monkeypatch, tmp_path, event="call.started", channel="#c")
    assert resp.status_code == 200
    assert calls == []


def test_a_failed_post_never_becomes_the_calls_problem(monkeypatch, tmp_path):
    """The call has already ended. Log and move on (ADR-0018)."""

    def _explode(url, **kw):
        if "actions/run" in url:
            raise RuntimeError("pipedream is down")
        return _Resp({"data": {
            "access_token": "pd_tok", "expires_at": 9_999_999_999.0,
            "external_user_id": "ws-1", "project_id": "p", "environment": "development",
            "accounts": [{"id": "apn_slack", "app": "slack_v2"}],
        }})

    resp, _ = _events(monkeypatch, tmp_path, channel="#c", responses=_explode)
    assert resp.status_code == 200


def test_a_workspace_with_no_slack_connected_is_skipped_not_crashed(monkeypatch, tmp_path):
    def _no_accounts(url, **kw):
        if url.endswith("/integrations/token"):
            return _Resp({"data": {
                "access_token": "pd_tok", "expires_at": 9_999_999_999.0,
                "external_user_id": "ws-1", "project_id": "p",
                "environment": "development", "accounts": [],
            }})
        return _Resp({"ok": True})

    resp, calls = _events(monkeypatch, tmp_path, channel="#c", responses=_no_accounts)
    assert resp.status_code == 200
    assert not any("actions/run" in c["url"] for c in calls)


def test_the_event_is_stored_even_when_the_automation_blows_up(monkeypatch, tmp_path):
    """An Automation failing must not cost us the record that triggered it.

    Proves the outcome, not the ordering: the handler swallows its own failures,
    so store-before-run is defence against a future edit rather than something
    this test can distinguish.
    """
    import json as json_mod
    import sqlite3

    def _explode(url, **kw):
        raise RuntimeError("everything is on fire")

    resp, _ = _events(monkeypatch, tmp_path, channel="#c", responses=_explode)
    assert resp.status_code == 200
    rows = sqlite3.connect(tmp_path / "events.db").execute(
        "SELECT payload FROM events"
    ).fetchall()
    assert json_mod.loads(rows[0][0])["event"] == "call.ended"
