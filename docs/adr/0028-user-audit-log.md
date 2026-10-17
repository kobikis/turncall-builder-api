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

## The audit log, as built (#101)

`audit_entries` (migration 0020) and `GET /audit`, admin-only, Workspace-scoped,
newest first, filtered by `member` (a user id), `agent` and `since`/`until`, paged
as every list here is (`page`, `limit` up to 200, `total`). An entry holds the
User and their email as it was, the Workspace, the Role, `resource_type`,
`resource_id`, a `resource_name` to read it by, the `action`, every Agent it is
about (`agent_ids`: a re-pointed number is about both agents), `changes` and a
`request_id` shared by every entry one request wrote. `turncall_project_id` and
`engine_audit_id` exist and stay null until the link to the engine's entry
(#102) fills them.

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
TurnCall cannot share a transaction with it: its entry is written once TurnCall
accepted the change, and a request that failed writes none.

**A Connected app is recorded at the callback.** It comes to exist when
Pipedream redirects back, not when the admin starts the flow, and that redirect
is no member's request: the signed connect state carries who started it and
their role, and the entry is written in their name.

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
