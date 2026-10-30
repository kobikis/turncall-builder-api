# Procedures in the Builder escalate by transfer

TurnCall's guided procedures (turncall#417) are graphs of steps an agent
follows on a call: TurnCall holds the route and shows the model one step at a
time. They are part of the agent's config (`procedures`), so the Builder carries
them on its [[Config surface]] like `skills` (ADR-0029): sent as written on
every path that pushes a config (Create, the Console's save, a chat edit's
Apply, a call's inline config), checked by TurnCall at save.

An `escalate` step names what happens when the procedure cannot resolve the
caller's problem: a `transfer_call` to a number, or a `handoff_to_agent` to
another agent **of the same TurnCall project**.

## Decision

**The Builder offers escalation by transfer only.** Each Builder agent is its
own TurnCall project (ADR-0004/0006), so a handoff names an agent no Builder
agent can reach: TurnCall refuses an `agent_id` outside the project. The
composer's schema has no `handoff_to_agent`, its builder skill says never to
write one, and the Console's editor does not offer it. A `handoff_to_agent`
already in a config (written in the Advanced JSON, or against TurnCall
directly) is kept exactly as written: the mapper passes `procedures` through
untouched, and TurnCall decides.

**The composer authors procedures.** A user who describes a step-by-step
process (troubleshooting, collections, intake, a regulated script) gets it as
`procedures`, not as prose in the system prompt; the `procedures` builder skill
says how. A step's `tool` names one of the agent's own tools (`tools` or
`custom_tools`).

**A chat edit that leaves `procedures` out keeps them.** A long nested field is
the easiest thing for a model to drop while editing something else, and losing
a procedure because the user changed the greeting is a silent regression. On
finalize, a config with no `procedures` key takes the current one's;
`"procedures": []` still removes them.

**TurnCall's refusal names the step.** TurnCall's graph rule answers
`procedures[i] '<name>': step '<id>' ...`. The Builder turns that into a field
the Console can put on the entry: `procedures[i]`, or `procedures[i].steps[j]`
when the message names a step the refused request carried, the index read from
the config it sent (`field_errors`). A schema refusal inside a step keeps its
path (`procedures[0].steps[3].escalate.transfer_call.target_number`).

## Consequences

- A procedure that needs another agent cannot be built in the Builder; it
  transfers to that team's number instead.
- Removing every procedure in a chat edit takes an explicit empty list, which
  the builder skill tells the model.
- A call's `procedures` summary (TurnCall's call read and `call.ended`) reaches
  the Console where its `skills` do: the call routes proxy TurnCall's call read
  as it is.
