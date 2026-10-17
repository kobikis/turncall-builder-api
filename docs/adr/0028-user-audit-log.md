# The builder audits people; TurnCall audits keys

TurnCall's ADR-0024 adds an append-only audit log to the engine, keyed by API
key, because TurnCall is identity-free. Every change the Console makes reaches
TurnCall through a key builder-api holds, so the engine's log records the
*builder's* key, not who pressed the button. Who did it is known only here:
the [[User]], through their [[Membership]] in a [[Workspace]].

## Decision

**builder-api keeps its own append-only audit log of user actions.** One entry
per change a person makes: agents (create, Apply, Teardown), numbers (bind,
re-point, buy, release), [[Integration]]s and [[Connected app]]s, members and
roles, [[Workspace skill]]s, [[Linked repo]] pushes. Each entry records the
User, the Workspace, the [[Role]] they acted under, what changed, and when.

**It links to the engine's entry, not a copy of it.** Where a change reached
TurnCall, the entry stores the TurnCall project and the engine audit entry's id.
"Who" lives here and "what TurnCall did" lives there. Neither log stores the
other's half.

**It never stores secrets or call data.** A credential change records that the
Deepgram key changed, never the key.

**Read by admins.** Editors and viewers do not see the audit log: it shows what
their colleagues did, which is an admin's business.

**Its retention is its own,** as in the engine: call [[retention policy]]
settings never touch it.

## Consequences

- A new `audit_entries` table and an admin-only `GET /audit` on builder-api; a
  Console page under Members.
- The compliance settings TurnCall's ADR-0024 adds (redaction, retention) get a
  Console page on the Workspace, projected into each Agent's TurnCall project
  like any other workspace-scoped record ([[Projection]]).

## Compliance settings, as built (#103)

`PUT /compliance` (admin) sets the Workspace's redaction and retention;
`POST /compliance/apply` retries; `GET|PUT /agents/{id}/compliance` reads (any
member) and overrides (admin) one Agent's. Storage is `workspace_compliance`,
`agent_compliance` and `compliance_projections` (migration 0019).

**An Agent's override is written into its own project's setting, not into the
engine's agent `config.retention`.** Every Agent is its own TurnCall project, so
the project setting already is the Agent's, and one mechanism then covers both
halves: redaction has no agent-level field in the engine. It also keeps
`retention` out of the agent config, which every Console Apply rewrites and which
the engine guards with an admin-only refusal on change. The engine's agent
override exists for projects holding several agents, which the Builder never
mints.

**Projection state is a fingerprint per target.** Each Agent's effective setting
(Workspace, then its override) is hashed; a target recorded `applied` with that
hash is skipped, so a re-run after a partial failure writes only what failed, and
a read reports each target `applied`, `failed` (with why) or `pending` (the
master changed since). A target never written and owed only the engine's default
already holds it. The Workspace's webhook project (#65) is a target too, with the
Workspace's setting and never an override, because custom call-init calls are
stored there.

**Targets are written concurrently, eight at a time.** Each target is two
TurnCall PUTs; written one after another, a Workspace of a hundred Agents kept
`PUT /compliance` open for half a minute. The report stays in target order and
each target's state is its own whatever order the writes finish in; a target
that raises lets the others finish before the error propagates. The per-Agent
read fetches that Agent's projection record only.

**Clearing an override is its own action, and a deleted Agent's rows go with
it.** An override set back to null on both blocks is recorded as
`override_cleared` (clearing one that was never set records nothing). Teardown
deletes the Agent's `agent_compliance` and `compliance_projections` rows in the
transaction that hides its registry row: they describe a project that no
longer exists.

## The audit log, as built (#101)

`audit_entries` (migration 0020) and `GET /audit`, admin-only, Workspace-scoped,
newest first, filtered by `member` (a user id), `agent` and `since`/`until`, paged
as every list here is (`page`, `limit` up to 200, `total`). An entry holds the
User and their email as it was, the Workspace, the Role, `resource_type`,
`resource_id`, a `resource_name` to read it by, the `action`, every Agent it is
about (`agent_ids`: a re-pointed number is about both agents), `changes` and a
`request_id` shared by every entry one request wrote, and its links to the
engine's entries (`engine_links`, #102, below).

**One mechanism.** An audited route takes an `Audit` trail and records on it
(app/audit.py). The trail's teardown runs as the route returns, before the
response: a route that returned normally without recording is a 500, so every
endpoint test of an audited route that expects success also proves it recorded.
`audit.unchanged()` is the explicit way out for a branch that changed nothing
(a Create retry, an abandoned Connected-app flow). `tests/test_audit_wiring.py`
walks every mutating route the app serves and fails on one that neither takes a
trail nor is listed, with its reason, as out of scope: Sessions (drafts until
Create/Apply), calls and test calls, knowledge, takeaways, evals, campaigns,
ambience and the do-not-call list (as in the engine's own log), login and
signup, the per-User GitHub connection, and the Agent Backend's own calls.
Workspace compliance settings and an Agent's override (#103) are audited
(`compliance`).

**What changed is a diff of safe values.** Field by field, as JSON Pointers, the
engine's shape. Every secret-named key (the names the GitHub push already
redacts, plus anything ending `_secret`, `_token`, `_key`, `_password`,
`_encrypted`) is compared on its raw value and shown as `***`, at any depth: an
MCP server's headers, a number's call-init secret, an invite's token. A
credential whose value is never read back (an Integration, the Workspace's
Twilio or Pipedream secret) is recorded as set, replaced or removed, never more.

**Same transaction where there is one.** A change that lives only in
builder-api (members and invites, skills, the Workspace's Twilio and Pipedream
records, registering an MCP server) writes its entry in the change's own
transaction (`db.transaction`, which hands stores written against a pool one
connection), so the two commit or roll back together. A change that reached
TurnCall cannot share a transaction with it: its entry is written once the
change is done.

**A change that lands halfway is recorded as it stands.** Many changes that
reach TurnCall are several steps with no transaction around them: an MCP
server edit saves the server, then re-pushes every opted-in agent; a number
moved across projects is unbound in one, then bound in another; Teardown takes
the container down, then the project, then the registry row. Writing the entry
only on success meant a failure at step two left a saved credential, or an
unbound number, with nothing in the log. So such a route begins its change
(`audit.begin`) as the first step lands and records it at the end; when the
route raises in between, the trail's own teardown writes the entry, `outcome:
partial` and `error` (the reason, `failure_reason`), with the agents
(`agent_ids`) and engine requests (`engine_links`) it had reached, then lets the
exception through. It is a property of the trail, not of each route: a route
only says where its change begins and, when that is before anything landed
(`landed=False`), the step that lands it (`change.landed()`; `apply_update`
calls it once TurnCall accepted the agent). A TurnCall write the engine audited
since the change began also counts as landed. A change that failed before
anything landed leaves no entry, as before. `outcome` is `completed` on every
other entry.

**A Connected app is recorded at the callback.** It comes to exist when
Pipedream redirects back, not when the admin starts the flow, and that redirect
is no member's request. Who started it is held server-side, in
`pipedream_connect_flows` (migration 0021): the Workspace, the app, the admin's
id, email and role, and the ids of the app's accounts the Workspace already
had. The connect state carries only an opaque random nonce (signed, so a forged
one is refused before the database is asked), and the row is keyed by the
nonce's digest. The state is signed, not encrypted, and it travels through
Pipedream, proxy logs and the browser history, so it carries nothing about the
admin. The callback deletes the flow as it reads it (`DELETE ... RETURNING`):
a replayed or reloaded callback finds nothing, records nothing and attributes
nothing, and an expired flow answers nothing either.

**Which account the flow made is told by difference, because Pipedream's
redirect does not say.** The success redirect carries only our own state, so the
callback lists the Workspace's accounts of the app and takes the one that was
not there when the flow started. That is exact unless two admins of one
Workspace connect the same app at once (both new accounts look like theirs; the
newest is taken), or Pipedream re-authorizes an existing account in place (none
is new; the one updated last is taken). The precise tie exists only on
Pipedream's side channel: a connect token created with a `webhook_uri` gets a
`CONNECTION_SUCCESS` webhook naming the `connect_token` and the `account.id`,
signed (`x-pd-signature`, HMAC-SHA256) with the project environment's webhook
signing key. Using it means a public webhook route and provisioning that key for
every Pipedream project in use, the deployment's and each Workspace's own, so
it is left for later; the difference rule is the best the redirect allows.

**A "before" nobody read is unknown, not null.** An Agent's limits change
records what it replaced from the builder's own copy (`agent_backends.limits`,
migration 0022), refreshed from TurnCall's answer to every read and save; the
builder holds the project's only admin key, so the copy is the project's. That
spares every save a GET to TurnCall. An Agent whose limits were never read or
set here has no copy, and its entry lists each field as `{"after": ...,
"before_unknown": true}` (`audit.before_unknown`) rather than claiming it was
null.

**Append-only in the database too.** A trigger refuses UPDATE, TRUNCATE and
any DELETE outside the retention janitor's transaction (`SET LOCAL
builder.audit_retention = 'on'`). It stops the application and anything
connecting as it, not the table's owner. No foreign keys: an entry outlives the
member, agent or number it is about.

**Retention is the operator's.** `AUDIT_LOG_RETENTION_DAYS` (1 to 36500; unset
keeps entries for good, anything else refuses to start), swept hourly by each
server in batches that skip each other's rows. It is deliberately not a
Workspace setting beside #103's call retention: the people who could shorten it
would be the admins it records. Call retention never touches it.

## The link to TurnCall's entries, as built (#102)

Every response to a request TurnCall audits names one entry in
`X-TurnCall-Audit-Id`, and that id is also the `request_id` of every entry the
request wrote, so `GET /v1/audit-log?request_id=<id>` reads its whole cascade
(TurnCall's audit-log guide).

**An entry holds a list, `engine_links`: one `{project_id, engine_audit_id}`
per TurnCall request the change made that TurnCall audited, in the order made.**
Not the primary call's id alone, because one person's change is routinely many
engine requests and often in many projects: Create makes the project, its key
and the agent and projects the Workspace's credentials and compliance settings
into it; a compliance or provider-key change writes into every Agent's project;
re-pointing a number across projects unbinds in one and binds in another. No one
of those ids reaches the others (each engine request has its own `request_id`),
so keeping only one would leave the console unable to show most of what the
change did. The list is the whole join, and nothing of the engine's entries is
copied. It is a column of `audit_entries` from migration 0020 on. An empty list is a
change that never reached TurnCall or that TurnCall does not audit (webhook
subscriptions, knowledge bases, takeaways and the rest its guide lists).

**Captured once, in the TurnCall client.** `_request` reads the header on every
successful response and notes it on the request's collection, a context
variable the `Audit` trail opens for the life of the route; routes parse no
headers. Each `audit.record` takes the calls noted since the request's previous
entry, so a route records a change after making its TurnCall calls (every
audited route already does). A failed engine request raises before it is noted;
a call made after the route returned (a task it started) is not collected.

**The project is the one the call was made in.** A project route names it, and a
platform bootstrap is in the project it creates or keys, as in the engine's own
entry. Every other call carries an Agent's or a webhook project's key, and the
trail resolves the key to its project from the builder's own records
(`registry.projects_for_keys`) as the entry is written; the key is held in
memory for that and never recorded. A key the builder no longer holds still
links the engine entry, with a null project.

**`GET /audit/{id}/engine` reads the cascade back,** admin-only, Workspace-scoped:
for each link, every engine entry its request wrote, read with that project's
key (`registry.key_for_project`: one of the Workspace's Agents', deleted ones
included, or a webhook project it uses). The key never leaves builder-api, so
the Console cannot read TurnCall's log itself. A link that cannot be read comes
back with `entries: null` and why rather than failing the rest; a torn-down
Agent's is the usual one, since deleting its project revokes its keys, and its
engine entries are then only in TurnCall's database.
