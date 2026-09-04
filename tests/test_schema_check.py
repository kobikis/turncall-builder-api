"""Drift guard: the builder's emitted config fields vs the live TurnCall schema."""

import logging

import pytest

from app import schema_check


def _spec(fields: set[str]) -> dict:
    return {
        "components": {
            "schemas": {
                "AgentConfigSchema": {"properties": {f: {} for f in fields}}
            }
        }
    }


def test_emitted_fields_match_the_mapper():
    # Guards the probe stays in sync with what mapper.to_create_agent_request emits.
    assert schema_check.emitted_config_fields() == {
        "system_prompt",
        "first_message",
        "llm",
        "tts",
        "tools",
        "analysis",
        "pipeline_mode",
        "s2s",
        "voicemail_detection",
        "guardrails",
    }


def test_live_config_fields_extracts_properties():
    assert schema_check.live_config_fields(_spec({"llm", "tts"})) == {"llm", "tts"}
    assert schema_check.live_config_fields(None) == set()
    assert schema_check.live_config_fields({}) == set()


def test_reconcile_splits_unknown_and_unused():
    unknown, unused = schema_check.reconcile({"a", "b"}, {"b", "c"})
    assert unknown == {"a"}  # builder sends it, API lacks it -> drift
    assert unused == {"c"}  # API has it, builder skips it -> headroom


class _Client:
    def __init__(self, spec):
        self._spec = spec

    async def get_openapi(self):
        return self._spec


@pytest.mark.asyncio
async def test_check_drift_errors_when_api_drops_a_field(caplog):
    # Live schema is missing 'analysis' that the builder sends -> ERROR.
    live = schema_check.emitted_config_fields() - {"analysis"}
    with caplog.at_level(logging.ERROR):
        await schema_check.check_drift(_Client(_spec(live)))
    assert any("DRIFT" in r.message and "analysis" in str(r.args) for r in caplog.records)


@pytest.mark.asyncio
async def test_check_drift_quiet_when_aligned(caplog):
    # Live schema covers every emitted field (plus headroom) -> no ERROR.
    live = schema_check.emitted_config_fields() | {"s2s", "knowledge_bases"}
    with caplog.at_level(logging.ERROR):
        await schema_check.check_drift(_Client(_spec(live)))
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_check_drift_skips_when_unreachable(caplog):
    # None spec (TurnCall down) must not raise or error.
    await schema_check.check_drift(_Client(None))
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
