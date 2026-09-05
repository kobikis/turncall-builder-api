"""The builder loop: prompt -> follow-up questions -> finalized TurnCall config.

One LLM call per turn (Anthropic or OpenAI — the Session's Builder model),
forced to a structured `compose` tool that returns either the next question
(`ask`) or a complete agent config (`finalize`). See ADR-0001 and ADR-0003.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import anthropic
import openai
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

# BUILDER_* is canonical; COMPOSER_* accepted as a legacy fallback (pre-rename).
DEFAULT_PROVIDER = os.environ.get(
    "BUILDER_PROVIDER", os.environ.get("COMPOSER_PROVIDER", "anthropic")
)
MODEL = os.environ.get(
    "BUILDER_MODEL", os.environ.get("COMPOSER_MODEL", "claude-sonnet-4-6")
)

# Which env key unlocks each provider — the picker only offers keyed ones.
_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


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
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
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
            "action": {"type": "string", "enum": ["ask", "finalize"]},
            "question": {
                "type": "string",
                "description": "What to ask the user next — one question on the "
                "opening turn, a short numbered round after that. Plain text, no "
                "markdown. Required when action='ask'.",
            },
            "agent_config": {
                **_AGENT_CONFIG_SCHEMA,
                "description": "The finalized config. Required when action='finalize'.",
            },
        },
        "required": ["action"],
    },
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

_FINALIZE = """When you could confidently build the agent, call compose with \
action='finalize' and a complete agent_config. The system_prompt is the main \
deliverable: write a strong, specific prompt in the second person."""

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

_D_PIPELINE = """Voice pipeline: default to cascade (leave pipeline_mode unset). \
Choose s2s ONLY when the user explicitly wants the most natural / real-time / \
ultra-low-latency voice ("as human as possible", "no lag", "real-time"). For \
s2s, set pipeline_mode='s2s' and an s2s block: default provider openai with \
model 'gpt-4o-realtime-preview' and an OpenAI voice (alloy/ash/ballad/coral/\
echo/sage/shimmer/verse); use google (model \
'models/gemini-3.1-flash-live-preview' + a Gemini voice like Kore/Puck/Charon) \
if the user prefers Gemini, or aws (model 'amazon.nova-2-sonic-v1:0' + a Nova \
Sonic voice like matthew/tiffany/amy) if they want it on their own AWS account. \
Pick the voice from the chosen provider's set. In s2s the tts block and \
voicemail detection do not apply — never combine s2s with voicemail.

Voicemail: enable voicemail_detection ONLY for outbound / dialer agents placing \
calls that may reach voicemail, when the user wants a message left. Set \
voicemail_detection.enabled=true and a voicemail_message. Cascade only. Leave it \
off for inbound-only agents."""

_D_TOOLS = """When the agent must perform an external action (book, cancel, look \
up an order), author a custom_tool for it (snake_case name, description, \
parameters_schema). Don't ask for a URL — the builder generates a backend and \
wires it. If the user VOLUNTEERS their own API URL, set server_url (agent-wide \
base) or a tool's server_url (full URL), but never ask.

When the agent should hand the call to a human, ask ONE question: is the \
transfer number fixed (and what is it), or does it depend on runtime logic \
(who's on call, the caller, the topic)? Fixed: enable transfer_call and write \
the number and the when-to-transfer conditions into the system_prompt. Depends: \
ALSO author a get_transfer_number custom_tool (description: "Returns the phone \
number of the human to transfer to right now"; no parameters unless the routing \
needs them) and instruct the system_prompt to call get_transfer_number first, \
then transfer_call with the returned number. Either way the system_prompt should \
tell the agent to announce the transfer (transfer_message) and what to say if \
the human doesn't answer."""

_D_GUARDRAILS = """Guardrails: when the user wants the agent to stay off certain \
subjects (no medical/legal/financial advice, no discussing competitors), set \
guardrails.prohibited_topics to a short list. The platform enforces them as a \
refusal rule — you don't also need to restate them in the system_prompt."""

_D_KNOWLEDGE = """Knowledge base: the user can attach documents (a menu, price \
list, policy, FAQ) to the agent. You CANNOT read them, but the agent gets their \
full contents at call time. When the user references an attached document, \
assume the agent will have it: write the system_prompt to answer from its \
knowledge base, and add any action tool the request implies (e.g. a \
place_room_service_order tool for ordering from a menu). NEVER ask the user to \
paste a document's contents."""

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
    parts += [
        _FINALIZE,
        _D_VOICE,
        _D_DEFAULTS,
        _D_PIPELINE,
        _D_TOOLS,
        _D_GUARDRAILS,
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
    action: Literal["ask", "finalize"]
    question: str | None = None
    agent_config: dict[str, Any] | None = None


def _to_result(tool_input: dict[str, Any]) -> ComposeResult:
    """Pure: map the compose tool's input dict to a validated ComposeResult."""
    action = tool_input.get("action")
    if action == "ask":
        question = tool_input.get("question")
        if not question:
            raise ValueError("action='ask' requires a question")
        return ComposeResult(action="ask", question=question)
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


class BuilderProvider(Protocol):
    """One vendor's forced-compose call. Returns the compose tool's input dict."""

    async def compose(
        self, *, model: str, system: str, messages: list[dict[str, str]]
    ) -> dict[str, Any]: ...


class AnthropicBuilder:
    def __init__(self, client: AsyncAnthropic | None = None) -> None:
        # Retries cover transient connect/timeout blips before the user sees them.
        self._client = client or AsyncAnthropic(max_retries=4, timeout=60.0)

    async def compose(
        self, *, model: str, system: str, messages: list[dict[str, str]]
    ) -> dict[str, Any]:
        try:
            resp = await self._client.messages.create(
                model=model,
                max_tokens=4096,
                system=system,
                tools=[COMPOSE_TOOL],
                tool_choice={"type": "tool", "name": "compose"},
                messages=messages,
            )
        except anthropic.APIError as exc:
            raise BuilderError(str(exc), kind=classify_vendor_error(exc)) from exc
        for block in resp.content:
            if block.type == "tool_use" and block.name == "compose":
                return block.input
        raise BuilderError("builder did not call the compose tool", kind="protocol")


class OpenAIBuilder:
    def __init__(self, client: AsyncOpenAI | None = None) -> None:
        self._client = client or AsyncOpenAI(max_retries=4, timeout=60.0)

    async def compose(
        self, *, model: str, system: str, messages: list[dict[str, str]]
    ) -> dict[str, Any]:
        # Responses API, not chat completions: it's the only endpoint that takes
        # function tools across ALL current OpenAI model families (standard,
        # reasoning gpt-5.x, and -pro models, which 404 on /v1/chat/completions).
        tool = {
            "type": "function",
            "name": COMPOSE_TOOL["name"],
            "description": COMPOSE_TOOL["description"],
            "parameters": COMPOSE_TOOL["input_schema"],
        }
        try:
            resp = await self._client.responses.create(
                model=model,
                # Reasoning models spend output budget thinking before the call.
                max_output_tokens=8192,
                instructions=system,
                input=messages,
                tools=[tool],
                tool_choice={"type": "function", "name": "compose"},
            )
        except openai.APIError as exc:
            raise BuilderError(str(exc), kind=classify_vendor_error(exc)) from exc
        for item in resp.output or []:
            if getattr(item, "type", None) == "function_call" and item.name == "compose":
                try:
                    return json.loads(item.arguments)
                except json.JSONDecodeError as exc:
                    raise BuilderError("malformed compose tool arguments", kind="protocol") from exc
        raise BuilderError("builder did not call the compose tool", kind="protocol")


_PROVIDERS: dict[str, type[AnthropicBuilder] | type[OpenAIBuilder]] = {
    "anthropic": AnthropicBuilder,
    "openai": OpenAIBuilder,
}


def provider_availability() -> dict[str, bool]:
    """provider name -> whether its API key is configured in this deployment."""
    return {name: bool(os.environ.get(env)) for name, env in _KEY_ENV.items()}


def default_choice() -> tuple[str, str]:
    """The deployment-default (provider, model) used when a Session picked none."""
    return DEFAULT_PROVIDER, MODEL


async def step(
    messages: list[dict[str, str]],
    client: AsyncAnthropic | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    current_config: dict[str, Any] | None = None,
    knowledge_docs: list[str] | None = None,
) -> ComposeResult:
    """Run one builder turn. `messages` is the running [{role, content}] history.

    `provider`/`model` are the Session's Builder model; both-or-neither, absent
    means the deployment default. `client` is a test seam: an Anthropic-shaped
    client that forces the anthropic path. When `current_config` is set the
    builder edits it (grounded) instead of building from scratch.
    `knowledge_docs` are filenames in the agent's knowledge base, so the
    builder knows the agent has that content.
    """
    if client is not None:
        provider, impl = "anthropic", AnthropicBuilder(client)
    else:
        provider = provider or DEFAULT_PROVIDER
        provider_cls = _PROVIDERS.get(provider)
        if provider_cls is None:
            raise BuilderError(f"unknown builder provider: {provider!r}")
        impl = provider_cls()
    # The opening turn gets one warm question; every turn after it gets a round.
    first_turn = not any(m.get("role") == "assistant" for m in messages)
    system = build_system_prompt(
        first_turn=first_turn,
        current_config=current_config,
        knowledge_docs=knowledge_docs,
    )
    tool_input = await impl.compose(
        model=model or MODEL, system=system, messages=messages
    )
    return _to_result(tool_input)
