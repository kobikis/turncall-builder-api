"""Searching, buying and releasing numbers (#108, turncall ADR-0025).

Only on the Workspace's own carrier accounts, through its webhook project's
key; a bought number lands in the Number registry unassigned, and a release
drops it."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app import telnyx_api, twilio_api
from app.routers.auth import COOKIE_NAME as COOKIE
from tests.conftest import TEST_WORKSPACE_ID, audit_entries, backend_row

WP = {"project_id": "wp-1", "api_key": "tc_wp"}
PAIR = ("AC" + "1" * 32, "workspace-token")
TELNYX_TARGET = f"webhook-numbers:{TEST_WORKSPACE_ID}"
E164 = "+13125550100"
# The real lookup, for its own tests: every other test gets the `lookup` mock.
FIND_NUMBER = twilio_api.find_number
FIND_TELNYX = telnyx_api.find_number


@pytest.fixture(autouse=True)
def _no_accounts(mocks):
    """No carrier account connected unless a test connects one."""
    mocks.integration_store.get.return_value = None
    mocks.phones.ensure_workspace_webhook_project.return_value = (WP, False)


@pytest.fixture(autouse=True)
def lookup(monkeypatch):
    """The release check's Twilio lookup: the account holds the number unless
    a test says otherwise. Never the network."""
    found = AsyncMock(return_value={
        "sid": "PN" + "b" * 32, "phone_number": E164, "friendly_name": None,
    })
    monkeypatch.setattr(twilio_api, "find_number", found)
    return found


@pytest.fixture(autouse=True)
def telnyx_lookup(monkeypatch):
    """The release check's Telnyx lookup: the Workspace's Telnyx account holds
    the number unless a test says otherwise. Never the network."""
    found = AsyncMock(return_value={"id": "2304", "phone_number": E164})
    monkeypatch.setattr(telnyx_api, "find_number", found)
    return found


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


def _refusal(
    status: int, error: str, path: str = "purchase", details: dict | None = None
) -> httpx.HTTPStatusError:
    """TurnCall's own refusal: its error envelope."""
    resp = httpx.Response(
        status,
        json={"success": False, "error": error, "code": "x", "details": details},
        request=httpx.Request("POST", f"http://tc/v1/phone-numbers/{path}"),
    )
    return httpx.HTTPStatusError(error, request=resp.request, response=resp)


def _gateway(status: int, path: str = "purchase") -> httpx.HTTPStatusError:
    """A proxy in front of TurnCall failing: not TurnCall's envelope."""
    resp = httpx.Response(
        status,
        text="<html>Bad Gateway</html>",
        request=httpx.Request("POST", f"http://tc/v1/phone-numbers/{path}"),
    )
    return httpx.HTTPStatusError("gateway", request=resp.request, response=resp)


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
    assert r.json()["reason"] == "not_connected"
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


@pytest.mark.parametrize("route", ["purchase", "release"])
def test_buying_and_releasing_rewrite_the_telnyx_copy_whatever_the_log_says(
    client, mocks, route
):
    """The log can say the project holds the key when TurnCall no longer
    does; buying or releasing on the platform's account by mistake costs
    money or a number, so the copy is written first, every time."""
    _telnyx(mocks, projected=True)
    if route == "purchase":
        mocks.client.purchase_phone_number.return_value = {
            "carrier": "telnyx", "e164_number": E164, "external_number_sid": "2304",
            "status": "active",
        }
        r = client.post("/phone-numbers/purchase", json={"carrier": "telnyx", "e164": E164})
    else:
        mocks.phones.get_number.return_value = _bought(
            carrier="telnyx", twilio_credentials="platform"
        )
        mocks.client.release_phone_number.return_value = _released("telnyx")
        r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 200
    assert mocks.client.put_credential.await_count == 1
    assert mocks.client.put_credential.await_args.args[2] == "tc_wp"


def test_a_webhook_project_made_for_a_purchase_gets_the_telnyx_key_once(client, mocks):
    _telnyx(mocks, projected=True)
    mocks.integration_store.list_for_workspace.return_value = [
        mocks.integration_store.get.return_value
    ]
    mocks.phones.ensure_workspace_webhook_project.return_value = (WP, True)
    mocks.client.purchase_phone_number.return_value = {
        "carrier": "telnyx", "e164_number": E164, "external_number_sid": "2304",
        "status": "active",
    }
    r = client.post("/phone-numbers/purchase", json={"carrier": "telnyx", "e164": E164})
    assert r.status_code == 200
    assert mocks.client.put_credential.await_count == 1


def test_searching_writes_the_telnyx_copy_only_until_it_is_held(client, mocks):
    """A search may need the webhook project (TurnCall reads the Telnyx key
    from it), but writes the key only while the log says it is missing."""
    _telnyx(mocks, projected=False)
    mocks.client.search_available_numbers.return_value = []
    client.get("/phone-numbers/available", params={"carrier": "telnyx"})
    assert mocks.client.put_credential.await_count == 1
    _telnyx(mocks, projected=True)
    client.get("/phone-numbers/available", params={"carrier": "telnyx"})
    client.get("/phone-numbers/available", params={"carrier": "telnyx"})
    assert mocks.client.put_credential.await_count == 1


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


# --- the Telnyx numbers check on every write of the key (#109 review) ------------

BOUND = "+13125550177"  # a Telnyx number bound in the Workspace's webhook project


@pytest.fixture
def listing(monkeypatch):
    """The Telnyx account's numbers, as the numbers check lists them;
    `listing.held` is what it holds. Never the network."""
    async def list_numbers(api_key, **kw):
        list_numbers.calls.append(api_key)
        if list_numbers.error:
            raise list_numbers.error
        return set(list_numbers.held)

    list_numbers.calls, list_numbers.held, list_numbers.error = [], set(), None
    monkeypatch.setattr(telnyx_api, "list_numbers", list_numbers)
    return list_numbers


def _bound_in_webhook_project(mocks, *numbers):
    mocks.phones.list_numbers.return_value = [
        {"id": f"pn-{n[-4:]}", "e164": n, "carrier": "telnyx", "project_id": WP["project_id"],
         "routing_type": "webhook"}
        for n in numbers
    ]


def _send(client, mocks, route):
    if route == "search":
        return client.get("/phone-numbers/available", params={"carrier": "telnyx"})
    if route == "purchase":
        return client.post("/phone-numbers/purchase", json={"carrier": "telnyx", "e164": E164})
    mocks.phones.get_number.return_value = _bought(carrier="telnyx", twilio_credentials="platform")
    return client.post("/phone-numbers/local-1/release", json={"confirm": E164})


ROUTES_WRITING_THE_KEY = ["search", "purchase", "release"]


@pytest.mark.parametrize("route", ROUTES_WRITING_THE_KEY)
def test_the_key_is_not_written_where_bound_numbers_arent_on_its_account(
    client, mocks, listing, route
):
    """Writing the Workspace's Telnyx key into its webhook project would move
    the numbers bound there onto that account: if it doesn't hold them, they
    stop answering calls. There is no acknowledgement here, so it refuses and
    points at Integrations."""
    _telnyx(mocks, projected=False)
    _bound_in_webhook_project(mocks, BOUND)
    r = _send(client, mocks, route)
    assert r.status_code == 409
    body = r.json()
    assert body["reason"] == "numbers_not_on_account"
    assert body["numbers"] == [BOUND]
    assert isinstance(body["detail"], str)
    assert BOUND in body["detail"]
    assert "Integrations" in body["detail"] and "acknowledge" in body["detail"]
    assert listing.calls == ["KEY_telnyx"]
    mocks.client.put_credential.assert_not_called()
    mocks.client.search_available_numbers.assert_not_called()
    mocks.client.purchase_phone_number.assert_not_called()
    mocks.client.release_phone_number.assert_not_called()
    mocks.integration_store.record_projection.assert_not_called()
    assert audit_entries(mocks) == []


@pytest.mark.parametrize("route", ROUTES_WRITING_THE_KEY)
def test_the_key_is_written_once_the_account_holds_the_bound_numbers(
    client, mocks, listing, route
):
    _telnyx(mocks, projected=False)
    _bound_in_webhook_project(mocks, BOUND)
    listing.held = {BOUND}
    mocks.client.search_available_numbers.return_value = []
    mocks.client.purchase_phone_number.return_value = {
        "carrier": "telnyx", "e164_number": E164, "external_number_sid": "2304",
        "status": "active",
    }
    mocks.client.release_phone_number.return_value = _released("telnyx")
    r = _send(client, mocks, route)
    assert r.status_code == 200, r.text
    assert mocks.client.put_credential.await_count == 1


@pytest.mark.parametrize("route", ["purchase", "release"])
def test_rewriting_the_key_a_project_holds_already_checks_nothing(
    client, mocks, listing, route
):
    """The log says the project holds this key: its numbers are on it already
    (or were acknowledged on Integrations), and the rewrite moves nothing."""
    _telnyx(mocks, projected=True)
    _bound_in_webhook_project(mocks, BOUND)
    mocks.client.purchase_phone_number.return_value = {
        "carrier": "telnyx", "e164_number": E164, "external_number_sid": "2304",
        "status": "active",
    }
    mocks.client.release_phone_number.return_value = _released("telnyx")
    r = _send(client, mocks, route)
    assert r.status_code == 200, r.text
    assert listing.calls == []
    assert mocks.client.put_credential.await_count == 1


def test_a_numbers_check_that_fails_writes_nothing(client, mocks, listing):
    _telnyx(mocks, projected=False)
    _bound_in_webhook_project(mocks, BOUND)
    listing.error = httpx.ConnectError("down")
    r = client.post("/phone-numbers/purchase", json={"carrier": "telnyx", "e164": E164})
    assert r.status_code == 502
    assert r.json() == {
        "detail": "Telnyx didn't answer when looking up the workspace's Telnyx numbers on "
        "the workspace's Telnyx account; retry in a moment.",
    }
    mocks.client.put_credential.assert_not_called()
    mocks.client.purchase_phone_number.assert_not_called()


def test_an_unassigned_or_twilio_number_doesnt_count(client, mocks, listing):
    _telnyx(mocks, projected=False)
    mocks.phones.list_numbers.return_value = [
        {"id": "pn-1", "e164": BOUND, "carrier": "telnyx", "project_id": None},
        {"id": "pn-2", "e164": "+13125550178", "carrier": "twilio",
         "project_id": WP["project_id"]},
    ]
    mocks.client.search_available_numbers.return_value = []
    r = client.get("/phone-numbers/available", params={"carrier": "telnyx"})
    assert r.status_code == 200
    assert listing.calls == []


# --- a search's writes are audited (#109 review) --------------------------------


def test_a_search_writing_the_telnyx_key_is_audited(client, mocks):
    _telnyx(mocks, projected=False)
    mocks.client.search_available_numbers.return_value = []
    r = client.get("/phone-numbers/available", params={"carrier": "telnyx"})
    assert r.status_code == 200
    (entry,) = audit_entries(mocks)
    assert (entry["resource_type"], entry["resource_id"], entry["action"]) == (
        "integration", "telnyx", "reapplied"
    )
    assert entry["outcome"] == "completed"


@pytest.mark.parametrize("carrier", ["twilio", "telnyx"])
def test_a_search_creating_the_webhook_project_is_audited(client, mocks, carrier):
    _twilio(mocks)
    _telnyx(mocks, projected=True)
    mocks.integration_store.list_for_workspace.return_value = [
        mocks.integration_store.get.return_value
    ]
    mocks.phones.ensure_workspace_webhook_project.return_value = (WP, True)
    mocks.client.search_available_numbers.return_value = []
    r = client.get("/phone-numbers/available", params={"carrier": carrier})
    assert r.status_code == 200
    (entry,) = audit_entries(mocks)
    assert (entry["resource_type"], entry["resource_id"], entry["action"]) == (
        "integration", "webhook_project", "created"
    )
    assert entry["changes"] == {"/project_id": {"before": None, "after": WP["project_id"]}}


@pytest.mark.parametrize("params", [{}, {"carrier": "twilio"}, {"carrier": "telnyx"}])
def test_a_search_that_writes_nothing_records_nothing(client, mocks, params):
    _twilio(mocks)
    _telnyx(mocks, projected=True)
    mocks.client.search_available_numbers.return_value = []
    r = client.get("/phone-numbers/available", params=params)
    assert r.status_code == 200
    assert audit_entries(mocks) == []


# --- each account read once (#109 review) -------------------------------------


def test_a_search_reads_the_twilio_account_once(client, mocks):
    _twilio(mocks)
    mocks.client.search_available_numbers.return_value = []
    r = client.get("/phone-numbers/available", params={"carrier": "twilio"})
    assert r.status_code == 200
    assert r.json()["data"]["carriers"] == ["twilio"]
    assert mocks.twilio_store.credentials.await_count + mocks.twilio_store.get.await_count == 1


def test_a_telnyx_release_reads_the_integration_and_its_key_once(client, mocks):
    _telnyx(mocks)
    mocks.phones.get_number.return_value = _bought(carrier="telnyx", twilio_credentials="platform")
    mocks.client.release_phone_number.return_value = _released("telnyx")
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 200
    assert mocks.integration_store.get.await_count == 1
    assert mocks.integration_store.get_document.await_count == 1


# --- a Twilio number on a disconnected Workspace account (#109 review) --------


def test_releasing_a_workspace_number_after_the_twilio_card_is_gone_is_not_connected(
    client, mocks, lookup
):
    """Disconnecting the card leaves unassigned rows saying "workspace": the
    pair they point at is gone, so the release says to connect one."""
    mocks.phones.get_number.return_value = _bought()  # twilio_credentials "workspace"
    mocks.phones.twilio_account.return_value = None  # the Workspace account is gone
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 400
    assert r.json() == {"detail": NO_TWILIO, "reason": "not_connected"}
    lookup.assert_not_called()
    _no_turncall(mocks)


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
    assert data["purchase_status"] is None
    assert row["purchase_status"] is None
    assert data["warning"] is None
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
    assert row["purchase_status"] == "pending"
    assert row["telnyx_order_id"] == "order-1"
    assert "twilio_source" not in row
    data = r.json()["data"]
    assert data["status"] == "pending"
    assert data["purchase_status"] == "pending"
    assert data["telnyx_order_id"] == "order-1"
    assert data["warning"] is None
    (entry,) = audit_entries(mocks)
    assert entry["changes"]["/purchase_status"] == {"before": None, "after": "pending"}


@pytest.mark.parametrize("carrier", ["twilio", "telnyx"])
def test_buying_on_an_unconnected_carrier_is_refused_before_turncall(client, mocks, carrier):
    r = client.post("/phone-numbers/purchase", json={"carrier": carrier, "e164": E164})
    assert r.status_code == 400
    assert r.json()["reason"] == "not_connected"
    assert isinstance(r.json()["detail"], str)
    _no_turncall(mocks)
    assert audit_entries(mocks) == []


@pytest.mark.parametrize("body, says, reason", [
    ({"e164": E164}, "Choose a carrier", None),
    ({"carrier": "asterisk", "e164": E164}, "PBX's dialplan", "asterisk"),
    ({"carrier": "vonage", "e164": E164}, "vonage is not a carrier", None),
    ({"carrier": "twilio"}, "E.164", None),
    ({"carrier": "twilio", "e164": "3125550100"}, "E.164", None),
])
def test_a_bad_purchase_is_refused_with_a_sentence(client, mocks, body, says, reason):
    _twilio(mocks)
    r = client.post("/phone-numbers/purchase", json=body)
    assert r.status_code == 422
    assert says in r.json()["detail"]
    assert r.json().get("reason") == reason
    _no_turncall(mocks)


@pytest.mark.parametrize("carrier, reason", [
    ("asterisk", "asterisk"), ("vonage", None),
])
def test_searching_on_a_carrier_numbers_arent_bought_on_is_a_422(
    client, mocks, carrier, reason
):
    _twilio(mocks)
    r = client.get("/phone-numbers/available", params={"carrier": carrier})
    assert (r.status_code, r.json().get("reason")) == (422, reason)
    assert isinstance(r.json()["detail"], str)
    _no_turncall(mocks)


@pytest.mark.parametrize("status, error", [
    (403, "This project did not buy +13125550100 on the platform's Twilio account"),
    (409, "Phone number +13125550100 is already bound"),
    (422, "Twilio: number not available"),
    # TurnCall's 503 is "no account", answered before any carrier call.
    (503, "This project has no Twilio account to buy on"),
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


def test_a_purchase_turncall_could_not_record_is_recorded_here_from_its_details(
    client, mocks
):
    """TurnCall's 500 "the carrier bought it, but I could not record it"
    carries the number: the builder records it, unassigned, and says so."""
    _twilio(mocks)
    sid = "PN" + "c" * 32
    mocks.client.purchase_phone_number.side_effect = _refusal(
        500,
        f"The carrier bought {E164}, but TurnCall could not record it; "
        "the number's details are in `details`",
        details={"bought": {
            "carrier": "twilio", "e164_number": E164, "external_number_sid": sid,
            "status": "active", "twilio_account_sid": PAIR[0],
        }},
    )
    r = client.post("/phone-numbers/purchase", json={"carrier": "twilio", "e164": E164})
    assert r.status_code == 200
    row = mocks.phones.record_number.await_args.args[1]
    assert row["sid"] == sid
    assert row["routing_type"] == "none"
    assert row["twilio_source"] == "workspace"
    data = r.json()["data"]
    assert data["id"] == row["id"]
    assert "refused" not in data["warning"]
    assert data["warning"] == (
        f"Twilio sold {E164} to this workspace and it is on Phone Numbers, "
        "but TurnCall could not record the purchase in its own log."
    )
    (entry,) = audit_entries(mocks)
    assert entry["action"] == "purchased"
    assert entry["outcome"] == "completed"


def test_a_purchase_turncall_failed_without_saying_what_happened_may_have_happened(
    client, mocks
):
    _twilio(mocks)
    mocks.client.purchase_phone_number.side_effect = _refusal(500, "Internal server error")
    r = client.post("/phone-numbers/purchase", json={"carrier": "twilio", "e164": E164})
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail == (
        f"TurnCall failed, so {E164} may have been bought: check the Twilio account "
        "before buying it again"
    )
    mocks.phones.record_number.assert_not_called()
    (entry,) = audit_entries(mocks)
    assert entry["outcome"] == "partial"
    assert entry["error"] == detail


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_gateway_failing_a_purchase_says_it_may_have_happened(client, mocks, status):
    _telnyx(mocks)
    mocks.client.purchase_phone_number.side_effect = _gateway(status)
    r = client.post("/phone-numbers/purchase", json={"carrier": "telnyx", "e164": E164})
    assert r.status_code == 502
    assert r.json()["detail"] == (
        f"TurnCall did not answer, so {E164} may have been bought: check the Telnyx "
        "account before buying it again"
    )
    mocks.phones.record_number.assert_not_called()
    (entry,) = audit_entries(mocks)
    assert entry["outcome"] == "partial"


def test_turncall_not_answering_a_purchase_is_audited_as_maybe(client, mocks):
    _twilio(mocks)
    mocks.client.purchase_phone_number.side_effect = httpx.ReadTimeout("slow")
    r = client.post("/phone-numbers/purchase", json={"carrier": "twilio", "e164": E164})
    assert r.status_code == 502
    assert "may have been bought" in r.json()["detail"]
    (entry,) = audit_entries(mocks)
    assert entry["outcome"] == "partial"
    assert "may have been bought" in entry["error"]


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


def _pending(**over):
    return _bought(
        sid="", carrier="telnyx", twilio_account_sid=None, twilio_credentials="platform",
        purchase_status="pending", telnyx_order_id="order-1", **over,
    )


def test_binding_a_pending_number_clears_its_purchase_status(client, mocks):
    mocks.phones.get_number.return_value = _pending()
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.bind_phone_number.return_value = {"id": "pn-9", "external_number_sid": "2304"}
    r = client.put(
        "/phone-numbers/local-1",
        json={"e164": E164, "routing_type": "agent", "agent_id": "a1"},
    )
    assert r.status_code == 200
    row = mocks.phones.record_number.await_args.args[1]
    assert row["id"] == "pn-9"
    # Named, so the write clears them even over an existing row.
    assert ("purchase_status", None) in row.items()
    assert ("telnyx_order_id", None) in row.items()
    assert r.json()["data"]["purchase_status"] is None


def test_binding_a_number_telnyx_has_not_finished_says_so(client, mocks):
    mocks.phones.get_number.return_value = _pending()
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.bind_phone_number.side_effect = _refusal(
        422, f"{E164} is not a phone number on this project's Telnyx account", "x"
    )
    r = client.put(
        "/phone-numbers/local-1",
        json={"e164": E164, "routing_type": "agent", "agent_id": "a1"},
    )
    assert r.status_code == 422
    assert r.json()["detail"] == (
        f"Telnyx hasn't finished the order for {E164} (order order-1) yet, so it "
        "can't be bound: try again in a few minutes. (TurnCall: "
        f"{E164} is not a phone number on this project's Telnyx account)"
    )
    mocks.phones.delete_number.assert_not_called()


def test_any_turncall_422_on_a_pending_bind_is_the_unfinished_order(client, mocks):
    """Decided by the row being pending and TurnCall answering 422, never by
    TurnCall's wording; its own sentence comes along in parentheses."""
    mocks.phones.get_number.return_value = {**_pending(), "telnyx_order_id": None}
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.bind_phone_number.side_effect = _refusal(422, "Validation error", "x")
    r = client.put(
        "/phone-numbers/local-1",
        json={"e164": E164, "routing_type": "agent", "agent_id": "a1"},
    )
    assert r.status_code == 422
    assert r.json() == {
        "detail": f"Telnyx hasn't finished the order for {E164} yet, so it can't be bound: "
        "try again in a few minutes. (TurnCall: Validation error)",
    }


def test_another_bind_refusal_of_a_pending_number_is_turncalls(client, mocks):
    mocks.phones.get_number.return_value = _pending()
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.bind_phone_number.side_effect = _refusal(503, "No Telnyx account", "x")
    r = client.put(
        "/phone-numbers/local-1",
        json={"e164": E164, "routing_type": "agent", "agent_id": "a1"},
    )
    assert r.status_code == 503
    assert r.json()["detail"] == "TurnCall rejected the bind: No Telnyx account"


def test_a_422_on_binding_a_number_that_isnt_pending_is_turncalls(client, mocks):
    mocks.phones.get_number.return_value = _bought(
        sid="", carrier="telnyx", twilio_account_sid=None, twilio_credentials="platform"
    )
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.bind_phone_number.side_effect = _refusal(422, "Validation error", "x")
    r = client.put(
        "/phone-numbers/local-1",
        json={"e164": E164, "routing_type": "agent", "agent_id": "a1"},
    )
    assert (r.status_code, r.json()["detail"]) == (
        422, "TurnCall rejected the bind: Validation error"
    )


def test_an_unassigned_edit_of_a_pending_number_keeps_it_pending(client, mocks):
    mocks.phones.get_number.return_value = _pending()
    r = client.put(
        "/phone-numbers/local-1",
        json={"e164": E164, "routing_type": "none", "label": "Support"},
    )
    assert r.status_code == 200
    row = mocks.phones.record_number.await_args.args[1]
    assert row["purchase_status"] == "pending"
    assert row["telnyx_order_id"] == "order-1"
    assert r.json()["data"]["purchase_status"] == "pending"


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
        "warning": None,
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


def test_release_uses_the_numbers_own_pair_before_the_workspaces(client, mocks, lookup):
    own = ("AC" + "2" * 32, "own-token")
    _twilio(mocks)
    mocks.phones.get_number.return_value = _bought(twilio_credentials="number")
    mocks.phones.twilio_account.return_value = own
    mocks.client.release_phone_number.return_value = _released()
    client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert lookup.await_args.args == (own[0], own[1], E164)
    payload = mocks.client.release_phone_number.await_args.args[0]
    assert (payload["twilio_account_sid"], payload["twilio_auth_token"]) == own
    mocks.phones.record_number.assert_not_called()


def test_a_number_with_no_pair_is_released_on_the_workspace_account_once_found_there(
    client, mocks, lookup
):
    """No stored pair says only that the builder holds no account for it: the
    Workspace's is asked first, and the row corrected before the release."""
    _twilio(mocks)
    mocks.phones.get_number.return_value = _bought(
        twilio_credentials="platform", twilio_account_sid=None, sid="PN-typed"
    )
    mocks.client.release_phone_number.return_value = _released()
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 200
    assert lookup.await_args.args == (PAIR[0], PAIR[1], E164)
    corrected = mocks.phones.record_number.await_args.args[1]
    assert corrected["twilio_source"] == "workspace"
    assert corrected["twilio_account_sid"] == PAIR[0]
    assert corrected["sid"] == "PN" + "b" * 32
    assert "twilio_auth_token_encrypted" not in corrected
    payload = mocks.client.release_phone_number.await_args.args[0]
    assert (payload["twilio_account_sid"], payload["twilio_auth_token"]) == PAIR
    updated, released = audit_entries(mocks)
    assert updated["action"] == "updated"
    assert updated["changes"]["/twilio_credentials"] == {"before": "platform", "after": "workspace"}
    assert updated["changes"]["/sid"] == {"before": "PN-typed", "after": "PN" + "b" * 32}
    assert released["action"] == "released"
    assert released["changes"]["/twilio_credentials"] == {"before": "workspace", "after": None}


def test_a_number_with_no_pair_the_workspace_account_lacks_is_a_422(client, mocks, lookup):
    _twilio(mocks)
    lookup.return_value = None
    mocks.phones.get_number.return_value = _bought(twilio_credentials="platform")
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 422
    assert r.json() == {"detail": NOT_ON_WORKSPACE, "reason": "not_on_account"}
    _no_turncall(mocks)
    mocks.phones.record_number.assert_not_called()
    mocks.phones.delete_number.assert_not_called()
    assert audit_entries(mocks) == []


def test_release_with_no_twilio_account_is_refused_not_sent_to_the_platform(
    client, mocks, lookup
):
    mocks.phones.get_number.return_value = _bought(twilio_credentials="platform")
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 400
    assert r.json() == {"detail": NO_TWILIO, "reason": "not_connected"}
    lookup.assert_not_called()
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


def test_release_of_a_telnyx_number_without_the_integration_is_refused(
    client, mocks, telnyx_lookup
):
    """Bound with no Integration, it is on TurnCall's platform account:
    connecting one wouldn't make it releasable, so that isn't the advice (#113)."""
    mocks.phones.get_number.return_value = _bought(carrier="telnyx", twilio_credentials="platform")
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 422
    assert r.json() == {"detail": ON_PLATFORM_TELNYX, "reason": "not_on_account"}
    assert "Integrations" not in r.json()["detail"]
    telnyx_lookup.assert_not_called()
    _no_turncall(mocks)


def test_release_of_a_routed_number_is_a_409_before_turncall(client, mocks):
    mocks.phones.get_number.return_value = _bought(
        routing_type="agent", agent_id="a1", project_id="p1"
    )
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 409
    assert r.json() == {
        "detail": f"{E164} is still routed. Set it to unassigned before releasing it.",
        "reason": "routed",
    }
    _no_turncall(mocks)
    mocks.phones.delete_number.assert_not_called()


@pytest.mark.parametrize("body", [{}, {"confirm": "yes"}, {"confirm": "+13125550199"}])
def test_release_without_the_number_confirmed_is_a_422_sentence(client, mocks, body):
    mocks.phones.get_number.return_value = _bought()
    r = client.post("/phone-numbers/local-1/release", json=body)
    assert r.status_code == 422
    assert r.json() == {
        "detail": f"Releasing is irreversible: confirm by typing the number being released "
        f"({E164}).",
    }
    _no_turncall(mocks)


def test_release_of_an_asterisk_number_is_refused(client, mocks):
    mocks.phones.get_number.return_value = _bought(carrier="asterisk")
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 422
    assert r.json() == {
        "detail": f"{E164} is on asterisk; only Twilio and Telnyx numbers can be released.",
        "reason": "asterisk",
    }
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
    assert r.json() == {"detail": f"TurnCall refused to release {E164}: {error}"}
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


def test_a_release_turncall_could_not_record_drops_the_row_and_says_so(client, mocks):
    mocks.phones.get_number.return_value = _bought()
    mocks.phones.twilio_account.return_value = PAIR
    mocks.client.release_phone_number.side_effect = _refusal(
        500,
        f"The carrier released {E164}, but TurnCall could not record it; "
        "the number's details are in `details`",
        "release",
        details={"released": _released()},
    )
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["released"] == E164
    assert data["warning"] == (
        f"Twilio took {E164} back and it is gone from Phone Numbers, "
        "but TurnCall could not record the release in its own log."
    )
    assert mocks.phones.delete_number.await_args.args[1] == "local-1"
    (entry,) = audit_entries(mocks)
    assert entry["action"] == "released"
    assert entry["outcome"] == "completed"


def test_a_release_turncall_failed_without_saying_what_happened_keeps_the_row(
    client, mocks
):
    mocks.phones.get_number.return_value = _bought()
    mocks.phones.twilio_account.return_value = PAIR
    mocks.client.release_phone_number.side_effect = _refusal(500, "boom", "release")
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 502
    assert r.json()["detail"] == (
        f"TurnCall failed, so {E164} may have been released: check the Twilio account "
        "before releasing it again"
    )
    mocks.phones.delete_number.assert_not_called()
    (entry,) = audit_entries(mocks)
    assert entry["outcome"] == "partial"


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_gateway_failing_a_release_says_it_may_have_happened(client, mocks, status):
    mocks.phones.get_number.return_value = _bought()
    mocks.phones.twilio_account.return_value = PAIR
    mocks.client.release_phone_number.side_effect = _gateway(status, "release")
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 502
    assert "may have been released" in r.json()["detail"]
    assert "refused" not in r.json()["detail"]
    mocks.phones.delete_number.assert_not_called()
    (entry,) = audit_entries(mocks)
    assert entry["outcome"] == "partial"


@pytest.mark.parametrize("status", [502, 504])
def test_turncalls_own_carrier_failure_on_a_release_may_have_happened(client, mocks, status):
    """TurnCall's 502 and 504 come after it asked the carrier, which may have
    acted: never "refused", and TurnCall's sentence comes along."""
    mocks.phones.get_number.return_value = _bought()
    mocks.phones.twilio_account.return_value = PAIR
    mocks.client.release_phone_number.side_effect = _refusal(status, "Twilio: down", "release")
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 502
    assert r.json() == {
        "detail": f"TurnCall answered {status}, so {E164} may have been released: check the "
        "Twilio account before releasing it again (TurnCall: Twilio: down)",
    }
    mocks.phones.delete_number.assert_not_called()
    (entry,) = audit_entries(mocks)
    assert (entry["action"], entry["outcome"]) == ("released", "partial")
    assert entry["error"] == r.json()["detail"]


@pytest.mark.parametrize("status", [502, 504])
def test_turncalls_own_carrier_failure_on_a_purchase_may_have_happened(client, mocks, status):
    _twilio(mocks)
    mocks.client.purchase_phone_number.side_effect = _refusal(status, "Twilio: upstream failure")
    r = client.post("/phone-numbers/purchase", json={"carrier": "twilio", "e164": E164})
    assert r.status_code == 502
    assert r.json() == {
        "detail": f"TurnCall answered {status}, so {E164} may have been bought: check the "
        "Twilio account before buying it again (TurnCall: Twilio: upstream failure)",
    }
    mocks.phones.record_number.assert_not_called()
    (entry,) = audit_entries(mocks)
    assert (entry["action"], entry["outcome"]) == ("purchased", "partial")


def test_turncalls_own_503_on_a_release_is_a_refusal(client, mocks):
    """No account: TurnCall answered before any carrier call."""
    mocks.phones.get_number.return_value = _bought()
    mocks.phones.twilio_account.return_value = PAIR
    mocks.client.release_phone_number.side_effect = _refusal(503, "No Twilio account", "release")
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 503
    assert r.json()["detail"] == f"TurnCall refused to release {E164}: No Twilio account"
    assert audit_entries(mocks) == []


@pytest.mark.parametrize("answer", [
    {"return_value": None},
    {"return_value": ["not", "a", "number"]},
    {"side_effect": ValueError("Expecting value: line 1 column 1")},
    {"side_effect": KeyError("data")},
])
@pytest.mark.parametrize("route", ["purchase", "release"])
def test_a_2xx_turncall_answer_that_cant_be_read_may_have_happened(
    client, mocks, route, answer
):
    """TurnCall answered 2xx, so the carrier acted, but its answer can't be
    read: never a bare 500, and nothing changes in the registry."""
    _twilio(mocks)
    mocks.phones.twilio_account.return_value = PAIR
    mocks.phones.get_number.return_value = _bought()
    if route == "purchase":
        mocks.client.purchase_phone_number.configure_mock(**answer)
        r = client.post("/phone-numbers/purchase", json={"carrier": "twilio", "e164": E164})
        done, doing, action = "bought", "buying", "purchased"
    else:
        mocks.client.release_phone_number.configure_mock(**answer)
        r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
        done, doing, action = "released", "releasing", "released"
    assert r.status_code == 502
    assert r.json() == {
        "detail": f"TurnCall's answer couldn't be read, so {E164} may have been {done}: "
        f"check the Twilio account before {doing} it again",
    }
    mocks.phones.record_number.assert_not_called()
    mocks.phones.delete_number.assert_not_called()
    (entry,) = audit_entries(mocks)
    assert (entry["action"], entry["outcome"]) == (action, "partial")


def test_release_of_a_pending_number_is_refused_before_turncall(client, mocks):
    _telnyx(mocks)
    mocks.phones.get_number.return_value = _pending()
    r = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert r.status_code == 409
    assert r.json() == {
        "detail": f"Telnyx hasn't finished the order for {E164} (order order-1) yet; "
        "it can be released once the order lands.",
        "reason": "pending",
    }
    _no_turncall(mocks)
    mocks.phones.delete_number.assert_not_called()


# --- release check ------------------------------------------------------------

NOT_ON_WORKSPACE = (
    f"{E164} isn't on the workspace's Twilio account ({PAIR[0]}). It may be on "
    "TurnCall's platform account, and only the TurnCall operator can release it there."
)
NO_TWILIO = "This workspace has no Twilio account connected — connect it on Integrations"
ON_PLATFORM_TELNYX = (
    f"{E164} is on TurnCall's platform Telnyx account, not the workspace's, so only the "
    "TurnCall operator can release it."
)
# Not found on the Workspace's Telnyx Integration: where it is can't be said.
NOT_ON_WORKSPACE_TELNYX = (
    f"{E164} isn't on the workspace's Telnyx account, so there is nothing to release "
    "there: it may be on TurnCall's platform Telnyx account (only the TurnCall operator "
    "can release it there), or already released."
)


def _check(client) -> httpx.Response:
    return client.get("/phone-numbers/local-1/release-check")


def test_the_check_finds_a_number_with_no_pair_on_the_workspace_account_and_says_so(
    client, mocks, lookup
):
    _twilio(mocks)
    mocks.phones.get_number.return_value = _bought(
        twilio_credentials="platform", twilio_account_sid=None
    )
    r = _check(client)
    assert r.status_code == 200
    assert r.json()["data"] == {
        "releasable": True, "reason": None, "detail": None, "twilio_credentials": "workspace",
    }
    corrected = mocks.phones.record_number.await_args.args[1]
    assert (corrected["twilio_source"], corrected["twilio_account_sid"]) == ("workspace", PAIR[0])
    assert corrected["routing_type"] == "none"
    (entry,) = audit_entries(mocks)
    assert (entry["action"], entry["resource_id"]) == ("updated", "local-1")
    assert entry["changes"]["/twilio_account_sid"] == {"before": None, "after": PAIR[0]}
    assert entry["outcome"] == "completed"
    # Unassigned: TurnCall holds nothing to correct.
    assert mocks.client.method_calls == []


def test_the_check_keeps_the_sid_twilio_already_agrees_with(client, mocks):
    _twilio(mocks)
    mocks.phones.get_number.return_value = _bought(twilio_credentials="platform")
    _check(client)
    (entry,) = audit_entries(mocks)
    assert "/sid" not in entry["changes"]


def test_the_check_of_a_number_with_no_pair_the_workspace_lacks_names_the_operator(
    client, mocks, lookup
):
    _twilio(mocks)
    lookup.return_value = None
    mocks.phones.get_number.return_value = _bought(twilio_credentials="platform")
    r = _check(client)
    assert r.status_code == 200
    assert r.json()["data"] == {
        "releasable": False, "reason": "not_on_account", "detail": NOT_ON_WORKSPACE,
        "twilio_credentials": "platform",
    }
    mocks.phones.record_number.assert_not_called()
    assert audit_entries(mocks) == []


def test_the_check_with_no_twilio_account_points_to_integrations(client, mocks, lookup):
    mocks.phones.get_number.return_value = _bought(twilio_credentials="platform")
    r = _check(client)
    assert r.json()["data"] == {
        "releasable": False, "reason": "not_connected", "detail": NO_TWILIO,
        "twilio_credentials": "platform",
    }
    lookup.assert_not_called()


def test_the_check_looks_a_number_up_on_its_stored_pair_and_changes_nothing(
    client, mocks, lookup
):
    _twilio(mocks)
    mocks.phones.get_number.return_value = _bought()
    mocks.phones.twilio_account.return_value = PAIR
    r = _check(client)
    assert r.json()["data"] == {
        "releasable": True, "reason": None, "detail": None, "twilio_credentials": "workspace",
    }
    assert lookup.await_args.args == (PAIR[0], PAIR[1], E164)
    mocks.phones.record_number.assert_not_called()
    assert audit_entries(mocks) == []


@pytest.mark.parametrize("credentials, whose", [
    ("workspace", "the workspace's Twilio account"),
    ("number", f"the Twilio account {E164} was added with"),
])
def test_the_check_of_a_number_its_stored_account_lacks_says_so(
    client, mocks, lookup, credentials, whose
):
    mocks.phones.get_number.return_value = _bought(twilio_credentials=credentials)
    mocks.phones.twilio_account.return_value = PAIR
    lookup.return_value = None
    data = _check(client).json()["data"]
    assert data["releasable"] is False
    assert data["reason"] == "not_on_account"
    assert data["detail"] == (
        f"{E164} isn't on {whose} ({PAIR[0]}), so there is nothing to release there: "
        "it may already have been released or moved on Twilio."
    )
    assert data["twilio_credentials"] == credentials


@pytest.mark.parametrize("row, connected, found, status, reason, says", [
    (
        {"routing_type": "agent", "agent_id": "a1", "project_id": "p1"}, True, True,
        409, "routed", f"{E164} is still routed. Set it to unassigned before releasing it.",
    ),
    (
        {"carrier": "telnyx", "twilio_credentials": "platform", "sid": "",
         "purchase_status": "pending", "telnyx_order_id": "order-1"}, True, True,
        409, "pending",
        f"Telnyx hasn't finished the order for {E164} (order order-1) yet; "
        "it can be released once the order lands.",
    ),
    (
        {"carrier": "asterisk", "twilio_credentials": "platform"}, True, True,
        422, "asterisk", f"{E164} is on asterisk; only Twilio and Telnyx numbers can be released.",
    ),
    (
        {"twilio_credentials": "platform"}, False, True,
        400, "not_connected", NO_TWILIO,
    ),
    (
        {"carrier": "telnyx", "twilio_credentials": "platform"}, False, True,
        422, "not_on_account", ON_PLATFORM_TELNYX,
    ),
    (
        {"carrier": "telnyx", "twilio_credentials": "platform"}, True, False,
        422, "not_on_account", NOT_ON_WORKSPACE_TELNYX,
    ),
    (
        {"twilio_credentials": "number"}, True, False,
        422, "not_on_account",
        f"{E164} isn't on the Twilio account {E164} was added with ({PAIR[0]}), so there is "
        "nothing to release there: it may already have been released or moved on Twilio.",
    ),
    (
        {"twilio_credentials": "platform"}, True, False,
        422, "not_on_account", NOT_ON_WORKSPACE,
    ),
])
def test_each_refusal_has_one_status_and_reason_on_the_check_and_the_release(
    client, mocks, lookup, telnyx_lookup, row, connected, found, status, reason, says
):
    """The check answers 200 with the reason; the release refuses with the
    same sentence and reason, at that reason's one status (#112)."""
    if connected:
        _twilio(mocks)
        _telnyx(mocks)
    if not found:
        lookup.return_value = None
        telnyx_lookup.return_value = None
    if row.get("twilio_credentials") == "number":
        mocks.phones.twilio_account.return_value = PAIR
    mocks.phones.get_number.return_value = _bought(**row)
    r = _check(client)
    assert r.status_code == 200
    data = r.json()["data"]
    assert (data["releasable"], data["reason"], data["detail"]) == (False, reason, says)
    released = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert released.status_code == status
    assert released.json() == {"detail": says, "reason": reason}
    _no_turncall(mocks)
    mocks.phones.delete_number.assert_not_called()


def test_release_checks_the_confirmation_before_the_number(client, mocks, lookup):
    """A refused number with no confirmation is the confirmation's 422."""
    mocks.phones.get_number.return_value = _bought(
        routing_type="agent", agent_id="a1", project_id="p1"
    )
    r = client.post("/phone-numbers/local-1/release", json={})
    assert r.status_code == 422
    assert "reason" not in r.json()
    lookup.assert_not_called()


def test_the_check_finds_a_telnyx_number_on_the_workspace_account(
    client, mocks, lookup, telnyx_lookup
):
    _telnyx(mocks)
    mocks.phones.get_number.return_value = _bought(carrier="telnyx", twilio_credentials="platform")
    assert _check(client).json()["data"] == {
        "releasable": True, "reason": None, "detail": None, "twilio_credentials": "platform",
    }
    assert telnyx_lookup.await_args.args == ("KEY_telnyx", E164)
    lookup.assert_not_called()
    mocks.phones.record_number.assert_not_called()
    assert audit_entries(mocks) == []
    _no_turncall(mocks)


def telnyx_answered(status, body="{}"):
    """The HTTPStatusError `raise_for_status()` raises for a Telnyx answer."""
    request = httpx.Request("GET", "https://api.telnyx.com/v2/phone_numbers")
    response = httpx.Response(status, text=body, request=request)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


RETRY = "retry in a moment."
NOT_A_RETRY = "this needs a fix in TurnCall's builder, not a retry."
# One per branch of telnyx_api.lookup_failure, as the release check says it.
TELNYX_LOOKUP_FAILURES = [
    (
        telnyx_api.TelnyxRejected("Telnyx answered 401"),
        f"Telnyx rejected the workspace's Telnyx API key while checking {E164}: "
        "update it on the Integrations page.",
    ),
    (
        httpx.ConnectError("down"),
        f"Telnyx didn't answer when looking up {E164} on the workspace's Telnyx account; {RETRY}",
    ),
    (
        httpx.ReadTimeout("slow"),
        f"Telnyx didn't answer when looking up {E164} on the workspace's Telnyx account; {RETRY}",
    ),
    (telnyx_answered(429), f"Telnyx answered 429 when looking up {E164}; {RETRY}"),
    (telnyx_answered(503), f"Telnyx answered 503 when looking up {E164}; {RETRY}"),
    (telnyx_answered(400), f"Telnyx answered 400 when looking up {E164}; {NOT_A_RETRY}"),
    (
        telnyx_api.TelnyxMalformed(200, "<html>"),
        f"Telnyx answered 200 when looking up {E164}; {NOT_A_RETRY}",
    ),
    (
        telnyx_api.TelnyxRetryLater(429, 30),
        "Telnyx is rate-limiting the workspace's Telnyx account; try again in about 30 seconds.",
    ),
    (
        telnyx_api.TelnyxRetryLater(503, 1),
        f"Telnyx answered 503 when looking up {E164}; try again in about 1 second.",
    ),
    (
        telnyx_api.TelnyxRetryLater(None, None),
        f"Telnyx is answering too slowly when looking up {E164} on the workspace's Telnyx "
        f"account; {RETRY}",
    ),
]


@pytest.mark.parametrize("failure, says", TELNYX_LOOKUP_FAILURES)
def test_telnyx_failing_the_lookup_is_a_502_sentence(
    client, mocks, telnyx_lookup, failure, says
):
    _telnyx(mocks)
    telnyx_lookup.side_effect = failure
    mocks.phones.get_number.return_value = _bought(carrier="telnyx", twilio_credentials="platform")
    r = _check(client)
    assert (r.status_code, r.json()) == (502, {"detail": says})
    released = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert (released.status_code, released.json()) == (502, {"detail": says})
    _no_turncall(mocks)


@pytest.mark.parametrize("failure, says", [
    (
        twilio_api.TwilioRejected("Twilio answered 401"),
        f"Twilio refused the workspace's Twilio account ({PAIR[0]}) when looking up "
        f"{E164}: check the Twilio card on Integrations.",
    ),
    (
        httpx.ConnectError("down"),
        f"Twilio didn't answer when looking up {E164} on the workspace's Twilio "
        "account — retry in a moment.",
    ),
])
def test_twilio_failing_the_lookup_is_a_502_sentence(client, mocks, lookup, failure, says):
    _twilio(mocks)
    lookup.side_effect = failure
    mocks.phones.get_number.return_value = _bought(twilio_credentials="platform")
    r = _check(client)
    assert r.status_code == 502
    assert r.json()["detail"] == says
    released = client.post("/phone-numbers/local-1/release", json={"confirm": E164})
    assert (released.status_code, released.json()["detail"]) == (502, says)
    mocks.phones.record_number.assert_not_called()
    _no_turncall(mocks)


def test_the_check_of_an_unknown_number_is_a_404(client, mocks):
    mocks.phones.get_number.return_value = None
    r = _check(client)
    assert (r.status_code, r.json()["detail"]) == (404, "phone number not found")


# --- the exact lookup ------------------------------------------------------------


def _twilio_pages(monkeypatch, pages):
    """Serve `pages` (each a list of numbers) as Twilio's pages, in order."""
    seen = []

    def handler(request):
        seen.append(request)
        index = len(seen) - 1
        more = index + 1 < len(pages)
        return httpx.Response(200, json={
            "incoming_phone_numbers": [
                {"sid": f"PN{n[-4:]}", "phone_number": n, "friendly_name": None}
                for n in pages[index]
            ],
            "next_page_uri": (
                f"/2010-04-01/Accounts/AC1/IncomingPhoneNumbers.json?Page={index + 1}"
                if more else None
            ),
        })

    real = httpx.AsyncClient
    monkeypatch.setattr(
        twilio_api.httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    return seen


def test_twilios_partial_match_on_a_longer_number_is_not_the_number(monkeypatch):
    """Twilio's PhoneNumber filter matches partially (kobikis/turncall#344): an
    account holding +13125550100 answers a filter for +1312555010."""
    seen = _twilio_pages(monkeypatch, [["+13125550100", "+13125550101"]])
    found = asyncio.run(FIND_NUMBER("AC1", "tok", "+1312555010"))
    assert found is None
    assert seen[0].url.params["PhoneNumber"] == "+1312555010"
    assert seen[0].headers["authorization"].startswith("Basic ")


def test_the_exact_number_is_found_on_a_later_page(monkeypatch):
    seen = _twilio_pages(monkeypatch, [["+131255501009"], ["+13125550100"]])
    found = asyncio.run(FIND_NUMBER("AC1", "tok", "+13125550100"))
    assert found == {"sid": "PN0100", "phone_number": "+13125550100", "friendly_name": None}
    assert len(seen) == 2
    assert seen[1].url.host == "api.twilio.com"
    assert seen[1].url.params["Page"] == "1"


def test_the_lookup_names_rejected_credentials(monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        twilio_api.httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(401)), **kw),
    )
    with pytest.raises(twilio_api.TwilioRejected):
        asyncio.run(FIND_NUMBER("AC1", "bad", E164))


def _telnyx_pages(monkeypatch, pages, status=200):
    """Serve `pages` (each a list of numbers) as Telnyx's pages, by page[number]."""
    seen = []

    def handler(request):
        seen.append(request)
        index = int(request.url.params["page[number]"]) - 1
        return httpx.Response(status, json={
            "data": [{"id": f"id-{n[-4:]}", "phone_number": n} for n in pages[index]],
            "meta": {"total_pages": len(pages), "page_number": index + 1},
        })

    real = httpx.AsyncClient
    monkeypatch.setattr(
        telnyx_api.httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    return seen


def test_telnyxs_partial_match_on_a_longer_number_is_not_the_number(monkeypatch):
    """Telnyx's filter takes digits only and matches partially: an account
    holding +13125550100 answers a filter for 1312555010."""
    seen = _telnyx_pages(monkeypatch, [["+13125550100", "+13125550101"]])
    assert asyncio.run(FIND_TELNYX("KEY", "+1312555010")) is None
    assert seen[0].url.params["filter[phone_number]"] == "1312555010"
    assert seen[0].url.host == "api.telnyx.com"
    assert seen[0].headers["authorization"] == "Bearer KEY"
    assert len(seen) == 1


def test_the_exact_telnyx_number_is_found_on_a_later_page(monkeypatch):
    seen = _telnyx_pages(monkeypatch, [["+131255501009"], ["+13125550100"]])
    found = asyncio.run(FIND_TELNYX("KEY", E164))
    assert found == {"id": "id-0100", "phone_number": E164}
    assert [r.url.params["page[number]"] for r in seen] == ["1", "2"]


@pytest.mark.parametrize("status", [401, 403])
def test_the_telnyx_lookup_names_a_refused_key(monkeypatch, status):
    _telnyx_pages(monkeypatch, [[]], status=status)
    with pytest.raises(telnyx_api.TelnyxRejected):
        asyncio.run(FIND_TELNYX("bad", E164))


def test_a_telnyx_failure_is_an_http_error(monkeypatch, telnyx_clock):
    seen = _telnyx_pages(monkeypatch, [[]], status=500)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(FIND_TELNYX("KEY", E164))
    assert (len(seen), telnyx_clock.waits) == (3, [1.0, 2.0])  # retried twice first


def test_a_telnyx_body_that_isnt_the_list_is_malformed(monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        telnyx_api.httpx, "AsyncClient",
        lambda **kw: real(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")), **kw
        ),
    )
    with pytest.raises(telnyx_api.TelnyxMalformed) as raised:
        asyncio.run(FIND_TELNYX("KEY", E164))
    assert (raised.value.status, raised.value.body) == (200, "<html>")


def test_an_unexpected_telnyx_answer_is_logged_truncated(caplog):
    with caplog.at_level("WARNING", logger="turncall_builder"):
        telnyx_api.lookup_failure(telnyx_answered(422, "x" * 1000), E164)
    [record] = [r for r in caplog.records if "telnyx lookup" in r.getMessage()]
    assert "status 422" in record.getMessage()
    assert "x" * 300 in record.getMessage() and "x" * 301 not in record.getMessage()


def test_a_retryable_telnyx_answer_is_not_logged(caplog):
    with caplog.at_level("WARNING", logger="turncall_builder"):
        telnyx_api.lookup_failure(telnyx_answered(503), E164)
    assert not [r for r in caplog.records if "telnyx lookup" in r.getMessage()]


# --- roles --------------------------------------------------------------------


ROUTES = [
    ("get", "/phone-numbers/available?carrier=twilio", None),
    ("get", "/phone-numbers/local-1/release-check", None),
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
