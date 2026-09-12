# Integrations via Pipedream; in-call config from the Console, post-call code in the backend

ADR-0017 let `mcp_servers` through the mapper, which made every MCP server a
customer already runs reachable. It did not give them one to run. The two cases
that keep coming up — book a meeting during a call, post a Slack summary after
one — need a catalog, somewhere to put a third-party credential, and a
deterministic post-call path. This is that decision.

## Decisions

- **Two mechanisms, not one.** An in-call integration is an ordinary TurnCall
  tool over MCP: the LLM invokes it, and may not. A post-call integration is an
  **Automation** — an Action the Agent Backend runs when `call.ended` arrives,
  deterministic and retriable. Routing "send the Slack message" through the model
  makes a delivery guarantee into a tool-selection outcome, which is not what the
  user asked for when they said "at the end of the call".

- **Pipedream Connect, not Composio.** Both hold the customer's Slack and Google
  tokens, so the choice is a custody choice. Composio disclosed a May 2026 breach
  in which ~5,241 API keys and 5,001 GitHub OAuth tokens were exfiltrated from
  their infrastructure — the exact failure mode this design hands the vendor.
  Their 2026 repricing (entry tier 200K → 50K tool calls at the same price,
  overage ~$0.25–0.30 → $4 per 1K) points the same way. Pipedream's risk is
  Workday, which acquired it in a deal closing Jan 2026; that is a migration
  cost, taken on our schedule, not a disclosure.

- **The Workspace owns the Connected app**, authorized through Pipedream's hosted
  **Connect Link**. Pipedream's `external_user_id` is the `workspace_id`, so
  tenants are isolated from the first call without building anything. A redirect
  costs no builder-web dependency and no token handling in the browser; their
  embedded SDK is polish for a page this ADR decides not to build. The
  Workspace's *own* customers connecting *their* accounts is out of scope — see
  below.

- **The builder never stores a third-party token.** Pipedream holds the Slack and
  Google credentials. builder-api holds one Pipedream client credential per
  deployment, encrypted, read back at mint time — the pattern ADR-0013 already
  established for the GitHub connection, and the same reasoning that keeps
  credentials outside the config surface.

- **The two mechanisms land in different places, and that asymmetry is the point.**
  An in-call integration must reach TurnCall — `mcp_servers` is agent config — so
  something has to write config, and per ADR-0017 that something is the Console,
  never the interview. An Automation reaches nothing: it is code the Agent Backend
  runs against an event it already receives. So it ships as a **[[Scaffold]] stub**
  — `on_call_ended()` with the Action pre-filled and the channel marked TODO — and
  gets **no Console surface at all**. This is the repo's existing shape for tools
  (ADR-0004) and call-init (ADR-0008): generate a handler, let the user own it.

- **The Agent Backend executes; builder-api mints.** ADR-0005 keeps the builder
  out of the event path and that does not change: TurnCall still POSTs
  `call.ended` straight to the backend. The backend asks builder-api for a
  Pipedream access token (they live one hour) and runs the Action itself. The
  alternative — Pipedream client credentials in each backend's `.env` — is not a
  leak risk (ADR-0008 already gitignores that file) but it is one copy of a
  workspace-wide credential per agent container, with nowhere to revoke it.

- **In-call credentials arrive through Managed call-init.** TurnCall's
  `render_agent_config()` templates `system_prompt`, `first_message` and tool
  *descriptions* only — it never touches `mcp_servers.headers`, so the
  `variables` route cannot carry a token. The backend's `/call-init` returns an
  inline agent with freshly minted headers instead. **No change to TurnCall.**

- **`tool_filter` is mandatory on catalog-added servers.** Pipedream generates MCP
  tools from an app's whole pre-built action registry — dozens for Slack. TurnCall
  caps at 50 per server and 100 total, and every advertised tool rides in every
  LLM request for the length of the call, so an unfiltered server costs latency
  and tool-selection quality, not just tokens. The catalog pins an explicit
  allowlist per app; the raw MCP form leaves it optional, where the caps are the
  only guard.

- **v1 catalog is Slack and Google Calendar.** The two that were actually asked
  for. A third app is added when a customer asks for one, not when we guess.
  ADR-0017's raw MCP form stays the escape hatch for everything else.

## Trace of what gets built

1. builder-api: encrypted Pipedream client credential, Connect Link redirect +
   callback, and a mint endpoint the backends call.
2. Console: an app picker that writes `mcp_servers` with a pinned `tool_filter`.
3. Scaffold: `/call-init` returns minted MCP headers; a generated
   `on_call_ended()` Automation stub.

## Trade-offs accepted

- **An Automation needs someone who will edit Python.** That is the user least
  likely to be the one who wants Slack summaries. Accepted because the Console
  form is a bigger build than the feature, and a generated stub with the Action
  already filled in is a much shorter distance than it sounds. If the demand for
  a no-code Automation shows up, this is the decision to revisit first.
- **The Agent Backend gains a runtime dependency on builder-api** for token
  minting, which cuts against ADR-0004's self-contained, independently deployable
  backend. The event path is untouched either way.
- **Voice only.** Call-init covers Twilio voice, WhatsApp voice and WebRTC.
  SMS/chat text sessions have no equivalent, so in-call integrations do not reach
  them. Automations are unaffected — they run off an event, not a call.
- **Pipedream is Workday-owned.** Budget for migration, not for permanence.
- **`x-pd-environment` stays `development`** (free) until launch; production is
  $99/mo plus credits and external-user count.

## What this does not include

- **Any Console surface for Automations** — deliberate, per the asymmetry above.
- **Automation failure handling.** Retry policy and whether a failed Slack post
  surfaces anywhere beyond the backend's own logs is undecided. ADR-0008's rule
  ("a failed registration must look like a failure, not like 'no calls yet'")
  applies in spirit, but with no Console surface there is nowhere obvious to put
  it. Decide when the stub exists.
- **A Workspace's own end-customers connecting their own accounts.** That needs
  per-end-user OAuth and an embedded connect UI, which Pipedream does not ship
  prebuilt. Nothing here forecloses it — `external_user_id` is the seam — but it
  is a product, not a follow-up commit.

## Status

Accepted, not implemented. Reverting before implementation costs nothing; after,
it is the Connect Link flow, the mint endpoint, the Console picker, and the
scaffold's Automation stub.
