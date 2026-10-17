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

**Build mode** / **Edit mode**:
The two modes of the [[Builder]]. Build mode interviews from nothing and produces
a new [[Config]]; edit mode applies a requested change to an existing one and must
not re-interview. They share the domain rules and differ only in the interview.
Distinct from [[Create]] — the Builder is done before Create begins.
_Avoid_: "creation agent" / "creation mode" (reads as [[Create]], which is a
different step), new vs update

**Round**:
One Builder turn's set of questions. The opening turn asks a single question;
every turn after it asks a *round* — up to four questions that are answerable
now, each carrying the Builder's own recommendation so the user can accept
instead of decide. Questions whose answers depend on another question in the
same round belong in the next one.
_Avoid_: batch, form, questionnaire

**Discipline**:
One editable piece of the Builder's system prompt — voice pipeline, tools,
guardrails, knowledge, defaults, plus the interview and language pieces.
[[Build mode]] and [[Edit mode]] assemble different sets of them per turn, so a
turn only carries guidance it can act on. Succeeded by [[Skill]] (ADR-0014).
_Avoid_: section, rule, prompt fragment

**Skill**:
A named piece of the Builder's own expertise, loaded on demand: a description the
Builder always sees, and instructions it reads only once that description matches
what it is doing. Succeeds [[Discipline]] — the same guidance, but a turn pays for
a skill's full text only when it needs it, which is what makes a large library of
them affordable. Always about how to *build* an agent. Never about what a finished
agent knows: that is [[Agent Knowledge]], which the voice agent reads to callers.
_Avoid_: plugin, module, prompt fragment, knowledge (the caller-facing one)

**Workspace skill**:
A [[Skill]] written by an admin rather than shipped with the builder, mounted
alongside the built-in ones (ADR-0015). Scoped to the [[Workspace]] — "how we
build agents here" — and guidance only: it teaches the Builder how to decide,
never what to pick, because a skill loads only when it looks relevant and a
default that applies most of the time is worse than none.
_Avoid_: custom skill, template, override

**Confirm**:
The Builder turn between the last question and [[Finalize]]: it states what it
understood, in the user's own words about their business, and waits to be told it
is right (ADR-0016). Nothing is built until the user accepts it. Distinct from the
[[Console]]'s Apply, which confirms a change to an agent that already exists.
_Avoid_: approve, review, summary (that's the field, not the turn)

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
system_prompt, first_message, llm, voice, built-in tools, and `aws.region` for
the AWS providers). Everything outside it takes TurnCall defaults.

**Credentials reach TurnCall as [[Integration]]s, never as config.** The Builder
may select an AWS *region* but never emits `access_key_id`, `secret_access_key`
or `role_arn` into a generated config — agent config is stored unencrypted, and a
config carrying a key would put it in a database row and in every API response
that isn't masked. That much is unchanged. What changed is that a [[Workspace]]'s
own provider keys are no longer the server operator's alone: they are entered on
the [[Console]], stored encrypted here, and projected into TurnCall's own
encrypted credential store. The config surface itself stays credential-free.
See ADR-0023, and turncall ADR-0020.
_Avoid_: schema (ambiguous with the full TurnCall schema)

**Agent Backend**:
The generated, dockerized, per-agent backend service — one per created agent.
It owns that agent's backend logic: custom tool webhooks, its `/events`
receiver + event store, and any custom endpoints. Lives in its own generated
repo, run as its own docker compose — or, on Kubernetes, as its own
Deployment and Service on a shared runtime image (ADR-0025).
_Avoid_: tool service, sidecar, microservice

**Scaffold**:
The fixed hand-written template for an Agent Backend (FastAPI bootstrap,
`/events` receiver, `Dockerfile`, `docker-compose.yml`, port wiring). Only the
per-tool handler bodies are LLM-authored and dropped into it.
_Avoid_: template, boilerplate

**GitHub connection**:
One [[User]]'s GitHub credential — what lets them push an [[Agent Backend]].
Per-user, not per-[[Workspace]]: it is authorized by a person against an account
they control, and pushes are attributed to them on GitHub. Currently a
fine-grained personal access token, scoped to chosen repositories with
`contents: write` and an expiry; stored encrypted, since it is the first
credential the builder must read back rather than hash. A GitHub App may replace
how the credential is obtained later without changing anything else. See
ADR-0013.
_Avoid_: GitHub account, integration, OAuth, "the workspace's GitHub" (there is
no such thing), "GitHub App" (not what this is today)

**Linked repo**:
Where an [[Agent Backend]] pushes: an organisation, an existing repository, a
branch, and an optional path inside it. The user *chooses* all four from what
their [[GitHub connection]] can see — nothing is ever created on their behalf.
The path is what lets a workspace keep every agent in one repository under
`agents/<slug>/` instead of one repository each; that is the user's call, not
ours. It records the connection that owns it: the one that first linked it and
whose token later pushes use. The *local* generated repo stays the source of truth; pushes are
one-way and never forced, so an edit made on GitHub causes a push to fail loudly
rather than be clobbered. A Linked repo outlives its owning connection — when
that connection goes, pushing stops and another user can take it over, but
nothing is deleted. Deleting the agent leaves it alone too.
_Avoid_: publish (means agent versioning in TurnCall), export (implies
one-time; this is continuous), remote, mirror

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
The builder-web management UI: a sidebar with Agents, Phone Numbers,
Integrations, Members and Skills (each list → detail → edit), plus the Builder
reached as the "new agent" path.
_Avoid_: dashboard, admin panel

**Number binding**:
A phone number bound to exactly one Agent, routing that number's inbound
calls to it. Its **carrier** — Twilio, Telnyx or Asterisk (a PBX over ARI;
turncall ADR-0022) — is fixed at bind. Binding lives in the Agent's TurnCall
project; on Twilio and Telnyx TurnCall points the number at itself on bind
(the builder never calls the carrier), and an Asterisk number is routed by
the PBX's own dialplan. SMS and a number's own Twilio account are Twilio-only.
Re-pointing to a different Agent moves the binding across projects.
_Avoid_: DID, line, phone

**Number registry**:
The builder's `phone_numbers` table mirroring every Number binding it made
(`e164`, `sid`, `carrier`, `agent_id`, `project_id`, `sms_enabled`; rows from
before carriers read as `twilio`). Source of truth for the
Console's Phone Numbers list, so listing is one local query, not an N-project
fan-out.
_Avoid_: phone book, directory

**Do-not-call list**:
The numbers an Agent never calls: TurnCall's suppression list for the Agent's
project. Every outbound call checks it, and the `do_not_call` tool adds callers
who ask not to be called again. The builder's routes say `do-not-call`; its
TurnCall client keeps TurnCall's name.
_Avoid_: suppression list (in Console copy), blocklist

**Campaign**:
TurnCall's outbound calls to a list of targets, dialled by its campaign worker
with a calling window, retries by ending and a per-number pace. In the builder
a Campaign belongs to one Agent and dials from one of its numbers: TurnCall's
weighted campaigns split targets between agents of one project, and every
Agent is its own project, so they are not offered.
_Avoid_: blast, dialer job

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
Number is bound in its Workspace's webhook project (the shared one, for numbers
bound before #65); the customer hosts and verifies the endpoint themselves.
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

**Staged document**:
A file attached during a [[Session]] before its Agent exists. The builder holds
it, not the browser, for the length of the interview — so the [[Builder]] is told
what is really there and can read it. At [[Create]] it is forwarded into
[[Agent Knowledge]] and the staged copy dropped; anything that fails to forward
stays staged, so Create can be pressed again to retry. Distinct from an Agent
Knowledge document, which belongs to a live Agent and is TurnCall's to hold.
_Avoid_: queued doc, attachment, upload

**Call record**:
A call as TurnCall recorded it — direction, numbers, status, ended reason,
duration, transcript, analysis. Shown in the Console's Calls tab, read from
the Agent's own project. Distinct from an Event (the raw signed envelope the
Agent Backend stores); the Calls tab shows Call records with the Event feed
alongside.
_Avoid_: call log, call history entry

**Eval scenario** / **Eval run**:
One saved behavioural test for an Agent, and one execution of it. Both live in
TurnCall, not here — builder-api proxies them workspace-scoped with the Agent's
own API key the way it proxies [[Call record]]s, and stores no eval state. Shown
on the Console's **Evals** tab beside Calls and Takeaways: scenarios for the
Agent you are looking at, run with one click, verdict read back inline. A run
reports `passed_count`/`failed_count` out of its iterations — there is no score
— and `errored` means TurnCall *could not check*, not that the agent failed.
Editors run; viewers read.

A scenario also carries the **judge** that decides its verdicts
(`{provider, model, temperature}`, turncall#118/#119). This router adds no
schema for it: the body passes through, and the closed set of providers is
enforced where it is defined. One thing it does have to get right — the update
forwards what the browser **sent**, nulls included, because `judge: null` means
"back to the platform default" and dropping it would leave the Console offering
a choice that silently did nothing.
_Avoid_: "test suite"; rendering `errored` as a failure; validating TurnCall's
eval fields here.

**Save as scenario**:
The button that turns a conversation that already happened into an [[Eval
scenario]] draft — on a row in the Calls tab (via TurnCall's `from-call`) and on
the manual text test (via `from-session`). Nobody sits down to author test cases
for a voice agent; they do click a button on a call that went wrong, which is
why the library populates itself. The result is a **draft**: it asserts whatever
the agent did that day, and the UI says so.
_Avoid_: presenting a derived scenario as a finished test.

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

**Widget**:
The "talk to us" button a Workspace puts on its own website: the `turncall-js`
drop-in, pointed at a mint URL. For an Agent built here that URL is its [[Agent
Backend]]'s `/webrtc-session`, which asks builder-api for a TurnCall **session
token** (one call, five minutes, this Agent, variables sealed) and refuses any
origin not on the Agent's allowed-origins list. The backend never holds a TurnCall
key. See ADR-0026 (decided, not yet built).
_Avoid_: "embed key", "public key" (there is none), "web call" (a test call from the
Console is not the Widget).

**Audit entry**:
One change a person made through the Console, as builder-api records it: the
[[User]], the [[Workspace]], the [[Role]] they acted under, what changed, and, when
the change reached TurnCall, a link to each of TurnCall's own audit entries it caused
(which record only the API key): the project and the engine entry id, one per
TurnCall request, since one change can reach many projects. Append-only, admin-readable (`GET /audit`), never holds a secret: a
credential change says the credential changed, and a secret-named field reads
`***` on both sides with `"secret": true` on its change (the flag, not the text,
says it was masked). Written by the change itself — in its own transaction when
the change is builder-side only, once it is done otherwise. A change of several
steps that fails after some of them landed (a credential saved but not yet
written into every agent, a number unbound but not yet bound again) is still
written, `outcome: partial`, with why and what it had reached; one that failed
before anything landed leaves none. Kept as long as the operator's
`AUDIT_LOG_RETENTION_DAYS` says, never a Workspace's choice, so no admin can
shorten the trail that records them. See ADR-0028.
_Avoid_: "activity", "history", "log" unqualified.

## Integrations

**Connected app**:
A [[Workspace]]'s standing authorization to act on a third party — a Slack
workspace, a Google account. Held by Pipedream, never by the builder: what
builder-api stores is the Pipedream client credential (encrypted, like a
[[GitHub connection]]), and what identifies the tenant to Pipedream is the
Workspace id as its `external_user_id`. An admin authorizes one through
Pipedream's hosted Connect Link — the builder redirects and never sees the
third party's consent screen or its tokens. Belongs to the business using the
builder, **not** to the caller on the phone and not to that business's own
customers. See ADR-0018.
_Avoid_: "connection" unqualified (that reads as [[GitHub connection]]),
"integration" (the same word [[GitHub connection]] avoids), "OAuth app"

**Action**:
One invocable operation on a [[Connected app]] — post a Slack message, create a
calendar event. The shared unit both integration paths reach: in-call it is
surfaced to the LLM as an ordinary TurnCall tool over MCP, post-call it is run
directly by an [[Automation]]. Named by Pipedream's action slug.
_Avoid_: tool (TurnCall's word, and only half of what an Action is), operation,
endpoint

**Automation**:
An [[Action]] bound to an [[Event]], run by the [[Agent Backend]] when that event
arrives — "post the summary to #sales when the call ends". Deterministic, which
is the whole point of it not being a tool: an LLM may decline to call a tool, and
"send the message" is not a decision the model should be making. Unconditional.
Distinct from an in-call integration, which *is* a tool and may legitimately not
fire.

Chosen in the [[Console]] on the agent, alongside [[mid-call]] exposure, and
stored in the agent's config — but only the builder reads that half: it drives
code generation, and nothing about an Automation reaches TurnCall. It lives as a
handler in the backend's [[Scaffold]], generated whole from the choice rather
than as a stub to be filled in, and still editable in the repo the user owns.
See ADR-0021.

**Automation run**: what one Automation did, recorded by the backend and read
back at `GET /automations` — `ok`, or one of four failures kept deliberately
apart: `not_connected` (nobody authorized the app — an admin's problem),
`refused` (the provider rejected it: an archived channel, a stale calendar id —
an editor's), `unfilled` (the call had nothing to put in a required input) and
`error` (the token or the network). The Console shows the latest one beside the
Automation it belongs to. Never having run is a distinct state from having
failed, and the surface has to say which — the failure mode this replaces was a
Slack post that silently stopped arriving and read as a quiet week. Recording it
never raises and never retries: the call has already ended (ADR-0018).
_Avoid_: post-call hook, trigger, workflow, "action" alone (that's the operation
it runs)

**Takeaway binding**:
An end-of-call input whose value is a field of one of the Agent's takeaways
(`{"takeaway": "caller", "field": "email"}`) rather than something fixed up front,
read from the `call.ended` the [[Automation]] is answering. A missing field records
the run as `unfilled`. What a CRM log entry needs, since its values exist only once
the call has happened. See ADR-0027 (decided, not yet built).
_Avoid_: "dynamic input", "mapping" (reads as a field-to-field sync).

**Trigger** — [[mid-call]] vs [[end-of-call]]:
When an attached [[Connected app]] runs, chosen per app and not symmetric,
because the two ask different questions. Mid-call asks *which tools may the
model choose from* — a set, with the arguments supplied at runtime by the model.
End-of-call asks *which one [[Action]] runs, and with what arguments* — nothing
chooses at runtime, so both are decided up front. An app may carry either, both,
or (before ADR-0021) neither, which is now refused. See ADR-0021.
_Avoid_: "schedule" for mid-call (nothing guarantees the model calls it),
treating the two as one list

**Catalog**:
The apps this build can connect, and for each the pinned tool allowlist, the
[[Action]]s worth running at [[end-of-call]], and what each Action needs asked.
Curated in code, not discovered: nothing in the system can answer "what can this
app do" without someone having written it down. Deliberate — the pinning is
load-bearing, because a provider generates one tool per registry action (50 for
Slack, against TurnCall's cap of 50 per server) and every advertised tool rides
in every LLM request for the length of the call. The exception is an input's
*values*, which are fetched live per [[Connected app]]: see [[input options]].
See ADR-0018, ADR-0021, ADR-0022.
_Avoid_: "registry" (that is the provider's, and it is far larger), "the list of
integrations"

**Input options**:
The values one [[Action]] input will accept, listed by asking the provider on
behalf of this [[Workspace]]'s [[Connected app]] — the channels in that Slack,
the calendars in that Google account. The one part of the [[Catalog]] that is
discovered rather than curated, because it is the one part that is nobody's to
curate: it belongs to the Workspace and changes without telling anyone. Shown as
a picker that stores an id and displays a name. Failing to fetch them costs the
picker and never the form. See ADR-0022.
_Avoid_: "schema" (the prop's *shape* is still curated; this is only its values),
"autocomplete"

**Integration**:
A [[Workspace]]'s own account with a provider — its Deepgram key, its AWS
credentials — entered on the [[Console]], stored encrypted here, and projected
into TurnCall as a Project credential. One per provider *account*, never per
role: Deepgram is one key serving transcription and voice, OpenAI one serving
four, so a card is a provider and never a (role, provider) pair. Absent one, a
provider falls back to the key the TurnCall server itself holds — which is why a
card states which key will run the call rather than only "connected". Entering
and disconnecting are admin-only; an editor sees presence, because the agent form
has to be able to flag a provider with no key. See ADR-0023.
_Avoid_: "provider" (that is the *selected backend*, TurnCall's word), "API key"
alone (the AWS one is a credential set), "[[Connected app]]" (Pipedream's, and
minted per call).

**Workspace Twilio account**:
A [[Workspace]]'s Twilio account, connected once on the Integrations page and
chosen when binding a number instead of entering a pair per number. Not an
[[Integration]] in the projected sense: TurnCall holds Twilio credentials per
*number* (Twilio's own unit), so a bind sends this account's pair and the phone
mirror marks the number `twilio_source = 'workspace'`; saving the card again
re-sends the pair to each such number. A number may still bring its own pair (a
second account, a subaccount). Disconnect is refused while numbers use it.
_Avoid_: "Twilio integration" (suggests projection into agents).

**Workspace Pipedream project**:
A [[Workspace]]'s own Pipedream Connect project, behind its [[Connected app]]s,
instead of the deployment's (`PIPEDREAM_*`). Every Connected-apps path resolves
the Workspace's first. Its apps live only in it, so disconnecting is refused
while any are connected.
_Avoid_: "Pipedream credential" unqualified (the deployment has one too).

**Workspace MCP server**:
An MCP server registered once for a [[Workspace]] and opted into per agent,
instead of retyped into each agent's config (ADR-0017 put them on the agent; this
puts the definition one level up). The master — URL and headers, which carry
bearer tokens — lives encrypted here; what TurnCall receives is a [[Projection]]
into that agent's `mcp_servers`, the same shape the Pipedream target already
takes in `apply_update`. Opt-in and never inherited, because TurnCall connects
every configured server at call start under one 10s discovery budget and past it
the call proceeds with *no* MCP tools: one slow server, inherited by default,
silently disarms every agent in the Workspace. An agent's own inline server still
wins a name collision. `stdio` stays refused (ADR-0017). See ADR-0023.
_Avoid_: "shared MCP server", "global server" (it belongs to a Workspace, not the
deployment).

**Compliance settings**:
A [[Workspace]]'s redaction (on/off, which entity types) and call retention
(days per kind: recordings, transcripts, events), set once by an admin and
[[Projection|projected]] into every Agent's TurnCall project and the Workspace's
webhook project (turncall ADR-0024). Unset is the engine's own default — no
redaction, everything kept — so nothing is written until an admin sets one.
An Agent may **override** either: its redaction whole, its retention field by
field (null takes the Workspace's). The override lands in the Agent's *project*
setting, never in its [[Config]]'s `retention` block: one project per Agent makes
the project's setting the Agent's, as with its limits, and redaction has no
agent-level field to land in anyway. Validated here as the engine validates
(known entity types, 1–36500 days), so a bad value fails before N writes. See
ADR-0028.
_Avoid_: "privacy settings", "data policy", "audit retention" (the audit log's
own retention is separate and never touched by these).

**Projection** (of an [[Integration]], a [[Workspace MCP server]] or the
[[Compliance settings]]):
The copy written into each agent or TurnCall project when a workspace-scoped
record changes. The master here is authoritative and the copy disposable —
rewritten, never read back. Because the Builder mints one TurnCall project per
agent, one change is N writes, so a projection is idempotent and reports
per-agent state; on a *removal* it projects first and deletes the master last,
since the reverse order strands agents holding a credential nothing points at.
_Avoid_: "sync" (nothing is bidirectional — a master never learns from a copy),
"broadcast".
