# BYO tool server: agent + per-tool server URLs

ADR-0004 gave every agent a generated Agent Backend that owns ALL its custom
tool webhooks. That's right for zero-config composition, but wrong the moment
the user already has an API: the generated stub becomes a middleman to rewrite
by hand. This ADR lets tools route to the user's own server.

Decisions:

- **Three-level routing.** A custom tool's webhook_url resolves as: the tool's
  own `server_url` (a full URL, used verbatim) → the agent's `server_url`
  (a base; the tool posts to `{base}/tools/{name}`) → the generated Agent
  Backend (unchanged default). Nothing set = exactly today's behavior.
- **Externally-routed tools get no generated handler.** toolgen skips them and
  the provenance chip reads `external` (vs `generated`/`stub`). The Agent
  Backend is still generated — it owns events and managed call-init regardless.
- **Signing stays on.** TurnCall signs every custom-tool POST with the agent's
  one webhook secret; the console reveals that secret whenever any tool routes
  externally, so the user's server can verify `X-TurnCall-Signature` with the
  documented scheme.
- **The composer accepts a volunteered URL but never asks.** "My API is at
  https://api.acme.com" fills the fields; otherwise composition stays
  zero-question and the URLs remain editable in the console config.

Amends ADR-0004 ("backend owns the agent's tools") — the backend now owns the
agent's tools *by default*.
