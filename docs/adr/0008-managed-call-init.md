# Managed call-init: the Agent Backend serves the endpoint, bound in the agent's project

ADR-0007 supports call-init with a customer-written endpoint (Custom call-init).
That leaves the core scenario — "an agent that loads caller info into context at
call start" — ending at "now go write and host a server". This ADR adds
**Managed call-init**: the generated Agent Backend itself serves `POST
/call-init`, and the console binds a Number to it in one step ("Agent + caller
info" routing).

Decisions:

- **The Agent Backend serves `/call-init`.** Same stub philosophy as tools: the
  scaffold's endpoint verifies the signature, returns the agent's own id (baked
  as `AGENT_ID` env) and a `knowledge_context` built from the caller number,
  with the CRM-lookup site marked for the user to fill in. `knowledge_context`
  is chosen as the carrier because TurnCall prepends it to the system prompt
  automatically — no template variables, so the Composer and the trimmed config
  surface (ADR-0003) stay untouched.
- **The Number is bound in the agent's own project**, not the shared webhook
  project. TurnCall's call-init resolution is not project-scoped (ADR-0007), but
  event subscriptions are — binding in the agent's project makes the call's
  events flow through the agent's existing subscription to its backend, closing
  ADR-0007's known gap for this mode. Custom call-init keeps the shared webhook
  project.
- **Secrets live in a gitignored `.env`**, not `docker-compose.yml`. The
  `server_url_secret` only exists after bind, so the builder writes
  `CALL_INIT_SECRET` into the backend's `.env` and recreates the container.
  Moving `WEBHOOK_SECRET` there too takes secrets out of the pushable repo and
  removes the preserve-compose-on-regen special case.
- **Verification fails closed.** An empty expected secret rejects (401) instead
  of accepting all (the old dev default), for `/events`, `/call-init`, and
  signed tool calls alike. The builder records the backend as Degraded with a
  reason, and the console badges it — a failed registration must look like a
  failure, not like "no calls yet".

Trade-offs accepted: a bind now restarts the agent's container (seconds, dev
tool); `_bind` grows a third routing case; the same physical number can't split
managed and custom modes.
