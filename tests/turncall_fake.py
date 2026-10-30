"""A TurnCall that answers on an httpx.MockTransport (#131-#134).

Enough of the engine for the Capability routes: the platform bootstrap,
skills (versions, publish, rollback, delete, the agents listing), the skill
library link and agent saves. Every request is kept in `requests`, so a test
asserts what reached TurnCall and with which key. A test can queue a refusal
for a path with `refuse(method, path, status, body)`.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from app import runtime
from app.turncall_client import AUDIT_ID_HEADER, TurnCallClient

PLATFORM_KEY = "platform-secret"


class FakeTurnCall:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.skills: dict[str, dict[str, Any]] = {}
        self.links: dict[str, str | None] = {}  # project -> library
        self.agents_listing: dict[str, list[dict[str, Any]]] = {}  # skill name -> rows
        self.agents: dict[str, dict[str, Any]] = {}  # saved agent bodies by id
        self.refusals: dict[tuple[str, str], list[httpx.Response]] = {}
        self._projects = 0

    # --- what a test sets up and reads -------------------------------------

    def refuse(self, method: str, path: str, status: int, body: dict[str, Any]) -> None:
        self.refusals.setdefault((method, path), []).append(httpx.Response(status, json=body))

    def sent(self, method: str, prefix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.startswith(prefix)]

    def client(self) -> TurnCallClient:
        c = TurnCallClient("http://tc.test", platform_key=PLATFORM_KEY)
        c._client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        return c

    def install(self) -> TurnCallClient:
        """Make this fake the TurnCall every route talks to."""
        client = self.client()
        runtime.set_runtime(object(), client)
        return client

    # --- the engine ---------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queued = self.refusals.get((request.method, request.url.path))
        if queued:
            return queued.pop(0)
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        parts = path.strip("/").split("/")
        if method == "POST" and path == "/v1/projects":
            self._projects += 1
            return _ok({"id": f"proj-{self._projects}"}, audited=True)
        if method == "POST" and path == "/v1/api-keys":
            return _ok({"raw_key": f"tc_{request.url.params['project_id']}"}, audited=True)
        if method == "PUT" and parts[:2] == ["v1", "projects"] and parts[-1] == "skill-library":
            if request.headers.get("X-Platform-Key") != PLATFORM_KEY:
                return httpx.Response(401, json={"success": False, "error": "platform key"})
            self.links[parts[2]] = body["library_project_id"]
            return _ok({"id": parts[2], "skill_library_id": body["library_project_id"]}, audited=True)
        if parts[:2] == ["v1", "skills"]:
            return self._skills(method, parts[2:], body, request)
        if parts[:2] == ["v1", "agents"]:
            agent_id = parts[2] if len(parts) > 2 else str(uuid.uuid4())
            self.agents[agent_id] = body
            return _ok({"id": agent_id}, audited=True)
        return httpx.Response(404, json={"success": False, "error": f"no route {method} {path}"})

    def _skills(
        self, method: str, rest: list[str], body: Any, request: httpx.Request
    ) -> httpx.Response:
        if method == "GET" and not rest:
            params = request.url.params
            rows = [
                s for s in self.skills.values()
                if (not params.get("name") or s["name"] == params["name"])
                and (not params.get("state") or s["state"] == params["state"])
            ]
            rows.sort(key=lambda s: s["version"], reverse=True)
            page, limit = int(params.get("page", 1)), int(params.get("limit", 50))
            return httpx.Response(200, json={
                "success": True, "data": rows[(page - 1) * limit: page * limit],
                "total": len(rows), "page": page, "limit": limit,
            })
        if method == "POST" and not rest:
            version = 1 + max(
                (s["version"] for s in self.skills.values() if s["name"] == body["name"]),
                default=0,
            )
            row = {
                "id": str(uuid.uuid4()), "project_id": "lib", "name": body["name"],
                "version": version, "state": "draft", "description": body["description"],
                "instructions": body["instructions"], "tools": body.get("tools") or [],
                "knowledge_bases": [], "warnings": [],
            }
            self.skills[row["id"]] = row
            return _ok(row, audited=True, status=201)
        if method == "GET" and len(rest) == 2 and rest[1] == "agents":
            return _ok(self.agents_listing.get(rest[0], []))
        row = self.skills.get(rest[0])
        if row is None:
            return httpx.Response(404, json={"success": False, "error": "Skill not found", "code": "not_found"})
        if method == "GET":
            return _ok(row)
        if method == "PUT":
            row.update(body)
            return _ok(row, audited=True)
        if method == "POST" and rest[1:] == ["publish"]:
            for other in self.skills.values():
                if other["name"] == row["name"] and other["state"] == "published":
                    other["state"] = "archived"
            row["state"] = "published"
            return _ok(row, audited=True)
        if method == "POST" and rest[1:] == ["rollback"]:
            for other in self.skills.values():
                if other["name"] == row["name"] and other["state"] == "published":
                    other["state"] = "archived"
            row["state"] = "published"
            return _ok(row, audited=True)
        if method == "DELETE":
            del self.skills[rest[0]]
            return _ok({"deleted": True}, audited=True)
        return httpx.Response(405)


def _ok(data: Any, *, audited: bool = False, status: int = 200) -> httpx.Response:
    headers = {AUDIT_ID_HEADER: str(uuid.uuid4())} if audited else {}
    return httpx.Response(status, json={"success": True, "data": data}, headers=headers)


class FakeLibraryStore:
    """capability_store as its SQL behaves, in memory, on the conftest mock:
    one library per Workspace (`create` runs only for the first) and one link
    record per project."""

    def __init__(self, mocks: Any) -> None:
        self.library: dict[str, Any] = {}
        self.links: dict[str, dict[str, Any]] = {}
        store = mocks.capability_store
        store.ensure.side_effect = self.ensure
        store.get.side_effect = self.get
        store.library_project.side_effect = self.library_project
        store.record_link.side_effect = self.record_link
        store.list_links.side_effect = self.list_links

    async def ensure(self, _pool: Any, _workspace_id: str, create: Any) -> tuple[dict, bool]:
        if self.library:
            return dict(self.library), False
        project_id, api_key = await create()
        self.library.update(project_id=project_id, api_key=api_key, created_at=None)
        return dict(self.library), True

    async def get(self, _pool: Any, _workspace_id: str) -> dict[str, Any] | None:
        return dict(self.library) if self.library else None

    async def library_project(self, _pool: Any, _workspace_id: str) -> str | None:
        return self.library.get("project_id")

    async def record_link(
        self, _pool: Any, _workspace_id: str, project_id: str, *,
        library_project_id: str, state: str, error: str | None = None,
    ) -> None:
        self.links[project_id] = {
            "library_project_id": library_project_id, "state": state, "error": error,
            "updated_at": None,
        }

    async def list_links(self, _pool: Any, _workspace_id: str) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self.links.items()}
