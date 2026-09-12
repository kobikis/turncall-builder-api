# An Agent Backend authenticates to builder-api with its own per-Agent token

ADR-0018 decided that the Agent Backend asks builder-api for a Pipedream access
token rather than holding a Pipedream credential of its own, and deliberately
left the authentication *scheme* open. This is that decision.

## Decision

**A per-Agent-Backend bearer token, distinct from every secret the backend
already holds.** `agent_backends.builder_token` is generated when the registry
row is created, written into the backend's gitignored `.env` as `BUILDER_TOKEN`,
and presented as `Authorization: Bearer <token>` on `POST /integrations/token`.
The token *is* the identity — a unique index makes the lookup resolve to exactly
one Agent — so the backend never names a Workspace and cannot ask for another
one's token.

**Not the `webhook_secret`.** That was the obvious reuse and it is the wrong one.
The webhook secret is shared with TurnCall, which signs its requests *into* the
backend with it. Making it an outbound credential would mean TurnCall could mint
Pipedream tokens for every Workspace it serves. Different direction, different
trust, different secret.

**Not mTLS or a signed request.** The backend and the builder are both ours and
both inside the operator's network; HMAC-signing this request would buy replay
resistance against an attacker who can already read the token out of the `.env`
that holds it. The webhook path signs because TurnCall is a *different* system
reaching in over a public URL — that asymmetry is the reason, not a house style.

**The response carries the tenant, not just the token.** Pipedream access tokens
are project-wide; what scopes a request to one tenant is `external_user_id`. So
the mint response returns the Workspace id alongside the token, with the project
id and environment. None of those is a secret, and the Pipedream client secret is
never part of any response.

**Reused until it expires.** Tokens live an hour. The builder caches per
credential and the generated backend caches what it is handed — a call is not the
unit of anything here, and minting per call would turn every conversation into an
extra round trip to Pipedream.

## Consequences

- **An existing Agent Backend cannot mint until its `.env` is rewritten.**
  Migration 0010 backfills the column, but ADR-0008 keeps regeneration from
  clobbering a live `.env` (it may carry a bind-time `CALL_INIT_SECRET`), so the
  value does not reach an already-generated backend on its own. Recreate the
  agent, or add the line by hand. Accepted: integrations are new, so an existing
  backend has nothing to lose yet.
- **The backend gains a runtime dependency on the control plane**, as ADR-0018
  already accepted. The event path is untouched — TurnCall still POSTs
  `call.ended` straight to the backend.
- **`BUILDER_URL` must actually be reachable from the agent container.** The dev
  compose binds builder-api to `127.0.0.1:8000`, which a sibling container cannot
  reach through `host.docker.internal`. Local minting therefore needs the builder
  published beyond loopback; in a deployment where the builder has a real address
  this is a non-issue. Not fixed here — changing that binding is an ADR-0002
  decision about an unauthenticated control plane, not a side effect of this one.
- **Rotating a token means regenerating the backend.** There is no rotation
  endpoint. Revocation is deleting the agent, which is what revocation of an
  Agent-scoped credential should mean.

## Status

Accepted. Implemented alongside #25.
