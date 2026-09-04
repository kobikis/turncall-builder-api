"""TurnCallClient._request: response unwrapping, auth header, error raising."""

import httpx
import pytest

from app.turncall_client import TurnCallClient


def _client_with(handler, platform_key: str = "") -> TurnCallClient:
    c = TurnCallClient("http://tc.test", platform_key=platform_key)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


@pytest.mark.asyncio
async def test_extract_data_unwraps_data_field():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"success": True, "data": {"id": "x1"}})

    client = _client_with(handler)
    out = await client.create_agent({"name": "a"}, "tc_key")
    assert out == {"id": "x1"}
    assert captured["auth"] == "Bearer tc_key"  # api_key -> Authorization
    assert captured["url"] == "http://tc.test/v1/agents"


@pytest.mark.asyncio
async def test_extract_json_returns_whole_body():
    def handler(request):
        return httpx.Response(200, json={"success": True, "data": [1], "total": 5})

    client = _client_with(handler)
    out = await client.list_calls("k")
    assert out == {"success": True, "data": [1], "total": 5}


@pytest.mark.asyncio
async def test_extract_content_returns_bytes():
    def handler(request):
        return httpx.Response(200, content=b"WAVDATA")

    client = _client_with(handler)
    assert await client.get_call_recording("c1", "k") == b"WAVDATA"


@pytest.mark.asyncio
async def test_extract_none_returns_none_for_delete():
    def handler(request):
        assert request.method == "DELETE"
        return httpx.Response(204)

    client = _client_with(handler)
    assert await client.delete_agent("a1", "k") is None


@pytest.mark.asyncio
async def test_delete_project_hits_projects_endpoint_with_auth():
    def handler(request):
        assert request.method == "DELETE"
        assert request.url.path == "/v1/projects/proj-1"
        assert request.headers["Authorization"] == "Bearer pk"
        return httpx.Response(200, json={"success": True, "data": {"deleted": True}})

    client = _client_with(handler)
    assert await client.delete_project("proj-1", "pk") is None


@pytest.mark.asyncio
async def test_raises_on_error_status():
    def handler(request):
        return httpx.Response(409, json={"error": "conflict"})

    client = _client_with(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.create_takeaway({"name": "x"}, "k")


# --- platform key on the gated bootstrap (turncall#102, #37) ------------------


@pytest.mark.asyncio
async def test_create_project_sends_platform_key_not_bearer():
    seen = {}

    def handler(request):
        seen["platform"] = request.headers.get("X-Platform-Key")
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": {"id": "p1"}})

    client = _client_with(handler, platform_key="secret-pk")
    assert await client.create_project("x") == "p1"
    assert seen["platform"] == "secret-pk"  # gated bootstrap presents the platform key
    assert seen["auth"] is None  # ...and not a Bearer key


@pytest.mark.asyncio
async def test_create_api_key_sends_platform_key():
    seen = {}

    def handler(request):
        seen["platform"] = request.headers.get("X-Platform-Key")
        return httpx.Response(200, json={"data": {"raw_key": "tc_new"}})

    client = _client_with(handler, platform_key="secret-pk")
    assert await client.create_api_key("proj-1") == "tc_new"
    assert seen["platform"] == "secret-pk"


@pytest.mark.asyncio
async def test_bearer_calls_never_send_platform_key():
    """Per-agent calls authenticate with the project key only — the platform key
    must not ride along on them even when configured."""
    seen = {}

    def handler(request):
        seen["platform"] = request.headers.get("X-Platform-Key")
        return httpx.Response(200, json={"data": {"id": "a1"}})

    client = _client_with(handler, platform_key="secret-pk")
    await client.create_agent({"name": "a"}, "tc_key")
    assert seen["platform"] is None


@pytest.mark.asyncio
async def test_no_platform_header_when_unconfigured():
    """Backward compatible: with no platform key set, the header is absent so an
    un-gated TurnCall (pre-#102) still accepts the bootstrap."""
    seen = {}

    def handler(request):
        seen["has_header"] = "X-Platform-Key" in request.headers
        return httpx.Response(200, json={"data": {"id": "p1"}})

    client = _client_with(handler, platform_key="")
    await client.create_project("x")
    assert seen["has_header"] is False


@pytest.mark.asyncio
async def test_gate_rejection_becomes_actionable_error():
    """A 401 from the gate (missing OR wrong key) is remapped to a clear message
    naming PLATFORM_API_KEY, not a bare HTTPStatusError the operator can't diagnose."""
    def handler(request):
        return httpx.Response(401, json={"error": "Missing or invalid platform credential"})

    client = _client_with(handler, platform_key="wrong")
    with pytest.raises(RuntimeError, match="PLATFORM_API_KEY"):
        await client.create_project("x")


@pytest.mark.asyncio
async def test_create_api_key_gate_rejection_becomes_actionable_error():
    """create_api_key shares _bootstrap, so it gets the same 401 remap."""
    def handler(request):
        return httpx.Response(401, json={"error": "Missing or invalid platform credential"})

    client = _client_with(handler, platform_key="wrong")
    with pytest.raises(RuntimeError, match="PLATFORM_API_KEY"):
        await client.create_api_key("proj-1")


@pytest.mark.asyncio
async def test_non_401_bootstrap_error_propagates_unchanged():
    """A non-gate error (e.g. 409 duplicate) is not disguised as a key problem."""
    def handler(request):
        return httpx.Response(409, json={"error": "project exists"})

    client = _client_with(handler, platform_key="pk")
    with pytest.raises(httpx.HTTPStatusError):
        await client.create_project("x")
