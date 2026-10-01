# Setup: connecting Slack and Google Calendar

How to turn on **Connected apps**, so an agent can act in a workspace's Slack or
Google Calendar. Design and reasoning: [ADR-0018](./adr/0018-integrations-via-pipedream.md),
[ADR-0021](./adr/0021-user-chooses-integration-timing.md),
[ADR-0022](./adr/0022-input-values-come-from-the-provider.md).

Authorization goes through [Pipedream Connect](https://pipedream.com/docs/connect/).
The builder never renders a third party's consent screen and never sees its
tokens — it holds one encrypted Pipedream client credential, and everything a
call needs is minted for that call.

**You do not need a Slack app or a Google Cloud project of your own.** Pipedream
supplies the OAuth client for each app. What you create is a Pipedream account
and one Connect project.

**Two places a Connect project can come from.** The deployment's, configured
below in `.env` — every Workspace without its own uses it. Or a Workspace's own:
an admin connects it on **Integrations → Connected apps → Pipedream** (client
id, client secret, project id, environment), and that Workspace's apps then live
in its project, on its bill. A Workspace's project always wins. A deployment
where every Workspace brings its own needs only `PIPEDREAM_REDIRECT_URI` from
the list below — the callback is this API's, whoever owns the project — and
unsetting the rest removes the stored deployment project at the next start.

## 1. Server: environment variables (the deployment's project)

In the builder API's `.env`:

```
PIPEDREAM_CLIENT_ID=<oauth client id>
PIPEDREAM_CLIENT_SECRET=<oauth client secret>
PIPEDREAM_PROJECT_ID=proj_xxxxxxx
PIPEDREAM_REDIRECT_URI=https://<your-builder-api>/integrations/callback
PIPEDREAM_ENCRYPTION_KEY=<44-character Fernet key>
PIPEDREAM_ENVIRONMENT=development     # or production
```

The encryption key is generated the same way as the GitHub one, and is a
different key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**Set it once and leave it.** Rotating it invalidates the stored client secret,
and integrations stop working until it is re-entered. Unset, the connect flow is
signed with `INTEGRATIONS_ENCRYPTION_KEY` instead — which is also what seals a
Workspace's own project secret. Decryption fails loudly
with a message naming the key rather than returning junk.

`PIPEDREAM_ENVIRONMENT` selects which Pipedream environment connected accounts
live in. `development` is for trying it out; move to `production` before real
traffic, and note that accounts do not carry across.

### The redirect URI is the one people get wrong

It is a **builder API** route — `/integrations/callback` — not the Console's
URL. It is where Pipedream sends the admin's browser after they approve, and it
must match what Pipedream is configured to allow, character for character.

Pointing it at the Console sends the admin to a page that cannot complete the
handshake, and the connection silently never appears.

### Restart, and mean it

The builder API image **bakes its code** — `docker compose up -d --build api`.
A plain restart, or editing files on the host, does not change what the
container serves. This is the most common reason a change appears to have had
no effect.

Without these variables the Integrations page says so plainly, rather than
failing obscurely. Integrations are simply off.

## 2. Each workspace: connect an app

On the Console's **Integrations** page, an admin picks Slack or Google Calendar
and is sent to Pipedream's hosted consent screen. Coming back, the builder asks
Pipedream what now exists — Pipedream stays the system of record, and there is
no local mirror to drift.

Connecting needs the **admin** role and is workspace-wide. Choosing which agent
uses an app, and when, is a per-agent choice on that agent's Configuration tab
and needs only **editor**.

## 3. Each agent: choose when it runs

Two triggers, deliberately not symmetric (ADR-0021):

| | During a call | When a call ends |
|---|---|---|
| What it is | the app's tools, offered to the model | one action, run because the event arrived |
| Who decides | the model, at runtime | you, up front |
| Guaranteed? | no — the model may not call them | yes, once per call |
| Arguments | supplied by the model | chosen here |

An app can carry either, both, or only the second — an agent that should post a
summary and nothing else hands the model no Slack tools at all.

Values you cannot check by eye are **chosen from a list** fetched from the
workspace's own connected account: a Slack channel is picked by name and stored
by id, and so is a calendar.

## What each app offers

### Slack

Slug `slack_v2` — the registry package is versioned and `slack` is not the name
of anything current. Tools are prefixed `slack_v2-`.

- **During a call:** `post-message`, `list-channels`, `find-user-by-email`
- **When a call ends:** *Post the call summary* to a channel

The tool list is pinned to three of Slack's 50 registry actions. That pinning is
load-bearing, not tidiness: TurnCall caps MCP tools at 50 per server and 100 per
agent, and every advertised tool rides in every LLM request for the length of
the call.

### Google Calendar

Slug `google_calendar`.

- **During a call:** `create-event`, `update-event`, `list-events`, `get-event`,
  `query-free-busy-calendars`
- **When a call ends:** *Add the call to a calendar*

The end-of-call action records **the call itself** — an entry spanning it, with
the agent's summary in the description. Its start and end come from the event,
so nothing you type can improve them; you choose the calendar and the title.

Booking something *for the caller* is a different job and belongs in the system
prompt, as a tool the agent calls during the call. Asking for both gets two
events.

Scheduling a **follow-up** when a call ends is not offered: a `call.ended`
payload says when the *call* finished, not when a meeting should be.

## Adding a third app

One `CatalogApp` entry in `app/pipedream.py`. Every consumer reads `CATALOG`
generically, so nothing else changes.

The code is the small part. Each of these must be **measured against the live
API**, because every one of them defaults to wrong:

- the app **slug**, which is also the tool-name prefix (`slack`, famously, is not Slack's)
- how many actions the app has, against the 50-per-server cap
- the component's **app prop**, which is *not* the slug — Slack's is `slack`,
  Calendar's is `googleCalendar`. The wrong key runs the action with no
  credential and the third party answers a cheerful `200` having done nothing.
- for an end-of-call action: which inputs can be known before the call happens,
  and which must come from the payload
- for a picker: what its listing action returns. Calendar answers with a list,
  Slack with `{"channels": [...]}`.

## Troubleshooting

**"Integrations are not configured"** — the five variables are not in the
builder API's environment, or the container was not rebuilt after they were
added.

**The consent screen completes but nothing appears** — `PIPEDREAM_REDIRECT_URI`
does not match, or points at the Console instead of the API.

**A picker says "No … account is connected yet"** — an admin has not connected
that app *in this workspace*. Connections are per workspace, not per user.

**An end-of-call Automation does nothing.** It runs in the agent's own backend,
so look there:

```bash
docker logs turncall-agent-<slug>-<id>-... 2>&1 | grep automation
```

A refusal by the third party looks like this, and is the common case:

```
automation: google_calendar refused google_calendar-create-event (Not Found)
```

`Not Found` from Calendar almost always means a calendar **id** that does not
exist — a display name, or one character short. Pick it from the list rather
than typing it. Note that Pipedream answers `200` whether or not the action
worked, so the failure is in the run's observations, never in the status code.

**An edit to an Automation seems not to take effect** — changing one regenerates
and rebuilds that agent's backend, which takes a moment. Editing something else
does not, by design.

**The same thing happened twice** — it should not. An Automation is delivered at
least once (TurnCall retries a `call.ended` it did not get an answer to in time)
and runs at most once, keyed on the event's `event_id`. A backend generated
before that landed will not have the guard; a single edit to its Automation
regenerates it.

## Anything else

Only these two apps are in the catalog, deliberately (ADR-0018). Anything else
is reachable today as a raw MCP server through the Console's **MCP servers**
section, with credentials you manage yourself (ADR-0017).
