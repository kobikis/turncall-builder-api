# One implicit knowledge base per agent (Agent Knowledge)

TurnCall's knowledge model is three-level: knowledge bases contain documents,
and agents link to KBs with a retrieval mode (`prompt`/`auto`/`tool`), top_k,
and threshold. The Console's Knowledge tab exposes none of that: each agent
gets ONE implicit KB ("builder-knowledge", created in the agent's own project
on first upload, auto-linked with mode `auto`), and the tab is just its
document list — upload, replace, delete, plus a test-search box.

Chosen over full KB management because the builder's mental model is "docs per
agent": a per-agent console gains nothing from KB juggling, cross-agent KB
sharing already can't happen (per-agent projects isolate KBs anyway), and one
level of UI beats three. The full model stays reachable via the TurnCall API
with the agent's key.

Consequences accepted:

- **Documents accumulate in that one KB** — outgrowing this means a migration,
  not a toggle. Retrieval mode/top_k are not surfaced; `auto` is the default
  that fits "the agent should know this" without prompt-size or tool-latency
  decisions.
- **"Replace" is delete + re-upload** (new document id). TurnCall has no
  document-update endpoint, and an update must re-chunk + re-embed regardless,
  so the builder does not add one.
- **The KB is resolved by listing the agent's project KBs** (it has at most
  one) — no registry column, no migration.
