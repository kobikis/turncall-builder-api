"""Render the files for a generated Agent Backend (ADR-0004/0005/0008).

The scaffold (FastAPI bootstrap, event receiver, tool dispatch, managed
call-init endpoint, Dockerfile, compose) is fixed and hand-written here. Only
the per-tool handler *bodies* are authored elsewhere (toolgen.py). The builder
is not in the event path — this backend receives events, tool calls, and
call-init requests straight from TurnCall and verifies every one with its
secrets (fail closed). Secrets live in a gitignored `.env`, never in the repo.
"""

from __future__ import annotations

import os
import re
from typing import Any

STUB_BODY = (
    "    # ponytail: stub — echoes args. Replace with real logic (or call your system).\n"
    '    return {"status": "ok", "tool": name, "arguments": args, "note": "stub"}'
)

# Tool names are interpolated into generated Python source — anything outside
# this shape is rejected at the boundary (mapper) and again here.
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def validate_tool_names(tools: list[dict[str, Any]]) -> None:
    """Reject tool names that can't safely become `async def tool_<name>`."""
    for t in tools:
        name = t.get("name", "")
        if not TOOL_NAME_RE.match(name):
            raise ValueError(
                f"invalid tool name {name!r}: must match {TOOL_NAME_RE.pattern}"
            )


def slugify(name: str) -> str:
    """agent name/id -> filesystem+docker safe slug."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "agent"


def _tool_function(tool: dict[str, Any], body: str | None) -> str:
    name = tool["name"]
    # The stub references `name` directly — it's a parameter of the generated
    # function (default below), so no fragile substring substitution is needed.
    body = body or STUB_BODY
    return f'async def tool_{name}(args: dict, name: str = {name!r}) -> dict:\n{body}\n'


def render_app_py(tools: list[dict[str, Any]], tool_bodies: dict[str, str]) -> str:
    validate_tool_names(tools)
    fns = "\n\n".join(_tool_function(t, tool_bodies.get(t["name"])) for t in tools)
    handlers = ", ".join(f'{t["name"]!r}: tool_{t["name"]}' for t in tools)
    return _APP_PY.format(tool_functions=fns or "# (no custom tools)", handlers=handlers)


def render(
    *,
    slug: str,
    port: int,
    tools: list[dict[str, Any]],
    tool_bodies: dict[str, str] | None = None,
    agent_id: str = "",
    webhook_secret: str = "",
    call_init_secret: str = "",
) -> dict[str, str]:
    """Return {relative_path: file_contents} for the whole Agent Backend repo.

    `.env` holds the secrets (gitignored). Regeneration callers pop it from the
    result so a bind-time CALL_INIT_SECRET is never clobbered.
    """
    tool_bodies = tool_bodies or {}
    console_origin = os.environ.get("CONSOLE_ORIGIN", "http://localhost:5173")
    return {
        "app.py": render_app_py(tools, tool_bodies),
        "requirements.txt": "fastapi>=0.115\nuvicorn[standard]>=0.30\n",
        "Dockerfile": _DOCKERFILE,
        "docker-compose.yml": _COMPOSE.format(slug=slug, port=port),
        ".env": _ENV.format(
            agent_id=agent_id,
            webhook_secret=webhook_secret,
            call_init_secret=call_init_secret,
            console_origin=console_origin,
        ),
        ".gitignore": "__pycache__/\n*.pyc\nevents.db\n.env\n",
        "README.md": _README.format(slug=slug, port=port),
    }


_APP_PY = '''"""Generated Agent Backend — owns this agent\'s tools + events (ADR-0004/0005/0008).

Receives TurnCall webhook events, tool calls, and call-init requests DIRECTLY
(the builder is not in the path), verifies each request\'s HMAC signature with
this agent\'s secrets, and stores everything in SQLite. Verification fails
closed: a missing secret rejects traffic rather than accepting it unsigned.
"""

import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import closing

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

DB = "events.db"
AGENT_ID = os.environ.get("AGENT_ID", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")      # signs events + tool calls
CALL_INIT_SECRET = os.environ.get("CALL_INIT_SECRET", "")  # set when a number binds with caller info
CONSOLE_ORIGIN = os.environ.get("CONSOLE_ORIGIN", "http://localhost:5173")


def _db():
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, payload TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS tool_calls (id INTEGER PRIMARY KEY, tool TEXT, input TEXT, output TEXT)")
    return conn


def _verify(raw: str, sig: str, ts: str, secret: str) -> bool:
    # Fail closed: no secret configured -> reject, never accept unsigned traffic.
    if not (secret and sig and ts):
        return False
    expected = "v1=" + hmac.new(secret.encode(), f"{{ts}}.{{raw}}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


async def _verified_body(request: Request, secret: str) -> str:
    raw = (await request.body()).decode()
    ok = _verify(
        raw,
        request.headers.get("X-TurnCall-Signature", ""),
        request.headers.get("X-TurnCall-Timestamp", ""),
        secret,
    )
    if not ok:
        raise HTTPException(status_code=401, detail="invalid signature")
    return raw


app = FastAPI(title="Agent Backend")
# Only the console reads this backend from a browser. Any LOCAL origin is fine
# (Vite bumps to 5174+ when 5173 is busy; localhost vs 127.0.0.1 differ) — the
# point of pinning is to block malicious *websites*, which are never local.
# CONSOLE_ORIGIN covers a console served from a non-local origin.
_ORIGINS = sorted({{CONSOLE_ORIGIN}})
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ORIGINS,
    allow_origin_regex=r"http://(localhost|127\\.0\\.0\\.1)(:\\d+)?",
    allow_methods=["GET"],
    allow_headers=["*"],
)


# --- tool handlers (bodies authored by the builder) ---

{tool_functions}

HANDLERS = {{{handlers}}}


@app.post("/tools/{{name}}")
async def call_tool(name: str, request: Request) -> dict:
    raw = await _verified_body(request, WEBHOOK_SECRET)
    body = json.loads(raw)
    args = body.get("arguments", {{}})
    handler = HANDLERS.get(name)
    result = await handler(args) if handler else {{"error": f"unknown tool {{name}}"}}
    with closing(_db()) as conn:
        conn.execute(
            "INSERT INTO tool_calls (tool, input, output) VALUES (?, ?, ?)",
            (name, json.dumps(args), json.dumps(result)),
        )
        conn.commit()
    return result


@app.post("/call-init")
async def call_init(request: Request) -> dict:
    """Managed call-init (ADR-0008): TurnCall asks which agent answers this call
    and what context to load. Returns this agent + caller info for the prompt."""
    raw = await _verified_body(request, CALL_INIT_SECRET)
    payload = json.loads(raw).get("payload", {{}})
    caller = (payload.get("customer") or {{}}).get("number") or "unknown"
    # ponytail: stub lookup — replace with your CRM/database lookup for this caller.
    context = f"The caller\'s phone number is {{caller}}. No CRM record loaded (stub)."
    return {{
        "agent_id": AGENT_ID,
        "variables": {{"caller_number": caller}},
        "metadata": {{}},
        "dynamic_data": {{"knowledge_context": context}},
    }}


@app.post("/events")
async def receive_event(request: Request) -> dict:
    """Receive a TurnCall event directly, verify its signature, store it."""
    raw = await _verified_body(request, WEBHOOK_SECRET)
    with closing(_db()) as conn:
        conn.execute("INSERT INTO events (payload) VALUES (?)", (raw,))
        conn.commit()
    return {{"success": True}}


@app.get("/events")
def list_events() -> dict:
    with closing(_db()) as conn:
        rows = conn.execute("SELECT payload FROM events ORDER BY id").fetchall()
    return {{"success": True, "data": {{"events": [json.loads(r[0]) for r in rows]}}}}


@app.get("/health")
def health() -> dict:
    return {{"status": "ok"}}
'''

_DOCKERFILE = """FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
"""

_COMPOSE = """services:
  {slug}:
    build: .
    ports:
      - "{port}:8000"
    env_file: .env
    restart: unless-stopped
"""

_ENV = """AGENT_ID={agent_id}
WEBHOOK_SECRET={webhook_secret}
CALL_INIT_SECRET={call_init_secret}
CONSOLE_ORIGIN={console_origin}
"""

_README = """# turncall-agent-{slug}

Generated Agent Backend for a TurnCall agent (see turncall-builder-api,
ADR-0004/0005/0008). Owns this agent's custom tool webhooks, receives its
TurnCall events directly, and serves managed call-init.

- Runs on host port **{port}** (TurnCall reaches it at `host.docker.internal:{port}`,
  the browser at `localhost:{port}`).
- `POST /tools/<name>` — tool webhooks (called by TurnCall, HMAC-verified).
- `POST /events` — TurnCall event sink (HMAC-verified).
- `POST /call-init` — managed call-init: picks this agent + loads caller info
  (HMAC-verified with this number's call-init secret).
- `GET  /events` — stored events (the console polls this; CORS pinned to it).

Secrets live in `.env` (gitignored) — do not commit or share it. Verification
fails closed: if a secret is missing the matching endpoint rejects traffic.

Run: `docker compose up -d`. Tool handler bodies and the call-init lookup are
stubs — fill in real logic. The builder auto-commits to git before it
regenerates, so your edits are always recoverable via `git log`.
"""
