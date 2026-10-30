# A Capability library per Workspace

TurnCall's skills (turncall#421) are versioned bundles of instructions and
webhook tools that an agent loads mid-call with `load_skill`. A skill belongs to
one TurnCall project, and an agent resolves the names it lists in its own
project. The Builder mints one project per agent (ADR-0004/0005), so on its own
that model makes sharing impossible: two agents of one [[Workspace]] could never
list the same skill, which is the point of the feature.

The Console calls a TurnCall skill a [[Capability]], because "Skill" here already
means the Builder's own build guidance (ADR-0014, ADR-0015).

## Decision

**A Workspace has one [[Capability library]]: one more TurnCall project, holding
all of its Capabilities.** It is made lazily, on the Workspace's first Capability,
through the platform bootstrap every agent project goes through (create the
project, then an admin API key), and its key is sealed here under
`INTEGRATIONS_ENCRYPTION_KEY`. Two first creates racing make one library: the
creation runs under a per-Workspace advisory lock, and the table's primary key is
the Workspace.

**Every agent project in the Workspace is linked to it**, and so is the
Workspace's webhook project, through TurnCall's skill library link (a project's
`skill_library.library_project_id`, set with `PUT /v1/projects/{id}/skill-library`). A linked
project resolves skill names in its library and nowhere else. Existing projects
are linked when the library is made, a new agent's project at Create (before its
agent is saved, so a finalized config naming Capabilities saves), and the
webhook project when it is first made.

**Linking is a projection** in ADR-0023's sense: one write per project, each
recorded with its state and, on failure, why; re-running skips projects already
linked to this library; a failed link is kept, shown on the Builder API
(`GET /capability-library`) and retried (`POST /capability-library/links`).

**The Builder proxies TurnCall's skill routes on the library's key**
(`/capabilities`, `/capability-versions/{id}`). TurnCall validates every write;
its 422 comes back naming the field. Reads need a member, writes an editor, the
same as agents, and every write is an audit entry (ADR-0028). Which agents use a
Capability is TurnCall's agents listing, which spans the library and every
project linked to it, mapped back to Console agents by project through the
[[Backend registry]].

**The library holds no provider keys.** A call's speech and model run on the
agent project's own keys, and a Capability's tools are webhooks to the author's
[[Tool server]]. Capability knowledge bases, which would need embeddings and so
a projected key, are out of scope.

## Why not copy each Capability into every agent's project

It is the obvious alternative: keep projects unlinked and write each skill into
every project that lists it. It fails on what a skill is for:

- **Versions stop meaning one thing.** A publish or a rollback would be N
  writes, and each agent's copy would carry its own version ids. A pin names a
  version id, so a pin made in one agent would name nothing in another, and
  "which version did this call run" would need a mapping table to answer.
- **A partial publish is a split Workspace.** With N copies, a publish that
  reaches three projects of five leaves the Workspace running two versions of
  one Capability with nothing on the Console saying which agent has which. One
  library publishes once.
- **The delete guard stops working.** TurnCall refuses to delete a skill while
  an agent lists it. With copies, each project only knows its own agents;
  with a library, the engine sees every linked project and refuses for all of
  them.
- **Copies are writes the author never asked for.** Projection is right for a
  credential (ADR-0023), whose master is here and whose copy is disposable. A
  Capability's master would be in TurnCall either way, with versions the engine
  owns.

The link is the smaller projection: one fact per project (which library it
reads), written once, idempotent, and the same for every Capability that will
ever exist.

## Why the link is the platform credential's

A linked project reads another project's skills. If a project's own key could
set the link, any key could read any project's skills by naming it. Only the
platform credential, which the Builder holds, can set it, and TurnCall records
each link in its own audit log as the platform's. TurnCall refuses a link to
itself, a chain either way (a library that is itself linked, or linking a
project other projects already read from), and a project that does not exist.

## Consequences

- A Workspace's first Capability costs one more TurnCall project and key.
- An agent created while the library was being made can miss its link; it then
  shows as not linked on `GET /capability-library` until the links are retried.
- A deleted or unlinked library degrades softly on a call: an agent's skills
  become a warning and a skipped skill, never a failed call. Saving an agent
  that lists them is refused, naming each entry (`skills[i] '<name>' cannot be
  resolved: this project's skill library is gone`).

## Addendum (2026-10-30): Skills and Builder skills

The Console now calls these [[Skill|Skills]] and the Builder's own guidance
[[Builder skill|Builder skills]]: one name for one thing, the name the agent's
config, TurnCall's API and its call records already use. The code follows:
the routes are `/skills`, `/skill-versions/{id}` and `/skill-library` (the
Builder skills' own moved to `/builder-skills`), and the tables are
`skill_libraries` and `skill_links` (migration 0026). The audit log keeps the
resource types its entries were written under, since they are append-only:
`skill` is a Builder skill, `capability` and `capability_library` a skill and
the skill library.
