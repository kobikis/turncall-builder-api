# Per-agent generated Agent Backend

Every created agent gets its own generated, dockerized backend service (an
"Agent Backend") holding that agent's backend logic — custom tool webhooks, its
event receiver + store, and room for custom endpoints. On Create, the builder
generates a new repo (`turncall-agent-<slug>/`), and runs it as its own docker
compose.

Chosen over a single shared backend hosting all agents' tools because the user
wants each agent's backend logic self-contained in its own repo/service it can
own, edit, and deploy independently. Trade-offs accepted:

- **The builder runs Docker** via a mounted `/var/run/docker.sock` + a
  host-mounted directory (so generated repos are real files on the host and the
  sibling compose can read them). This grants the builder host-Docker control —
  acceptable for a local dev tool, **not** something to ship to prod.
- **Codegen is hybrid**: the scaffold (FastAPI bootstrap, `/events` receiver,
  Dockerfile, compose, port wiring) is a fixed hand-written template — reliable,
  testable, no LLM variance. Only per-tool handler *bodies* are LLM-authored
  stubs. Fully-LLM generation risks broken boilerplate every time; fully-fixed
  gives generic echo handlers with no per-tool shape.
- **A container per agent**, generated for every created agent (not only ones
  with tools). Ports are assigned sequentially from a base and tracked in an
  `agent_backends` registry (`agent_id → {port, service_dir, status}`). Create is
  idempotent (reuse the registry row); no regen-on-update (V1 has no agent-update
  flow); teardown is manual.

Supersedes the V1-C plan's "builder hosts one shared events/tools endpoint".
