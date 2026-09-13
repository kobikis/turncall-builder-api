---
name: connected-apps
description: Use whenever the agent will lean on a Connected app — Google Calendar or Slack — to book, schedule, invite a caller, check availability or post a summary. Covers what the system_prompt must actually say for the integration to work on a live call.
---

# Connected apps

A Connected app is authorized once by a workspace admin and attached per agent.
You cannot attach one. Put its slug in `needs_integrations`, write the
system_prompt as though it is attached, and never author a `custom_tool` for
work one of these apps does — a custom tool has no credentials, so it can only
pretend to succeed.

Everything below is for **you**. Never copy a table, a backtick or a bullet into
the system_prompt: every word of that is spoken aloud.

## Name the tool exactly

The agent sees Pipedream's own tool names, prefix included —
`google_calendar-create-event`, not `create-event`. Write the full name in the
system_prompt. A prompt that names a tool the agent does not have is a prompt
that makes it improvise.

## What the agent already knows

At the start of every call it is given the current date, time and timezone, and
the caller's number. A prompt that says "work the date out from the current
date you were given, and never guess" is the difference between a booking
tonight and a booking in 2023.

## Google Calendar

`google_calendar-create-event` is the one that writes. The prompt has to settle
what the agent cannot know on its own:

| It needs | Say in the prompt |
|---|---|
| An end time | How long this kind of booking lasts. The tool takes a start AND an end; without a stated duration the agent invents one. |
| A timezone | Name the business's timezone and tell the agent to pass it explicitly rather than relying on the calendar default. |
| A title | What goes in it, in order: the business name, then who it is for. "Nonna — Rossi, 4 guests" reads correctly in a calendar list where every event belongs to the same business. |
| A location | The business's street address, when the user has given one. Ask for it — a calendar entry with no address is one the staff cannot act on and the guest cannot navigate to. |
| A description | The details that do not fit a title: the caller's phone number, allergies, what they asked for. |

Timestamps are RFC3339 with an offset — `2026-09-13T19:00:00+03:00`. Tell the
agent to build them from the date it was given.

Use `google_calendar-query-free-busy-calendars` before booking when the user
cares about double-booking, and `google_calendar-list-events` when the caller
asks what is already scheduled.

### Inviting the caller

When the agent should put the meeting in the *caller's* calendar too, three
things have to be in the prompt, and leaving out the third is silent:

1. **Ask for an email address** — and only when the caller wants an invitation.
   A phone number cannot be invited. Offer it ("would you like a calendar
   invitation?"), do not demand it.
2. **Pass the address as an attendee** on `google_calendar-create-event`.
3. **Set send updates to "all"**, so Google actually emails the invitation.
   Left unset, the attendee is added and no one is ever told.

If the caller declines or has no email, create the event anyway, on the
business's calendar, with their name and number in the description. The booking
is the point; the invitation is a courtesy.

Add a Google Meet room only for a call or video meeting — never for something
the caller is turning up to in person.

## Slack

Mid-call, `slack_v2-post-message` lets the agent tell a channel something while
the caller is still on the line — an escalation, an urgent booking. Say which
channel and what warrants it, or the agent will post on every call.

Posting a **summary when the call ends** is not yours to write. It is an
end-of-call Automation the workspace turns on per agent, and it composes the
summary itself. Do not put "post a summary to Slack at the end" in the
system_prompt: the agent cannot do it, and the workspace already can.
