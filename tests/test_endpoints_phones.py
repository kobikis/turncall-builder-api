"""Phone-number endpoints: list, get, bind, unbind, routing validation."""

import httpx

from tests.conftest import backend_row


def _phone(**over):
    n = {
        "id": "pn-1",
        "e164": "+15551234567",
        "sid": "PN" + "a" * 32,
        "routing_type": "agent",
        "agent_id": "a1",
        "project_id": "p1",
        "server_url": None,
        "server_url_secret": None,
        "sms_enabled": False,
    }
    n.update(over)
    return n


def test_list_phone_numbers(client, mocks):
    mocks.phones.list_numbers.return_value = [_phone()]
    r = client.get("/phone-numbers")
    assert r.status_code == 200
    assert r.json()["data"]["phone_numbers"][0]["id"] == "pn-1"


def test_get_phone_number_404(client, mocks):
    mocks.phones.get_number.return_value = None
    assert client.get("/phone-numbers/nope").status_code == 404


def _bind_body(**over):
    b = {
        "e164": "+15551234567",
        "sid": "PN" + "a" * 32,
        "routing_type": "agent",
        "agent_id": "a1",
        "sms_enabled": False,
    }
    b.update(over)
    return b


def test_bind_unassigned_only_mirrors_locally(client, mocks):
    # routing_type "none" never touches TurnCall — just a local mirror row.
    r = client.post("/phone-numbers", json=_bind_body(routing_type="none", agent_id=None))
    assert r.status_code == 200
    assert r.json()["data"]["routing_type"] == "none"
    mocks.client.bind_phone_number.assert_not_called()
    mocks.phones.record_number.assert_awaited_once()


def test_bind_agent_routes_through_turncall(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.bind_phone_number.return_value = {
        "id": "pn-9",
        "twilio_webhooks_configured": True,
    }
    r = client.post("/phone-numbers", json=_bind_body())
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["id"] == "pn-9"
    assert data["twilio_webhooks_configured"] is True


def test_bind_agent_routing_requires_agent_id(client, mocks):
    r = client.post("/phone-numbers", json=_bind_body(agent_id=None))
    assert r.status_code == 400


def test_bind_agent_not_found_404(client, mocks):
    mocks.registry.get_backend.return_value = None
    assert client.post("/phone-numbers", json=_bind_body()).status_code == 404


def test_update_phone_number_404(client, mocks):
    mocks.phones.get_number.return_value = None
    r = client.put("/phone-numbers/nope", json=_bind_body())
    assert r.status_code == 404


def test_update_same_project_is_in_place(client, mocks):
    # Same owning project -> TurnCall PUT, id + secret stable, rebound False.
    mocks.phones.get_number.return_value = _phone(
        routing_type="agent", agent_id="a1", project_id="p1"
    )
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1", project_id="p1")
    mocks.client.update_phone_number.return_value = {
        "server_url_secret": None,
        "twilio_webhooks_configured": True,
    }
    r = client.put("/phone-numbers/pn-1", json=_bind_body(agent_id="a1"))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["rebound"] is False
    assert data["id"] == "pn-1"  # id stable
    mocks.client.update_phone_number.assert_awaited_once()


def test_update_cross_project_rebinds(client, mocks):
    # Different owning project -> unbind + rebind, rebound True, old mirror dropped.
    mocks.phones.get_number.return_value = _phone(
        routing_type="agent", agent_id="a1", project_id="p1"
    )
    # _resolve_routing (new) resolves to a different project; _key_for_number
    # (unbind) + _bind both call get_backend — return the new-project backend.
    mocks.registry.get_backend.return_value = backend_row(agent_id="a2", project_id="p2")
    mocks.client.bind_phone_number.return_value = {"id": "pn-2", "twilio_webhooks_configured": True}
    r = client.put("/phone-numbers/pn-1", json=_bind_body(agent_id="a2"))
    assert r.status_code == 200
    assert r.json()["data"]["rebound"] is True
    mocks.client.unbind_phone_number.assert_awaited_once()
    mocks.phones.delete_number.assert_awaited_once()  # old mirror removed after new bind


def test_delete_phone_number_404(client, mocks):
    mocks.phones.get_number.return_value = None
    assert client.delete("/phone-numbers/nope").status_code == 404


def test_delete_unassigned_skips_turncall(client, mocks):
    mocks.phones.get_number.return_value = _phone(routing_type="none", agent_id=None)
    r = client.delete("/phone-numbers/pn-1")
    assert r.status_code == 200
    mocks.client.unbind_phone_number.assert_not_called()
    mocks.phones.delete_number.assert_awaited_once()


def test_delete_agent_routed_unbinds_in_turncall(client, mocks):
    mocks.phones.get_number.return_value = _phone(routing_type="agent", agent_id="a1")
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    r = client.delete("/phone-numbers/pn-1")
    assert r.status_code == 200
    mocks.client.unbind_phone_number.assert_awaited_once()


def test_delete_maps_turncall_unreachable_to_502(client, mocks):
    mocks.phones.get_number.return_value = _phone(routing_type="agent", agent_id="a1")
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.unbind_phone_number.side_effect = httpx.ConnectError("down")
    r = client.delete("/phone-numbers/pn-1")
    assert r.status_code == 502


def test_delete_treats_turncall_404_as_already_unbound(client, mocks):
    mocks.phones.get_number.return_value = _phone(routing_type="agent", agent_id="a1")
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    resp = httpx.Response(404, request=httpx.Request("DELETE", "http://tc/x"))
    mocks.client.unbind_phone_number.side_effect = httpx.HTTPStatusError(
        "nf", request=resp.request, response=resp
    )
    r = client.delete("/phone-numbers/pn-1")
    assert r.status_code == 200  # already gone in TurnCall = success
    mocks.phones.delete_number.assert_awaited_once()


def _conflict(project_id: str = "p-other") -> httpx.HTTPStatusError:
    """TurnCall's 409 when the e164 is already bound — anywhere, in any project.
    Its uniqueness is global on the number, not scoped to the caller's project."""
    resp = httpx.Response(
        409,
        json={
            "success": False,
            "error": "Phone number +15551234567 is already bound",
            "code": "conflict",
            "details": {"existing_project_id": project_id},
        },
        request=httpx.Request("POST", "http://tc/v1/phone-numbers"),
    )
    return httpx.HTTPStatusError("conflict", request=resp.request, response=resp)


def test_bind_conflict_is_a_409_not_a_500(client, mocks):
    """A number bound outside the builder used to surface as an ASGI stack
    trace: _bind called raise_for_status() with nothing catching it."""
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.bind_phone_number.side_effect = _conflict()

    r = client.post("/phone-numbers", json=_bind_body())

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "+15551234567" in detail
    assert "p-other" in detail, "the operator needs to know which project holds it"


def test_bind_maps_turncall_unreachable_to_502(client, mocks):
    """Matches the unbind path, which already does this."""
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.bind_phone_number.side_effect = httpx.ConnectError("down")

    r = client.post("/phone-numbers", json=_bind_body())
    assert r.status_code == 502


def test_bind_other_turncall_error_is_502_with_its_message(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    resp = httpx.Response(400, text="bad routing target", request=httpx.Request("POST", "http://tc/x"))
    mocks.client.bind_phone_number.side_effect = httpx.HTTPStatusError(
        "bad", request=resp.request, response=resp
    )

    r = client.post("/phone-numbers", json=_bind_body())
    assert r.status_code == 502
    assert "bad routing target" in r.json()["detail"]


def test_update_of_a_number_turncall_already_holds_explains_itself(client, mocks):
    """The reported bug. The mirror says 'none', so the in-place PUT path is
    skipped and so is the unbind — then the bind hits TurnCall's conflict,
    because a number bound outside the builder is invisible to the mirror."""
    mocks.phones.get_number.return_value = _phone(
        routing_type="none", agent_id=None, project_id=None
    )
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.bind_phone_number.side_effect = _conflict()

    r = client.put("/phone-numbers/pn-1", json=_bind_body())

    assert r.status_code == 409
    mocks.client.unbind_phone_number.assert_not_called()


# --- a number's own Twilio account --------------------------------------------

AC = "AC" + "b" * 32
TOKEN = "twilio-token-secret"


def test_bind_with_an_own_twilio_account_sends_it_and_never_returns_it(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.bind_phone_number.return_value = {
        "id": "pn-9",
        "external_number_sid": "PNlooked",
        "twilio_webhooks_configured": True,
    }
    body = _bind_body(sid="", twilio_account_sid=AC, twilio_auth_token=TOKEN)

    r = client.post("/phone-numbers", json=body)

    assert r.status_code == 200, r.text
    payload = mocks.client.bind_phone_number.await_args.args[0]
    assert payload["twilio_account_sid"] == AC
    assert payload["twilio_auth_token"] == TOKEN
    assert "external_number_sid" not in payload  # TurnCall looks it up
    mirrored = mocks.phones.record_number.await_args.args[1]
    assert mirrored["sid"] == "PNlooked"
    assert TOKEN not in mirrored["twilio_auth_token_encrypted"]
    data = r.json()["data"]
    assert TOKEN not in r.text and "twilio_auth_token_encrypted" not in data
    assert data["twilio_credentials"] == "number"
    assert data["twilio_account_sid"] == AC


def test_half_a_twilio_pair_is_refused(client, mocks):
    r = client.post("/phone-numbers", json=_bind_body(twilio_account_sid=AC))
    assert r.status_code == 422
    mocks.client.bind_phone_number.assert_not_called()


def test_a_rebind_carries_the_stored_twilio_account(client, mocks):
    """TurnCall never returns the token, so moving a number between projects
    can only re-send the mirror's sealed copy."""
    mocks.phones.get_number.return_value = _phone(routing_type="webhook", agent_id=None, project_id="wp")
    mocks.phones.twilio_account.return_value = (AC, TOKEN)
    mocks.phones.ensure_webhook_project.return_value = {"project_id": "wp", "api_key": "tc_wp"}
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1", project_id="p1")
    mocks.client.bind_phone_number.return_value = {"id": "pn-new", "external_number_sid": "PN1"}

    r = client.put("/phone-numbers/pn-1", json=_bind_body())

    assert r.status_code == 200, r.text
    payload = mocks.client.bind_phone_number.await_args.args[0]
    assert (payload["twilio_account_sid"], payload["twilio_auth_token"]) == (AC, TOKEN)


def test_an_in_place_edit_sends_twilio_only_when_re_entered(client, mocks):
    mocks.phones.get_number.return_value = _phone()
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1", project_id="p1")
    mocks.client.update_phone_number.return_value = {"id": "pn-1"}

    client.put("/phone-numbers/pn-1", json=_bind_body())
    assert "twilio_auth_token" not in mocks.client.update_phone_number.await_args.args[1]

    client.put("/phone-numbers/pn-1", json=_bind_body(twilio_account_sid=AC, twilio_auth_token="rotated"))
    assert mocks.client.update_phone_number.await_args.args[1]["twilio_auth_token"] == "rotated"


def test_a_label_is_mirrored_trimmed(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.bind_phone_number.return_value = {"id": "pn-9"}
    r = client.post("/phone-numbers", json=_bind_body(label="  Support line "))
    assert r.status_code == 200
    assert mocks.phones.record_number.await_args.args[1]["label"] == "Support line"
    assert "label" not in mocks.client.bind_phone_number.await_args.args[0]  # builder-only


def test_a_label_too_long_is_refused(client, mocks):
    assert client.post("/phone-numbers", json=_bind_body(label="x" * 101)).status_code == 422


def test_the_first_webhook_bind_gets_its_workspace_a_project_with_its_keys(client, mocks, monkeypatch):
    """A webhook number runs in its Workspace's own project (#65): the shared
    one could not hold any Workspace's keys without lending them to the rest."""
    from app import provider_integrations

    projected = []

    async def fake_project(pool, client_, workspace_id, target):
        projected.append(target["api_key"])

    monkeypatch.setattr(provider_integrations, "project_into_new_agent", fake_project)
    mocks.phones.ensure_workspace_webhook_project.return_value = (
        {"project_id": "wp-ws", "api_key": "tc_wp_ws"},
        True,
    )
    mocks.client.bind_phone_number.return_value = {"id": "pn-9"}

    r = client.post(
        "/phone-numbers",
        json=_bind_body(routing_type="webhook", agent_id=None, server_url="https://crm.example.com/init"),
    )

    assert r.status_code == 200, r.text
    assert mocks.client.bind_phone_number.await_args.args[1] == "tc_wp_ws"
    assert projected == ["tc_wp_ws"]
    mocks.phones.ensure_webhook_project.assert_not_called()


def test_a_number_bound_before_65_is_unbound_with_the_shared_projects_key(client, mocks):
    mocks.phones.get_number.return_value = _phone(routing_type="webhook", agent_id=None, project_id="wp-shared")
    mocks.phones.key_for_project.return_value = "tc_shared"

    r = client.delete("/phone-numbers/pn-1")

    assert r.status_code in (200, 204), r.text
    assert mocks.client.unbind_phone_number.await_args.args[1] == "tc_shared"
