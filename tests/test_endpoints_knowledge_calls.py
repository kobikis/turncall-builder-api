"""Calls, knowledge, takeaways, credentials endpoints (per-agent proxies)."""

import json

import httpx
import pytest
from app.backends import registry

from tests.conftest import backend_row

KB = {"name": "builder-knowledge", "id": "kb-1"}


# ---- calls -----------------------------------------------------------------


def test_agent_calls_404_when_no_backend(client, mocks):
    mocks.registry.get_backend.return_value = None
    assert client.get("/agents/nope/calls").status_code == 404


def test_agent_calls_proxies_turncall(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.list_calls.return_value = {"success": True, "data": [{"id": "c1"}]}
    r = client.get("/agents/a1/calls")
    assert r.status_code == 200
    assert r.json()["data"][0]["id"] == "c1"


def test_agent_calls_forward_derived_call_fields_unchanged(client, mocks):
    call = {
        "id": "c1",
        "ended_reason": "customer_ended_call",
        "agent_name": "support",
        "agent_version": 3,
        "latency_summary": {"turns": 2},
    }
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.list_calls.return_value = {"success": True, "data": [call]}
    assert client.get("/agents/a1/calls").json()["data"] == [call]


def test_test_chat_proxies_to_turncall(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.send_chat.return_value = {"reply": "hello!", "session_id": "s1"}
    r = client.post("/agents/a1/chat", json={"message": "hi", "session_id": "s1"})
    assert r.status_code == 200
    assert r.json()["data"]["reply"] == "hello!"
    mocks.client.send_chat.assert_awaited_once()


def test_test_chat_404_when_no_backend(client, mocks):
    mocks.registry.get_backend.return_value = None
    assert client.post("/agents/nope/chat", json={"message": "hi"}).status_code == 404


def test_call_events_forwards_event_type(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.get_call_events.return_value = [{"event_type": "metrics.turn"}]
    r = client.get("/agents/a1/calls/c1/events?event_type=metrics.turn")
    assert r.status_code == 200
    assert r.json()["data"] == [{"event_type": "metrics.turn"}]
    mocks.client.get_call_events.assert_awaited_once_with(
        "c1", backend_row()["api_key"], event_type="metrics.turn"
    )


def test_call_events_without_type_asks_for_all(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.get_call_events.return_value = []
    client.get("/agents/a1/calls/c1/events")
    assert mocks.client.get_call_events.await_args.kwargs["event_type"] is None


def test_call_of_another_project_is_404(client, mocks):
    # The agent's key is scoped to its own project, so TurnCall 404s a call
    # belonging to any other agent; that answer must reach the browser as-is.
    mocks.registry.get_backend.return_value = backend_row()
    resp = httpx.Response(404, request=httpx.Request("GET", "http://tc/x"))
    for method in ("get_call_events", "get_call_tool_invocations"):
        getattr(mocks.client, method).side_effect = httpx.HTTPStatusError(
            "nf", request=resp.request, response=resp
        )
    assert client.get("/agents/a1/calls/other/events").status_code == 404
    assert client.get("/agents/a1/calls/other/tool-invocations").status_code == 404


def test_call_events_and_context_404_when_no_backend(client, mocks):
    mocks.registry.get_backend.return_value = None
    assert client.get("/agents/nope/calls/c1/events").status_code == 404


def test_recording_missing_maps_status(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    resp = httpx.Response(404, request=httpx.Request("GET", "http://tc/x"))
    mocks.client.get_call_recording.side_effect = httpx.HTTPStatusError(
        "nf", request=resp.request, response=resp
    )
    r = client.get("/agents/a1/calls/c1/recording")
    assert r.status_code == 404


def test_recording_full_body_advertises_ranges(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.get_call_recording.return_value = b"RIFFwavdata"
    r = client.get("/agents/a1/calls/c1/recording")
    assert r.status_code == 200
    assert r.content == b"RIFFwavdata"
    assert r.headers["accept-ranges"] == "bytes"


def test_recording_range_request_gets_206_slice(client, mocks):
    """Safari sends Range and refuses to play without a 206 + Content-Range."""
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.get_call_recording.return_value = b"0123456789"
    r = client.get("/agents/a1/calls/c1/recording", headers={"Range": "bytes=2-5"})
    assert r.status_code == 206
    assert r.content == b"2345"
    assert r.headers["content-range"] == "bytes 2-5/10"


def test_recording_open_ended_and_invalid_ranges(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.get_call_recording.return_value = b"0123456789"
    r = client.get("/agents/a1/calls/c1/recording", headers={"Range": "bytes=8-"})
    assert (r.status_code, r.content) == (206, b"89")
    r = client.get("/agents/a1/calls/c1/recording", headers={"Range": "bytes=99-"})
    assert r.status_code == 416


# ---- knowledge -------------------------------------------------------------


def test_list_documents_no_kb_returns_empty(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.list_knowledge_bases.return_value = []  # no KB yet
    r = client.get("/agents/a1/knowledge/documents")
    assert r.status_code == 200
    assert r.json()["data"]["documents"] == []


def test_list_documents_with_kb(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.list_knowledge_bases.return_value = [KB]
    mocks.client.list_documents.return_value = [{"id": "d1"}]
    r = client.get("/agents/a1/knowledge/documents")
    assert r.status_code == 200
    assert r.json()["data"]["documents"][0]["id"] == "d1"


def test_each_upload_gets_a_knowledge_base_of_its_own(client, mocks):
    """TurnCall sets how an agent reads knowledge per link, so a mode per
    document needs a knowledge base per document (ADR-0024)."""
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.create_knowledge_base.return_value = {"id": "kb-9", "name": "builder-knowledge-ab12cd34"}
    mocks.client.upload_document.return_value = {"id": "d1", "status": "processing"}
    r = client.post(
        "/agents/a1/knowledge/documents",
        files={"file": ("f.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 200
    assert mocks.client.create_knowledge_base.await_args.args[0].startswith("builder-knowledge-")
    link = mocks.client.link_knowledge_base.await_args
    assert link.args[:2] == ("a1", "kb-9")
    assert link.args[3] == "prompt"  # whole, as every document was before modes
    assert mocks.client.upload_document.await_args.args[0] == "kb-9"


def test_an_upload_can_choose_its_mode(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.create_knowledge_base.return_value = {"id": "kb-9", "name": "builder-knowledge-x"}
    client.post(
        "/agents/a1/knowledge/documents",
        files={"file": ("menu.pdf", b"%PDF", "application/pdf")},
        data={"mode": "auto"},
    )
    assert mocks.client.link_knowledge_base.await_args.args[3] == "auto"


def test_a_failed_upload_takes_its_empty_knowledge_base_with_it(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.create_knowledge_base.return_value = {"id": "kb-9", "name": "builder-knowledge-x"}
    mocks.client.upload_document.side_effect = _http_status_error(415, "unsupported")
    r = client.post(
        "/agents/a1/knowledge/documents",
        files={"file": ("f.bin", b"x", "application/octet-stream")},
    )
    assert r.status_code == 415
    mocks.client.unlink_knowledge_base.assert_awaited_once()
    assert mocks.client.delete_knowledge_base.await_args.args[0] == "kb-9"


def test_upload_document_surfaces_turncall_error(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.list_knowledge_bases.return_value = [KB]
    resp = httpx.Response(
        415, json={"error": "unsupported"}, request=httpx.Request("POST", "http://tc/x")
    )
    mocks.client.upload_document.side_effect = httpx.HTTPStatusError(
        "bad", request=resp.request, response=resp
    )
    r = client.post(
        "/agents/a1/knowledge/documents",
        files={"file": ("f.bin", b"x", "application/octet-stream")},
    )
    assert r.status_code == 415


def test_search_knowledge_no_kb_empty(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.list_knowledge_bases.return_value = []
    r = client.post("/agents/a1/knowledge/search", json={"query": "hi"})
    assert r.status_code == 200
    assert r.json()["data"]["results"] == []


# ---- takeaways -------------------------------------------------------------


def test_list_takeaways(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.list_takeaways.return_value = [{"id": "t1", "name": "sentiment"}]
    r = client.get("/agents/a1/takeaways")
    assert r.status_code == 200
    assert r.json()["data"]["takeaways"][0]["id"] == "t1"


def test_create_takeaway_attaches_to_agent(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.create_takeaway.return_value = {"id": "t1", "name": "sentiment"}
    r = client.post(
        "/agents/a1/takeaways",
        json={"name": "sentiment", "schema": {"type": "object"}},
    )
    assert r.status_code == 200
    # attach = update_agent with the new takeaway id folded into analysis
    mocks.client.update_agent.assert_awaited_once()


def test_create_takeaway_surfaces_conflict(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    resp = httpx.Response(
        409, json={"error": "name taken"}, request=httpx.Request("POST", "http://tc/x")
    )
    mocks.client.create_takeaway.side_effect = httpx.HTTPStatusError(
        "dup", request=resp.request, response=resp
    )
    r = client.post(
        "/agents/a1/takeaways", json={"name": "dup", "schema": {"type": "object"}}
    )
    assert r.status_code == 409


def test_update_takeaway(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.update_takeaway.return_value = {"id": "t1", "prompt": "new"}
    r = client.put("/agents/a1/takeaways/t1", json={"prompt": "new"})
    assert r.status_code == 200
    assert r.json()["data"]["prompt"] == "new"


def test_update_takeaway_surfaces_error(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    resp = httpx.Response(
        404, json={"error": "gone"}, request=httpx.Request("PUT", "http://tc/x")
    )
    mocks.client.update_takeaway.side_effect = httpx.HTTPStatusError(
        "nf", request=resp.request, response=resp
    )
    r = client.put("/agents/a1/takeaways/t1", json={"prompt": "x"})
    assert r.status_code == 404


def test_delete_takeaway_detaches_then_deletes(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(
        agent_id="a1",
        config={"name": "A", "analysis": {"takeaway_ids": ["t1"]}},
    )
    r = client.delete("/agents/a1/takeaways/t1")
    assert r.status_code == 200
    mocks.client.update_agent.assert_awaited_once()  # detach first
    mocks.client.delete_takeaway.assert_awaited_once()


# ---- webrtc test-call signaling proxy (#35) --------------------------------


def test_webrtc_connect_404(client, mocks):
    mocks.registry.get_backend.return_value = None
    assert client.post("/agents/nope/webrtc/connect", json={"sdp": "o"}).status_code == 404


def test_webrtc_connect_proxies_answer_without_leaking_key(client, mocks):
    """The proxy forwards the offer with the agent's key server-side and returns
    the raw SDP answer — no TurnCall key ever reaches the browser."""
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")  # api_key tc_key
    mocks.client.webrtc_connect.return_value = {"sdp": "answer", "type": "answer"}
    r = client.post(
        "/agents/a1/webrtc/connect",
        json={"sdp": "offer", "type": "offer"},
    )
    assert r.status_code == 200
    assert r.json() == {"sdp": "answer", "type": "answer"}  # raw passthrough
    # forwarded with the agent's key; nothing key-shaped comes back to the browser.
    fwd_body, fwd_key = mocks.client.webrtc_connect.await_args.args
    assert fwd_key == "tc_key"
    assert fwd_body["sdp"] == "offer"
    assert "tc_key" not in r.text and "api_key" not in r.text


def test_webrtc_connect_pins_agent_and_strips_client_routing(client, mocks):
    """A caller can't retarget the leg: agent_id is forced to the path and any
    client-supplied server_url/agent_id is dropped before forwarding."""
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.webrtc_connect.return_value = {"sdp": "answer"}
    client.post(
        "/agents/a1/webrtc/connect",
        json={"sdp": "o", "agent_id": "someone-else", "server_url": "http://evil"},
    )
    fwd_body = mocks.client.webrtc_connect.await_args.args[0]
    assert fwd_body["agent_id"] == "a1"  # pinned to the path, not "someone-else"
    assert "server_url" not in fwd_body


def test_webrtc_connect_strips_nested_request_data_routing(client, mocks):
    """TurnCall reads routing from a nested request_data dict (with precedence), so
    the proxy must sanitize it too — a nested server_url/agent_id can't retarget."""
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.webrtc_connect.return_value = {"sdp": "answer"}
    client.post(
        "/agents/a1/webrtc/connect",
        json={
            "sdp": "o",
            "request_data": {"server_url": "http://evil", "agent_id": "someone-else"},
        },
    )
    fwd_body = mocks.client.webrtc_connect.await_args.args[0]
    assert fwd_body["agent_id"] == "a1"
    assert fwd_body["request_data"]["agent_id"] == "a1"  # nested pin too
    assert "server_url" not in fwd_body["request_data"]


def test_webrtc_connect_maps_unreachable_turncall_to_502(client, mocks):
    import httpx

    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.webrtc_connect.side_effect = httpx.ConnectError("down")
    r = client.post("/agents/a1/webrtc/connect", json={"sdp": "o"})
    assert r.status_code == 502


def _http_status_error(status: int, text: str):
    import httpx

    req = httpx.Request("POST", "http://tc/v1/webrtc/connect")
    resp = httpx.Response(status, text=text, request=req)
    return httpx.HTTPStatusError("upstream", request=req, response=resp)


def test_webrtc_connect_bad_offer_maps_to_422_without_leaking_upstream(client, mocks):
    """A TurnCall 4xx (malformed SDP) becomes a 422 with a generic message — the raw
    upstream body (which can echo the key/ids) is never forwarded to the browser."""
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.webrtc_connect.side_effect = _http_status_error(
        400, "agent_id must be a UUID, got 'tc_key'"
    )
    r = client.post("/agents/a1/webrtc/connect", json={"sdp": "o"})
    assert r.status_code == 422
    assert "tc_key" not in r.text  # upstream detail not echoed


def test_webrtc_connect_stale_key_maps_to_502_not_401(client, mocks):
    """A TurnCall 401 (stale agent key) is an upstream failure → 502, not a 401 that
    the browser would mistake for its own login expiring."""
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.webrtc_connect.side_effect = _http_status_error(401, "Unauthorized")
    r = client.post("/agents/a1/webrtc/connect", json={"sdp": "o"})
    assert r.status_code == 502



def test_webrtc_connect_relays_why_the_agent_could_not_start(client, mocks):
    """TurnCall's pipeline_start_failed carries a reason written for the caller
    (a provider with no key, say). Without it the test call only timed out."""
    import json

    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    body = {
        "success": False,
        "code": "pipeline_start_failed",
        "error": "The agent could not start (call c1): Missing credentials",
    }
    mocks.client.webrtc_connect.side_effect = _http_status_error(500, json.dumps(body))
    r = client.post("/agents/a1/webrtc/connect", json={"sdp": "o"})
    assert r.status_code == 502
    assert "Missing credentials" in r.json()["detail"]
    assert "Integrations" in r.json()["detail"]
    assert r.json()["missing_keys"] == []


def test_webrtc_connect_names_the_missing_keys(client, mocks):
    import json

    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    body = {
        "code": "pipeline_start_failed",
        "error": "The agent could not start (call c1): No API key for deepgram",
        "details": {"missing_keys": ["deepgram"]},
    }
    mocks.client.webrtc_connect.side_effect = _http_status_error(500, json.dumps(body))
    r = client.post("/agents/a1/webrtc/connect", json={"sdp": "o"})
    assert r.status_code == 502
    assert r.json()["missing_keys"] == ["deepgram"]


def test_webrtc_connect_other_upstream_errors_stay_generic(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.webrtc_connect.side_effect = _http_status_error(
        500, '{"code": "internal_error", "error": "secret internals"}'
    )
    r = client.post("/agents/a1/webrtc/connect", json={"sdp": "o"})
    assert r.status_code == 502
    assert "secret internals" not in r.text


# --- a test call resolves the way a phone call does (ADR-0020) ----------------
#
# Resolving by agent_id loads the STORED config, whose mcp_servers deliberately
# carry no credential — so Connected apps were unreachable from the Test tab,
# and silently: the agent simply had no tool to call and confirmed the booking
# anyway. Routing through the backend's own /call-init is the path that carries
# a freshly minted one.


def test_webrtc_connect_routes_through_call_init_when_we_hold_the_secret(client, mocks):
    row = backend_row(agent_id="a1")
    row["call_init_secret"] = "cis-1"
    mocks.registry.get_backend.return_value = row
    mocks.client.webrtc_connect.return_value = {"sdp": "answer"}

    client.post("/agents/a1/webrtc/connect", json={"sdp": "o"})

    fwd = mocks.client.webrtc_connect.await_args.args[0]
    # The agent's own backend, derived from its port — never anything the
    # caller named. The full endpoint, because TurnCall POSTs to server_url as
    # given: a bare base URL lands on `POST /` and comes back as "server_url did
    # not return a valid agent".
    assert fwd["server_url"] == f"{registry.backend_url(row['port'])}/call-init"
    assert fwd["server_url_secret"] == "cis-1"
    # agent_id would win over server_url in TurnCall's resolver, taking the
    # stored config and the credential with it.
    assert "agent_id" not in fwd
    assert "agent_id" not in fwd["request_data"]


def test_webrtc_connect_falls_back_to_agent_id_without_a_secret(client, mocks):
    """An agent older than the secret column still connects — it just cannot
    carry credentials, exactly as before this existed."""
    row = backend_row(agent_id="a1")
    row["call_init_secret"] = None
    mocks.registry.get_backend.return_value = row
    mocks.client.webrtc_connect.return_value = {"sdp": "answer"}

    client.post("/agents/a1/webrtc/connect", json={"sdp": "o"})

    fwd = mocks.client.webrtc_connect.await_args.args[0]
    assert fwd["agent_id"] == "a1"
    assert "server_url" not in fwd


def test_webrtc_connect_still_refuses_a_client_chosen_backend(client, mocks):
    """The routing is server-chosen either way. A caller supplying their own
    server_url must not be able to point the leg at a backend they control."""
    row = backend_row(agent_id="a1")
    row["call_init_secret"] = "cis-1"
    mocks.registry.get_backend.return_value = row
    mocks.client.webrtc_connect.return_value = {"sdp": "answer"}

    client.post(
        "/agents/a1/webrtc/connect",
        json={
            "sdp": "o",
            "server_url": "http://evil",
            "server_url_secret": "stolen",
            "request_data": {"server_url": "http://evil", "server_url_secret": "stolen"},
        },
    )

    fwd = mocks.client.webrtc_connect.await_args.args[0]
    assert fwd["server_url"].endswith("/call-init")
    assert "evil" not in repr(fwd)
    assert "stolen" not in repr(fwd)
    assert fwd["server_url_secret"] == "cis-1"
    assert fwd["request_data"]["server_url_secret"] == "cis-1"


def test_unreachable_turncall_is_a_502_not_a_500(client, mocks):
    # TurnCall restarting (or down) is an upstream outage: a traceback and a
    # 500 made the console read it as a builder bug.
    import httpx

    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.list_calls.side_effect = httpx.ConnectTimeout("timed out")
    mocks.client.get_call_events.side_effect = httpx.ConnectError("refused")
    r = client.get("/agents/a1/calls")
    assert r.status_code == 502
    assert "unreachable" in r.json()["detail"].lower()
    assert client.get("/agents/a1/calls/c1/events").status_code == 502


# ---- test variables: the agent's {{placeholders}} on a test chat or call ----


def test_test_chat_forwards_variables(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.send_chat.return_value = {"reply": "hi Ada", "session_id": "s1"}
    r = client.post(
        "/agents/a1/chat", json={"message": "hi", "variables": {"name": "Ada"}}
    )
    assert r.status_code == 200
    assert mocks.client.send_chat.await_args.kwargs["variables"] == {"name": "Ada"}


def test_test_chat_refuses_variables_past_turncalls_bound(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    too_many = {f"v{n}": "x" for n in range(51)}
    r = client.post("/agents/a1/chat", json={"message": "hi", "variables": too_many})
    assert r.status_code == 422
    assert "at most 50" in r.text
    mocks.client.send_chat.assert_not_awaited()


def test_webrtc_connect_carries_the_header_variables_to_turncall(client, mocks):
    """The transport replaces the offer's body with the SDP alone, so a test
    call's variables come in a header — URL-encoded JSON, since a header is
    ASCII and an address need not be."""
    from urllib.parse import quote

    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.webrtc_connect.return_value = {"sdp": "answer"}
    variables = {"address": "רחוב הרצל 12", "vip": True}
    r = client.post(
        "/agents/a1/webrtc/connect",
        json={"sdp": "o"},
        headers={"X-Call-Variables": quote(json.dumps(variables))},
    )
    assert r.status_code == 200
    fwd_body = mocks.client.webrtc_connect.await_args.args[0]
    assert fwd_body["request_data"]["variables"] == {
        "address": "רחוב הרצל 12",
        "vip": "True",
    }


def test_webrtc_connect_without_the_header_sends_no_variables(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.webrtc_connect.return_value = {"sdp": "answer"}
    client.post("/agents/a1/webrtc/connect", json={"sdp": "o"})
    assert "variables" not in mocks.client.webrtc_connect.await_args.args[0]["request_data"]


@pytest.mark.parametrize("header", ["not json", "%5B1%5D", "%7B%22a%22%3A%7B%7D%7D"])
def test_webrtc_connect_refuses_a_malformed_variables_header(client, mocks, header):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    r = client.post(
        "/agents/a1/webrtc/connect",
        json={"sdp": "o"},
        headers={"X-Call-Variables": header},
    )
    assert r.status_code == 400
    mocks.client.webrtc_connect.assert_not_awaited()


# ---- knowledge retrieval mode, per document (ADR-0024) ----------------------

MENU_KB = {"id": "kb-1", "name": "builder-knowledge"}
FAQ_KB = {"id": "kb-2", "name": "builder-knowledge-ab12cd34"}
OTHER_KB = {"id": "kb-x", "name": "something-else"}


def _two_documents(mocks, menu_mode="prompt", faq_mode="auto"):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.list_knowledge_bases.return_value = [MENU_KB, FAQ_KB, OTHER_KB]
    mocks.client.list_documents.side_effect = lambda kb_id, _key: {
        "kb-1": [{"id": "d-menu", "knowledge_base_id": "kb-1", "filename": "menu.pdf"}],
        "kb-2": [{"id": "d-faq", "knowledge_base_id": "kb-2", "filename": "faq.md"}],
    }[kb_id]
    mocks.client.list_agent_knowledge_bases.return_value = [
        {"knowledge_base_id": "kb-1", "mode": menu_mode, "top_k": 5, "tool_description": None},
        {"knowledge_base_id": "kb-2", "mode": faq_mode, "top_k": 5, "tool_description": None},
    ]


def test_each_document_says_how_the_agent_reads_it(client, mocks):
    _two_documents(mocks)
    docs = client.get("/agents/a1/knowledge/documents").json()["data"]["documents"]
    assert {d["filename"]: d["retrieval"]["mode"] for d in docs} == {
        "menu.pdf": "prompt",
        "faq.md": "auto",
    }


def test_one_documents_mode_changes_and_not_the_others(client, mocks):
    _two_documents(mocks)
    r = client.put("/agents/a1/knowledge/documents/d-menu/retrieval", json={"mode": "auto", "top_k": 3})
    assert r.status_code == 200, r.text
    assert mocks.client.unlink_knowledge_base.await_args.args[:2] == ("a1", "kb-1")
    link = mocks.client.link_knowledge_base.await_args
    assert (link.args[1], link.args[3], link.kwargs["top_k"]) == ("kb-1", "auto", 3)


def test_an_unchanged_mode_touches_nothing(client, mocks):
    _two_documents(mocks)
    r = client.put("/agents/a1/knowledge/documents/d-faq/retrieval", json={"mode": "auto"})
    assert r.status_code == 200
    mocks.client.unlink_knowledge_base.assert_not_awaited()
    mocks.client.link_knowledge_base.assert_not_awaited()


def test_tool_mode_gets_a_description_when_none_is_given(client, mocks):
    _two_documents(mocks)
    client.put("/agents/a1/knowledge/documents/d-menu/retrieval", json={"mode": "tool"})
    assert mocks.client.link_knowledge_base.await_args.kwargs["tool_description"]


def test_a_refused_relink_puts_the_old_link_back(client, mocks):
    """TurnCall has no update for a link: unlink, then link. If the new link
    is refused the document must not be left unread."""
    _two_documents(mocks)
    mocks.client.link_knowledge_base.side_effect = [_http_status_error(422, "no"), None]
    r = client.put("/agents/a1/knowledge/documents/d-menu/retrieval", json={"mode": "auto"})
    assert r.status_code == 422
    restored = mocks.client.link_knowledge_base.await_args_list[-1]
    assert (restored.args[1], restored.args[3]) == ("kb-1", "prompt")


def test_mode_of_an_unknown_document_is_404(client, mocks):
    _two_documents(mocks)
    r = client.put("/agents/a1/knowledge/documents/nope/retrieval", json={"mode": "auto"})
    assert r.status_code == 404


def test_retrieval_refuses_an_unknown_mode(client, mocks):
    _two_documents(mocks)
    r = client.put("/agents/a1/knowledge/documents/d-menu/retrieval", json={"mode": "magic"})
    assert r.status_code == 422


def test_deleting_a_document_deletes_its_knowledge_base(client, mocks):
    _two_documents(mocks)
    r = client.delete("/agents/a1/knowledge/documents/d-faq")
    assert r.status_code == 200
    assert mocks.client.delete_document.await_args.args[:2] == ("kb-2", "d-faq")
    assert mocks.client.delete_knowledge_base.await_args.args[0] == "kb-2"


def test_search_covers_every_document_best_first(client, mocks):
    _two_documents(mocks)
    mocks.client.search_knowledge_base.side_effect = lambda kb_id, _q, _key: {
        "kb-1": {"results": [{"content": "Salmon roll", "similarity": 0.4}]},
        "kb-2": {"results": [{"content": "We open at 10", "similarity": 0.8}]},
    }[kb_id]
    r = client.post("/agents/a1/knowledge/search", json={"query": "hours"})
    assert [x["content"] for x in r.json()["data"]["results"]] == ["We open at 10", "Salmon roll"]
    # Only the agent's own document knowledge bases are searched.
    assert {c.args[0] for c in mocks.client.search_knowledge_base.await_args_list} == {"kb-1", "kb-2"}
