# MCP servers come from the Console, never from the interview

ADR-0014 noted that MCP was "considered alongside this and dropped for now",
and that "exposing `mcp_servers` in the Console is a separate decision that has
nothing to do with this one". ADR-0015 left the mechanics for whoever picked it
up: `mapper.py` is an allowlist, so `mcp_servers` in a config was silently
dropped at create *and* apply.

This is that decision. `mcp_servers` now passes through the mapper to TurnCall.
It is deliberately **not** in the compose schema.

## Why the split

TurnCall's whole integrations story runs through MCP — a Slack message, a
calendar booking, a CRM lookup. Everything the Builder can express reaches
those via `custom_tools` and a generated backend; anything a customer already
runs as an MCP server was unreachable. That is the gap worth closing.

The interview is a different matter. A composer that turns a conversation into
a config would have to produce two things it has no business producing:

- **An outbound URL.** The agent's MCP servers are targets TurnCall connects
  out to, from inside the network. A model inferring `https://mcp.acme.com/mcp`
  from "we use Acme" is guessing at an SSRF-shaped surface. TurnCall gates
  these against `BYOM_ALLOWED_URL_PATTERNS`, but an operator who has not set
  that gate is relying on nobody writing a bad URL, and a language model is a
  poor choice of nobody.
- **A bearer token.** Authenticated MCP servers carry one in `headers`. Getting
  it from the interview means the customer types a live credential into a chat
  transcript, which is stored, replayed into later turns, and pushed to GitHub.
  Redaction covers the push (`headers` and `env` are in `_SECRET_KEYS`); it does
  not un-store the transcript.

Neither problem exists when the Console writes the field: the URL is typed by
someone who knows it, and the token goes in once, through the same path as
every other credential.

This is the shape `analysis` already has — the Console manages takeaway
attachments and they pass through untrimmed — so it is a precedent rather than
a new idea.

## stdio is refused

`transport: "stdio"` runs a subprocess on the TurnCall host. TurnCall gates it
behind `MCP_STDIO_ENABLED` and an allowed-commands list, so nothing here is the
last line of defence — but a Builder-authored config should not be the thing
asking for local process execution. Only `http` and `sse` are forwarded; a
stdio entry is dropped and does not take the rest of the list with it.

## What this does not include

A Console UI. `turncall-builder-web` has no MCP surface yet, so today this is
reachable by PUTting a config that carries `mcp_servers`. The field now
survives that round trip, which is the part that had to be true first.

## Status

Accepted and implemented in `mapper.py`. Reverting means deleting the block;
nothing in Postgres changes, and an agent whose stored config carries
`mcp_servers` simply stops sending it.
