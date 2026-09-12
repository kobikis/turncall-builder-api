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
  null `agent_id` in its `call.ended` envelope. Tolerable here: the builder gives
  each agent its own project, so calls are still attributable, and the backend
  receiving the event knows which agent it is. It is the main reason the
  `agent_id` path is kept for agents with nothing attached.
- **Twilio voice cannot run an inline agent today.** `media_stream.py` builds its
  `CallContext` with `agent_id=UUID(agent_id_str)`, where `agent_id_str` is the
  literal string `"dynamic"` when no agent was resolved by id — which is exactly
  the inline case. WebRTC and WhatsApp voice both use a `UUID(int=0)` sentinel at
  the same place and work. This is an engine defect on a path nothing previously
  exercised, not a limit of this design, and #27's acceptance criteria say to stop
  rather than edit the engine for it. **In-call integrations therefore reach
  WebRTC and WhatsApp voice, and not Twilio voice, until that is fixed.**

## Status

Accepted. Implemented alongside #27, which stays open on the Twilio path.
