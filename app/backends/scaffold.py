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
    builder_token: str = "",
) -> dict[str, str]:
    """Return {relative_path: file_contents} for the whole Agent Backend repo.

    `.env` holds the secrets (gitignored). Regeneration callers pop it from the
    result so a bind-time CALL_INIT_SECRET is never clobbered.
    """
    tool_bodies = tool_bodies or {}
    console_origin = os.environ.get("CONSOLE_ORIGIN", "http://localhost:5173")
    # Where this backend asks for a Pipedream token (ADR-0019). Container-to-
    # container, so the host gateway — not the console's origin.
    builder_url = os.environ.get("BUILDER_URL", "http://host.docker.internal:8000")
    return {
        "app.py": render_app_py(tools, tool_bodies),
        # httpx: this backend calls builder-api for a Pipedream access token.
        "requirements.txt": "fastapi>=0.115\nuvicorn[standard]>=0.30\nhttpx>=0.27\n",
        "Dockerfile": _DOCKERFILE,
        "docker-compose.yml": _COMPOSE.format(slug=slug, port=port),
        ".env": _ENV.format(
            agent_id=agent_id,
            webhook_secret=webhook_secret,
            call_init_secret=call_init_secret,
            console_origin=console_origin,
            builder_token=builder_token,
            builder_url=builder_url,
        ),
        ".env.example": _ENV_EXAMPLE,
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
import time
from contextlib import closing

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

DB = "events.db"
AGENT_ID = os.environ.get("AGENT_ID", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")      # signs events + tool calls
CALL_INIT_SECRET = os.environ.get("CALL_INIT_SECRET", "")  # set when a number binds with caller info
CONSOLE_ORIGIN = os.environ.get("CONSOLE_ORIGIN", "http://localhost:5173")
BUILDER_URL = os.environ.get("BUILDER_URL", "")       # where to ask for a Pipedream token
BUILDER_TOKEN = os.environ.get("BUILDER_TOKEN", "")   # this backend's own credential


# Cached until it expires — Pipedream tokens live an hour and a call is not the
# unit of anything here.
_pd_token = {{"access_token": "", "expires_at": 0.0}}


async def pipedream_token() -> dict:
    """A Pipedream access token, plus the tenant fields its headers need.

    This backend deliberately holds no Pipedream credential of its own: one
    revocable copy lives in the builder (ADR-0018/0019), and this exchanges
    BUILDER_TOKEN for a short-lived access token.
    """
    if _pd_token["access_token"] and _pd_token["expires_at"] > time.time() + 30:
        return _pd_token
    if not (BUILDER_URL and BUILDER_TOKEN):
        raise RuntimeError(
            "BUILDER_URL / BUILDER_TOKEN are not set — integrations are off for "
            "this backend. The builder writes both when it generates .env."
        )
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            BUILDER_URL.rstrip("/") + "/integrations/token",
            headers={{"Authorization": "Bearer " + BUILDER_TOKEN}},
        )
    r.raise_for_status()
    _pd_token.update(r.json()["data"])
    return _pd_token


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


# --- Automations (ADR-0018) ---

# An Automation runs because an event arrived, not because a model chose to call
# a tool. That is the whole reason it lives here rather than in the agent's MCP
# servers: "post the summary when the call ends" is a delivery guarantee, and
# routing it through tool selection would turn it into an outcome.

# TODO: your channel, e.g. "#sales-calls". Empty means this Automation does
# nothing — which is exactly what an untouched backend should do.
SLACK_CHANNEL = ""


async def run_pipedream_action(action_id: str, app: str, props: dict) -> dict | None:
    """Run one Pipedream action as this Workspace. None on any failure.

    Holds no credential of its own: the access token comes from the builder
    (ADR-0019), and the account it acts through is named by id, never by secret.
    """
    try:
        pd = await pipedream_token()
        account = next((a for a in pd.get("accounts", []) if a["app"] == app), None)
        if account is None:
            print(f"automation: no connected {{app}} account — skipping {{action_id}}")
            return None
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://api.pipedream.com/v1/connect/{{pd['project_id']}}/actions/run",
                headers={{
                    "Authorization": "Bearer " + pd["access_token"],
                    "X-PD-Environment": pd["environment"],
                }},
                json={{
                    "id": action_id,
                    "external_user_id": pd["external_user_id"],
                    # The account rides under the app slug; the action's own
                    # inputs are its siblings.
                    "configured_props": {{
                        app: {{"authProvisionId": account["id"]}},
                        **props,
                    }},
                }},
            )
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        # ponytail: log and move on. Retry policy and failure surfacing are
        # deliberately out of scope (ADR-0018) — the call has already ended, and
        # a failed post must never become its problem.
        print(f"automation: {{action_id}} failed ({{exc}})")
        return None


async def on_call_ended(payload: dict) -> None:
    """Post the call summary to Slack when a call ends.

    `payload` is the full call.ended body: summary, analysis, duration_ms,
    ended_reason, from_number, to_number, direction, transcript, recording_url.
    Edit freely — this file is yours.
    """
    if not SLACK_CHANNEL:
        return
    seconds = round((payload.get("duration_ms") or 0) / 1000)
    text = (
        f"*Call ended* — {{payload.get('from_number') or 'unknown'}}"
        f" → {{payload.get('to_number') or 'unknown'}}\\n"
        f"{{payload.get('summary') or '(no summary)'}}\\n"
        f"_{{seconds}}s · {{payload.get('ended_reason') or 'unknown'}}_"
    )
    await run_pipedream_action(
        "slack_v2-post-message",
        "slack_v2",
        {{"channel": SLACK_CHANNEL, "text": text}},
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


async def _inline_agent() -> dict | None:
    """This agent\'s config with Pipedream credentials minted for this call.

    None when the agent has no attached apps, or when anything at all goes wrong.
    Deliberately swallowing: an integration that cannot be credentialed costs the
    agent its tools, and must never cost the caller their call (ADR-0020).
    """
    if not (BUILDER_URL and BUILDER_TOKEN):
        return None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(
                BUILDER_URL.rstrip("/") + "/integrations/call-config",
                headers={{"Authorization": "Bearer " + BUILDER_TOKEN}},
            )
        r.raise_for_status()
        return r.json()["data"]["agent"]
    except Exception as exc:  # noqa: BLE001
        print(f"call-init: no inline agent ({{exc}}) — falling back to agent_id")
        return None


@app.post("/call-init")
async def call_init(request: Request) -> dict:
    """Managed call-init (ADR-0008): TurnCall asks which agent answers this call
    and what context to load. Returns this agent + caller info for the prompt.

    An agent with attached apps (Slack, Google Calendar) answers with an *inline*
    config instead of its agent_id: TurnCall never templates MCP headers and a
    Pipedream token lives an hour, so a credential can only reach a call this way
    (ADR-0018/0020). Without attached apps — or if minting fails — it answers with
    agent_id exactly as it always did.
    """
    raw = await _verified_body(request, CALL_INIT_SECRET)
    payload = json.loads(raw).get("payload", {{}})
    caller = (payload.get("customer") or {{}}).get("number") or "unknown"
    # ponytail: stub lookup — replace with your CRM/database lookup for this caller.
    context = f"The caller\'s phone number is {{caller}}. No CRM record loaded (stub)."
    agent = await _inline_agent()
    return {{
        # Exactly one of these: TurnCall ignores an inline agent when agent_id is
        # present, so sending both would silently drop the credentials.
        **({{"agent": agent}} if agent else {{"agent_id": AGENT_ID}}),
        "variables": {{"caller_number": caller}},
        "metadata": {{}},
        "dynamic_data": {{"knowledge_context": context}},
    }}


@app.post("/events")
async def receive_event(request: Request) -> dict:
    """Receive a TurnCall event directly, verify its signature, store it, and
    run any Automation bound to it.

    Stored before the Automation runs. The handler below swallows its own
    failures, so today the order does not change the outcome — it is here so a
    future edit that lets an exception escape cannot cost us the record of the
    event that triggered it.
    """
    raw = await _verified_body(request, WEBHOOK_SECRET)
    with closing(_db()) as conn:
        conn.execute("INSERT INTO events (payload) VALUES (?)", (raw,))
        conn.commit()
    try:
        body = json.loads(raw)
        if body.get("event") == "call.ended":
            await on_call_ended(body.get("payload") or {{}})
    except Exception as exc:  # noqa: BLE001
        print(f"automation dispatch failed ({{exc}})")
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

# Committed alongside the gitignored .env so someone who clones this repo — from
# GitHub or anywhere else — can see what it needs to run. Values deliberately
# empty: this file is in git.
_ENV_EXAMPLE = """# Copy to .env and fill in. .env is gitignored; never commit it.
# The builder writes a real .env when it generates this backend; these are the
# values you need if you are running the repo yourself.

# The TurnCall agent this backend serves.
AGENT_ID=
# Signs tool webhooks and events. Must match the agent's webhook_secret in
# TurnCall, or every request is rejected (verification fails closed).
WEBHOOK_SECRET=
# Set when a phone number binds with managed call-init. Leave empty otherwise.
CALL_INIT_SECRET=
# Origin allowed to read GET /events. The console, or your own dashboard.
CONSOLE_ORIGIN=http://localhost:5173
# Where to ask for a Pipedream access token, and this backend's own credential
# for asking (ADR-0019). This backend holds no Pipedream credential of its own.
# Distinct from WEBHOOK_SECRET on purpose: that one is shared with TurnCall.
BUILDER_URL=http://host.docker.internal:8000
BUILDER_TOKEN=
"""

_ENV = """AGENT_ID={agent_id}
WEBHOOK_SECRET={webhook_secret}
CALL_INIT_SECRET={call_init_secret}
CONSOLE_ORIGIN={console_origin}
BUILDER_URL={builder_url}
BUILDER_TOKEN={builder_token}
"""

_README = """# turncall-agent-{slug}

Generated Agent Backend for a TurnCall agent (see turncall-builder-api,
ADR-0004/0005/0008). Owns this agent's custom tool webhooks, receives its
TurnCall events directly, and serves managed call-init.

- Runs on host port **{port}** (TurnCall reaches it at `host.docker.internal:{port}`,
  the browser at `localhost:{port}`).
- `POST /tools/<name>` — tool webhooks (called by TurnCall, HMAC-verified).
- `POST /events` — TurnCall event sink (HMAC-verified). Runs `on_call_ended()`
  when a call ends: an **Automation**, which fires because the event arrived and
  not because a model chose it. Set `SLACK_CHANNEL` in `app.py` to switch it on.
- `POST /call-init` — managed call-init: picks this agent + loads caller info
  (HMAC-verified with this number's call-init secret).
- `GET  /events` — stored events (the console polls this; CORS pinned to it).

Secrets live in `.env` (gitignored) — do not commit or share it. Verification
fails closed: if a secret is missing the matching endpoint rejects traffic.

Run: `docker compose up -d`. Tool handler bodies and the call-init lookup are
stubs — fill in real logic. The builder auto-commits to git before it
regenerates, so your edits are always recoverable via `git log`.

## Running this yourself

This repo is self-contained — you can take it and run it anywhere Docker runs,
independently of the builder. `.env` is gitignored, so a fresh clone has no
secrets:

```bash
cp .env.example .env     # then fill in the values
docker compose up -d --build
```

`WEBHOOK_SECRET` must match the agent's `webhook_secret` in TurnCall, and
TurnCall must be able to reach this service at the tool `webhook_url`s
configured on the agent. Verification fails closed: a missing secret rejects
traffic rather than accepting it unsigned.
"""
