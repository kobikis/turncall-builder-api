# Call-init support (customer-writes-endpoint)

Use case: many phone numbers; per call, select the agent and inject properties
based on the dialed/caller number. TurnCall's **call-init** covers this — a number
bound with `routing_target_type: "webhook"` + `server_url` makes TurnCall POST a
call-init event to that URL on each inbound call; the response chooses the agent
and supplies template variables. The customer writes the endpoint (option A);
the builder's job is to support it.

Decisions:

- **Return `agent_id`, not inline config.** `call_init_resolver` loads the agent
  by id with no project filter, so an endpoint can reference any agent by id
  regardless of per-agent projects (corrects ADR-0006). Inline `agent` config
  remains an option but isn't required.
- **One endpoint (and secret) per number.** Each bound number gets its own
  `server_url` and its own `server_url_secret`, so each endpoint verifies against
  a single secret — no `number → secret` map.
- **Signature verification is supported (not skipped).** TurnCall HMAC-signs each
  call-init request (`X-TurnCall-Signature: v1=<hex>` over `"{timestamp}.{body}"`,
  same scheme as webhooks). The endpoint must verify it — call-init decides which
  agent answers a real call, so an unauthenticated hook is a real hole.
- **The builder surfaces the secret.** TurnCall must return `server_url_secret` on
  bind (small TurnCall change — `PhoneNumberResponse` currently omits it); the
  builder mirrors it in `phone_numbers` and shows it on the number's detail/edit
  page so the customer can paste it into that number's endpoint.

Call-init payload → endpoint: `phoneNumber.number` (dialed), `customer.number`
(caller), `call.{id,provider_call_id,type}`. Response ← endpoint: `agent_id`,
`variables` (template vars), `metadata`, `dynamic_data.knowledge_context`.

Known gap: events for call-init calls land in the shared webhook project, not the
selected agent's backend (the call runs on the webhook number). Tools still work
(their URLs are in the resolved agent's config). Revisit if per-agent event
capture is needed for these calls.

Amended by ADR-0008: **Managed call-init** binds the number in the agent's own
project with the agent's backend serving the endpoint, which closes the known
gap for that mode. This ADR's customer-written mode ("Custom call-init") and the
shared webhook project remain as described.
