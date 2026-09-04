# Management console over per-agent projects

The builder-web grows from a single composer flow into a Vapi-style management
Console: sidebar with **Agents** (list → detail → edit) and **Phone Numbers**
(list → add → edit), with the Composer as the "new agent" path. This layers on
the per-agent-project model (ADR-0005) rather than reverting to a shared project.

Key decisions:

- **Registries are the source of truth, not TurnCall queries.** The Agents list
  reads `agent_backends`; the Phone Numbers list reads a new `phone_numbers`
  mirror table. Listing is one local query instead of an N-project fan-out.
- **All TurnCall mutations use the agent's stored per-agent key** (`agent_backends.api_key`).
  There is no cross-agent admin key — each agent lives in its own project.
- **Edit = plain overwrite (`PUT`), regenerate the Agent Backend iff `custom_tools`
  changed.** No publish/versions/rollback UI in V1. Keeps config and backend in
  sync without a versioning surface.
- **Phone numbers are entered manually** (Twilio PN SID + E.164 + target agent +
  SMS toggle); the builder binds via TurnCall, which configures the Twilio webhook.
  No Twilio SDK / credentials in the builder.
- **Re-pointing a number to another agent moves it across projects** (unbind in the
  old project, rebind in the new), done transparently behind one "Update".

**Number routing type.** The Add-Number form offers `agent` (route to a chosen
Agent, bound in that agent's project), `webhook` (call-init: dynamic per-call
resolution via a `server_url`), or `none` (unassigned). Webhook-routed numbers
live in a **shared builder-owned "webhook" project**.

_Correction (2026-07-04):_ an earlier version of this ADR claimed a call-init
endpoint could not return `agent_id` for a per-agent-project agent (cross-project)
and had to return inline `agent` config. That is **wrong**: `call_init_resolver`
loads the agent via `get_agent_by_id(session, id)` with **no project scope**, so a
call-init response may return `agent_id` for any agent regardless of project. See
ADR-0007.

Trade-offs / consequences: no cross-agent A/B weighting on a single number (needs
multiple agents per project); the `phone_numbers` mirror can drift only if numbers
are changed outside the builder (it is the sole writer). Frontend uses
`react-router-dom` for real routes (deep-linkable list/detail/edit).
