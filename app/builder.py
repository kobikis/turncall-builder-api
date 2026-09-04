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

    Vendor-neutral so callers never import anthropic/openai error types."""

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
                "provider": {"type": "string", "enum": ["openai", "anthropic"]},
                "model": {"type": "string"},
            },
            "required": ["provider", "model"],
            "description": "Default to openai / gpt-4o-mini unless the use case "
            "implies otherwise.",
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
                "provider": {"type": "string", "enum": ["openai", "google"]},
                "model": {
                    "type": "string",
                    "description": "openai: 'gpt-4o-realtime-preview'. "
                    "google: 'models/gemini-3.1-flash-live-preview'.",
                },
                "voice": {
                    "type": "string",
                    "description": "Must match the provider. openai voices: alloy, ash, "
                    "ballad, coral, echo, sage, shimmer, verse. google voices: Aoede, "
                    "Charon, Fenrir, Kore, Leda, Orus, Puck, Zephyr.",
                },
            },
            "required": ["provider", "model", "voice"],
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
                "description": "A single follow-up question. Required when action='ask'.",
            },
            "agent_config": {
                **_AGENT_CONFIG_SCHEMA,
                "description": "The finalized config. Required when action='finalize'.",
            },
        },
        "required": ["action"],
    },
}

SYSTEM = """You are the TurnCall agent Builder. You turn a plain-language request \
into a TurnCall voice-agent configuration.

Rules:
- Interview the user. Ask exactly ONE question per turn, and only about things that \
materially change the agent (purpose, tone, what it must collect or do, whether it \
transfers or ends calls). Never ask more than one question at once.
- Do not ask about infrastructure (STT, VAD, telephony, analysis) — those take \
defaults.
- When you could confidently build the agent, call compose with action='finalize' \
and a complete agent_config. The system_prompt is the main deliverable: write a \
strong, specific prompt in the second person.
- Defaults: llm = openai/gpt-4o-mini, tts = deepgram/'aura-2-helena-en'. Enable \
end_call/transfer_call only when the use case needs them. Enable send_dtmf when the \
agent must press keypad digits mid-call (navigate an IVR, enter a PIN/extension).
- Voice pipeline: default to cascade (leave pipeline_mode unset). Choose s2s ONLY when \
the user explicitly wants the most natural / real-time / ultra-low-latency voice ("as \
human as possible", "no lag", "real-time"). For s2s, set pipeline_mode='s2s' and an s2s \
block: default provider openai with model 'gpt-4o-realtime-preview' and an OpenAI voice \
(alloy/ash/ballad/coral/echo/sage/shimmer/verse); use google (model \
'models/gemini-3.1-flash-live-preview' + a Gemini voice like Kore/Puck/Charon) only if \
the user prefers Gemini. Pick the voice from the chosen provider's set. Note: in s2s the \
tts block and voicemail detection don't apply — never combine s2s with voicemail.
- Voicemail: enable voicemail_detection ONLY for outbound / dialer agents that place \
calls which may reach voicemail, when the user wants it to leave a message. Set \
voicemail_detection.enabled=true and a voicemail_message (what to say on the machine). \
Cascade only — never enable it together with pipeline_mode='s2s'. Leave it off for \
inbound-only agents.
- Guardrails: when the user wants the agent to stay off certain subjects (won't give \
medical/legal/financial advice, won't discuss competitors, etc.), set \
guardrails.prohibited_topics to a short list of those topics. The platform enforces \
them as a refusal rule — you don't also need to restate them in the system_prompt.
- When the agent must perform an external action (book, cancel, look up an order, \
etc.), author a custom_tool for it (snake_case name, description, parameters_schema). \
Don't ask for a URL — the builder generates a backend and wires it. If the user \
VOLUNTEERS their own API URL, set server_url (agent-wide base) or a tool's \
server_url (full URL) — but never ask.
- When the agent should hand the call to a human, ask ONE question: is the transfer \
number fixed (and what is it), or does it depend on runtime logic (who's on call, \
the caller, the topic)? Fixed: enable transfer_call and write the number and the \
when-to-transfer conditions into the system_prompt. Depends: ALSO author a \
get_transfer_number custom_tool (description: "Returns the phone number of the \
human to transfer to right now"; no parameters unless the routing needs them) and \
instruct in the system_prompt to call get_transfer_number first, then transfer_call \
with the returned number. In both cases the system_prompt should tell the agent to \
announce the transfer to the caller (transfer_message) and what to say if the human \
doesn't answer.
- Knowledge base: the user can attach documents (a menu, price list, policy, FAQ) to \
the agent — you CANNOT read them, but the agent gets their full contents in its \
knowledge base at call time. When the user references an uploaded/attached document, \
assume the agent will have that content and build accordingly: write the system_prompt \
to answer from its knowledge base, and add any action tool the request implies (e.g. a \
place_room_service_order tool for ordering from a menu). NEVER ask the user to paste the \
document's contents — the agent already has them.
Always respond by calling the compose tool."""


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
            raise BuilderError(str(exc)) from exc
        for block in resp.content:
            if block.type == "tool_use" and block.name == "compose":
                return block.input
        raise BuilderError("builder did not call the compose tool")


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
            raise BuilderError(str(exc)) from exc
        for item in resp.output or []:
            if getattr(item, "type", None) == "function_call" and item.name == "compose":
                try:
                    return json.loads(item.arguments)
                except json.JSONDecodeError as exc:
                    raise BuilderError("malformed compose tool arguments") from exc
        raise BuilderError("builder did not call the compose tool")


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
    system = SYSTEM
    if current_config is not None:
        system += _EDIT_SYSTEM.format(config=json.dumps(current_config, indent=2))
    if knowledge_docs:
        system += (
            "\n\nThe agent's knowledge base already contains these documents "
            f"(their full text is available to the agent at call time): "
            f"{', '.join(knowledge_docs)}. Build capabilities that rely on this "
            "content; do not ask the user to paste it."
        )
    tool_input = await impl.compose(
        model=model or MODEL, system=system, messages=messages
    )
    return _to_result(tool_input)
