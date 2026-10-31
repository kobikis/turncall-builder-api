# Procedures

A **procedure** is a task the agent walks a caller through in a fixed shape: a
booking, a diagnostic, an identity check, a cancellation. You write it as a
graph of steps. TurnCall holds where the call is in that graph; the model is
shown only the step it is on and the moves it may make from there, and reports
each move. A move the graph does not allow is refused.

This page is the concepts and where they live in the Console. The engine's
reference (every field, the rules a save checks, the events a call records)
is [docs.turncall.io/guides/procedures](https://docs.turncall.io/guides/procedures).

Procedures run on cascade voice calls. A speech-to-speech agent keeps them in
its config but runs its calls without them (the Procedures section says so);
text conversations do not run them.

## In the Console

- **Build → Procedures** edits them, saved with the rest of the agent. A
  procedure has a *Name*, *Starts* (with the call, or on request), *When it
  applies*, a *Start step* and *Steps shown ahead*. Each step has an *id*, a
  *Kind*, an optional *Tool*, *What to do*, and its moves (a target and a
  condition). TurnCall checks the graph on Save, and a refusal is shown on the
  procedure or the step it names.
- **The composer** writes one when you describe a process in order ("first
  ask…, if…, then…, otherwise send them to a person"), following its
  `procedures` skill (`app/skills/procedures/SKILL.md`). It keeps procedures
  you did not ask to change exactly as they are.
- **Calls → a call** shows a *Procedures* section: each procedure's path, where
  it ended (completed, escalated at a step, or left), and its refused moves.

## Why not just a prompt

A system prompt can say "ask the party size, then the date, then the time".
On a short call it mostly works. As the script grows, the order lives only in
the model's reading of a long prompt, and three things go wrong:

- **Steps get skipped.** The model jumps to the read-back with the phone number
  never asked, and nothing notices.
- **Nobody can tell where a call went.** Did the caller drop out at the date or
  at the read-back? A transcript has to be read to know.
- **Every rule is in every turn.** The model sees the whole script on every
  reply, including the parts that do not apply yet.

A procedure moves the order out of the prompt and into something TurnCall
checks and records.

## The concepts

| Concept | What it is |
|---------|-----------|
| **Procedure** | A named graph of steps with a description saying when it applies. Part of the agent's config, so it versions and rolls back with the agent |
| **Step** | One point in the task: an id, a kind, and what to do there. Optionally a tool to call at that point |
| **Kind** | *Ask* (get an answer from the caller), *Instruct* (have the caller do something), *Check* (verify something, often with a tool), *End* (done), *Escalate* (transfer the call to a person) |
| **Move** | An edge out of a step: a target and a condition in plain language that the model judges ("the party is more than eight guests") |
| **Window** | What the model is shown of the procedure on a turn: the current step, its moves, the move back, and a preview of the next steps (*Steps shown ahead*). Never the rest of the graph |
| **`move_to_step`** | The built-in tool the model reports a move with. TurnCall adds it; it is never listed in the agent's tools. TurnCall accepts a move the current step allows (or one going back a step) and refuses anything else, naming what is allowed |
| **Starts** | *With the call*: running from the first turn. *On request*: the agent handles the call freely until the task applies, then starts the procedure |
| **Finished** | Reaching an End or Escalate step finishes the procedure for good: no more window, no more moves, and the call records it as completed or escalated |

A small one, a restaurant booking:

```text
party_size ──(8 or fewer)──▶ date ──▶ time ──▶ guest_name ──▶ phone ──▶ dietary ──▶ read_back
    │                                                                                   │
    └──(more than 8)──▶ host (escalate: warm transfer)     confirmed (end) ◀── book ◀── invite
                              ▲                                                │
                              └──────────(calendar tool failed twice)──────────┘
```

## How it sits beside the system prompt

The two are separate inputs, built at different times:

```text
SYSTEM   built once, as the call starts
  knowledge-base preamble
  the agent's prompt (with {{variables}} filled in)
  ## Procedures you can start        ← on-request procedures: name, description, start step
  ## Skills you can load             ← if the agent has skills

TOOLS    move_to_step, the agent's tools, ...

CONVERSATION ...

[Procedure]                          ← rebuilt every turn, right before the caller's words
Procedure "table_reservation" — current step: time (ask)
  Do now: Ask what time they would like the table. ...
  Allowed moves:
    → guest_name  when: the caller chose a time between six and eleven pm
    ← back to date  (always allowed)
  Next steps (preview):
    guest_name (ask): Ask for the name the booking should be under.
  Report every move with move_to_step. Do not skip a step. ...

CALLER   "Seven thirty."
```

| | Prompt | Procedure |
|---|---|---|
| Changes | Once per call (a skill load or a handoff rebuilds it) | Every turn |
| Shows | Everything you wrote | The current step and what is near it |
| Sits | First | Last, right before the caller's turn |
| Enforced | No, it is guidance | Moves are: a move the graph does not allow is refused |

The procedure adds only two things to the prompt: the list of on-request
procedures the agent can start (names and descriptions, never a step's text),
and the `move_to_step` tool. A procedure that starts with the call needs no
mention at all.

After a tool runs, the model replies without a fresh window, since the
conversation no longer ends on the caller's turn. So a move's result carries
what the window would: the new step's text, its tool, and where it moves next
(`Then move on: confirmed (when the calendar event was created); ...`). That
is how a step whose tool answers still knows to move on. The step's own tool
says it again when it answers, since a skill load or another call can come
between the move and the tool.

### Which wins when they disagree

Neither, formally: both are text the model reads, and TurnCall enforces only
which moves are allowed, never wording. In practice the window wins on *what
to do now* (it is specific, recent and last) and the prompt on *how to say
it*. Two versions of one rule drift, and the model follows whichever it read
last, so give each rule one home:

| Belongs in the prompt | Belongs in a step |
|---|---|
| Persona, tone, reply length, "everything is spoken aloud" | The order of the task |
| Facts true all call (opening hours, what the menu is) | What to do at this point |
| When to start an on-request procedure | When to move on (each move's condition) |
| What to do with a side question ("answer, then carry on from the step you were on") | Which tool to call here, which skill to load here |
| Anything after the procedure finishes | When to hand over (an Escalate step) |

A side question is the prompt's job. When a caller asks "do you have sushi?"
at the read-back, no move fits, so the procedure stays put; the prompt (or a
skill) answers, and the next turn's window brings the agent back to the
read-back. Once the procedure finishes, the window stops and the prompt alone
runs the rest of the call.

## With skills and tools

- **A step's Tool** names the tool the model should call there; the window
  shows it. The picker offers the agent's own tools and the tools of the
  Connected apps it uses during calls; an agent with MCP servers can type any
  name, since those tools are only known once a call starts. The tool does not
  run by itself: the model calls it.
- **A skill from a step.** A step's text can say "load the
  nonna_calendar_rules skill, then add the event following it". The skill's
  instructions join the prompt for the rest of the call. Keep such a skill to
  that step's job: a skill written as its own script for the whole task
  competes with the procedure.
- **Skills** are for capabilities the agent may need at any point (menu
  questions, identity checks); a procedure is for a task with an order. One
  agent can have both.

## What it buys you

- **Order is checked, not hoped for.** A skipped step is a refused move, which
  the model is told about and corrects; the caller usually never notices.
- **Every call has a path.** The call's Procedures section (and `call.ended`)
  shows the steps reached, whether it completed or escalated, and the refused
  moves. Completion and drop-off rates are a query, not a transcript read.
- **Escalation is an action, not a hope.** An Escalate step transfers the call
  when the call reaches it, cold or warm, with a briefing for the person.
- **A smaller prompt per turn.** The model reads the step it is on, not the
  whole script.
- **Testable.** In an eval, a move is a tool call, so a scenario asserts the
  path, and an agent that skips a step fails.
- **No added pause.** A move reported beside what the model says costs no
  second model call.
- **Versioned with the agent**, and edited as a form rather than as prose.

## What it costs

- **The model still judges every condition.** TurnCall checks that a move is
  allowed, not that its condition is true. "The caller confirmed the phone
  number" is the model's reading of the call.
- **One step at a time.** A caller who answers two steps at once ("tomorrow,
  seven thirty") still passes through both. Write the later step to accept an
  answer already given ("If the caller already gave a time, do not ask again:
  move on"), or merge the two steps.
- **A step with a tool costs replies.** Moving, loading a skill and calling a
  tool are each a model reply, plus the tool's own time. A step that loads a
  skill and calls a slow API can be the slowest turn of the call.
- **Little of the graph is visible.** The model sees *Steps shown ahead* and
  nothing further, so a rule it needs throughout belongs in the prompt, not in
  a later step.
- **Cascade voice only.** Speech-to-speech agents and text conversations run
  the prompt alone.

## When to use one

Use a procedure when the task has an order that matters, a point where it
must hand over, or an outcome you want to count: bookings, diagnostics,
identity verification, cancellations, intake forms. Keep to the prompt (or a
skill) for open conversation, a single question, or knowledge the caller can
ask about in any order.

## Writing a good one

- **One step, one thing.** A step's text is what to do *now*. "Ask for a phone
  number, then read it back" is one thing; "ask the name and the phone" is two
  steps.
- **Write a condition as something said on the call.** "The caller confirmed
  the email you read back" works; "the email is valid" asks the model to know
  what it cannot.
- **Give every hard step a way out.** A move to an Escalate step ("the
  calendar tool failed twice") keeps a call from going round in circles.
- **Tell the caller in the step's text.** The model says it in the same reply
  as the move: "Warmly explain that our host takes care of this booking, and
  that you are putting them through now."
- **Then read the calls.** Refused moves and paths that stop early show the
  steps to rewrite.
