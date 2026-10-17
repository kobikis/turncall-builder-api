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
