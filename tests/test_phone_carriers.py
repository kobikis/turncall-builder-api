"""Numbers on Telnyx and Asterisk as well as Twilio (#73, turncall ADR-0022)."""

import pytest

from tests.conftest import backend_row


def _body(**over):
    b = {"e164": "+15551234567", "routing_type": "agent", "agent_id": "a1"}
    b.update(over)
    return b


def _bound(mocks, **data):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.bind_phone_number.return_value = {"id": "pn-9", **data}


def test_a_telnyx_number_binds_with_its_carrier_and_no_sid(client, mocks):
    _bound(mocks, external_number_sid="1293384261075731499", webhooks_configured=True)
    r = client.post("/phone-numbers", json=_body(carrier="telnyx"))
    assert r.status_code == 200
    payload = mocks.client.bind_phone_number.await_args.args[0]
    assert payload["carrier"] == "telnyx"
    assert "external_number_sid" not in payload
    assert "twilio_account_sid" not in payload
    data = r.json()["data"]
    assert data["carrier"] == "telnyx"
    assert data["webhooks_configured"] is True
    assert mocks.phones.record_number.await_args.args[1]["carrier"] == "telnyx"


def test_an_asterisk_number_binds_with_its_carrier(client, mocks):
    _bound(mocks)
    r = client.post("/phone-numbers", json=_body(carrier="asterisk"))
    assert r.status_code == 200
    assert mocks.client.bind_phone_number.await_args.args[0]["carrier"] == "asterisk"


def test_a_number_with_no_carrier_is_twilio(client, mocks):
    _bound(mocks)
    client.post("/phone-numbers", json=_body(sid="PN" + "a" * 32))
    assert mocks.client.bind_phone_number.await_args.args[0]["carrier"] == "twilio"
    assert mocks.phones.record_number.await_args.args[1]["carrier"] == "twilio"


@pytest.mark.parametrize("twilio_only", [
    {"sms_enabled": True},
    {"use_workspace_twilio": True},
    {"twilio_account_sid": "AC" + "1" * 32, "twilio_auth_token": "t"},
])
@pytest.mark.parametrize("carrier", ["telnyx", "asterisk"])
def test_twilio_only_settings_are_refused_on_another_carrier(client, mocks, carrier, twilio_only):
    r = client.post("/phone-numbers", json=_body(carrier=carrier, **twilio_only))
    assert r.status_code == 422
    mocks.client.bind_phone_number.assert_not_called()


def test_an_unknown_carrier_is_refused(client, mocks):
    assert client.post("/phone-numbers", json=_body(carrier="vonage")).status_code == 422


def test_an_edit_keeps_the_numbers_carrier_and_refuses_another(client, mocks):
    existing = {
        "id": "pn-1", "e164": "+15551234567", "sid": "", "routing_type": "agent", "agent_id": "a1",
        "project_id": "p1", "server_url": None, "server_url_secret": None, "sms_enabled": False,
        "carrier": "telnyx",
    }
    mocks.phones.get_number.return_value = existing
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.update_phone_number.return_value = {}

    # An edit that names no carrier keeps the number's own.
    r = client.put("/phone-numbers/pn-1", json=_body(label="Support"))
    assert r.status_code == 200
    assert mocks.phones.record_number.await_args.args[1]["carrier"] == "telnyx"

    # A number's carrier is fixed at bind.
    r = client.put("/phone-numbers/pn-1", json=_body(carrier="twilio"))
    assert r.status_code == 422
    assert "telnyx" in r.json()["detail"]


def test_a_rebind_across_projects_keeps_the_carrier(client, mocks):
    existing = {
        "id": "pn-1", "e164": "+15551234567", "sid": "", "routing_type": "agent", "agent_id": "a1",
        "project_id": "p-old", "server_url": None, "server_url_secret": None, "sms_enabled": False,
        "carrier": "asterisk",
    }
    mocks.phones.get_number.return_value = existing
    mocks.registry.get_backend.return_value = backend_row(agent_id="a2", project_id="p-new")
    mocks.client.bind_phone_number.return_value = {"id": "pn-2"}
    r = client.put("/phone-numbers/pn-1", json=_body(agent_id="a2"))
    assert r.status_code == 200
    assert mocks.client.bind_phone_number.await_args.args[0]["carrier"] == "asterisk"


def test_a_telnyx_bind_without_an_account_says_so(client, mocks):
    import httpx

    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    resp = httpx.Response(503, json={
        "success": False, "code": "no_telnyx_account",
        "error": "No Telnyx account: set a telnyx credential or TELNYX_API_KEY",
    }, request=httpx.Request("POST", "http://tc/x"))
    mocks.client.bind_phone_number.side_effect = httpx.HTTPStatusError("x", request=resp.request, response=resp)
    r = client.post("/phone-numbers", json=_body(carrier="telnyx"))
    assert r.status_code == 502
    assert r.json()["detail"] == (
        "TurnCall rejected the bind: No Telnyx account: set a telnyx credential or TELNYX_API_KEY"
    )
