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
                  "summary_arg": "text"}},   // from the catalog, not the form
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

**The Workspace names a destination, never a prop.** An end-of-call Action has to
put the summary in one of its own inputs — Slack's is `text` — so `summary_arg` is
required in the stored config and an empty one is a 422. But it is the same
string for every Workspace that ever picks that Action, so it is a fact about the
Action, not a question for a person. It lives in `CATALOG` beside the pinned tool
list and the Console fills it in.

An earlier draft made it a required field on the form. It was defensible —
guessing it would run the action with an empty body and report success, the
failure shape this ADR keeps refusing to ship — and it was still wrong: not
guessing does not require making someone else guess. Asking a person to type a
constant is not configuration, it is an implementation detail that leaked into a
form. The same reasoning retires the free-form argument editor: an Action's
inputs are named and labelled in the catalog, so the Console asks for a Channel
rather than for a key and a value.

**The end-of-call menu is not the mid-call tool list.** `tools` is the allowlist
the model may call; `actions` is what is worth running once a call is over, and an
app may have one and not the other. `slack_v2-list-channels` is useful mid-call
and meaningless afterwards. Google Calendar offers no end-of-call Action at all:
`create-event` needs a start and an end, and a `call.ended` payload says when a
*call* finished, not when a meeting should be. Offering it would configure
cleanly and fail every night, which is worse than not offering it.

> **Amended (2026-09-13): Calendar gets one, and the mechanism generalizes.**
> The paragraph above is half right and drew the wrong conclusion from it. A
> `call.ended` payload cannot say when a *meeting* should be — still true — but
> it says precisely when the *call* ran, and an entry for the call itself is a
> real thing to want. What was actually missing was a third kind of input.
>
> An Action's inputs now come from three places, not two: the form (`args`),
> the summary (`summary_arg`), and the call (`derived` — prop name to a format
> template over the payload, e.g. `eventStartDate` from `{started_at}`). Like
> `summary_arg`, `derived` is a fact about the Action and lives in `CATALOG`;
> the config shape, the validator and the Console are untouched, and the
> Workspace sees the same Action picker every other app has.
>
> A derived value that the call does not have skips that Automation rather than
> sending a blank: an unanswered call has no start, and an event spanning
> nothing is wrong, not merely empty.
>
> Scheduling a *follow-up* is still not offered, and this is the line that
> matters — no template over `call.ended` knows when it should be. That needs
> takeaway fields (ADR-0013) bound to inputs, which widens `derived` from a
> catalog constant to a config-time reference. Separate change.
>
> This also discharges the consequence flagged below: the summary wrapper was
> Slack-flavoured, and a calendar description renders `*bold*` as punctuation.
> `summary_style` moved the markup behind the Action, as predicted.

**A bare slug still means `mid_call`.** Existing agents carry `["slack_v2"]` and
must keep working untouched; the reader widens rather than migrating. An
end-of-call trigger is something a Workspace opts into, never something an
upgrade turns on underneath it.

**The Builder LLM still never writes `integrations`.** `app/builder.py` has no
schema for the field, and gains none. Attachment is a human act through the
Console, and a prompt that asks for Slack cannot produce one.

**Connecting lives on its own Console page; choosing triggers stays with the
agent.** `/integrations` is where a Workspace authorizes an app and revokes it —
work that had no Console surface at all, and the landing page the callback
already redirects to. Which agent uses an app, and when it runs, is written into
that agent's config, so it belongs on the agent's Configuration tab next to
everything else that config holds. An earlier draft of this ADR put both on
`/integrations`; that would have needed an agent picker on a Workspace page to
edit per-agent state, which is a worse page and a worse seam.

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

**This ADR increases that dependency, deliberately.** An end-of-call Action needs
a label, the input its summary rides in, and its other inputs named and labelled —
none of which the codebase could previously discover. All of it is now curated in
`CATALOG` beside the pinned tool list. Adding a third app was a tuple; it is now a
tuple plus an entry per Action.

That is the trade, taken with open eyes: the alternative was leaving the
knowledge with the person configuring the agent, which is what the earlier draft
did, and it produced a form asking for a Pipedream prop name. Curating it is more
work per app and no work at all for the Workspace. It is also the same *kind* of
work the tool allowlist already demands, so it adds no new obligation — and when
introspection arrives it replaces the curation without changing the config or the
form.

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

> **Update: one slice of this was taken — see ADR-0022.** An input's *values* are
> now fetched live per connected account, because they are the one part of the
> catalog that is nobody's to curate. The round trip this paragraph predicted
> exists. Everything else here stands: the tool allowlist, the Action menu and
> the form's fields are still curated, and `CATALOG` is still the gate.

ADR-0017's raw `mcp_servers` form remains the escape hatch for anything the
catalog does not carry, unchanged by this ADR.

## Consequences

The automation stops being a constant, so the generator needs the integrations
list: `render()` takes it, and the block is emitted per `call_ended` entry with
its action and bound arguments, or not at all.

**An Automation is delivered at least once and runs at most once.** TurnCall
retries a `call.ended` it did not get an answer to in time, and a third-party
Action is a round trip long enough that the first attempt timed out while the
action was still running — so the retry ran it again, and a second calendar
entry for one call is not a cosmetic fault. The generated handler therefore
answers *before* running the Automation, and keys on the envelope's `event_id`,
which TurnCall documents as stable across retries. Backgrounding makes a
redelivery unlikely; honouring the key is what makes one harmless. The event
itself is still stored on every delivery — a redelivery did happen, and the log
should say so.

The message is assembled by the generated handler; the summary inside it is the
agent's. TurnCall sends a written summary in the `call.ended` payload and the
handler wraps it with who called whom, how long, and why it ended. Neither half
is configured — what varies between Workspaces is where the message goes, not
what it says.

That wrapper is currently Slack-flavoured (`*bold*`, `_italic_`), which is
invisible today because Slack has the only end-of-call Action. The moment a
second one arrives it will render as literal punctuation, and the formatting will
have to move behind the Action rather than sitting in one shared function.
*(Done, with the amendment above: `CatalogAction.summary_style`.)*

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

`ConnectedApps.tsx` goes from `apps: string[]` to the object form — one card per
app, with two controls that are deliberately not symmetric: a single *available
during calls* toggle, with the app's pinned tool list shown read-only beneath it
so the Workspace can see what the model gets; and a *when a call ends* Action
picker, labelled from the catalog, followed by that Action's own inputs as
labelled fields. An app with no end-of-call Action says so in words rather than
showing an empty dropdown.

## Sequencing

The config reader, the `mapper.py` filter and the regeneration condition are one
shippable slice: it closes the leak and makes edits take effect, with no Console
work. The scaffold and `call-config` follow. The Console page lands last, against
a settled API — except that it is also the only way to connect an app without
curl, which is an argument for pulling it forward.
