# The user chooses when an integration runs

ADR-0018 gave a Workspace two ways to spend a Connected app, and #28 built the
second one: attaching an app emits an `mcp_servers` entry the model may call
during a call, and a `call.ended` event runs an Automation in the agent's own
backend. Both work. Neither is chosen.

Today the timing is a consequence of the mechanism rather than a decision
anyone makes:

- **Attaching an app forces in-call exposure.** `mapper.py` turns every entry of
  `config.integrations` into an `mcp_servers` entry with a pinned tool filter. A
  Workspace that wants nothing but an end-of-call summary still hands the model
  `slack_v2-post-message` to call mid-call, and has no way to decline it.
- **The Automation is not configuration.** `render_app_py()` takes tools and tool
  bodies; the automation block is a constant in the template. Every generated
  backend gets the same hardcoded `slack_v2-post-message` handler with
  `SLACK_CHANNEL = ""`, switched on by hand-editing the generated file — whether
  or not Slack is attached, and with no equivalent for Google Calendar.
- **Connecting an app has no Console surface at all.** There is no `/integrations`
  route in the Console and no caller of `POST /integrations/connect`. The
  authorization flow is reachable only by hand, and its callback redirects the
  browser to `{PUBLIC_BASE_URL}/integrations` — a route that does not exist.

## Decision

**An attached app carries its triggers.** `config.integrations` becomes a list of
objects rather than slugs:

```json
"integrations": [
  {"app": "slack_v2",
   "mid_call": true,
   "call_ended": {"action": "slack_v2-post-message",
                  "args": {"channel": "#calls"},
                  "summary_arg": "text"}},
  {"app": "google_calendar", "mid_call": true}
]
```

`mid_call` emits the `mcp_servers` entry. `call_ended` generates the automation.
Both may be set on one app: posting mid-call and summarizing afterwards are
different jobs, and forcing a choice between them would be an artifact of how the
two are built, not something a Workspace should have to think about. The same
action may appear on both sides — `slack_v2-post-message` can sit in the model's
menu *and* be the end-of-call action, with the end-of-call copy carrying a fixed
channel the model never sees.

**The two halves do not share a granularity, and should not be made to.** They
are asking different questions:

- Mid-call asks *which tools may the model choose from*. That is a set, with no
  arguments — the model supplies those at runtime.
- End-of-call asks *which one action runs, and with what arguments*. Nothing
  chooses at runtime, so both must be decided up front.

So `mid_call` is a boolean over the app, and `call_ended` names an action and
binds its arguments. An earlier draft of this ADR gave both a symmetric
`triggers: [...]` list with a bare `{"channel": ...}` alongside. That only read
correctly because the scaffold hardcodes `slack_v2-post-message`; it has no
meaning for Google Calendar, where an end-of-call action would be a different
action taking entirely different arguments.

**Mid-call means exposure, not scheduling, and stays app-level.** The model calls
the tool if it decides to; there is no guaranteed mid-call trigger and this ADR
does not add one. What changes is that the exposure is now opted into. An
end-of-call-only attachment emits no `mcp_servers` entry, which is the leak this
ADR exists to close.

Per-tool mid-call selection is deliberately not part of this. `tool_filter` is
the app's pinned `CATALOG` list, and that pinning is load-bearing: Pipedream
generates one tool per registry action (50 for Slack, 17 for Calendar) against
TurnCall's caps of 50 per server and 100 per agent, and every advertised tool
rides in every LLM request for the length of the call. Letting a Workspace narrow
that list is safe but buys little, and it complicates the cap reasoning for no
stated need. It remains available as a separate change.

**`summary_arg` names where the summary goes, and is required.** The handler
composes the text from the `call.ended` payload — that is derived, not configured,
because what varies between Workspaces is where a summary goes rather than what
it says. But the text has to land in one of the action's own inputs, and those
are the action's to name: Slack calls it `text`, and nothing says the next catalog
app will agree. Defaulting it would mean guessing, and a wrong guess runs the
action with an empty body and reports success — the failure shape this ADR keeps
refusing to ship. Required, it is a 422 instead.

**A bare slug still means `mid_call`.** Existing agents carry `["slack_v2"]` and
must keep working untouched; the reader widens rather than migrating. An
end-of-call trigger is something a Workspace opts into, never something an
upgrade turns on underneath it.

**The Builder LLM still never writes `integrations`.** `app/builder.py` has no
schema for the field, and gains none. Attachment is a human act through the
Console, and a prompt that asks for Slack cannot produce one.

**Selection lives on its own Console page.** Connecting an app, disconnecting it,
and choosing its triggers belong together on `/integrations` — which is also the
landing page the callback already redirects to.

## This does not reopen #28

#28 argued that an Automation runs because an event arrived, never because a
model chose to call something, and that routing a delivery guarantee through tool
selection turns it into an outcome. That argument survives intact: the objection
was to the *model* choosing, and nothing here lets it. A Workspace choosing which
apps summarize its calls is not tool selection.

`ConnectedApps.tsx` carries a second reason — Automations are "deliberately not a
Console feature: it is a handler in the agent's own backend, because nothing about
it has to reach TurnCall." That architecture also survives. `call_ended`
configuration never enters `to_create_agent_request()`; only `mid_call` reaches
TurnCall. The end-of-call half stays builder-side and drives code generation.

What goes is the hardcoding, not the design.

## Generality

The two-app catalog is inherited from ADR-0018, not decided here, but this ADR
leans on it harder than its predecessor did and the boundary is worth stating.

ADR-0018 gives two reasons for a curated catalog. One is demand — "a third app is
added when a customer asks for one, not when we guess". The other is structural:
`tool_filter` is mandatory because Pipedream generates one MCP tool per registry
action, 50 for Slack against TurnCall's cap of 50 per server, so every catalog app
needs a hand-picked allowlist. ADR-0018 notes that number was an estimate when
written and is now a measurement, and that it landing *on* the cap rather than
near it is the only reason the bullet is a rule.

**The ceiling is not the count, it is the absence of a lookup.** The code runs
Pipedream actions — `/v1/connect/{project_id}/actions/run` — but never asks what
actions an app has or what arguments they take. Every such fact lives in the
`CATALOG` tuple. There is no path from "a Workspace connected an app" to "here is
what it can do" that does not go through someone editing Python.

**This ADR increases that dependency.** The end-of-call action is chosen from the
app's pinned tool list, and its arguments need per-action knowledge that exists
nowhere in the codebase: `{"channel": ...}` works only because the scaffold
hardcodes `slack_v2-post-message`. Adding a third app was a tuple; under this ADR
it is a tuple plus a hand-written argument form.

**Decision: keep the catalog curated, and keep the code introspection-ready.**
The action and its arguments travel as opaque data from config to generated call
— no Slack-shaped field in the mapper, the scaffold, the API, or the Console's
data model. The config shape already does this: `args` is an open dict, not a
`channel`. Generalizing later must be a new *source* for the tool list and a new
form renderer, never a re-cut of the config.

**Deferred: introspection-driven generality.** Fetching an app's actions and their
prop schemas at connect time would let a Workspace pick its own allowlist under
the caps and render the end-of-call form from the action's schema, demoting
`CATALOG` from a gate to a default. Not now, and not blocked by anything here. Two
things to check first: Pipedream's prop configuration is not purely static —
options for a field like "which channel" are fetched per connected account at
configure time, so the form needs round-trips rather than a schema render — and
the endpoint shapes have not been verified against current Pipedream docs.

ADR-0017's raw `mcp_servers` form remains the escape hatch for anything the
catalog does not carry, unchanged by this ADR.

## Consequences

The automation stops being a constant, so the generator needs the integrations
list: `render()` takes it, and the block is emitted per `call_ended` entry with
its action and bound arguments, or not at all. The generated handler still builds
the summary text itself — that is derived from the `call.ended` payload, not
configured — so `args` supplies everything else the action needs.

**Regeneration has to widen, or every automation edit is silently a no-op.**
`PUT /agents/{agent_id}` regenerates the backend *iff `custom_tools` changed*
(ADR-0004, and `agents.py` says so in one line). A channel edit or a newly added
`call_ended` trigger changes only generated code, so under today's condition it
would be accepted, stored, and never reach the container. This is the trap in the
whole change: everything else fails loudly, and this one fails by appearing to
work.

`POST /integrations/call-config` should gate on mid-call apps rather than any
attached app. An end-of-call-only agent has nothing to inline, and an inline
agent costs the call its `active_agent_id` (ADR-0020).

`ConnectedApps.tsx` goes from `apps: string[]` to the object form — one row per
app, with two controls that are deliberately not symmetric: a single *available
during calls* toggle, with the app's pinned tool list shown read-only beneath it
so the Workspace can see what the model gets; and a *when a call ends* section
with an action dropdown over that same tool list, plus argument fields for
whichever action is chosen.

## Sequencing

The config reader, the `mapper.py` filter and the regeneration condition are one
shippable slice: it closes the leak and makes edits take effect, with no Console
work. The scaffold and `call-config` follow. The Console page lands last, against
a settled API — except that it is also the only way to connect an app without
curl, which is an argument for pulling it forward.
