# The interview loop runs on Deep Agents

The [[Builder]]'s interview is one forced tool call per turn. It cannot look
anything up — not the menu the user just attached, not the voice list it recites
from memory. This records moving that loop onto LangChain's `deepagents`, and
the boundaries drawn around it.

## Scope: the interview loop, nothing else

`step()` and `build_system_prompt` in `app/builder.py`. Everything else stays as
it is: `app/backends/*` (including `toolgen.py`'s own Anthropic client),
`mapper.py`, `turncall_client.py`, [[Create]], apply, and the [[Console]].

The generated [[Agent Backend]] is deterministic scaffolding that works;
nondeterminism buys it nothing and puts the Create saga at risk. The voice agent
itself runs on Pipecat and was never in question.

## The loop is what pays for the dependency, not the skills

`SkillsMiddleware` is the most attractive part of `deepagents`, and it is not the
reason to take it. Progressive disclosure over the existing
`build_system_prompt` is about sixty lines and no new dependency; skills alone do
not justify langchain plus langgraph in a project with fourteen lean deps and no
langchain at all.

What justifies it is the thing `_D_KNOWLEDGE` admits in writing: *"You CANNOT
read them."* The Builder today authors a `place_room_service_order` tool for a
menu it has never seen, and is instructed never to ask for the contents. A loop
with `ls` / `read_file` / `grep` over a backend closes that, and hand-rolling a
loop, a tool schema and a filesystem is rebuilding `deepagents` badly.

Secondary but real: `create_deep_agent(model=…)` deletes `AnthropicBuilder`,
`OpenAIBuilder`, `BuilderProvider` and `_PROVIDERS` — about ninety lines,
including the Responses-vs-Messages divergence that had to be solved by hand.

## Some disciplines became Skills — not all of them

The plan was to migrate all seven `_D_*` disciplines verbatim. Building it
showed that to be wrong, and the reason is worth recording because it is the
real cost of progressive disclosure.

**Skills are loaded when the model judges they apply.** That is the whole point,
and it is exactly what you do not want for a rule that must hold on every turn.
Two of the disciplines are load-bearing in that way. `_D_VOICE` — ask for one
thing at a time, never enumerate a list, never emit markdown — applies to every
agent this builder writes; it is also the fix for a real Sushi Samba transcript
where the agent read "1. Your name 2. The time 3. A contact number" aloud to a
caller. And ADR-0012 already established that "never ask the user to paste a
document's contents" has to hold *before* anything is attached. Neither survives
being something the model might not open.

So the split is by whether the guidance is conditional, not by tidiness:

| Stays in the prompt | Became a skill |
|---|---|
| `_D_VOICE`, `_D_DEFAULTS`, the cascade default, "never ask for pasted content", the interview and language disciplines | `agent-tools`, `guardrails`, `speech-to-speech`, `knowledge-documents` |

Four skills in `app/skills/<name>/SKILL.md`, mounted read-only at `/skills/`.
The saving on an average turn is therefore smaller than "4.5KB every turn"
suggested — the s2s provider tables and both transfer patterns are the bulk of
it, and they are genuinely conditional. The structural gain is the one that
mattered anyway: a library can now grow without every agent paying for it, which
is what makes vertical skills (`restaurant-reservations`, `dental-clinic`)
affordable at all.

Built-in only for now: shipped in the repo, read-only, no UI. Workspace-authored
skills are their own decision, because a workspace-authored skill is prompt text
injected into a Builder that holds the deployment's API key and provisions real
TurnCall projects. ADR-0012 rejected per-deployment override files on exactly
these grounds. Nothing here reverses that; it only makes revisiting it cheap.

Guidance that moves out of the prompt can vanish silently — a skill whose
frontmatter breaks or whose name stops matching its directory simply is not
found. So the tests assert the transfer patterns and the Nova Sonic model id at
their new locations, and one test lists what deepagents actually discovers
through the mount and fails if the set does not match the directories on disk.

## What is deliberately not adopted

**No checkpointer.** `langgraph-checkpoint-postgres` is psycopg 3; builder-api is
asyncpg end to end. Durable agent state means a second driver and a second pool
in one process, plus a `.setup()` living outside alembic. What it buys is
cross-turn todos and a persistent filesystem, which an interview does not need —
the turn boundary is an HTTP request already carrying the whole history.
`store.py` and the alembic chain stay untouched, and that is what keeps this
revertible.

**No human-in-the-loop interrupts.** `interrupt_on` is the idiomatic way for a
deep agent to ask the user something, and it requires a checkpointer. The
`compose(ask|finalize)` contract survives instead, expressed as
`response_format`. That contract is load-bearing: `_result_payload`,
`pending_apply`, apply and the whole Console are built on the guarantee that a
turn returns a question or a config, never prose.

**No subagents.** Same rule that leaves `toolgen.py` alone.

## The turn stays one synchronous POST

`post_message` keeps its shape, so no Console change is required. The loop is
capped on three axes, because they fail differently: model calls (LangGraph's
`recursion_limit` defaults to 25 supersteps, which is a runaway, not a budget),
wall clock, and tool-output size.

Capped-and-stateless is a deliberate pair. Uncapped-and-stateless is the bad
combination: a ninety-second turn that dies at a proxy has spent the tokens,
produced nothing and saved nothing, so the retry pays for all of it again.

Streaming landed straight after, as its own change, and is now what the console
uses: `POST /sessions/{id}/messages/stream` beside the original. What streams is
the loop's steps, not the model's tokens — the compose result is emitted last,
after the reading, so tokens would arrive during the wrong twenty seconds and be
half-formed JSON besides. Both endpoints share one `_turn_events` generator, so
history is written in exactly one place.

Two consequences worth naming. Once the response has begun the status code is
spent, so a failure mid-turn travels as an `{"type": "error", status, detail}`
event rather than an HTTP status — the session check runs first, which is why an
expired session is still a real 404 the console can retry. And the progress feed
is an allowlist: document reads, skill loads and the final write. A feed that
reports the agent's every scratch write is a spinner with extra steps.

## The error taxonomy is kept, and tested

`classify_vendor_error` reads exception *text*, because Anthropic reports an
exhausted credit balance as a 400 that is otherwise indistinguishable from a
malformed request. langchain wraps vendor exceptions, so classification walks
`__cause__`. A test asserts that a wrapped 400 carrying "credit balance" still
classifies as `credit`. This is exactly the kind of behaviour a framework swap
regresses in silence, and telling someone to retry a request that cannot succeed
until they top up an account is worse than saying nothing.

## The prerequisite, resolved first

Reading documents during the interview is the whole case above, and for the flow
that matters it does not currently work.

Documents are staged in the browser — `NewAgent.tsx` holds `queuedDocs: File[]`
— and uploaded only at [[Create]], into the Agent's own project. During a
[[Build mode]] interview, builder-api has the *filenames* (`doc_names`) and
nothing else. Every knowledge endpoint begins at `_backend_or_404`, so they all
need an Agent that does not exist yet.

Three ways out:

1. **Stage documents on the [[Session]]** — bytes held server-side, forwarded to
   [[Agent Knowledge]] at Create as they are today. Mounts directly as the
   read-only documents backend. Costs a new endpoint and a Console change, which
   breaches the scope line drawn at the top of this ADR.
2. **Edit mode only** — an existing Agent has a knowledge base, so its text could
   be fetched from TurnCall. Misses the flow that matters, and TurnCall's KB API
   returns document *metadata*; retrieving the text back out is not offered.
3. **Ship the loop without documents** — skills, model-agnosticism and a readable
   provider catalog only. Honest, and it weakens the case for the dependency to
   roughly what option (b) of the original question was.

**Built: (1).** `app/session_docs.py` stages documents under
`GENERATED_DIR/session-<sid>/`, `raw/` forwarded to TurnCall verbatim and `text/`
holding extracted plain text for the Builder to read. Three endpoints on the
Session (upload, list, delete), and Create forwards then discards — keeping
anything that failed staged, so pressing Create again is a real retry rather than
an instruction to go and do it by hand.

It shipped ahead of the deepagents work and stands on its own, though less
dramatically than first argued: the console's `sessionId` is plain component
state, so a reload abandons the whole session rather than just the files. What it
genuinely fixes is the failure at `NewAgent.tsx:285` — a document upload that
fails *after* the agent is created leaves a provisioned agent, an empty knowledge
base and an instruction to go and redo it by hand. With the bytes server-side,
pressing Create again is a real retry.

## Consequences

- `requirements.txt` gains langchain, langgraph and deepagents.
- Per-turn cost and latency become variable where they were near-constant.
- `provider_catalog.py` holds the canonical voice lists, and `builder.py`
  restates them as prose twice — in `_AGENT_CONFIG_SCHEMA`'s `s2s.voice` and
  again in `_D_PIPELINE`. Three copies, already drifting. A catalog the Builder
  can read is one copy.
- [[Skill]] enters the vocabulary and must not blur into [[Agent Knowledge]]
  (what the finished voice agent reads to callers) or [[Discipline]] (the term
  it replaces).
- Reverting means deleting `app/skills/` and restoring `step()`. Nothing in
  Postgres changes, by design.
- MCP stays outside the [[Config surface]] (ADR-0003). Exposing `mcp_servers` in
  the Console is a separate decision that has nothing to do with this one.

## Status

Accepted and implemented. `step()` runs on `create_deep_agent` with the compose
schema as `ToolStrategy`, four skills mounted at `/skills/`, staged documents at
`/documents/`, no checkpointer, and the loop capped by `BUILDER_MAX_STEPS` and
`BUILDER_TURN_TIMEOUT`. Streaming shipped separately, as planned.
