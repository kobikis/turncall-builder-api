# Input values come from the provider; the rest stays curated

ADR-0021 asks a Workspace for an [[Action]]'s inputs as labelled fields — a
Channel, a Calendar — rather than for a key and a value. That removed the
Pipedream prop name from the form. It did not remove the harder problem, which
is that the *value* is an opaque identifier nobody can check by eye.

A Google calendar ID one character short of the right one is accepted by the
form, stored, generated into the backend, refused by Google with a 404, logged
in a container nobody is watching, and surfaces as a calendar entry that never
appears. That is not a hypothetical: it cost three rounds of "I don't see it"
in one afternoon, against a value whose first two characters were right and
whose last one was missing.

No validation catches it. A regex for "looks like a calendar id" passes
`…google.co` as happily as `…google.com`. The only thing that distinguishes a
real identifier from a plausible one is the list of real ones.

## Decision

**A catalog input may name an Action that lists what it accepts.**

```python
CatalogInput(name="calendarId", label="Calendar",
             options_action="google_calendar-list-calendars",
             option_label="summary")
```

`GET /integrations/{app}/options/{prop}` runs it against this Workspace's
connected account and answers `[{value, label}]`. The Console renders a picker,
showing the name and storing the id.

**Its own endpoint, not part of `/catalog`.** The catalog is the same for every
Workspace and could be cached forever; which calendars exist belongs to one
Workspace and changes without telling us. Folding them together would make the
whole catalog per-tenant and per-request to serve one field.

**Two shapes, because two apps were already enough.**
`google_calendar-list-calendars` answers with a list;
`slack_v2-list-channels` answers with `{"channels": [...]}`. `option_root` names
the key when there is one. This was measured against both live accounts before
it was written, not designed for a third app that does not exist.

**Every failure raises; none answers an empty list.** An empty dropdown reads as
"you have no calendars", which is a lie the Workspace cannot act on. The Console
falls back to the text field it replaced, with the reason shown — so an app
nobody has connected, a scope the grant lacks, or a third party having a bad
morning costs the picker and never the form. The convenience may fail; the form
may not.

**A stored value the list does not contain is kept.** An archived channel, a
calendar someone stopped sharing, or a value saved before there was a picker
stays selected and labelled as no longer listed. A saved setting must not
disappear because a page rendered.

## What this does not do

ADR-0021 deferred "introspection-driven generality": fetching an app's actions
and their prop schemas so the catalog could be demoted from a gate to a default.
**This is not that, and deliberately stops short of it.**

What is discovered here is one input's *values*. Which apps exist, which tools
they expose, which Actions are worth running when a call ends, and what each
Action needs asked — all of that is still curated in `CATALOG`, still the gate,
and adding an app is still an edit to Python.

The line is where the knowledge lives. A tool allowlist is ours to choose: the
pinning is load-bearing against TurnCall's per-server tool cap, and a Workspace
cannot pick it better than we can. An input's values are nobody's to choose —
they are facts about the Workspace's own account, and curating them would mean
curating someone else's Slack.

One of ADR-0021's two stated reasons for deferring is now settled rather than
assumed: prop options really are fetched per connected account at configure
time, so the form needs a round trip rather than a schema render. That round
trip exists now. Whether the rest is worth building is unchanged, and still not
blocked by anything.

## Consequences

`run_action` and `_action_error` move into the provider module. Pipedream
answers 200 whether or not the Action worked — the error rides in the run's
observations — and reading only the status code is exactly how the original
failure stayed invisible. The generated Agent Backend carries its own copy of
`_action_error`; it runs in a different repo with no import path back, and the
duplication is deliberate rather than overlooked.

Every catalog app now wants a listing Action per opaque input. Slack's `channel`
gained one at the same time as Calendar's `calendarId`, for the same reason: a
channel name typed by hand fails in exactly the same silent way.

Adding a provider (see ADR-0018) now has one more function in its surface. That
surface is still concentrated in one module, and the engine still has no idea
who minted an `mcp_servers` entry.
