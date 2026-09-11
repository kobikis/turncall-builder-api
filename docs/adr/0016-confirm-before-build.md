# The Builder confirms what it understood before it builds

The interview used to end by producing a configuration. It now ends by saying
what it understood and waiting to be told it is right.

## The approval that existed was not one

There was already a gate: the config appears in the right-hand panel and
**Create in TurnCall** lights up. In principle nothing is provisioned without a
click.

In practice that is not an approval, because of what is being approved. A
practice manager can read "transfers billing questions to the practice manager
on 020 7946 0321" and know whether it is true. Nobody can read
`custom_tools[0].parameters_schema` and know anything. The gate was real and the
check behind it was theatre.

## What is shown: the understanding, not the transcript

The obvious alternative — replay the questions and the answers — was rejected.
That transcript is already on screen, a few centimetres up; scrolling it back is
not a check. And it answers the wrong question. What the user needs to verify is
not *what did I say* but *did it understand me*, and those differ precisely where
it matters.

So `action='confirm'` carries a `summary`: one short line per decision, in the
user's own words, about their business. Providers, models, voices, tool schemas
and pipeline settings are explicitly excluded from it — they are not decisions
the user made, and listing them buries the four lines that are.

## It is a turn, not a console step

The recap could have been rendered by the console from the finalized config.
That would mean reverse-engineering the decisions back out of the JSON — the
exact artifact this ADR exists to stop showing people — and it would mean the
config had already been built before anyone agreed to it.

So it is a third compose action. One turn, one of `ask | confirm | finalize`, and
the protocol stays the shape every consumer already knows.

## Approving is just replying

No Approve button, no endpoint, no state machine. The summary ends by asking what
is wrong; saying "looks right" is the approval, and saying what is wrong is the
correction. Both are the same text box.

Create needs no new gate either: the config does not exist until after the
confirm, so the button cannot light up early. The gate is the absence of a
config, which is a fact rather than a flag.

## Being asked twice is the failure that would sink it

The risk is a model that confirms, gets agreement, and confirms again. Nothing
would make this feature more hated.

Three things address it. The confirm is written into the session history like any
other thing the builder said, so the next turn can see it was offered. The
discipline says, in as many words, that an accepted summary is settled and that
the same summary is never sent twice. And a correction only earns a second
confirm if it opened a genuinely new question.

That is instruction, not enforcement — it is a model judgment and the tests can
only assert that the instruction is present and unambiguous. Verifying the
behaviour needs a live model, and should be the first thing checked when one is
pointed at this.

## Not for editing

Editing an existing agent already confirms: a finalize on a generated agent is a
proposal (`pending_apply`) that Apply pushes live. Making someone approve a
summary in order to change one sentence is friction wearing safety's clothes.

## Consequences

- `ComposeResult` gains `summary`; the turn payload carries it. A consumer that
  only knows `ask` and `finalize` will see an action it does not handle.
- The interview is one turn longer. That is the point, and it is the cheapest
  turn in the conversation — nothing is provisioned, nothing is generated.
- The console marks a confirm differently from a question ("Check this before I
  build it"), because a message meant to be read carefully must not look like
  the four before it that were meant to be answered quickly.

## Status

Accepted and implemented.
