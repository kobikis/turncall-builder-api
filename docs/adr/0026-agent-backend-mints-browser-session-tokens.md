# The Agent Backend mints browser session tokens

TurnCall's ADR-0026 lets a browser join a call with a **session token** instead
of an API key: one call, five minutes, one agent, with its template variables
sealed by whoever minted it. Minting needs a server that holds a project API
key. A developer already has one. A Workspace that built its agent in the
Console, and wants a "talk to us" button on its own site, has no server of its
own except the [[Agent Backend]] we generated for it.

## Decision

**The Agent Backend serves the mint endpoint, the same way it serves
[[Managed call-init]]** (ADR-0008). `POST /webrtc-session` on the backend returns
a session token for its own Agent. The widget on the customer's page calls it,
then connects to TurnCall with the token.

**The backend does not hold a TurnCall key.** It asks builder-api for the token
with the per-backend `BUILDER_TOKEN` (ADR-0019), and builder-api mints it with
the Agent's own TurnCall key. That is the pattern ADR-0018 set for Pipedream:
the backend proves which Agent it is, and the secret that can act on TurnCall
stays here. A backend pushed to GitHub and run elsewhere (ADR-0013) carries no
key that could be used against the project.

**The endpoint is public, so it is fenced:**

- an **allowed-origins** list per Agent, set on the Console, empty by default
  (empty means the endpoint refuses every browser request);
- **per-IP rate limits** on minting;
- the template variables come from the backend's own stub, marked for the user
  to fill in from their own lookup, like call-init's caller lookup. The browser
  sends none.

An Origin header can be forged outside a browser, so these limit cost rather
than prove identity. The real bound is TurnCall's own per-project live-call
limit, which still applies.

**The Console shows the snippet.** On the agent's workbench: the allowed origins,
and the `<script>` tag for the `turncall-js` widget pointed at this backend's
mint URL.

## Consequences

- builder-api gains `POST /agents/{id}/webrtc-session`, authorized by
  `BUILDER_TOKEN` alone, which proxies TurnCall's `POST /v1/webrtc/sessions`.
- Developers who mint from their own server never touch this path.
- Follow-ups: the endpoint and scaffold here; the snippet and origins in
  turncall-builder-web.
