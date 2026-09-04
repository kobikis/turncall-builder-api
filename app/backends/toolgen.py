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
                            "Must `return` a dict. Realistic stub logic; no imports beyond stdlib.",
                        },
                    },
                    "required": ["name", "body"],
                },
            }
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
            "Write realistic stub logic (validate expected args, return a plausible result). "
            "No external imports. Always call the tool_bodies tool.",
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
            items = (block.input or {}).get("bodies", [])
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                body = _normalize(str(item.get("body") or ""))
                if name and body and _valid(body):
                    out[name] = body
    except Exception:
        return out  # whatever parsed so far; the rest stub out
    return out
