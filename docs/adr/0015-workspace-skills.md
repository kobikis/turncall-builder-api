# A Workspace can write its own Builder skills

ADR-0014 gave the Builder a skills library and shipped four in the repo. This
records opening it: a Workspace admin can add their own, and they are mounted
alongside the built-ins on every turn.

## Why this is affordable now and was not before

The old prompt was one string. Adding "how we take a table booking" to it meant
every agent — dental clinics, debt collection, dispatch — paid for it on every
turn. Progressive disclosure changes the arithmetic: a skill costs one line of
frontmatter until the model decides it applies, so twenty vertical skills cost
twenty lines rather than forty kilobytes. That is the whole reason this feature
exists, and it is why it could not have existed one ADR ago.

## Guidance, not configuration

A skill teaches the Builder **how to decide**, not what to pick.

This needs saying because the first thing people will want is "always our voice,
our model, our greeting", and a skill is the thing that exists, so they will
reach for it. It would half-work: a skill loads when the model judges it
relevant, so a default expressed as a skill is a default that silently does not
apply sometimes. That is precisely the trap the ADR-0014 discipline split
avoided — unconditional rules stayed in the prompt for exactly this reason.

The console says so in the editor. Workspace **defaults** are a real gap and
should be filled as a deterministic setting, not approximated here.

## Workspace-scoped, admin-written

**Workspace, not Agent.** A skill is "how we build agents here". Per-agent
guidance already exists and is called that agent's system prompt.

**Admin, not editor.** A skill is prompt text injected into a Builder that holds
the deployment's LLM key and provisions real TurnCall projects. That is a wider
blast radius than editing one agent, so it takes the same role as managing
membership. Reading is any member's — the API allows it, though the console page
is admin-only, since there is nothing to do there without write access.

## Built-in names are reserved

deepagents loads sources in order and resolves a name clash last-wins, so
mounting the workspace's skills second means a workspace skill named
`agent-tools` would replace the built-in one. Silently. Overriding a built-in is
a legitimate thing to want eventually, but a silent override is
indistinguishable from a bug, and two of the four built-ins encode behaviour
fixed deliberately after a real bad call transcript.

So the four names are rejected at write time. The mount order still puts the
workspace last, which means a mistake in that check degrades into an override
rather than a hard failure — the safer direction of the two.

## Frontmatter is generated, not written

`name` and `description` are columns; `body` is markdown without frontmatter.
The `SKILL.md` is rendered at materialise time.

Nobody should hand-write YAML to teach the builder something, and the failure
mode if they get it wrong is the bad one: a malformed frontmatter block does not
error, the skill just is not there. The description is JSON-encoded into the
YAML scalar — YAML accepts JSON scalars — so a colon or a quote in someone's
sentence cannot break it.

## Materialised per turn

Storage is Postgres; deepagents reads skills through a backend. So a turn writes
the workspace's skills to `GENERATED_DIR/workspace-skills/<workspace>/` and
mounts that as a second source.

Per turn rather than on write, for two reasons. An edited skill applies to the
very next message of every conversation already in progress, which is what
someone tuning a skill expects. And there is no cache to invalidate across hosts
— the directory is rebuilt to match the database each time, stale directories
removed, so a deleted skill stops applying immediately rather than lingering on
whichever host happened to write it. Writes are atomic (temp file, rename) so a
concurrent turn in the same workspace cannot read a half-written `SKILL.md` and
drop the skill as malformed.

The cost is a handful of small file writes per turn. That is nothing next to a
model call.

## Consequences

- Migration `0008` adds `workspace_skills`, unique on `(workspace_id, name)` —
  the name is the identity deepagents uses.
- `PUT /skills/{name}` creates-or-replaces, because that is the operation that
  actually exists when the name is the identity.
- A skill can make an agent worse. It cannot make it read "1. 2. 3." aloud to a
  caller: the unconditional rules live in the base prompt, which is assembled
  separately from skills and cannot be reached from one.
- Importing skills from a GitHub repo is the natural next step — `SKILL.md` is
  Anthropic's format and the per-user GitHub connection already exists
  (ADR-0013). Not built.
- MCP was considered alongside this and dropped for now. Note for whoever picks
  it up: `mapper.py` is an allowlist, so `mcp_servers` in a config is silently
  dropped at create *and* apply. Redaction already covers it — `headers` and
  `env` are in `_SECRET_KEYS`.

## Status

Accepted and implemented.
