# TurnCall Builder — Roadmap

What's built, what's next, and what we've deliberately ruled out. If you're
considering a contribution, read this first — an idea already listed here as
out of scope will be declined however good the patch is, and something listed
under a future version is worth an issue before you start.

Versions below are planning milestones, not release tags.


A conversational agent builder for TurnCall, modeled on Vapi Composer. The user
describes an agent in plain language; the app asks follow-up questions until the
design is clear, generates a TurnCall agent config, displays it (editable), and
on demand creates the agent in TurnCall and shows the events that agent's calls
produce.

Two repos:
- `turncall-builder-api` — FastAPI backend (this repo)
- `turncall-builder-web` — React + Vite + TypeScript frontend

## V1 scope — shipped

Generate + **display** an editable config; a "Create in TurnCall" button
provisions the agent on demand; a single endpoint receives that project's
TurnCall events and the UI shows them. No multi-tenancy, no post-finalize
LLM re-loop, no TurnCall agent updates/versioning.

## End-to-end flow

```
user prompt
  → composer loop (Claude, structured output):
      {action: "ask", question}  → show question, user answers, loop
      {action: "finalize", agent_config} → done
  → display config (editable form / raw JSON)
  → [Create in TurnCall] → POST /v1/agents (once, idempotent)
  → calls happen → TurnCall POSTs signed events → /events (verified, stored)
  → UI polls events, filters by agent_id / call_id
```

## Backend (FastAPI + Python 3.12, Postgres)

Endpoints:
- `POST /sessions` — start a composer session (returns session_id)
- `POST /sessions/{id}/messages` — one composer turn; returns `{action, question?, agent_config?}`
- `PUT  /sessions/{id}/config` — save user edits to the finalized config
- `POST /sessions/{id}/create` — create the agent in TurnCall (idempotent)
- `POST /events` — receive TurnCall webhook events (signature-verified)
- `GET  /events?agent_id=…` — list stored events for polling

Tables (Postgres):
- `sessions` — id, message history (jsonb), generated config (jsonb), turncall_agent_id (nullable)
- `events` — id, event_id (unique, dedupe), agent_id, call_id, type, payload (jsonb), received_at

The composer brain: Claude with a forced structured-output tool
`compose(action, question?, agent_config?)`. Each turn sends the message
history + the **trimmed** TurnCall agent schema (see ADR-0003).

### Config surface the composer targets (V1)

`name`, `system_prompt`, `first_message`, `llm` (provider+model, default
openai/gpt-4o-mini), `voice`/TTS (default deepgram), built-in `tools`
(`end_call`, `transfer_call`) when the use case needs them.

Everything else (STT, VAD, smart turn, voicemail, S2S, analysis, custom/MCP
tools, knowledge bases, avatar) takes TurnCall defaults. Editable in raw JSON
if the user insists.

## Frontend (React + Vite + TS)

Two columns: a chat panel (composer Q&A) and a config viewer (editable form +
raw JSON toggle) with a "Create in TurnCall" button. After creation, an events
panel polls `GET /events`. Plain `fetch`, no heavy state lib.

## Integration & infra

- **TurnCall API key** (`tc_…`) in backend `.env`. Single project, no app login.
- **Public URL** via ngrok / Cloudflare tunnel in dev (`PUBLIC_BASE_URL`).
- **Webhook registration**: once at startup, idempotently register
  `<PUBLIC_BASE_URL>/events` via TurnCall `POST /v1/webhooks`.
- **Signature verification**: verify `X-TurnCall-Signature` (HMAC-SHA256)
  before storing any event — a trust boundary.

## Explicitly out of scope

Accounts / multi-tenant / per-user keys · post-finalize LLM re-loop &
approval-gated updates · TurnCall agent versioning from the composer ·
live event streaming (SSE/ws) · composer control of analysis/KB/custom tools ·
deletion of TurnCall resources.

## V2 — Agent Backends (supersedes parts of V1)

Each created agent gets its own generated, dockerized backend service. On
Create, the builder:
1. Generates `turncall-agent-<slug>/` from a fixed **scaffold** (FastAPI, `/events`
   receiver + store, `Dockerfile`, `docker-compose.yml`), with **LLM-authored
   per-tool handler bodies** dropped in.
2. Allocates a sequential host port, records `agent_id → {port, service_dir,
   status}` in an `agent_backends` registry table.
3. Sets the agent's tool `webhook_url`s to `http://host.docker.internal:<port>/tools/<name>`.
4. Runs `docker compose up -d` for it (builder mounts the Docker socket + a
   host dir).

Events: the builder holds the single TurnCall webhook subscription, verifies
each event, and **routes** it to the owning Agent Backend by `agent_id`; the
backend stores it. The builder proxies `GET /agents/{id}/events` for the UI and
keeps no events store. See ADR-0004, ADR-0005.

Defaulted (object if wrong): each Agent Backend uses **SQLite** for its event
store (self-contained repo, no shared-DB dependency); each generated repo is
`git init`'d; the builder→agent and TurnCall→agent hops both use
`host.docker.internal:<port>`.

## V3 — Management console (Agents + Phone Numbers)

The builder-web becomes a Vapi-style console (ADR-0006): a sidebar with **Agents**
and **Phone Numbers** sections; the Composer is the "new agent" path.

New backend endpoints (`turncall-builder-api`), all using each agent's stored
per-agent key:
- `GET  /agents` — list from the `agent_backends` registry
- `GET  /agents/{id}` — detail (live config from TurnCall via the agent's key)
- `PUT  /agents/{id}` — overwrite config; **regenerate the Agent Backend iff
  `custom_tools` changed** (same port, `docker compose up --build`)
- `GET  /phone-numbers` — list from the new `phone_numbers` mirror table
- `POST /phone-numbers` — bind: SID + E.164 + `routing_type` + sms
- `PUT  /phone-numbers/{id}` — re-point / change routing / toggle SMS (transparent
  cross-project rebind)
- `DELETE /phone-numbers/{id}` — unbind

**Routing type** (the Add-Number form's key choice, mirrors TurnCall's
`routing_target_type`):
- `agent` — route straight to a chosen Agent. Bound in that agent's project,
  `routing_target_id = agent`.
- `webhook` (call-init) — dynamic per-call agent resolution. The form collects a
  `server_url`; TurnCall POSTs a call-init event there on each inbound call. These
  numbers are bound in a **shared builder-owned "webhook" project** (created once),
  and call-init must resolve via **inline `agent` config** (not `agent_id`) —
  because per-agent-project agents can't be referenced cross-project (see ADR-0006).

New table `phone_numbers` (mirror): `e164`, `sid`, `routing_type`, `agent_id`
(null for webhook), `project_id`, `server_url` (null for agent), `sms_enabled`,
`created_at`.

Frontend: `react-router-dom`; routes `/agents`, `/agents/:id`, `/agents/new`
(composer), `/phone-numbers`, `/phone-numbers/new`. The Add-Number form shows a
**routing-type toggle** — "Route to agent" (agent picker) vs "Call-init webhook"
(`server_url` field). Agent edit reuses the composer's editable-config panel.

Defaulted (object if wrong): "add agent" = the Composer only (no separate manual
form); agent detail fetched live from TurnCall; no agent delete in V1 (numbers
have unbind); binding auto-configures the Twilio webhook (no Twilio SDK).

## V4 — Call-init support (customer writes the endpoint)

Support "per phone number → select agent + inject properties" via TurnCall
call-init (ADR-0007). The customer writes the endpoint (option A); the builder
supports it. Build surface:

- **TurnCall change:** return `server_url_secret` in the bind response
  (`PhoneNumberResponse` currently omits it).
- **Builder:** capture the secret on bind → store in `phone_numbers` → expose on
  `GET /phone-numbers/{id}` and show it on the Number's console page (per-number:
  one endpoint, one secret).
- **Docs:** [call-init.md](./docs/call-init.md) — request/response shape + verify snippet.

Contract: request has `phoneNumber.number` (dialed) + `customer.number` (caller);
response returns `agent_id` (any agent — call-init isn't project-scoped),
`variables`, `metadata`, `dynamic_data.knowledge_context`. Signed like webhooks.

## Ideas, not scheduled

- **Dry-run a Config before Create** — talk to a finalized Config as text before
  provisioning anything, so the `system_prompt` can be judged without phoning
  the agent. Design and open questions in
  [design-config-dry-run.md](./docs/design-config-dry-run.md). Cheaper than it
  looks: TurnCall's `complete_text` already takes a config rather than an agent.
  A static check on the finalized `system_prompt` (numbered lists, markdown)
  is the cheap subset and worth doing on its own.
- **Design-tree panel** — showing what the Builder has settled vs what is still
  open. Considered and dropped; see the same doc for why.

## Decisions

- ADR-0001 — LLM-driven composer (structured-output loop)
- ADR-0002 — Single-tenant V1, key in env
- ADR-0003 — Trimmed config surface
- ADR-0004 — Per-agent generated Agent Backend
- ADR-0005 — Agent Backends own their events; builder is control-plane only
- ADR-0006 — Management console over per-agent projects
- ADR-0007 — Call-init support (customer writes the endpoint)
- ADR-0008 — Managed call-init
- ADR-0009 — Implicit agent knowledge
- ADR-0010 — BYO tool server
- ADR-0011 — Multi-tenant identity in builder
- ADR-0012 — Interview rounds + composed disciplines
