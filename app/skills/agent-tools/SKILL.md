---
name: agent-tools
description: Use when the agent must DO something outside the conversation — book, cancel, look up an order, check availability — or hand the call to a human. Covers authoring custom tools and both transfer patterns.
---

# Agent tools

## Actions the agent performs

**First: is a Connected app already doing this?** Putting a calendar event on
Google Calendar, posting to Slack — those have real integrations, and a custom
tool written beside one has no credentials, so it can only pretend to succeed
while competing with the real tool for the model's attention. That is not
hypothetical: a `schedule_google_calendar_event` tool authored from this page
sat next to the Calendar integration and won. Open the connected-apps skill
instead, and record the requirement in `needs_integrations`.

Otherwise, when the agent must perform an external action (book, cancel, look up
an order), author a custom_tool for it: snake_case name, description,
parameters_schema.

Don't ask for a URL — the builder generates a backend and wires it. If the user
VOLUNTEERS their own API URL, set `server_url` (agent-wide base) or a tool's
`server_url` (full URL), but never ask.

Name tools in the user's own vocabulary. `add_covers`, `cancel_booking` and
`reschedule_job` belong to a business; `create_record` does not.

## Handing the call to a human

Ask ONE question: is the transfer number fixed (and what is it), or does it
depend on runtime logic — who's on call, the caller, the topic?

**Fixed.** Enable `transfer_call`, and write the number and the
when-to-transfer conditions into the system_prompt.

**Depends.** Also author a `get_transfer_number` custom_tool — description
"Returns the phone number of the human to transfer to right now", no parameters
unless the routing needs them — and instruct the system_prompt to call
`get_transfer_number` first, then `transfer_call` with the number it returns.

Either way the system_prompt should tell the agent to announce the transfer
(`transfer_message`) and what to say if the human doesn't answer.
