# Agent Backends own their events; builder is control-plane only

TurnCall webhook subscriptions are project-scoped (a subscription receives all of
a project's events; no per-agent filter). So each created agent gets its **own
TurnCall project + key**: on Create the builder creates the project, provisions
the agent in it, registers a webhook pointing straight at the agent's generated
backend (`host.docker.internal:<port>/events`), and bakes the returned signing
secret into that backend. TurnCall then POSTs events **directly to the backend**,
which verifies the signature (its own secret) and stores them. The builder is
**never in the event path** — no `/events` endpoint, no routing, no store. The UI
polls each backend directly at `localhost:<port>/events` (CORS is open there).

Chosen over the earlier "builder holds one subscription and routes" design (and
over one shared project with backends filtering by agent_id) because the user
wants the builder to be purely control-plane and each generated backend to fully
own its own events. Per-agent projects give exactly-scoped delivery with no
cross-talk and no filtering.

Trade-offs: this **supersedes ADR-0002's single-project model** for created
agents (the builder now spins up a project + key per agent). The builder no
longer needs a platform `TURNCALL_API_KEY` — project/key creation is
unauthenticated (dev). Consequence: events only land once the backend is up; the
signing secret lives in the generated repo's compose env.
