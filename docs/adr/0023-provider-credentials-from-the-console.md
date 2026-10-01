# Provider credentials from the Console

ADR-0002 built this as an operator's tool, where provider keys were the server
operator's business and nothing else. The Console said so in three places — *"the
browser only holds identity, never a provider credential"*, *"credentials are
configured on the TurnCall server, never here"* — and CONTEXT.md said credentials
were outside the config surface *permanently*.

ADR-0011 then made the Builder multi-tenant, and those two positions stopped
being compatible: one set of keys in the operator's environment cannot serve
several [[Workspace]]s. This records the reversal and its exact extent.

## What changes

A Workspace admin connects provider accounts on one page of cards:
[[Integration]]s. A key is entered once, encrypted here, validated against that
provider's own list endpoint, and projected into TurnCall as a Project credential
(turncall ADR-0020). Editors see which providers are connected — they need it, so
the agent form can flag a provider with no key — but cannot enter or reveal one.

## The principle being reversed, precisely

The Console becomes a credential **entry** surface. It is still not a credential
**storage** surface: the field is write-only, the value is never returned, and a
card afterwards shows presence and a last-four. The three comments asserting
otherwise are corrected rather than deleted, because the distinction they were
protecting is the one still being kept.

## Why not leave keys in the server's environment

That is today's behaviour and the cheap answer. It fails on tenancy: one `.env`
behind N workspaces means every workspace spends the operator's accounts, which
the Members, Roles and invite flows say is not the product.

It also fails on an axis nobody expects. The Builder reads provider keys from
*its own* environment to populate model and voice lists (`provider_catalog.py`),
while the engine reads a different set to run the call. Two key sets, silently
divergent, and no surface that can tell them apart — a console saying "connected"
about a key that lists models says nothing about the key answering the phone. One
Integration now feeds both, and `GET /v1/providers` on the engine reports its own
platform key separately so a card can name which key will run the call.

## Why not per-role cards

A dashboard that lists Deepgram under Voice and again under Transcription is a
data model that stores two Deepgram keys that can disagree, and the first
support question is why transcription 401s while voice works. Cards are grouped
by role for browsing; the credential is keyed by provider account.

## Not in this cut

**Cloud providers.** TurnCall's storage backend is platform-wide, its S3 auth is
boto3's ambient chain, and `ProjectRow` has nowhere to hang a per-project bucket.
A Cloud card needs all of that before it can mean anything, and a card that means
nothing is worse than an absent one.

**Tools as a card category.** [[Connected app]]s (ADR-0018) already are the
account-level tool integration, with better credentials than this could offer —
minted per call, expiring within the hour. What this adds instead is the
[[Workspace MCP server]]: a registry, not a credential store, and opt-in per
agent because TurnCall connects every configured server at call start under one
10s discovery budget, past which the call proceeds with *no* MCP tools at all.

## Status

Accepted; implemented (#54-#59). `/integrations/providers` (cards, connect,
apply, disconnect, adopt) and `/integrations/mcp-servers`; storage in
`provider_integrations`, `integration_projections` and `workspace_mcp_servers`
(migration 0012), encrypted under `INTEGRATIONS_ENCRYPTION_KEY`. Two details the
build settled: the account namespace is read from TurnCall's `GET /v1/providers`
rather than copied here, and the picker cache is keyed by a fingerprint of the
key that fetched it, which is what invalidates a Workspace's lists the moment
its Integration changes. The shared webhook-numbers project is not a projection
target — it is one project behind every Workspace, so a call routed through it
still runs on the platform key. Withdrawable in the sense that the page can be
removed — but not cheaply, since withdrawing it means an operator re-entering
every workspace's keys into an environment file.
