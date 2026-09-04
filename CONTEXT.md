# TurnCall Builder

A conversational agent builder for TurnCall: turns a plain-language prompt into
a TurnCall agent configuration through a follow-up-question loop, then displays
and (on demand) provisions it.

## Identity & tenancy

**Workspace**:
The tenant — a company. Owns everything the builder tracks (Agents, their Agent
Backends, Number bindings, Sessions) and the [[User]]s who can access it. A
builder-api concept only: TurnCall stays identity-free and never sees a Workspace.
One company = one Workspace; the model is **flat** (no Workspace-above-Workspace
layer). Each Agent under a Workspace still maps to its own TurnCall [[Control
plane|project]] for webhook isolation — that project is plumbing, not the tenant.
_Avoid_: "Organization", "tenant" as a separate object, "project" (that's TurnCall's
per-agent unit).

**User**:
A human who logs in (email+password or Google). Belongs to one or more Workspaces
via a [[Membership]]. Distinct from a TurnCall API key, which the builder holds
server-side to call TurnCall on the user's behalf — the browser authenticates as a
User, never with a TurnCall key.
_Avoid_: account, member (member = the User-in-a-Workspace, i.e. the Membership)

**Membership**:
Puts a [[User]] in a [[Workspace]] with a [[Role]]. The unit RBAC checks: every
builder endpoint resolves the caller's Membership for the target Workspace before
acting.
_Avoid_: seat, assignment

**Login session** (the `user_sessions` table):
Proof a [[User]] is authenticated — an opaque random token in Postgres
(`token, user_id, expires_at`), delivered as an httpOnly/Secure/SameSite cookie and
looked up per request. Server-side so it is instantly revocable (logout, kick,
re-role). **Not** the builder [[Session]] (that's a conversation) — different table,
different meaning; never call this one just "session".
_Avoid_: token, JWT (deliberately not used), "session" unqualified

**Role**:
The permission level on a [[Membership]], one of three:
- `admin` — everything, plus manage members/billing and delete the Workspace.
- `editor` — create/edit/delete Agents, Numbers, KB; run test calls; view calls.
  No member/billing management.
- `viewer` — read-only: view Agents and calls/analytics. **No test calls**, no edits.
Enforced in builder-api; unrelated to TurnCall's own `ProjectRole` on API keys.
_Avoid_: permission, grant, "developer" (TurnCall's machine-key term, not used here)

## Language

**Builder**:
The LLM-driven loop that interviews the user and emits either a follow-up
question or a finalized agent config. The product itself.
_Avoid_: composer (the pre-rename term — historical ADRs still use it), wizard,
generator

**Session**:
One builder conversation — its message history plus the config it produced and,
once provisioned, the TurnCall agent it maps to.
_Avoid_: thread, chat, conversation

**Builder model**:
The provider + model pair driving a [[Session]]'s builder turns (OpenAI or
Anthropic). Chosen at Session creation from providers whose keys builder-api
holds; immutable for the Session's life; absent a choice, the deployment
default applies. Distinct from the [[Config]]'s llm — that is the *created
agent's* runtime model, not the Builder's.
_Avoid_: composer model, LLM (unqualified), engine, "model" alone where ambiguous

**Config** (agent config):
The TurnCall agent configuration the Builder produces. Lives in a Session,
editable by the user before creation.
_Avoid_: spec, settings, definition

**Finalize**:
The Builder turn where it stops asking questions and emits a complete Config.
The boundary between the Q&A loop and the display/create phase.
_Avoid_: complete, submit, done

**Create**:
Provisioning the Config as a real agent in TurnCall via the TurnCall API, and
generating + starting that agent's Agent Backend. Distinct from Finalize —
Finalize produces the Config; Create pushes it to TurnCall and stands up its
backend.
_Avoid_: deploy, publish, provision

**Event**:
A signed webhook envelope TurnCall POSTs when a call changes state (`call.ended`,
etc.). TurnCall POSTs it **directly** to the owning agent's Agent Backend, which
verifies the signature (its own secret) and stores it. The builder is never in
the event path.
_Avoid_: webhook, callback, notification

**Config surface**:
The subset of TurnCall agent fields the Builder is allowed to set (name,
system_prompt, first_message, llm, voice, built-in tools). Everything outside
it takes TurnCall defaults.
_Avoid_: schema (ambiguous with the full TurnCall schema)

**Agent Backend**:
The generated, dockerized, per-agent backend service — one per created agent.
It owns that agent's backend logic: custom tool webhooks, its `/events`
receiver + event store, and any custom endpoints. Lives in its own generated
repo, run as its own docker compose.
_Avoid_: tool service, sidecar, microservice

**Scaffold**:
The fixed hand-written template for an Agent Backend (FastAPI bootstrap,
`/events` receiver, `Dockerfile`, `docker-compose.yml`, port wiring). Only the
per-tool handler bodies are LLM-authored and dropped into it.
_Avoid_: template, boilerplate

**Backend registry**:
The builder's record of every Agent Backend — the `agent_backends` table mapping
`agent_id → {port, service_dir, status}`. The builder reads it to route events,
set tool `webhook_url`s, and avoid duplicate generation.
_Avoid_: catalog, index

**Control plane**:
The builder's role: it composes configs and, on Create, provisions each agent's
own TurnCall project + key + agent + webhook and generates/runs its Agent
Backend — but it never handles Events or tool calls itself. Those are the Agent
Backend's data plane.
_Avoid_: orchestrator, gateway

**Console**:
The builder-web management UI: a sidebar with an Agents section and a Phone
Numbers section (each list → detail → edit), plus the Builder reached as the
"new agent" path. Modeled on the Vapi dashboard.
_Avoid_: dashboard, admin panel

**Number binding**:
A Twilio phone number bound to exactly one Agent, routing that number's inbound
calls to it. Binding lives in the Agent's TurnCall project and auto-configures
the Twilio webhook (TurnCall does this on bind — the builder never calls Twilio).
Re-pointing to a different Agent moves the binding across projects.
_Avoid_: DID, line, phone

**Number registry**:
The builder's `phone_numbers` table mirroring every Number binding it made
(`e164`, `sid`, `agent_id`, `project_id`, `sms_enabled`). Source of truth for the
Console's Phone Numbers list, so listing is one local query, not an N-project
fan-out.
_Avoid_: phone book, directory

**Call-init**:
Per-call, dynamic agent selection + property injection. A Number bound with
`webhook` routing makes TurnCall POST a call-init request to the customer's
`server_url`, whose response picks the agent (`agent_id`) and supplies template
`variables`/`metadata`/`knowledge_context`. One endpoint + one signing secret per
Number. Comes in two modes: Managed call-init and Custom call-init. See
docs/call-init.md, ADR-0007, ADR-0008.
_Avoid_: dynamic routing, dispatch

**Managed call-init**:
Call-init where the endpoint is the Agent's own Agent Backend (`/call-init`),
generated by the builder. The Number is bound in the Agent's project, so its
Events flow to that backend. The stub response returns the Agent's own id plus
caller info as `knowledge_context` — the user replaces the lookup with real
logic in the backend repo. See ADR-0008.
_Avoid_: auto call-init, builder call-init

**Custom call-init**:
Call-init against a customer-written endpoint (ADR-0007's original mode). The
Number is bound in the shared webhook project; the customer hosts and verifies
the endpoint themselves.
_Avoid_: manual call-init, external webhook

**Degraded**:
An Agent Backend status: running, but a secret it needs (event or call-init) is
missing, so signature verification fails closed and it rejects those requests.
Recorded in the Backend registry and surfaced as a Console badge with the
reason.
_Avoid_: unhealthy, broken

**Teardown**:
Deleting a created agent: its container is stopped and removed, its Numbers must
be unbound first, the TurnCall agent + project are deleted, and the registry row
is marked deleted. The generated repo stays on disk — it is the user's code.
_Avoid_: delete (ambiguous), destroy

**Agent Knowledge**:
The single knowledge base the builder manages for an Agent — auto-created in
the Agent's project on first document upload and auto-linked for retrieval.
The Console's Knowledge tab shows only its documents; the underlying
knowledge-base machinery stays invisible. See ADR-0009.
_Avoid_: KB, knowledge base (in console copy), corpus

**Call record**:
A call as TurnCall recorded it — direction, numbers, status, ended reason,
duration, transcript, analysis. Shown in the Console's Calls tab, read from
the Agent's own project. Distinct from an Event (the raw signed envelope the
Agent Backend stores); the Calls tab shows Call records with the Event feed
alongside.
_Avoid_: call log, call history entry

**Tool server**:
Where a custom tool executes. By default the Agent Backend; a Config may name
the user's own server instead — per agent (a base URL all tools route to) or
per tool (a full URL for that tool alone). Externally-routed tools get no
generated handler; TurnCall signs the calls either way. See ADR-0010.
_Avoid_: webhook host, tool endpoint (ambiguous with the URL itself)

**Server URL secret**:
The per-Number secret TurnCall generates on bind and uses to HMAC-sign call-init
requests. The builder mirrors it and shows it on the Number's page so the
customer's endpoint can verify signatures.
_Avoid_: webhook secret (that's the Agent Backend's event secret), token
