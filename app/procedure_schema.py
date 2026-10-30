"""The composer's schema for an agent's procedures (#137).

TurnCall's shape (turncall#417, `api/v1/schemas/procedures.py`), less the
handoff escalation. TurnCall is the authority: it checks the graph at save and
its 422 names the procedure and the step (`_helpers.field_errors`).
"""

from __future__ import annotations

from typing import Any

# Guided procedures (turncall#417, #137): TurnCall's shape, less the handoff
# escalation — each Builder agent is its own project, so a handoff to another
# Builder agent cannot work. One already in a config is kept as written: the
# edit prompt carries the whole config, and an edit that leaves `procedures`
# out keeps them (sessions._keep_procedures).
_STEP_ID = {"type": "string", "pattern": "^[A-Za-z0-9_-]+$", "maxLength": 64}
_PROCEDURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "pattern": "^[a-z0-9_]+$",
            "description": "snake_case, in the user's words: no_internet, missed_payment.",
        },
        "description": {
            "type": "string",
            "description": "When the procedure applies, in one sentence.",
        },
        "start": {
            "type": "string",
            "enum": ["call", "on_request"],
            "description": "call: it runs from the first turn (an agent whose "
            "every call is this process). on_request: the agent starts it when "
            "it applies (one of several things a caller may want).",
        },
        "window_depth": {
            "type": "integer",
            "minimum": 0,
            "maximum": 5,
            "description": "Steps ahead the agent is shown. Leave it out (1).",
        },
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 50,
            "items": {
                "type": "object",
                "properties": {
                    "id": {**_STEP_ID, "description": "snake_case, unique in the procedure."},
                    "kind": {
                        "type": "string",
                        "enum": ["ask", "instruct", "check", "escalate", "end"],
                        "description": "ask a question, instruct the caller, check "
                        "something (a tool may help), escalate (a transfer), or end.",
                    },
                    "text": {
                        "type": "string",
                        "description": "What the agent does at this step, in the "
                        "second person. On escalate and end, what the caller hears.",
                    },
                    "start": {
                        "type": "boolean",
                        "description": "true on exactly one step: where it starts.",
                    },
                    "tool": {
                        "type": "string",
                        "description": "A tool the agent calls at this step: one of "
                        "its own `tools` or `custom_tools`, by name.",
                    },
                    "moves": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "to": {**_STEP_ID, "description": "The step it goes to."},
                                "when": {
                                    "type": "string",
                                    "description": "The condition, in plain language.",
                                },
                            },
                            "required": ["to", "when"],
                        },
                        "description": "Where the agent may go from here. Going "
                        "back one step is always allowed and needs none.",
                    },
                    "escalate": {
                        "type": "object",
                        "description": "Required on an escalate step, nowhere else.",
                        "properties": {
                            "transfer_call": {
                                "type": "object",
                                "properties": {
                                    "target_number": {
                                        "type": "string",
                                        "description": "E.164, e.g. +15550009999: a "
                                        "number the user gave.",
                                    },
                                    "transfer_mode": {"type": "string", "enum": ["cold", "warm"]},
                                    "briefing": {
                                        "anyOf": [
                                            {"type": "string"},
                                            {
                                                "type": "object",
                                                "properties": {"from_summary": {"type": "boolean"}},
                                            },
                                        ],
                                        "description": "Warm only: what the human "
                                        'hears first — a text, or {"from_summary": '
                                        "true} for a summary of the call.",
                                    },
                                    "transfer_message": {"type": "string"},
                                    "fallback_message": {
                                        "type": "string",
                                        "description": "Said when nobody answers.",
                                    },
                                },
                                "required": ["target_number"],
                            },
                        },
                        "required": ["transfer_call"],
                    },
                },
                "required": ["id", "kind", "text"],
            },
        },
    },
    "required": ["name", "steps"],
}
PROCEDURES_PROPERTY: dict[str, Any] = {
    "type": "array",
    "items": _PROCEDURE_SCHEMA,
    "description": "Step-by-step processes the agent follows on a call "
    "(troubleshooting, collections, intake, a regulated script). Write one "
    "whenever the user describes such a process; open the procedures builder "
    "skill first. Keep the ones already in the config unless the user changes them.",
}
