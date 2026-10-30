---
name: procedures
description: Use when the user describes a step-by-step process the agent must follow on a call — troubleshooting, collections, intake, a regulated script — or asks to change one. Covers writing agent_config.procedures, its steps, moves, step tools and escalation.
---

# Procedures

A procedure is a graph of steps the agent follows on a call. The platform holds
the route: on every caller turn the agent is shown only the step it is on, the
moves allowed from it, and a short preview. It reports each move itself with
`move_to_step`, which the platform adds — never list `move_to_step` in `tools`.

Write one whenever the user describes a process in order ("first ask..., if
..., then ..., otherwise send them to a technician"). Do not also write the
steps into the system_prompt: the prompt keeps the persona, the tone and what
the agent does outside the procedure.

## Shape

```json
"procedures": [{
  "name": "no_internet",
  "description": "Caller has no internet connection at home",
  "start": "call",
  "steps": [
    {"id": "lights", "kind": "ask", "start": true,
     "text": "Ask which lights on the router are on and their colour.",
     "moves": [{"to": "restart", "when": "the internet light is red or orange"},
               {"to": "resolved", "when": "all lights are green"}]},
    {"id": "restart", "kind": "instruct",
     "text": "Have the caller restart the router and wait two minutes.",
     "moves": [{"to": "resolved", "when": "the caller confirms it is back"},
               {"to": "technician", "when": "it is still down"}]},
    {"id": "resolved", "kind": "end", "text": "Confirm it works and close politely."},
    {"id": "technician", "kind": "escalate",
     "text": "Tell the caller a technician will take it from here.",
     "escalate": {"transfer_call": {"target_number": "+15550009999",
                                    "transfer_mode": "warm",
                                    "briefing": {"from_summary": true}}}}
  ]
}]
```

## Rules the platform checks at save

A broken procedure refuses the whole save, so get these right:

- `name` is lowercase snake_case, in the user's words; names are unique.
- Step ids are unique within the procedure; exactly one step has `start: true`.
- Every move goes to a step that exists, and every step can be reached from the
  start by its moves.
- At least one step is `end` or `escalate`. Give every way the process can go a
  place to finish: resolved (`end`) and could-not-resolve (`escalate`).
- At most 50 steps; leave `window_depth` out unless the user asks.
- `escalate` is set on an `escalate` step and on no other.

## Kinds and moves

- `ask` a question, `instruct` the caller to do something, `check` something
  (usually with a tool), `escalate`, `end`.
- `text` is what the agent does there, in the second person, spoken style. On
  `escalate` and `end` it is what the caller hears, said in the same reply.
- `when` is a plain-language condition the agent judges from the caller's
  answer. Cover each answer the user mentioned; going back one step needs no move.
- `start`: `"call"` when every call is this process; `"on_request"` when it is
  one of several things a caller may want — the agent starts it when it applies.
  An agent with one procedure per problem uses `on_request` on each.

## Step tools

A step's `tool` names a tool the agent calls there, so a lookup happens where it
belongs. It must be one of the agent's own: a name in `tools` (built-ins) or in
`custom_tools`. When a step needs a lookup the agent has no tool for, write
that tool as a custom_tool (see the agent-tools builder skill) and name it.

## Escalation is a transfer

An `escalate` step transfers the call to a person:
`{"transfer_call": {"target_number": ..., "transfer_mode": "cold" | "warm",
"briefing": ..., "transfer_message": ..., "fallback_message": ...}}`.

- `target_number` is E.164 (`+15550009999`) and must be a number the user gave.
  Ask for it if they have not; never invent one.
- Warm when the user wants the human briefed: `"briefing": {"from_summary": true}`
  summarises the call so far, or write a fixed text.
- Never write `handoff_to_agent`: this agent cannot hand a call to another
  Builder agent. A `handoff_to_agent` already in a config stays exactly as it is.

## Editing

The current config is in your instructions. Keep every procedure the user did
not ask to change exactly as it is — ids, moves and escalation included. To
change one, rewrite that procedure whole; to remove one, leave it out of the
list; to remove all, send `"procedures": []`.

## Cascade only

Procedures run on the cascade pipeline. An s2s agent ignores them, so an agent
with procedures stays on cascade (leave `pipeline_mode` unset).
