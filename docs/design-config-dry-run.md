# Design: dry-run a Config before Create

**Status: proposed, not scheduled.** Nothing here is decided — this is the
design captured while it was fresh, so it doesn't have to be rediscovered.

Lets a user talk to a finalized [[Config]] as text, before [[Create]]
provisions anything. Domain terms: `CONTEXT.md`.

## Why

The `system_prompt` is the Builder's main deliverable, and today the only way
to judge it is: **Finalize → Create → provision an agent → generate and start a
backend container → phone it.** That is a minutes-long, side-effect-heavy loop
to evaluate a piece of text.

The motivating evidence is a real Sushi Samba call. Asked for a table, the
generated agent replied:

```
Could you please provide me with the following details?
1. Your name
2. The time you'd like the reservation for
3. A contact phone number
4. Any special requests
```

TTS reads that aloud, numbers and all. Earlier in the same call, an out-of-scope
request got a flat *"I'm only able to assist with reservations at the moment"*.

Both are **content** faults, plainly visible in text. Neither was caught by any
test, review, or design conversation — only by phoning the agent. Fixed in
ADR-0012's follow-up (the voice-output discipline), but the point stands: there
was nowhere to look but a phone call.

## Why it is cheaper than it sounds

`turncall`'s `services/llm_text.py` exposes:

```python
async def complete_text(config: LLMConfig, messages, *, aws=None) -> CompletionResult
```

It takes a **config, not an agent**. `POST /v1/chat` requires an `agent_id` only
for session persistence, not for the completion. The primitive a dry run needs
already exists and needs no agent row.

## Flow

```
Builder finalizes ──► Config sits in the Session (not yet an agent)
                            │
                     [ Try it ] in the console
                            │
   console ──► builder-api ──► TurnCall  POST /v1/chat/preview
                                          { config, messages }
                            │
              assembles the same prompt the runtime would
              (system_prompt + knowledge context + tool schemas)
                            │
                     complete_text(config.llm, messages)
                            │
   console ◄── reply, plus any tool call it decided to make
                            │
        not right? ──► back to the Builder to edit ──► try again
                            │
                     happy ──► Create
```

**TurnCall runs it, not builder-api.** builder-api has its own LLM clients and
could answer directly, but it would then reimplement prompt assembly and drift
from what the real agent does. A preview that diverges from reality is worse
than no preview. Routing through the engine means one code path answers both.

## What it tests, and what it does not

Tests **what the agent says**: tone, whether it enumerates, how it handles an
out-of-scope request, whether it wires tool arguments correctly.

Does not test **how the call flows**: barge-in, turn-taking, latency, TTS
mangling numbers and times. Cascade is STT → LLM → TTS; this exercises the LLM
leg only.

That limit is real, and was the main argument against building it. The Sushi
transcript is the counter-argument: the faults that actually shipped were
content faults a text dry run would have caught in seconds.

## Open questions

1. **Tools.** The generated Agent Backend does not exist before Create.
   Inclination: *show the call and stub the result* — a transcript line reading
   `→ book_table(name="Kobi", party=4, time="19:00")` is arguably the most
   valuable single output, because it proves the prompt wired the arguments.
   Silently skipping tools would hide exactly what needs checking. Refusing to
   dry-run tool-using agents is too restrictive; most useful agents have tools.
2. **Knowledge.** If documents are attached to the Session but not yet ingested
   into a knowledge base, the dry run knows filenames and not contents — so an
   agent designed around a menu dry-runs worse than it will behave. Needs
   checking, and probably a note in the UI when it applies.
3. **Spend.** Runs the agent's configured model on the platform key: cheap per
   turn, unbounded per user. Probably wants a turn cap per Session.
4. **Auth.** A config-in-body endpoint lets anyone with an API key run arbitrary
   prompts through the platform's LLM keys. Already true of agent creation, but
   worth being deliberate rather than accidental about.

## Cheaper adjacent idea

A **static check on the finalized Config** before Create: does the
`system_prompt` contain numbered lists, markdown, or headings? That is a regex,
needs no LLM call, and permanently catches the exact class of bug above instead
of relying on someone noticing during a call. Worth doing whether or not the
dry run is built.

## Not chosen

A **design-tree panel** in the console (what the Builder has settled vs what is
still open) was considered alongside this and dropped. It is polish on an
interview that ADR-0012 already improved, and it depends on the model
accurately self-reporting its own state turn after turn — if that report drifts,
the panel confidently shows a decision as settled when it was never asked.
