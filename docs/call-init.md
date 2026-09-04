# Call-init: loading caller info per call

Dynamically pick the agent and inject properties per call, based on the phone
number. Two modes (ADR-0007, ADR-0008):

## Managed call-init (no code to host)

Bind the number as **Agent + caller info** in the console. The agent's own
generated backend serves `POST /call-init`: it returns the agent's id plus a
`knowledge_context` built from the caller's number, which TurnCall prepends to
the system prompt before the conversation starts. The builder wires the
signing secret into the backend's `.env` automatically.

The lookup is a stub — edit the `/call-init` handler in the generated repo
(`turncall-agent-<slug>/app.py`) to query your CRM and return real caller info.
Events for these calls flow to the same backend, so they appear in the
console's Events pane.

## Custom call-init (you host the endpoint)

Bind the number in the console as **Custom call-init webhook** with your
`server_url`; TurnCall POSTs to it on every inbound call. The rest of this
document describes this mode.

## Request (TurnCall → your endpoint)

`POST <server_url>` with headers `X-TurnCall-Signature`, `X-TurnCall-Timestamp`
and body:

```json
{
  "event_type": "call.init",
  "call_id": "…",
  "timestamp": "ISO-8601",
  "payload": {
    "phoneNumber": { "number": "+1555…" },   // the number that was DIALED
    "customer":    { "number": "+1444…" },   // the CALLER
    "call": { "id": "…", "provider_call_id": "CA…", "type": "inboundPhoneCall" }
  }
}
```

## Response (your endpoint → TurnCall)

```json
{
  "agent_id": "uuid-of-an-agent",            // from the console's Agents list
  "variables": { "name": "Jane", "tier": "gold" },   // {{name}} in the prompt
  "metadata": { "crm_id": "C-123" },                 // stored on the call
  "dynamic_data": { "knowledge_context": "…" }       // prepended to the prompt
}
```

`agent_id` may reference **any** agent (call-init isn't project-scoped). Inline
`agent` config also works but isn't needed.

## Verify the signature (required)

Same scheme as webhooks. Each number has one secret — copy it from the number's
page in the console.

```python
import hashlib, hmac

def verify(raw_body: str, sig: str, ts: str, secret: str) -> bool:
    expected = "v1=" + hmac.new(
        secret.encode(), f"{ts}.{raw_body}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig)
```

Reject the request if it doesn't verify — call-init decides which agent answers a
real call.

## Reachability

`server_url` must be reachable **by TurnCall's container**. Local endpoint →
`http://host.docker.internal:<port>/…`; otherwise a public HTTPS URL.
