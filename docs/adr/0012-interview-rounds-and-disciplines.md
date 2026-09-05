# The Builder asks rounds with recommendations, and its prompt is composed

Two changes to how [[Build mode]] talks to the user, and one to how its prompt is
assembled. The interview shape is the part worth recording, because it is easy to
revert and the case against it is real.

## What changed

- The opening turn still asks exactly one question. Every turn after it asks a
  **round** — up to four questions that are answerable now, numbered.
- Every question carries the Builder's own recommendation (`Suggested: …`), and
  the round closes by telling the user they can reply "all suggested".
- The Builder is told to reuse the user's own vocabulary in the generated
  `system_prompt` and tool names.
- The system prompt is assembled from named disciplines instead of one string.

## Why rounds, when one question at a time is friendlier

The old rule was *"Ask exactly ONE question per turn. Never ask more than one
question at once."* It is genuinely warmer, and it is the right call for the
first reply — that is where someone decides whether this is a conversation or a
form.

It is the wrong call for everything after. An agent worth building involves five
or six decisions, and one-per-turn turns that into a ten-turn interrogation
where the user cannot see how much is left. Asking everything currently
answerable is fewer turns and, more importantly, shows the shape of what is
being decided.

The counter-argument is not theoretical: this Builder's user may be a restaurant
owner, not an engineer, and four numbered questions can read as colder than one
friendly one. That is why the opening turn is exempt, and why the rounds are
capped at four and forbidden from including questions that depend on each other.

## Why recommendations matter more than rounds

Ranked above the batching. *"What should the agent do when nobody answers?"* is a
question a domain expert can answer and a first-time user cannot. Attaching the
Builder's own answer turns every question into something acceptable with a
"yes", so a user who does not know voice-agent conventions still ends up with a
specific agent instead of a vague one.

It also makes the whole round acceptable in one reply, which is what stops the
batching from feeling like a form.

## Why the prompt is composed rather than one string

The old prompt was ~55 lines covering eight domains at once. Split into named
disciplines, each is readable and testable on its own, and a turn only carries
guidance it can act on: [[Edit mode]] drops the interview disciplines entirely,
because not re-interviewing is its whole point.

**The knowledge discipline is deliberately unconditional.** Gating it on whether
documents are attached looks right and is wrong: "never ask the user to paste a
document's contents" has to hold *before* anything is attached, which is exactly
when someone says "I'll upload our menu". Only the list of attached filenames is
conditional. An existing test caught this.

Per-deployment override files — letting an operator hack the disciplines without
a code change — were considered and rejected for now. It suits a hackable skills
library; in a product it becomes drift and support burden. Revisit when someone
asks.

## Consequences

- **The console must preserve newlines.** Builder text is rendered verbatim in a
  chat bubble with no markdown, so `white-space: pre-wrap` is what makes a
  numbered round legible at all. The disciplines forbid markdown for the same
  reason — asterisks would show up raw.
- **`SYSTEM` is now derived**, kept as the build-mode prompt so callers and tests
  can still read it as one string.
- **Prompt behaviour is now unit-testable** without calling a model: the tests
  assert the opening turn asks one question, that rounds carry recommendations,
  and that edit mode keeps the domain rules while dropping the interview.
- Vocabulary for all of this — [[Build mode]], [[Edit mode]], [[Round]],
  [[Discipline]] — is in `CONTEXT.md`. "Creation agent" is explicitly avoided: it
  reads as [[Create]], which is a different step.

## Status

Accepted.
