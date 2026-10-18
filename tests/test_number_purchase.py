"""Searching, buying and releasing numbers (#108, turncall ADR-0025).

Only on the Workspace's own carrier accounts, through its webhook project's
key; a bought number lands in the Number registry unassigned, and a release
drops it."""

import httpx
import pytest

from app.routers.auth import COOKIE_NAME as COOKIE
from tests.conftest import TEST_WORKSPACE_ID, audit_entries, backend_row

WP = {"project_id": "wp-1", "api_key": "tc_wp"}
PAIR = ("AC" + "1" * 32, "workspace-token")
TELNYX_TARGET = f"webhook-numbers:{TEST_WORKSPACE_ID}"
E164 = "+13125550100"


@pytest.fixture(autouse=True)
def _no_accounts(mocks):
    """No carrier account connected unless a test connects one."""
    mocks.integration_store.get.return_value = None
    mocks.phones.ensure_workspace_webhook_project.return_value = (WP, False)


def _twilio(mocks):
    mocks.twilio_store.get.return_value = {"account_sid": PAIR[0], "last4": "oken"}
    mocks.twilio_store.credentials.return_value = PAIR


def _telnyx(mocks, *, projected=True):
    mocks.integration_store.get.return_value = {
        "provider": "telnyx", "disconnecting": False, "version": "v1",
    }
    mocks.integration_store.list_projections.return_value = (
        [{"provider": "telnyx", "agent_id": TELNYX_TARGET, "state": "applied", "version": "v1"}]
        if projected
        else []
    )
    mocks.integration_store.get_document.return_value = {"api_key": "KEY_telnyx"}


def _refusal(status: int, error: str, path: str = "purchase") -> httpx.HTTPStatusError:
    resp = httpx.Response(
        status,
        json={"success": False, "error": error, "code": "x"},
        request=httpx.Request("POST", f"http://tc/v1/phone-numbers/{path}"),
    )
    return httpx.HTTPStatusError(error, request=resp.request, response=resp)


def _no_turncall(mocks):
    """Refused before TurnCall: no project made, nothing asked."""
    mocks.phones.ensure_workspace_webhook_project.assert_not_called()
    assert mocks.client.method_calls == []


# --- search -------------------------------------------------------------------


def test_search_without_a_carrier_says_which_carriers_and_searches_nothing(client, mocks):
    _twilio(mocks)
    _telnyx(mocks)
    r = client.get("/phone-numbers/available")
    assert r.status_code == 200
    assert r.json()["data"] == {"carriers": ["twilio", "telnyx"], "numbers": []}
    _no_turncall(mocks)


def test_a_telnyx_integration_being_disconnected_is_not_a_carrier(client, mocks):
    _telnyx(mocks)
    mocks.integration_store.get.return_value["disconnecting"] = True
    assert client.get("/phone-numbers/available").json()["data"]["carriers"] == []


def test_search_on_twilio_sends_the_workspace_pair_with_the_webhook_key(client, mocks):
    _twilio(mocks)
    found = [{"carrier": "twilio", "e164_number": E164, "number_type": "local"}]
    mocks.client.search_available_numbers.return_value = found
    r = client.get(
        "/phone-numbers/available",
        params={"carrier": "twilio", "country": "US", "area_code": "312", "limit": "5"},
    )
    assert r.status_code == 200
    assert r.json()["data"] == {"carriers": ["twilio"], "numbers": found}
    params, key, twilio = mocks.client.search_available_numbers.await_args.args
    assert key == "tc_wp"
    assert twilio == PAIR
    assert params["carrier"] == "twilio"
    assert params["area_code"] == "312"
    assert params["limit"] == "5"


def test_search_on_telnyx_sends_no_pair(client, mocks):
    _telnyx(mocks)
    mocks.client.search_available_numbers.return_value = []
    r = client.get("/phone-numbers/available", params={"carrier": "telnyx"})
    assert r.status_code == 200
    assert mocks.client.search_available_numbers.await_args.args[2] is None
    mocks.client.put_credential.assert_not_called()  # already holds the current key


@pytest.mark.parametrize("carrier", ["twilio", "telnyx"])
def test_search_on_an_unconnected_carrier_is_refused_before_turncall(client, mocks, carrier):
    r = client.get("/phone-numbers/available", params={"carrier": carrier})
    assert r.status_code == 400
    assert "connect it on Integrations" in r.json()["detail"]
    _no_turncall(mocks)


def test_a_turncall_refusal_of_a_search_keeps_its_status_and_sentence(client, mocks):
    _twilio(mocks)
    mocks.client.search_available_numbers.side_effect = _refusal(
        422, "Twilio sells no mobile numbers in US", "available"
    )
    r = client.get("/phone-numbers/available", params={"carrier": "twilio"})
    assert r.status_code == 422
    assert r.json()["detail"] == (
        "TurnCall refused to search for numbers: Twilio sells no mobile numbers in US"
    )


# --- the Telnyx credential in the webhook project ----------------------------


def test_a_stale_telnyx_copy_is_rewritten_before_buying(client, mocks):
    _telnyx(mocks, projected=False)
    mocks.client.purchase_phone_number.return_value = {
        "carrier": "telnyx", "e164_number": E164, "external_number_sid": "2304", "status": "active",
    }
    r = client.post("/phone-numbers/purchase", json={"carrier": "telnyx", "e164": E164})
    assert r.status_code == 200
    provider, document, key = mocks.client.put_credential.await_args.args
    assert (provider, document, key) == ("telnyx", {"api_key": "KEY_telnyx"}, "tc_wp")
    recorded = mocks.integration_store.record_projection.await_args
    assert recorded.args[2:4] == ("telnyx", TELNYX_TARGET)
    assert recorded.kwargs["state"] == "applied"


def test_a_telnyx_copy_that_cannot_be_written_refuses_rather_than_use_the_platforms(
    client, mocks
):
    _telnyx(mocks, projected=False)
    mocks.client.put_credential.side_effect = RuntimeError("TurnCall down")
    r = client.post("/phone-numbers/purchase", json={"carrier": "telnyx", "e164": E164})
    assert r.status_code == 502
    assert "nothing was bought" in r.json()["detail"]
    mocks.client.purchase_phone_number.assert_not_called()
    mocks.phones.record_number.assert_not_called()


# --- buy ----------------------------------------------------------------------


def test_buying_on_twilio_records_an_unassigned_number_on_the_workspace_account(client, mocks):
    _twilio(mocks)
    mocks.client.purchase_phone_number.return_value = {
        "carrier": "twilio", "e164_number": E164, "external_number_sid": "PN" + "b" * 32,
        "status": "active", "twilio_account_sid": PAIR[0], "twilio_credentials": "number",
    }
    r = client.post("/phone-numbers/purchase", json={"carrier": "twilio", "e164": E164})
    assert r.status_code == 200
    payload, key = mocks.client.purchase_phone_number.await_args.args
    assert payload == {
        "carrier": "twilio", "e164_number": E164,
        "twilio_account_sid": PAIR[0], "twilio_auth_token": PAIR[1],
    }
    assert key == "tc_wp"
    row = mocks.phones.record_number.await_args.args[1]
    assert row["routing_type"] == "none"
    assert row["project_id"] is None
    assert row["sid"] == "PN" + "b" * 32
    assert row["carrier"] == "twilio"
    assert row["twilio_source"] == "workspace"
    assert row["twilio_account_sid"] == PAIR[0]
    assert "twilio_auth_token_encrypted" not in row  # the account row is the copy
    assert row["workspace_id"] == TEST_WORKSPACE_ID
    data = r.json()["data"]
    assert data["id"] == row["id"]
    assert data["routing_type"] == "none"
    assert data["status"] == "active"
    assert data["twilio_credentials"] == "workspace"
    assert "twilio_source" not in data
    (entry,) = audit_entries(mocks)
    assert entry["action"] == "purchased"
    assert entry["resource_type"] == "phone_number"
    assert entry["resource_id"] == row["id"]
    assert entry["resource_name"] == E164
    assert entry["changes"]["/status"] == {"before": None, "after": "active"}
    assert entry["outcome"] == "completed"


def test_a_pending_telnyx_purchase_is_unassigned_with_no_sid(client, mocks):
    _telnyx(mocks)
    mocks.client.purchase_phone_number.return_value = {
        "carrier": "telnyx", "e164_number": E164, "external_number_sid": None,
        "status": "pending", "telnyx_order_id": "order-1",
    }
    r = client.post("/phone-numbers/purchase", json={"carrier": "telnyx", "e164": E164})
    assert r.status_code == 200
    assert mocks.client.purchase_phone_number.await_args.args[0] == {
        "carrier": "telnyx", "e164_number": E164,
    }
    row = mocks.phones.record_number.await_args.args[1]
    assert row["sid"] == ""
    assert row["carrier"] == "telnyx"
    assert row["routing_type"] == "none"
    assert "twilio_source" not in row
    data = r.json()["data"]
    assert data["status"] == "pending"
    assert data["telnyx_order_id"] == "order-1"


@pytest.mark.parametrize("carrier", ["twilio", "telnyx"])
def test_buying_on_an_unconnected_carrier_is_refused_before_turncall(client, mocks, carrier):
    r = client.post("/phone-numbers/purchase", json={"carrier": carrier, "e164": E164})
    assert r.status_code == 400
    assert isinstance(r.json()["detail"], str)
    _no_turncall(mocks)
    assert audit_entries(mocks) == []


@pytest.mark.parametrize("body, says", [
    ({"e164": E164}, "Choose a carrier"),
    ({"carrier": "asterisk", "e164": E164}, "PBX's dialplan"),
    ({"carrier": "vonage", "e164": E164}, "vonage is not a carrier"),
    ({"carrier": "twilio"}, "E.164"),
    ({"carrier": "twilio", "e164": "3125550100"}, "E.164"),
])
def test_a_bad_purchase_is_refused_with_a_sentence(client, mocks, body, says):
    _twilio(mocks)
    r = client.post("/phone-numbers/purchase", json=body)
    assert r.status_code == 422
    assert says in r.json()["detail"]
    _no_turncall(mocks)


@pytest.mark.parametrize("status, error", [
    (403, "This project did not buy +13125550100 on the platform's Twilio account"),
    (409, "Phone number +13125550100 is already bound"),
    (422, "Twilio: number not available"),
    (502, "Twilio: upstream failure"),
])
def test_a_turncall_refusal_of_a_purchase_keeps_its_status_and_records_nothing(
    client, mocks, status, error
):
    _twilio(mocks)
    mocks.client.purchase_phone_number.side_effect = _refusal(status, error)
    r = client.post("/phone-numbers/purchase", json={"carrier": "twilio", "e164": E164})
    assert r.status_code == status
    assert r.json()["detail"] == f"TurnCall refused to buy {E164}: {error}"
    mocks.phones.record_number.assert_not_called()
    assert audit_entries(mocks) == []


def test_turncall_rejecting_the_builders_key_is_not_a_401(client, mocks):
    _twilio(mocks)
    mocks.client.purchase_phone_number.side_effect = _refusal(401, "Invalid or missing API key")
    r = client.post("/phone-numbers/purchase", json={"carrier": "twilio", "e164": E164})
    assert r.status_code == 502


def test_a_purchase_turncall_could_not_record_is_still_audited(client, mocks):
    _twilio(mocks)
    mocks.client.purchase_phone_number.side_effect = _refusal(
        500, "The carrier bought +13125550100, but TurnCall could not record it"
    )
    r = client.post("/phone-numbers/purchase", json={"carrier": "twilio", "e164": E164})
    assert r.status_code == 500
    (entry,) = audit_entries(mocks)
    assert entry["action"] == "purchased"
    assert entry["outcome"] == "partial"


def test_a_bought_number_appears_unassigned_in_the_list(client, mocks):
    mocks.phones.list_numbers.return_value = [{
        "id": "local-1", "e164": E164, "sid": "", "routing_type": "none", "carrier": "telnyx",
    }]
    (number,) = client.get("/phone-numbers").json()["data"]["phone_numbers"]
    assert number["routing_type"] == "none"


# --- bind after buy (the existing edit path) ---------------------------------


def _bought(**over):
    n = {
        "id": "local-1", "e164": E164, "sid": "PN" + "b" * 32, "routing_type": "none",
        "agent_id": None, "project_id": None, "server_url": None, "server_url_secret": None,
        "sms_enabled": False, "label": None, "carrier": "twilio",
        "twilio_account_sid": PAIR[0], "twilio_credentials": "workspace",
    }
    n.update(over)
    return n


def test_a_bought_twilio_number_binds_on_the_workspace_account(client, mocks):
    mocks.phones.get_number.return_value = _bought()
    mocks.phones.twilio_account.return_value = PAIR
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.bind_phone_number.return_value = {"id": "pn-9"}
    r = client.put(
        "/phone-numbers/local-1",
        json={"e164": E164, "sid": "PN" + "b" * 32, "routing_type": "agent", "agent_id": "a1"},
    )
    assert r.status_code == 200
    payload = mocks.client.bind_phone_number.await_args.args[0]
    assert (payload["twilio_account_sid"], payload["twilio_auth_token"]) == PAIR
    assert mocks.phones.record_number.await_args.args[1]["twilio_source"] == "workspace"
    mocks.client.unbind_phone_number.assert_not_called()
    assert mocks.phones.delete_number.await_args.args[1] == "local-1"  # the local row goes


def test_a_pending_telnyx_number_binds_by_its_e164(client, mocks):
    mocks.phones.get_number.return_value = _bought(
        sid="", carrier="telnyx", twilio_account_sid=None, twilio_credentials="platform"
    )
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.bind_phone_number.return_value = {"id": "pn-9", "external_number_sid": "2304"}
    r = client.put(
        "/phone-numbers/local-1",
        json={"e164": E164, "routing_type": "agent", "agent_id": "a1"},
    )
    assert r.status_code == 200
    payload = mocks.client.bind_phone_number.await_args.args[0]
    assert payload["carrier"] == "telnyx"
    assert "external_number_sid" not in payload
    assert mocks.phones.record_number.await_args.args[1]["sid"] == "2304"


# --- release ------------------------------------------------------------------


def _released(carrier="twilio"):
    return {
        "carrier": carrier, "e164_number": E164, "external_number_sid": "PN" + "b" * 32,
        "status": "released",
    }


def test_release_gives_the_number_back_and_drops_it_from_the_registry(client, mocks):
    mocks.phones.get_number.return_value = _bought()
    mocks.phones.twilio_account.return_value = PAIR
    mocks.client.release_phone_number.return_value = _released()
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 200
    assert r.json()["data"] == {
        "released": E164, "carrier": "twilio", "external_number_sid": "PN" + "b" * 32,
    }
    payload, key = mocks.client.release_phone_number.await_args.args
    assert payload == {
        "carrier": "twilio", "e164_number": E164, "confirm": E164,
        "twilio_account_sid": PAIR[0], "twilio_auth_token": PAIR[1],
    }
    assert key == "tc_wp"
    assert mocks.phones.delete_number.await_args.args[1] == "local-1"
    (entry,) = audit_entries(mocks)
    assert entry["action"] == "released"
    assert entry["resource_id"] == "local-1"
    assert entry["changes"]["/e164"] == {"before": E164, "after": None}
    assert entry["outcome"] == "completed"


def test_release_uses_the_numbers_own_pair_before_the_workspaces(client, mocks):
    own = ("AC" + "2" * 32, "own-token")
    _twilio(mocks)
    mocks.phones.get_number.return_value = _bought(twilio_credentials="number")
    mocks.phones.twilio_account.return_value = own
    mocks.client.release_phone_number.return_value = _released()
    client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    payload = mocks.client.release_phone_number.await_args.args[0]
    assert (payload["twilio_account_sid"], payload["twilio_auth_token"]) == own


def test_release_falls_back_to_the_workspace_pair(client, mocks):
    _twilio(mocks)
    mocks.phones.get_number.return_value = _bought(twilio_credentials="platform")
    mocks.client.release_phone_number.return_value = _released()
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 200
    payload = mocks.client.release_phone_number.await_args.args[0]
    assert (payload["twilio_account_sid"], payload["twilio_auth_token"]) == PAIR


def test_release_with_no_twilio_account_is_refused_not_sent_to_the_platform(client, mocks):
    mocks.phones.get_number.return_value = _bought(twilio_credentials="platform")
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 400
    _no_turncall(mocks)
    mocks.phones.delete_number.assert_not_called()


def test_release_on_telnyx_uses_the_workspace_integration(client, mocks):
    _telnyx(mocks)
    mocks.phones.get_number.return_value = _bought(carrier="telnyx", twilio_credentials="platform")
    mocks.client.release_phone_number.return_value = _released("telnyx")
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 200
    assert mocks.client.release_phone_number.await_args.args[0] == {
        "carrier": "telnyx", "e164_number": E164, "confirm": E164,
    }


def test_release_of_a_telnyx_number_without_the_integration_is_refused(client, mocks):
    mocks.phones.get_number.return_value = _bought(carrier="telnyx", twilio_credentials="platform")
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 400
    _no_turncall(mocks)


def test_release_of_a_routed_number_is_a_409_before_turncall(client, mocks):
    mocks.phones.get_number.return_value = _bought(
        routing_type="agent", agent_id="a1", project_id="p1"
    )
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 409
    assert r.json()["detail"] == f"{E164} is still routed. Set it to unassigned before releasing it."
    _no_turncall(mocks)
    mocks.phones.delete_number.assert_not_called()


@pytest.mark.parametrize("body", [{}, {"confirm": "yes"}, {"confirm": "+13125550199"}])
def test_release_without_the_number_confirmed_is_a_422_sentence(client, mocks, body):
    mocks.phones.get_number.return_value = _bought()
    r = client.post("/phone-numbers/local-1/release", json=body)
    assert r.status_code == 422
    assert r.json()["detail"] == (
        f"Releasing is irreversible: confirm by typing the number being released ({E164})."
    )
    _no_turncall(mocks)


def test_release_of_an_asterisk_number_is_refused(client, mocks):
    mocks.phones.get_number.return_value = _bought(carrier="asterisk")
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 422
    _no_turncall(mocks)


def test_release_of_an_unknown_number_is_a_404(client, mocks):
    mocks.phones.get_number.return_value = None
    assert client.post("/phone-numbers/nope/release", json={"confirm": E164}).status_code == 404


def test_turncalls_bound_refusal_keeps_the_registry_row(client, mocks):
    mocks.phones.get_number.return_value = _bought()
    mocks.phones.twilio_account.return_value = PAIR
    error = f"Phone number {E164} is bound; unbind it before releasing it"
    mocks.client.release_phone_number.side_effect = _refusal(409, error, "release")
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 409
    assert r.json()["detail"] == f"TurnCall refused to release {E164}: {error}"
    mocks.phones.delete_number.assert_not_called()
    assert audit_entries(mocks) == []


def test_turncall_not_answering_a_release_says_it_may_have_happened(client, mocks):
    mocks.phones.get_number.return_value = _bought()
    mocks.phones.twilio_account.return_value = PAIR
    mocks.client.release_phone_number.side_effect = httpx.ConnectError("down")
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 502
    assert "may have been released" in r.json()["detail"]
    mocks.phones.delete_number.assert_not_called()


# --- roles --------------------------------------------------------------------


ROUTES = [
    ("get", "/phone-numbers/available?carrier=twilio", None),
    ("post", "/phone-numbers/purchase", {"carrier": "twilio", "e164": E164}),
    ("post", "/phone-numbers/local-1/release", {"confirm": E164}),
]


@pytest.mark.parametrize("role", ["viewer", "editor"])
@pytest.mark.parametrize("method, path, body", ROUTES)
def test_only_admins_search_buy_and_release(gate_client, mocks, role, method, path, body):
    mocks.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}
    mocks.auth_store.resolve_membership.return_value = role
    _twilio(mocks)
    gate_client.cookies.set(COOKIE, "live")
    kwargs = {"headers": {"X-Workspace-Id": TEST_WORKSPACE_ID}}
    if body is not None:
        kwargs["json"] = body
    r = getattr(gate_client, method)(path, **kwargs)
    assert r.status_code == 403
    assert r.json()["detail"] == "requires admin role"
    _no_turncall(mocks)
