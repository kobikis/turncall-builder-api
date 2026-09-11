"""The builder loop: prompt -> follow-up questions -> finalized TurnCall config.

A deep agent per turn (ADR-0014). It can read the documents staged on the
Session and load a skill when one applies, and it must finish by producing a
`compose` result: either the next question (`ask`) or a complete agent config
(`finalize`). That contract is unchanged from ADR-0001 — the console is built on
it — and so is the trimmed config surface of ADR-0003.

Stateless between turns by design: no checkpointer, so the running history the
caller passes IS the state. See ADR-0014 for why.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from langchain.agents.structured_output import ToolStrategy

# BUILDER_* is canonical; COMPOSER_* accepted as a legacy fallback (pre-rename).
DEFAULT_PROVIDER = os.environ.get(
    "BUILDER_PROVIDER", os.environ.get("COMPOSER_PROVIDER", "anthropic")
)
MODEL = os.environ.get(
    "BUILDER_MODEL", os.environ.get("COMPOSER_MODEL", "claude-sonnet-4-6")
)

# Which env key unlocks each provider — the picker only offers keyed ones.
_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}

# Skills shipped with the builder. Read-only, and mounted at /skills/ inside the
# agent's backend. Workspace-authored skills are a separate decision (ADR-0014).
SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")

# A turn is one HTTP request the console waits on with no timeout of its own, so
# the loop is capped on both axes. MAX_STEPS counts LangGraph supersteps, which
# is roughly two per model call — not a budget, a runaway guard.
MAX_STEPS = int(os.environ.get("BUILDER_MAX_STEPS", "20"))
TURN_TIMEOUT_SECONDS = float(os.environ.get("BUILDER_TURN_TIMEOUT", "90"))


class BuilderError(RuntimeError):
    """A builder turn failed (vendor API error or malformed tool call).

    Vendor-neutral so callers never import anthropic/openai error types.

    `kind` classifies the failure so the API can tell the operator something
    actionable without echoing the vendor's raw message back to the browser:

      auth        the provider rejected the builder's API key
      credit      the account is out of credit / over quota — retrying is futile
      rate_limit  transient; retrying is exactly right
      protocol    the model answered without calling the compose tool
      upstream    anything else
    """

    def __init__(self, message: str, *, kind: str = "upstream") -> None:
        super().__init__(message)
        self.kind = kind


def classify_vendor_error(exc: Exception) -> str:
    """Map a vendor API exception onto a BuilderError kind.

    Status alone is not enough: Anthropic reports an exhausted credit balance as
    a 400, which is indistinguishable from a malformed request without reading
    the message.
    """
    # langchain wraps vendor exceptions, so the vendor's own error is usually the
    # cause rather than the exception itself. Walk the chain and read every link:
    # dropping this loses the credit-vs-retry distinction the console depends on.
    chain: list[BaseException] = []
    cursor: BaseException | None = exc
    while cursor is not None and cursor not in chain:
        chain.append(cursor)
        cursor = cursor.__cause__ or cursor.__context__

    status = next(
        (s for s in (getattr(link, "status_code", None) for link in chain) if s), None
    )
    text = " ".join(str(link) for link in chain).lower()
    if any(s in text for s in ("credit balance", "billing", "insufficient_quota", "exceeded your current quota")):
        return "credit"
    if status in (401, 403) or "authentication" in text or "invalid api key" in text or "invalid x-api-key" in text:
        return "auth"
    if status == 429 or "rate limit" in text:
        return "rate_limit"
    return "upstream"

# The trimmed slice of the TurnCall agent schema the builder may set (ADR-0003).
# Everything else takes TurnCall defaults.
_AGENT_CONFIG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Human-readable agent name, e.g. 'Reservations Bot'.",
        },
        "system_prompt": {
            "type": "string",
            "description": "Full persona + instructions for the agent. This is the "
            "main deliverable: write it well, in the second person.",
        },
        "first_message": {
            "type": ["string", "null"],
            "description": "What the agent says first when the call connects. "
            "null to let the caller speak first.",
        },
        "llm": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "enum": ["openai", "anthropic", "bedrock"]},
                "model": {"type": "string"},
            },
            "required": ["provider", "model"],
            "description": "Default to openai / gpt-4o-mini unless the use case "
            "implies otherwise. Use 'bedrock' only when the user says they want to "
            "run on AWS / Bedrock / their own AWS account — its models are AWS ids "
            "like 'anthropic.claude-3-5-sonnet-20241022-v2:0'. AWS credentials are "
            "NEVER part of the config you generate; set aws.region only.",
        },
        "tts": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "enum": ["deepgram", "openai", "elevenlabs", "cartesia"]},
                "voice": {"type": "string"},
            },
            "required": ["provider", "voice"],
            "description": "Default deepgram / 'aura-2-helena-en'. Pick another voice "
            "only if the user expresses a vibe.",
        },
        "tools": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["end_call", "transfer_call", "send_dtmf"],
            },
            "description": "Built-in tools to enable. Only include ones the use case "
            "clearly needs. send_dtmf lets the agent press keypad digits (navigate "
            "an IVR, enter a code) during a call.",
        },
        "server_url": {
            "type": ["string", "null"],
            "description": "Base URL of the user's OWN tool server — set ONLY when "
            "the user explicitly provides one (custom tools then POST to "
            "{server_url}/tools/{name}). Never ask for it; omit to use the "
            "generated backend.",
        },
        "custom_tools": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "snake_case action name, e.g. cancel_reservation.",
                    },
                    "description": {
                        "type": "string",
                        "description": "When/why the agent should call it.",
                    },
                    "parameters_schema": {
                        "type": "object",
                        "description": "JSON Schema of the tool's arguments.",
                    },
                    "server_url": {
                        "type": "string",
                        "description": "Full URL where THIS tool executes on the "
                        "user's server — only when the user explicitly gives one.",
                    },
                },
                "required": ["name", "description"],
            },
            "description": "Actions the agent must DO — book, cancel, look up, etc. "
            "Author these whenever the use case implies an external action. You do NOT "
            "provide URLs: the builder generates a backend and wires each tool to it.",
        },
        "pipeline_mode": {
            "type": "string",
            "enum": ["cascade", "s2s"],
            "description": "Voice pipeline. Omit / 'cascade' (default) = STT→LLM→TTS, "
            "uses the tts block. 's2s' = a native speech-to-speech model (OpenAI "
            "Realtime / Gemini Live) for the lowest latency and most natural "
            "turn-taking. Set 's2s' ONLY when the user wants a real-time, ultra-low-"
            "latency, human-like voice. In s2s the tts block is ignored — the voice "
            "comes from s2s.voice.",
        },
        "s2s": {
            "type": "object",
            "description": "Speech-to-speech config. Required when pipeline_mode='s2s'.",
            "properties": {
                "provider": {"type": "string", "enum": ["openai", "google", "aws"]},
                "model": {
                    "type": "string",
                    "description": "openai: 'gpt-4o-realtime-preview'. "
                    "google: 'models/gemini-3.1-flash-live-preview'. "
                    "aws: 'amazon.nova-2-sonic-v1:0' (Amazon Nova Sonic 2).",
                },
                "voice": {
                    "type": "string",
                    "description": "Must match the provider. openai voices: alloy, ash, "
                    "ballad, coral, echo, sage, shimmer, verse. google voices: Aoede, "
                    "Charon, Fenrir, Kore, Leda, Orus, Puck, Zephyr. aws voices: "
                    "matthew, tiffany, amy, lupe, carlos.",
                },
            },
            "required": ["provider", "model", "voice"],
        },
        "aws": {
            "type": "object",
            "description": "AWS region for the 'bedrock' LLM or 'aws' S2S provider. "
            "Set ONLY when one of those is used, and ONLY the region — credentials "
            "are an operator concern (server environment or an assumed role) and "
            "must never appear in a generated config.",
            "properties": {
                "region": {
                    "type": "string",
                    "description": "e.g. 'us-east-1'. Bedrock model availability is "
                    "region-specific.",
                },
            },
        },
        "voicemail_detection": {
            "type": "object",
            "description": "Detect when an OUTBOUND call reaches voicemail and leave "
            "a message. Cascade only — never set alongside pipeline_mode='s2s'. Set "
            "ONLY for outbound/dialer agents that may hit voicemail.",
            "properties": {
                "enabled": {"type": "boolean"},
                "voicemail_message": {
                    "type": "string",
                    "description": "What the agent leaves on voicemail (spoken via TTS).",
                },
            },
            "required": ["enabled"],
        },
        "guardrails": {
            "type": "object",
            "description": "Hard limits the agent must respect.",
            "properties": {
                "prohibited_topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Topics the agent must refuse to discuss or advise on "
                    "(e.g. 'medical advice', 'competitor pricing'). Enforced as a rule "
                    "in the agent's instructions. Set when the user wants the agent to "
                    "stay off certain subjects.",
                },
            },
        },
    },
    "required": ["name", "system_prompt", "llm", "tts"],
}

COMPOSE_TOOL: dict[str, Any] = {
    "name": "compose",
    "description": "Drive the agent-building conversation. Ask ONE clarifying "
    "question while anything material is still ambiguous; finalize only when you "
    "could confidently build the agent.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["ask", "confirm", "finalize"]},
            "question": {
                "type": "string",
                "description": "What to ask the user next — one question on the "
                "opening turn, a short numbered round after that. Plain text, no "
                "markdown. Required when action='ask'.",
            },
            "summary": {
                "type": "string",
                "description": "What you understood, for the user to check before "
                "anything is built. Required when action='confirm'. Plain text, one "
                "short bullet per decision, starting each line with '- '. Write it in "
                "the user's own words about their business — not about voice-agent "
                "configuration. End by inviting a correction.",
            },
            "agent_config": {
                **_AGENT_CONFIG_SCHEMA,
                "description": "The finalized config. Required when action='finalize'.",
            },
        },
        "required": ["action"],
    },
}

# The turn's structured output. ToolStrategy accepts a JSON-Schema dict directly,
# so this reuses the schema above rather than maintaining a parallel Pydantic
# mirror of ADR-0003's surface. `title` names the tool the model calls.
COMPOSE_SCHEMA: dict[str, Any] = {
    "title": COMPOSE_TOOL["name"],
    "description": COMPOSE_TOOL["description"],
    **COMPOSE_TOOL["input_schema"],
}


# --- Disciplines -------------------------------------------------------------
# The Builder's prompt is assembled from small, separately-editable pieces
# rather than one blob: a turn only carries the guidance it can act on, and each
# discipline can be read and tested on its own. Assembled by build_system_prompt.

_ROLE = """You are the TurnCall agent Builder. You turn a plain-language request \
into a TurnCall voice-agent configuration."""

# Opening turn. Someone deciding whether this feels like a conversation or a
# form decides it here, so it stays a single warm question.
_INTERVIEW_OPENING = """This is your first reply, so ask exactly ONE question — \
the single thing that most changes what you would build. Keep it warm and plain: \
someone who has never configured a voice agent should answer it in a sentence. \
Do not number it, and do not ask anything else yet."""

# Every turn after the first. One question per turn turns a five-decision design
# into a ten-turn interrogation, so ask everything that is answerable now.
_INTERVIEW_ROUNDS = """From here on, ask everything you can answer NOW in a single \
round rather than one question per turn.

- At most 4 questions, and only things that materially change the agent: its \
purpose, its tone, what it must collect or do, when it transfers or ends a call.
- Never include a question whose answer depends on another question in the same \
round — that one belongs in the next round.
- Number them 1., 2., 3.
- End every question with your own recommendation, on its own line, written so \
the user can accept it without deciding anything:
      Suggested: <what you would do, and why, in a few words>
- Close the round by telling them they can reply "all suggested" to take every \
suggestion as-is.
- Plain text only — no markdown, no asterisks, no bold. The console shows your \
text exactly as written.
- Never ask about infrastructure (STT, VAD, telephony, analysis). Those take \
defaults."""

# The user's own vocabulary is the most valuable thing they say and the easiest
# thing to throw away.
_LANGUAGE = """Use the user's own words. They will name what their business \
actually cares about — covers, guests, patients, jobs, riders, tickets. Carry \
those exact nouns and verbs into the system_prompt and into tool names, instead \
of translating them into generic ones like "customer" or "booking". The agent \
should sound like it already works there. A term you are unsure of is a good \
thing to ask about."""

_FINALIZE = """When you could confidently build the agent, do NOT build it yet. \
Call compose with action='confirm' and a summary of what you understood, so the \
user can catch a misunderstanding before anything is created.

The summary is about THEIR business, not about voice-agent configuration. One \
short line per decision, each starting with '- ': what the agent is for, what it \
collects or does, when it transfers or ends a call, what it must not do. Use \
their words and their numbers. Never list providers, models, voices, tool \
schemas or pipeline settings — those are not decisions they made. Close by \
asking them to say what is wrong, or to confirm.

Then read their reply:

- They confirm, or say anything approving ("looks right", "yes", "go ahead"): \
call compose with action='finalize' and the complete agent_config. Do NOT \
confirm a second time. A summary the user has already accepted is settled.
- They correct something: fold the correction in. Confirm again ONLY if the \
correction opened a genuinely new question; otherwise finalize directly.

Never send the same summary twice. If a summary already appears earlier in this \
conversation and the user did not reject it, finalize.

The system_prompt is the main deliverable: write a strong, specific prompt in \
the second person."""

_D_VOICE = """The agent you are writing answers a PHONE CALL. Its \
system_prompt must say how to SPEAK, not only what to know. Put these in it:

- Ask for ONE thing at a time. Never enumerate ("1. your name 2. the time 3. a \
phone number") — the caller cannot hold a list in their head, and TTS reads the \
numbers out loud. Gather details across a few short turns instead, confirming as \
you go.
- Keep replies to a sentence or two. A caller cannot skim.
- Never emit markdown, bullets, numbered lists or headings — everything is \
spoken verbatim.
- Say numbers, times and prices the way a person would say them out loud.
- When the caller asks for something the agent cannot do, warmly say what it CAN \
do and carry the call forward. Never a flat refusal, and never leave the caller \
with nothing to do next.
- Expect interruptions, half-sentences and background noise. Ask a short \
clarifying question rather than guessing.

This is the OPPOSITE of how you talk in the console. Your numbered rounds are \
for a text UI that the user reads; never copy that style into the agent's \
system_prompt."""

_D_DEFAULTS = """Defaults: llm = openai/gpt-4o-mini, tts = deepgram/\
'aura-2-helena-en'. Enable end_call/transfer_call only when the use case needs \
them. Enable send_dtmf when the agent must press keypad digits mid-call \
(navigate an IVR, enter a PIN/extension)."""

_D_PIPELINE = """Voice pipeline: default to cascade — leave pipeline_mode \
unset and use the tts block. Anything else (speech-to-speech, voicemail \
detection) is a departure from the default and has a skill covering it."""

_D_KNOWLEDGE = """Knowledge base: the user can attach documents (a menu, price \
list, policy, FAQ) to the agent, and the agent gets their full contents at call \
time. NEVER ask the user to paste a document's contents — this holds before \
anything is attached, which is exactly when someone says "I'll upload our menu"."""

_CLOSING = """Always respond by calling the compose tool."""


def build_system_prompt(
    *,
    first_turn: bool,
    current_config: dict[str, Any] | None = None,
    knowledge_docs: list[str] | None = None,
) -> str:
    """Assemble the Builder's prompt from the disciplines this turn can use.

    Editing an existing agent skips the interview disciplines entirely — the
    edit prompt's whole point is not to re-interview — but keeps the domain
    rules, because an edit still has to produce a valid config.
    """
    parts = [_ROLE]

    if current_config is None:
        parts.append(_INTERVIEW_OPENING if first_turn else _INTERVIEW_ROUNDS)
        parts.append(_LANGUAGE)

    # _D_KNOWLEDGE is unconditional: "never ask the user to paste a document"
    # has to hold *before* anything is attached — that is exactly when someone
    # says "I'll upload our menu". Only the filename list is conditional.
    # Tools, guardrails, speech-to-speech and reading attached documents are
    # skills now: each applies to some agents, so each is loaded when it applies
    # rather than shipped on every turn. What stays here is what is true of every
    # agent this builder writes (ADR-0014).
    parts += [
        _FINALIZE,
        _D_VOICE,
        _D_DEFAULTS,
        _D_PIPELINE,
        _D_KNOWLEDGE,
    ]

    if knowledge_docs:
        parts.append(
            "The agent's knowledge base already contains these documents (their "
            "full text is available to the agent at call time): "
            f"{', '.join(knowledge_docs)}. Build capabilities that rely on this "
            "content; do not ask the user to paste it."
        )

    if current_config is not None:
        parts.append(_EDIT_SYSTEM.format(config=json.dumps(current_config, indent=2)))

    parts.append(_CLOSING)
    return "\n\n".join(parts)


# Kept so callers and tests can still read the build-mode prompt as one string.
SYSTEM = build_system_prompt(first_turn=False)


@dataclass(frozen=True)
class ComposeResult:
    action: Literal["ask", "confirm", "finalize"]
    question: str | None = None
    # What the builder understood, shown for approval before anything is built.
    summary: str | None = None
    agent_config: dict[str, Any] | None = None


def _to_result(tool_input: dict[str, Any]) -> ComposeResult:
    """Pure: map the compose tool's input dict to a validated ComposeResult."""
    action = tool_input.get("action")
    if action == "ask":
        question = tool_input.get("question")
        if not question:
            raise ValueError("action='ask' requires a question")
        return ComposeResult(action="ask", question=question)
    if action == "confirm":
        summary = tool_input.get("summary")
        if not summary:
            raise ValueError("action='confirm' requires a summary")
        return ComposeResult(action="confirm", summary=summary)
    if action == "finalize":
        config = tool_input.get("agent_config")
        if not config:
            raise ValueError("action='finalize' requires an agent_config")
        return ComposeResult(action="finalize", agent_config=config)
    raise ValueError(f"unknown action: {action!r}")


_EDIT_SYSTEM = """

You are EDITING an existing agent, not building a new one. Its current \
configuration is:

{config}

Apply the user's requested change to THIS config and finalize the FULL updated \
config — keep every field the user did not ask to change. Do not re-interview or \
rebuild from scratch. Only action='ask' if the requested change is genuinely \
ambiguous."""


def _backend(documents_dir: str | None) -> CompositeBackend:
    """Scratch in state; skills and the user's documents read-only on disk.

    A mount appears only when there is something behind it — an empty one costs
    the agent a turn discovering it is empty.
    """
    routes: dict[str, Any] = {
        "/skills/": FilesystemBackend(root_dir=SKILLS_DIR, virtual_mode=True)
    }
    if documents_dir and os.path.isdir(documents_dir):
        routes["/documents/"] = FilesystemBackend(
            root_dir=documents_dir, virtual_mode=True
        )
    return CompositeBackend(default=StateBackend(), routes=routes)


def provider_availability() -> dict[str, bool]:
    """provider name -> whether its API key is configured in this deployment."""
    return {name: bool(os.environ.get(env)) for name, env in _KEY_ENV.items()}


def default_choice() -> tuple[str, str]:
    """The deployment-default (provider, model) used when a Session picked none."""
    return DEFAULT_PROVIDER, MODEL


@dataclass(frozen=True)
class Progress:
    """One human-readable thing the turn is doing, for the console to show.

    Derived, not raw: `read_file("/documents/menu.pdf", limit=1000)` is
    developer output, "Reading menu.pdf" is the product. `skill` carries the
    skill's own name because a skill's author has no other way to learn whether
    the description they wrote actually got their skill opened.
    """

    text: str
    skill: str | None = None


def _progress_for(name: str, args: dict[str, Any]) -> Progress | None:
    """A tool call the user should see, or None for the rest.

    An allowlist rather than a filter: the agent's scratch writes and its
    grep over the skills library are noise, and a progress feed that reports
    everything is a spinner with extra steps.
    """
    path = str(args.get("file_path") or args.get("path") or "")
    if name == "compose":
        return Progress("Writing the configuration")
    if name in ("ls", "glob", "grep") and path.startswith("/documents"):
        return Progress("Looking at what you attached")
    if name == "read_file" and path.startswith("/documents/"):
        return Progress(f"Reading {os.path.basename(path)}")
    if name == "read_file" and "/SKILL.md" in path:
        skill = path.rsplit("/", 2)[-2]
        return Progress(f"Using the {skill} skill", skill=skill)
    return None


def _messages_in(update: Any) -> list[Any]:
    """Pull messages out of a LangGraph `updates` payload — `{node: delta}`.

    Deliberately tolerant: node names belong to the framework and may change,
    and a progress line is not worth coupling to them. Missing a line degrades
    the feed; guessing a node name breaks it.
    """
    if not isinstance(update, dict):
        return []
    found: list[Any] = []
    for value in update.values():
        if isinstance(value, dict) and isinstance(value.get("messages"), list):
            found.extend(value["messages"])
    return found


def _progress_in(update: Any) -> list[Progress]:
    """Every progress line this update earns, in order."""
    out = []
    for message in _messages_in(update):
        for call in getattr(message, "tool_calls", None) or []:
            progress = _progress_for(call.get("name", ""), call.get("args") or {})
            if progress is not None:
                out.append(progress)
    return out


async def step(
    messages: list[dict[str, str]],
    *,
    provider: str | None = None,
    model: str | None = None,
    current_config: dict[str, Any] | None = None,
    knowledge_docs: list[str] | None = None,
    documents_dir: str | None = None,
) -> ComposeResult:
    """Run one builder turn. `messages` is the running [{role, content}] history.

    `provider`/`model` are the Session's Builder model; both-or-neither, absent
    means the deployment default. When `current_config` is set the builder edits
    it (grounded) instead of building from scratch. `knowledge_docs` are the
    filenames the agent has, and `documents_dir` is where their text can be read
    — the names alone tell it what exists, the directory lets it look inside.

    The agent may take several steps (reading a document, opening a skill), but
    the turn is bounded on both axes and there is no state kept between turns:
    the history passed in is the whole of it.
    """
    result: ComposeResult | None = None
    async for event in stream(
        messages,
        provider=provider,
        model=model,
        current_config=current_config,
        knowledge_docs=knowledge_docs,
        documents_dir=documents_dir,
    ):
        if isinstance(event, ComposeResult):
            result = event
    if result is None:  # stream() raises rather than finishing empty
        raise BuilderError("the builder finished without a result", kind="protocol")
    return result


async def stream(
    messages: list[dict[str, str]],
    *,
    provider: str | None = None,
    model: str | None = None,
    current_config: dict[str, Any] | None = None,
    knowledge_docs: list[str] | None = None,
    documents_dir: str | None = None,
) -> AsyncIterator[Progress | ComposeResult]:
    """One turn, yielding what it is doing and finally what it decided.

    The silence this exists to fill is not the model writing slowly — the
    compose result is emitted last, after the reading is done. It is the four
    things it does first, which is why the progress is tool calls rather than
    tokens.
    """
    provider = provider or DEFAULT_PROVIDER
    if provider not in _KEY_ENV:
        raise BuilderError(f"unknown builder provider: {provider!r}")

    # The opening turn gets one warm question; every turn after it gets a round.
    first_turn = not any(m.get("role") == "assistant" for m in messages)
    agent = create_deep_agent(
        model=f"{provider}:{model or MODEL}",
        system_prompt=build_system_prompt(
            first_turn=first_turn,
            current_config=current_config,
            knowledge_docs=knowledge_docs,
        ),
        backend=_backend(documents_dir),
        skills=["/skills/"],
        response_format=ToolStrategy(COMPOSE_SCHEMA),
    )

    # Two modes: `updates` carries each node's delta, which is where the tool
    # calls are; `values` carries the whole state, which is where the answer
    # reliably ends up. Deriving the result from deltas alone would couple us to
    # which node happens to emit it.
    final: dict[str, Any] = {}
    seen: set[str] = set()
    try:
        async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
            async for mode, chunk in agent.astream(
                # A copy, deliberately. LangGraph coerces the input messages to
                # BaseMessage objects *in place* — before the first model call,
                # so even a failed turn does it — and the list the caller passed
                # is the session history, which has to stay JSON-serialisable.
                {"messages": [dict(m) for m in messages]},
                config={"recursion_limit": MAX_STEPS},
                stream_mode=["updates", "values"],
            ):
                if mode == "values":
                    final = chunk or {}
                    continue
                for progress in _progress_in(chunk):
                    # The same skill read twice is one line; a feed that repeats
                    # itself reads as a stutter, not as progress.
                    if progress.text not in seen:
                        seen.add(progress.text)
                        yield progress
    except TimeoutError as exc:
        raise BuilderError(
            f"the builder took longer than {TURN_TIMEOUT_SECONDS:.0f}s", kind="timeout"
        ) from exc
    except BuilderError:
        raise
    except Exception as exc:
        raise BuilderError(str(exc), kind=classify_vendor_error(exc)) from exc

    result = final.get("structured_response")
    if result is None:
        # It ran out of steps, or answered in prose. Either way there is nothing
        # the console can render, so say so rather than inventing a turn.
        raise BuilderError("the builder finished without a result", kind="protocol")
    if not isinstance(result, dict):
        result = result.model_dump()
    try:
        yield _to_result(result)
    except ValueError as exc:
        raise BuilderError(str(exc), kind="protocol") from exc
