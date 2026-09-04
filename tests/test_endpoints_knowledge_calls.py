"""Calls, knowledge, takeaways, credentials endpoints (per-agent proxies)."""

import httpx

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


def test_upload_document_creates_kb_on_first_upload(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.list_knowledge_bases.return_value = []  # triggers create+link
    mocks.client.create_knowledge_base.return_value = KB
    mocks.client.upload_document.return_value = {"id": "d1", "status": "ready"}
    r = client.post(
        "/agents/a1/knowledge/documents",
        files={"file": ("f.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 200
    mocks.client.create_knowledge_base.assert_awaited_once()
    mocks.client.link_knowledge_base.assert_awaited_once()
    # Linked in prompt mode so small docs are always in the agent's context.
    assert mocks.client.link_knowledge_base.await_args.kwargs.get("mode") == "prompt"


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
