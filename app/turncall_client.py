"""Thin async client for the TurnCall REST API.

Each created agent gets its OWN TurnCall project + key + webhook (ADR-0004/0005,
revised): project webhooks are project-scoped, so one project per agent is what
gives each generated backend exactly its own events. Project/key creation is
unauthenticated (dev); agent + webhook calls use that agent's key.

One pooled httpx client for the process (closed via close() on app shutdown);
every method routes through _request(), so auth, error-raising, and response
unwrapping live in exactly one place.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx


class TurnCallClient:
    def __init__(self, base_url: str, platform_key: str = "") -> None:
        self._base = base_url.rstrip("/")
        # Presented as X-Platform-Key on project/key bootstrap only (turncall#102).
        self._platform_key = platform_key
        self._client = httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
        )

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _auth(api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        api_key: str | None = None,
        extract: Literal["data", "json", "content", "none"] = "data",
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """One request. `extract` picks what to return: the response body's
        `data` field (default), the whole JSON, raw bytes, or nothing."""
        headers = dict(kwargs.pop("headers", {}) or {})
        if api_key:
            headers.update(self._auth(api_key))
        resp = await self._client.request(
            method,
            f"{self._base}{path}",
            headers=headers,
            timeout=timeout if timeout is not None else 30,
            **kwargs,
        )
        resp.raise_for_status()
        if extract == "none":
            return None
        if extract == "content":
            return resp.content
        body = resp.json()
        return body if extract == "json" else body["data"]

    async def get_openapi(self) -> dict[str, Any] | None:
        """Fetch the live OpenAPI spec (served unauthenticated at the host root).
        Best-effort — returns None if TurnCall is unreachable."""
        try:
            return await self._request("GET", "/openapi.json", extract="json")
        except Exception:
            return None

    async def create_project(self, name: str) -> str:
        data = await self._bootstrap("POST", "/v1/projects", json={"name": name})
        return data["id"]

    async def create_api_key(self, project_id: str, name: str = "agent") -> str:
        data = await self._bootstrap(
            "POST",
            "/v1/api-keys",
            params={"project_id": project_id},
            json={"name": name, "role": "admin"},
        )
        return data["raw_key"]

    async def _bootstrap(self, method: str, path: str, **kwargs: Any) -> Any:
        """A platform-gated bootstrap call (project/key creation, turncall#102).
        Attaches X-Platform-Key (when configured — an empty key omits it, so this
        still works against an un-gated TurnCall) and turns the gate's 401 into an
        actionable error instead of a bare 401 the caller can't diagnose. Building
        the header here, not in _request, means a per-agent Bearer call can never
        attach the platform credential by accident. The gate answers a missing OR
        wrong key with 401 (turncall#102), so 401 is the exact signal to remap."""
        headers = {"X-Platform-Key": self._platform_key} if self._platform_key else {}
        try:
            return await self._request(method, path, headers=headers, **kwargs)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise RuntimeError(
                    "TurnCall rejected project/key bootstrap — set builder-api's "
                    "PLATFORM_API_KEY to match TurnCall's PLATFORM_API_KEY."
                ) from exc
            raise

    async def webrtc_connect(self, body: dict[str, Any], api_key: str) -> dict[str, Any]:
        """Forward a browser's SDP offer to TurnCall's WebRTC signaling and return
        the raw SDP answer (not the {success,data} envelope — it's a signaling
        payload). Used by the builder proxy (#35) so the agent key stays server-side.
        Pipeline build can take a few seconds, hence the longer timeout."""
        return await self._request(
            "POST", "/v1/webrtc/connect", api_key=api_key, extract="json",
            json=body, timeout=60,
        )

    async def create_agent(self, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        return await self._request("POST", "/v1/agents", api_key=api_key, json=payload)

    async def get_agent(self, agent_id: str, api_key: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/agents/{agent_id}", api_key=api_key)

    async def update_agent(
        self, agent_id: str, payload: dict[str, Any], api_key: str
    ) -> dict[str, Any]:
        return await self._request(
            "PUT", f"/v1/agents/{agent_id}", api_key=api_key, json=payload
        )

    async def delete_agent(self, agent_id: str, api_key: str) -> None:
        """Delete (archive) the agent in TurnCall — call history stays."""
        await self._request(
            "DELETE", f"/v1/agents/{agent_id}", api_key=api_key, extract="none"
        )

    async def delete_project(self, project_id: str, api_key: str) -> None:
        """Soft-delete the agent's dedicated project (ADR-0011) — removes the
        project + its agent/key/KB and unbinds its numbers in one call.
        Self-delete: the key must belong to this project."""
        await self._request(
            "DELETE", f"/v1/projects/{project_id}", api_key=api_key, extract="none"
        )

    async def bind_phone_number(
        self, payload: dict[str, Any], api_key: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST", "/v1/phone-numbers", api_key=api_key, json=payload
        )

    async def update_phone_number(
        self, phone_id: str, payload: dict[str, Any], api_key: str
    ) -> dict[str, Any]:
        """In-place binding update — same phone id, same call-init secret."""
        return await self._request(
            "PUT", f"/v1/phone-numbers/{phone_id}", api_key=api_key, json=payload
        )

    async def unbind_phone_number(self, phone_id: str, api_key: str) -> None:
        await self._request(
            "DELETE", f"/v1/phone-numbers/{phone_id}", api_key=api_key, extract="none"
        )

    # ------------------------------------------------------------ calls

    async def list_calls(
        self, api_key: str, page: int = 1, limit: int = 50
    ) -> dict[str, Any]:
        """Paginated call records for the key's project (== one agent)."""
        return await self._request(
            "GET",
            "/v1/calls",
            api_key=api_key,
            extract="json",
            params={"page": page, "limit": limit},
        )

    async def get_call_transcript(self, call_id: str, api_key: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/v1/calls/{call_id}/transcript", api_key=api_key
        )

    async def get_call_recording(self, call_id: str, api_key: str) -> bytes:
        """The call's WAV audio."""
        return await self._request(
            "GET",
            f"/v1/calls/{call_id}/recording",
            api_key=api_key,
            extract="content",
            timeout=60,
        )

    async def send_chat(
        self,
        agent_id: str,
        message: str,
        api_key: str,
        *,
        session_id: str | None = None,
        customer_number: str = "+15550000000",
    ) -> dict[str, Any]:
        """Send a message to the agent over the text Chat API (for in-console
        testing). Threads session_id to keep the test conversation going."""
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "message": message,
            "channel": "web",
            "customer_number": customer_number,
            "turncall_number": "+15550000001",
        }
        if session_id:
            body["session_id"] = session_id
        return await self._request("POST", "/v1/chat", api_key=api_key, json=body)

    # -------------------------------------------------------- knowledge

    async def list_knowledge_bases(self, api_key: str) -> list[dict[str, Any]]:
        return await self._request("GET", "/v1/knowledge-bases", api_key=api_key)

    async def create_knowledge_base(self, name: str, api_key: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/v1/knowledge-bases", api_key=api_key, json={"name": name}
        )

    async def link_knowledge_base(
        self, agent_id: str, kb_id: str, api_key: str, mode: str = "auto"
    ) -> None:
        """Attach the KB to the agent. Default retrieval mode is auto; the builder
        uses prompt so a small doc set is always in the agent's context."""
        await self._request(
            "POST",
            f"/v1/agents/{agent_id}/knowledge-bases",
            api_key=api_key,
            extract="none",
            json={"knowledge_base_id": kb_id, "mode": mode},
        )

    async def list_documents(self, kb_id: str, api_key: str) -> list[dict[str, Any]]:
        return await self._request(
            "GET", f"/v1/knowledge-bases/{kb_id}/documents", api_key=api_key
        )

    async def upload_document(
        self, kb_id: str, filename: str, content: bytes, content_type: str, api_key: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/v1/knowledge-bases/{kb_id}/documents",
            api_key=api_key,
            timeout=120,  # ingest = extract + embed
            files={"file": (filename, content, content_type)},
        )

    async def delete_document(self, kb_id: str, doc_id: str, api_key: str) -> None:
        await self._request(
            "DELETE",
            f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}",
            api_key=api_key,
            extract="none",
        )

    async def search_knowledge_base(
        self, kb_id: str, query: str, api_key: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/v1/knowledge-bases/{kb_id}/search",
            api_key=api_key,
            json={"query": query},
        )

    # -------------------------------------------------------- takeaways

    async def list_takeaways(self, api_key: str) -> list[dict[str, Any]]:
        return await self._request("GET", "/v1/takeaways", api_key=api_key)

    async def create_takeaway(
        self, payload: dict[str, Any], api_key: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST", "/v1/takeaways", api_key=api_key, json=payload
        )

    async def update_takeaway(
        self, takeaway_id: str, payload: dict[str, Any], api_key: str
    ) -> dict[str, Any]:
        return await self._request(
            "PUT", f"/v1/takeaways/{takeaway_id}", api_key=api_key, json=payload
        )

    async def delete_takeaway(self, takeaway_id: str, api_key: str) -> None:
        await self._request(
            "DELETE", f"/v1/takeaways/{takeaway_id}", api_key=api_key, extract="none"
        )

    async def register_webhook(
        self, url: str, events: list[str], api_key: str
    ) -> dict[str, Any]:
        """Register a webhook in this agent's project — returns the signing secret."""
        return await self._request(
            "POST",
            "/v1/webhooks",
            api_key=api_key,
            json={"url": url, "events": events},
        )
