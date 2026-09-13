"""LLM-authored per-tool handler bodies (ADR-0004, codegen = hybrid).

Best-effort: asks Claude to write the body of each `async def tool_<name>(args)`,
validates each compiles, and drops any that don't. On any failure the scaffold's
stub body is used instead — so a bad LLM turn never blocks agent creation.
"""

from __future__ import annotations

import ast
import textwrap
from typing import Any

from anthropic import AsyncAnthropic

_SCHEMA = {
    "name": "tool_bodies",
    "description": "Return a Python body for each tool handler.",
    "input_schema": {
        "type": "object",
        "properties": {
            "bodies": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "body": {
                            "type": "string",
                            "description": "Body statements for `async def tool_<name>(args: dict) -> dict:`. "
                            "Must `return` a dict. Local logic only — validate arguments, compute, "
                            "record. No imports beyond stdlib.",
                        },
                    },
                    "required": ["name", "body"],
                },
            },
            "needs_a_service_i_cannot_reach": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Names of tools whose work requires a third-party service "
                "(calendar, CRM, email, messaging, payments, any external API). Do NOT write "
                "bodies for these — list them here instead.",
            },
        },
        "required": ["bodies"],
    },
}


def _valid(body: str) -> bool:
    try:
        ast.parse(f"async def _f(args, name=''):\n{body}")
        return True
    except SyntaxError:
        return False


def _normalize(body: str) -> str:
    body = textwrap.dedent(body).strip("\n")
    return textwrap.indent(body, "    ")


async def generate_tool_bodies(
    tools: list[dict[str, Any]], client: AsyncAnthropic | None = None
) -> dict[str, str]:
    """Return {tool_name: indented_body}. Missing/invalid tools fall back to stubs."""
    if not tools:
        return {}
    listing = "\n".join(
        f"- {t['name']}: {t.get('description', '')} params={t.get('parameters_schema', {})}"
        for t in tools
    )
    try:
        client = client or AsyncAnthropic(max_retries=2, timeout=60.0)
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system="You write Python handler bodies for voice-agent tools. Each body is the "
            "statements of `async def tool_<name>(args: dict) -> dict:` and must return a dict. "
            "No external imports.\n\n"
            "Write a body ONLY for a tool whose work is local: validating arguments, computing "
            "something, recording it in the backend's own store. For anything needing a "
            "third-party service — a calendar, a CRM, email, messaging, payments — you have no "
            "credentials and cannot reach it. Do not simulate one. Put its name in "
            "`needs_a_service_i_cannot_reach` and write no body.\n\n"
            "A fabricated success is the worst thing you can return here: it reports `success` "
            "for a write that never happened, the caller is told their booking is confirmed, and "
            "nothing distinguishes it from a working integration. An honest stub is labelled as "
            "one in the console; a convincing lie is not.\n\n"
            "Always call the tool_bodies tool.",
            tools=[_SCHEMA],
            tool_choice={"type": "tool", "name": "tool_bodies"},
            messages=[{"role": "user", "content": f"Tools:\n{listing}"}],
        )
    except Exception:
        return {}

    return _parse_bodies(resp)


def _parse_bodies(resp: Any) -> dict[str, str]:
    """Extract {name: body} from the model response. The contract is best-effort:
    any malformed item (the model sometimes emits bare strings instead of
    objects) is skipped — its tool falls back to the stub, never an exception."""
    out: dict[str, str] = {}
    try:
        for block in resp.content:
            if block.type != "tool_use" or block.name != "tool_bodies":
                continue
            payload = block.input or {}
            # Belt and braces: a model that both lists a tool as unreachable and
            # writes it a body meant the refusal — take that, not the fabrication.
            declined = {
                n for n in payload.get("needs_a_service_i_cannot_reach") or [] if isinstance(n, str)
            }
            for item in payload.get("bodies", []):
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                body = _normalize(str(item.get("body") or ""))
                if name and name not in declined and body and _valid(body):
                    out[name] = body
    except Exception:
        return out  # whatever parsed so far; the rest stub out
    return out
