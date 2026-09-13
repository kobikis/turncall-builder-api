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

from ..pipedream import CATALOG, automations

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


def _automations_literal(integrations: Any) -> str:
    """The `AUTOMATIONS` assignment, as source.

    Rendered with `repr` on data that has already passed `_validate_agent_config`.
    Keys are written in a fixed order so an unrelated edit does not rewrite the
    file and cost a rebuild.
    """
    entries = []
    for i in automations(integrations):
        app = CATALOG.get(i.app)
        action = next((a for a in app.actions if a.id == i.call_ended.action), None) if app else None
        entries.append(
            {
                "app": i.app,
                # Resolved here rather than stored: the prop name is the
                # component's, identical for every Workspace, and nothing a
                # config should be able to get wrong.
                "app_prop": action.app_prop if action else i.app,
                "action": i.call_ended.action,
                "args": dict(i.call_ended.args),
                "summary_arg": i.call_ended.summary_arg,
            }
        )
    if not entries:
        return "AUTOMATIONS: list[dict] = []"
    body = "".join(f"    {e!r},\n" for e in entries)
    return f"AUTOMATIONS: list[dict] = [\n{body}]"


def render_app_py(
    tools: list[dict[str, Any]],
    tool_bodies: dict[str, str],
    integrations: Any = None,
) -> str:
    validate_tool_names(tools)
    fns = "\n\n".join(_tool_function(t, tool_bodies.get(t["name"])) for t in tools)
    handlers = ", ".join(f'{t["name"]!r}: tool_{t["name"]}' for t in tools)
    return _APP_PY.format(
        tool_functions=fns or "# (no custom tools)",
        handlers=handlers,
        automations=_automations_literal(integrations),
    )


def render(
    *,
    slug: str,
    port: int,
    tools: list[dict[str, Any]],
    tool_bodies: dict[str, str] | None = None,
    integrations: Any = None,
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
    # One deployment, one clock. Set AGENT_TIMEZONE where the builder runs and
    # every agent it generates reasons in that zone.
    timezone = os.environ.get("AGENT_TIMEZONE", "UTC")
    return {
        "app.py": render_app_py(tools, tool_bodies, integrations),
        # httpx: this backend calls builder-api for a Pipedream access token.
        "requirements.txt": "fastapi>=0.115\nuvicorn[standard]>=0.30\nhttpx>=0.27\ntzdata>=2024.1\n",
        "Dockerfile": _DOCKERFILE,
        "docker-compose.yml": _COMPOSE.format(slug=slug, port=port),
        ".env": _ENV.format(
            agent_id=agent_id,
            webhook_secret=webhook_secret,
            call_init_secret=call_init_secret,
            console_origin=console_origin,
            builder_token=builder_token,
            builder_url=builder_url,
            timezone=timezone,
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
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

DB = "events.db"
AGENT_ID = os.environ.get("AGENT_ID", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")      # signs events + tool calls
CALL_INIT_SECRET = os.environ.get("CALL_INIT_SECRET", "")  # set when a number binds with caller info
CONSOLE_ORIGIN = os.environ.get("CONSOLE_ORIGIN", "http://localhost:5173")
TIMEZONE = os.environ.get("TIMEZONE", "UTC")   # the restaurant's clock, not the server's
BUILDER_URL = os.environ.get("BUILDER_URL", "")       # where to ask for a Pipedream token
BUILDER_TOKEN = os.environ.get("BUILDER_TOKEN", "")   # this backend's own credential


async def pipedream_token() -> dict:
    """A Pipedream access token, the tenant fields its headers need, and the
    accounts this Workspace has connected.

    This backend deliberately holds no Pipedream credential of its own: one
    revocable copy lives in the builder (ADR-0018/0019), and this exchanges
    BUILDER_TOKEN for a short-lived access token.

    Not cached. The token would keep for an hour, but the *accounts* come back in
    the same response and they do not: reconnecting an app mints a new account id
    and destroys the old one. Caching them turned a reconnect into an hour of
    `not_authed` — an error that reads like a Slack permission problem and is
    really a stale id. The builder caches the token itself, so this costs one
    local request per Automation.
    """
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
    return r.json()["data"]


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


# --- Automations (ADR-0018, ADR-0021) ---

# An Automation runs because an event arrived, not because a model chose to call
# a tool. That is the whole reason it lives here rather than in the agent's MCP
# servers: "post the summary when the call ends" is a delivery guarantee, and
# routing it through tool selection would turn it into an outcome.

# Written from this agent's configuration — an app, one Action, and the inputs it
# runs with. An agent with none posts nothing and fails nothing. Editing this list
# works and survives until the next regeneration; the Console is what persists.
{automations}


def _action_error(result: dict) -> str | None:
    """What the third-party API refused, if it did.

    Pipedream answers 200 whether or not the Action worked — a run that Slack
    rejected comes back as a successful HTTP call whose observations carry the
    error. Reading only the status code is why a summary that never arrived
    looked exactly like one that did.
    """
    for observation in result.get("os") or []:
        if observation.get("k") != "error":
            continue
        err = observation.get("err") or {{}}
        data = err.get("data") or {{}}
        return str(data.get("error") or err.get("message") or "the action failed")
    return None


async def run_pipedream_action(
    action_id: str, app: str, app_prop: str, props: dict
) -> dict | None:
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
                        # Keyed by the component's prop name, not the app slug —
                        # `slack_v2-post-message` declares `slack`, and the wrong
                        # key means the action runs with no credential at all.
                        app_prop: {{"authProvisionId": account["id"]}},
                        **props,
                    }},
                }},
            )
        r.raise_for_status()
        result = r.json()
        refused = _action_error(result)
        if refused:
            print(f"automation: {{app}} refused {{action_id}} ({{refused}})")
            return None
        return result
    except Exception as exc:  # noqa: BLE001
        # ponytail: log and move on. Retry policy and failure surfacing are
        # deliberately out of scope (ADR-0018) — the call has already ended, and
        # a failed post must never become its problem.
        print(f"automation: {{action_id}} failed ({{exc}})")
        return None


def call_summary(payload: dict) -> str:
    """The text an Automation sends. Derived from the event, never configured —
    what varies between Workspaces is where it goes, not what it says."""
    seconds = round((payload.get("duration_ms") or 0) / 1000)
    return (
        f"*Call ended* — {{payload.get('from_number') or 'unknown'}}"
        f" → {{payload.get('to_number') or 'unknown'}}\\n"
        f"{{payload.get('summary') or '(no summary)'}}\\n"
        f"_{{seconds}}s · {{payload.get('ended_reason') or 'unknown'}}_"
    )


async def on_call_ended(payload: dict) -> None:
    """Run this agent's end-of-call Automations, in order.

    `payload` is the full call.ended body: summary, analysis, duration_ms,
    ended_reason, from_number, to_number, direction, transcript, recording_url.
    Edit freely — this file is yours.
    """
    if not AUTOMATIONS:
        return
    summary = call_summary(payload)
    for entry in AUTOMATIONS:
        # One failure must not cost the others theirs: run_pipedream_action
        # swallows its own, so the loop always runs to the end.
        await run_pipedream_action(
            entry["action"],
            entry["app"],
            entry["app_prop"],
            {{**entry["args"], entry["summary_arg"]: summary}},
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


def _now_line() -> str:
    """What day it is, for a model that cannot know.

    An LLM has no clock. Asked for "a table for two today at 7pm" it will invent
    a date from its training data — one booking landed in October 2023 — and any
    rule about opening days ("Tuesday to Sunday") is unenforceable without this.
    It belongs in call-init rather than the system prompt because it is true per
    call, not per agent.
    """
    try:
        now = datetime.now(ZoneInfo(TIMEZONE))
        zone = TIMEZONE
    except Exception:  # noqa: BLE001 — a bad or missing zone must not fail a call
        now = datetime.now(UTC)
        zone = "UTC"
    return (
        f"Right now it is {{now:%A}}, {{now:%d %B %Y}}, {{now:%H:%M}} ({{zone}}). "
        f"Today's date is {{now:%Y-%m-%d}}. Work out any date the caller implies "
        f'("today", "tomorrow", "Friday") from this, and never guess one.'
    )


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
    context = (
        _now_line()
        + f" The caller\'s phone number is {{caller}}. No CRM record loaded (stub)."
    )
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
# The clock the agent reasons in — call-init tells it today's date in this zone.
# An IANA name, e.g. Asia/Jerusalem. Wrong or missing falls back to UTC.
TIMEZONE=UTC
# Where to ask for a Pipedream access token, and this backend's own credential
# for asking (ADR-0019). This backend holds no Pipedream credential of its own.
# Distinct from WEBHOOK_SECRET on purpose: that one is shared with TurnCall.
BUILDER_URL=http://host.docker.internal:8000
BUILDER_TOKEN=
"""

_ENV = """AGENT_ID={agent_id}
WEBHOOK_SECRET={webhook_secret}
CALL_INIT_SECRET={call_init_secret}
TIMEZONE={timezone}
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
  not because a model chose it. The `AUTOMATIONS` list in `app.py` is written from
  this agent's configuration — change it in the Console, not here, or the next
  regeneration overwrites you.
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
