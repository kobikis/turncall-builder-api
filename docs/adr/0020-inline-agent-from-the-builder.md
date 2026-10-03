# The Agent Backend asks the builder for the config it runs a call with

ADR-0018 decided that in-call credentials arrive through Managed call-init,
because TurnCall's `render_agent_config()` templates the system prompt, the first
message and tool descriptions only — it never touches `mcp_servers.headers` — and
a Pipedream access token lives an hour. A stored Config therefore cannot carry a
working credential.

What it did not decide is where the backend gets the *rest* of the config from.
An inline agent is a whole `AgentConfig`: TurnCall's resolver takes `agent_id`
**or** `agent`, never both, and an inline one replaces rather than merges. So the
backend cannot send just the headers.

## Decision

**The backend asks builder-api for the config, per call.** `POST
/integrations/call-config`, authenticated with the same per-Agent bearer token as
the mint endpoint (ADR-0019), returns the agent's current Config with a freshly
minted credential already on its Pipedream MCP entries. The backend relays it
into its call-init response.

**Not baked into the scaffold.** The obvious alternative is to render the config
into the generated repo and merge headers at call time. It is wrong because the
backend is only regenerated when its *tools* change (ADR-0004): a prompt edit, a
voice change, a newly attached app would all leave a stale config baked in — and
being stale silently, in the config that overrides the live one, is the worst
shape that failure could take.

**Only when there is something to inline.** With no attached apps the endpoint
answers `null` and the backend replies with its `agent_id` exactly as before. An
inline agent is not free (see below), so it is what an attached app requires, not
a new default.

**Failure costs the tools, never the call.** Any failure — the builder
unreachable, minting refused, a config the mapper rejects — and the backend falls
back to `agent_id`. The caller gets a working agent without its integration
tools. An MCP discovery failure is already this shape in the engine: TurnCall
proceeds with no MCP tools rather than waiting past
`MCP_CONNECT_TIMEOUT_SECONDS`.

## Consequences

- **The backend gains a second runtime dependency on the control plane**, on the
  call path this time rather than beside it. ADR-0018 already accepted the first;
  this one is felt by a caller if the builder is down, which is why the fallback
  is not optional.
- **An inline agent costs the call its `active_agent_id`.** TurnCall records the
  agent only when it resolved one by id, so a call running an inline config has a
  null `agent_id` in its `call.ended` envelope. Tolerable: the builder gives each
  agent its own project, so calls are still attributable, and the backend
  receiving the event knows which agent it is. It is the main reason the
  `agent_id` path is kept for agents with nothing attached.

  **Correction (2026-09-13).** That understated it, and the understatement hid a
  bug. A null `agent_id` was not the consequence — *no event at all* was. Every
  post-call trigger in the engine gated on `call.active_agent_id`, and
  `trigger_post_call_analysis` is what dispatches `call.ended`, so a call running
  an inline agent completed and told nobody: no analysis, no transcript, no
  webhook, and therefore no Automation. The in-call and post-call halves of
  ADR-0018 were mutually exclusive in practice. Fixed in kobikis/turncall#53,
  which resolves a finished call's config from the inline config when there is no
  agent row. The envelope now arrives, with a null `agent_id`, as this bullet
  originally claimed.
- **Twilio voice could not run an inline agent.** ~~`media_stream.py` builds its
  `CallContext` with `agent_id=UUID(agent_id_str)`, where `agent_id_str` is the
  literal string `"dynamic"` when no agent was resolved by id.~~ **Resolved** in
  kobikis/turncall#52 and #53. It took two goes: the first fixed the
  `CallContext` line and missed a *log* line in `inbound_voice_webhook` that
  dereferenced the same absent agent and crashed the webhook outright. The
  lesson, recorded because it will recur: the inline path needed sweeping for
  every `agent` dereference, not just the one in the traceback. In-call
  integrations now reach all three voice transports.
- **An inline agent has no knowledge-base links.** TurnCall reads KBs off the
  agent row, so an agent with attached apps answered every call without its KB
  (call 1f3438e8: "we don't serve sushi" with a sushi menu linked). Since
  kobikis/turncall#260 an inline config names its own in `knowledge_bases`, and
  `call-config` copies the agent's links into it. Best-effort, like the rest of
  this path: a KB read that fails costs the call its KB, never its credentials.

## Status

Accepted and implemented (#27). The two engine defects this ADR recorded as open
are fixed; see the corrections above.
