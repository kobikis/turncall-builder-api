"""When an attached app runs: mid-call, end-of-call, or both (ADR-0021)."""

import pytest
from fastapi import HTTPException

from app._helpers import _validate_agent_config
from app.backends.diff import automations_changed
from app.mapper import to_create_agent_request
from app.pipedream import mid_call_apps, parse_integrations

TARGET = {
    "project_id": "proj_1",
    "environment": "development",
    "external_user_id": "ws-1",
}

SLACK_SUMMARY = {
    "app": "slack_v2",
    "call_ended": {
        "action": "slack_v2-post-message",
        "args": {"channel": "#calls"},
    },
}


def _config(integrations, **over):
    cfg = {"name": "Bot", "system_prompt": "hi", "integrations": integrations, **over}
    return to_create_agent_request(cfg, pipedream_target=TARGET)["config"]


class TestReader:
    """`config.integrations` in either shape."""

    def test_a_bare_slug_means_mid_call(self):
        """The pre-ADR-0021 shape. Every stored config is this one, and an
        upgrade must not turn an end-of-call trigger on underneath a Workspace."""
        [entry] = parse_integrations(["slack_v2"])
        assert entry.app == "slack_v2"
        assert entry.mid_call is True
        assert entry.call_ended is None

    def test_the_object_form_carries_both_triggers(self):
        [entry] = parse_integrations([{**SLACK_SUMMARY, "mid_call": True}])
        assert entry.mid_call is True
        assert entry.call_ended.action == "slack_v2-post-message"
        assert entry.call_ended.args == {"channel": "#calls"}

    def test_end_of_call_only_is_not_a_mid_call_app(self):
        """The leak this ADR exists to close, at the reader."""
        assert mid_call_apps([SLACK_SUMMARY]) == []
        assert parse_integrations([SLACK_SUMMARY])[0].call_ended is not None

    def test_args_default_to_empty_rather_than_none(self):
        """The generated handler splats them; None would be a TypeError at the
        end of a call, where nothing is watching."""
        [entry] = parse_integrations([{"app": "slack_v2", "call_ended": {"action": "slack_v2-post-message"}}])
        assert entry.call_ended.args == {}

    def test_unreadable_entries_are_skipped_not_raised_on(self):
        """This runs against configs that are already stored. A 500 deep in the
        mapper is worse than an attachment that quietly does nothing — the API
        edge is where a bad shape gets refused."""
        assert parse_integrations([None, 7, {}, {"app": ""}, "slack_v2"]) == parse_integrations(["slack_v2"])

    def test_none_and_missing_read_as_empty(self):
        assert parse_integrations(None) == []
        assert parse_integrations([]) == []


class TestMapper:
    """Only the mid-call half reaches TurnCall."""

    def test_a_bare_slug_still_emits_its_server(self):
        servers = _config(["slack_v2"])["mcp_servers"]
        assert [s["headers"]["x-pd-app-slug"] for s in servers] == ["slack_v2"]

    def test_end_of_call_only_emits_no_server(self):
        """Before ADR-0021 every attachment put its tools in front of the model.
        A Workspace that wants only a summary must be able to decline that."""
        assert "mcp_servers" not in _config([SLACK_SUMMARY])

    def test_both_triggers_emit_the_server_once(self):
        servers = _config([{**SLACK_SUMMARY, "mid_call": True}])["mcp_servers"]
        assert len(servers) == 1
        assert servers[0]["tool_filter"]

    def test_the_end_of_call_half_never_reaches_turncall(self):
        """It drives code generation in the agent's own backend and has no
        business in an agent config."""
        config = _config([{**SLACK_SUMMARY, "mid_call": True}])
        assert "call_ended" not in repr(config)
        assert "#calls" not in repr(config)

    def test_mixed_shapes_coexist(self):
        servers = _config(["google_calendar", {**SLACK_SUMMARY, "mid_call": True}])["mcp_servers"]
        assert [s["headers"]["x-pd-app-slug"] for s in servers] == [
            "google_calendar",
            "slack_v2",
        ]


class TestValidation:
    """A bad attachment is refused at the edge, not stored."""

    def _reject(self, integrations):
        with pytest.raises(HTTPException) as exc:
            _validate_agent_config({"name": "Bot", "integrations": integrations})
        assert exc.value.status_code == 422
        return str(exc.value.detail)

    def test_an_entry_with_no_trigger_is_refused(self):
        """It would read as attached while exposing nothing and posting nothing."""
        assert "no trigger" in self._reject([{"app": "slack_v2"}])

    def test_an_unknown_app_is_refused(self):
        assert "unknown app" in self._reject([{"app": "notion", "mid_call": True}])

    def test_an_action_the_app_does_not_have_is_refused(self):
        """The handler logs and drops, so a wrong action looks exactly like a
        call that ended with nothing to say."""
        detail = self._reject(
            [{"app": "slack_v2", "call_ended": {"action": "slack_v2-delete-everything"}}]
        )
        assert "is not an action of" in detail

    def test_another_apps_action_is_refused(self):
        detail = self._reject(
            [{"app": "slack_v2", "call_ended": {"action": "google_calendar-create-event"}}]
        )
        assert "is not an action of" in detail

    def test_a_legacy_bare_slug_is_not_checked_against_the_catalog(self):
        """Validating it now would 422 an edit to a working agent over a field
        the edit did not touch."""
        _validate_agent_config({"name": "Bot", "integrations": ["retired_app"]})

    def test_a_good_attachment_passes(self):
        _validate_agent_config(
            {"name": "Bot", "integrations": [{**SLACK_SUMMARY, "mid_call": True}]}
        )


class TestRegeneration:
    """Automations are generated code, so an edit to one must rebuild."""

    def test_a_channel_edit_is_a_change(self):
        """The trap in ADR-0021: under the old condition this was accepted,
        stored, and never reached the container."""
        other = {**SLACK_SUMMARY, "call_ended": {**SLACK_SUMMARY["call_ended"], "args": {"channel": "#ops"}}}
        assert automations_changed({"integrations": [SLACK_SUMMARY]}, {"integrations": [other]})

    def test_adding_an_automation_is_a_change(self):
        assert automations_changed({"integrations": ["slack_v2"]}, {"integrations": [SLACK_SUMMARY]})

    def test_removing_an_automation_is_a_change(self):
        assert automations_changed({"integrations": [SLACK_SUMMARY]}, {"integrations": []})

    def test_a_prompt_edit_is_not_a_change(self):
        """A rebuild costs tens of seconds of docker build; an edit that touches
        no generated code must not pay for one."""
        old = {"integrations": [SLACK_SUMMARY], "system_prompt": "hi"}
        new = {"integrations": [SLACK_SUMMARY], "system_prompt": "hello"}
        assert not automations_changed(old, new)

    def test_toggling_mid_call_is_not_a_change(self):
        """It moves `mcp_servers`, which is config pushed to TurnCall — no
        generated code changes, so no rebuild."""
        old = {"integrations": [SLACK_SUMMARY]}
        new = {"integrations": [{**SLACK_SUMMARY, "mid_call": True}]}
        assert not automations_changed(old, new)

    def test_arg_key_order_is_not_a_change(self):
        a = {"app": "slack_v2", "call_ended": {"action": "slack_v2-post-message", "args": {"channel": "#c", "thread": "1"}}}
        b = {"app": "slack_v2", "call_ended": {"action": "slack_v2-post-message", "args": {"thread": "1", "channel": "#c"}}}
        assert not automations_changed({"integrations": [a]}, {"integrations": [b]})

    def test_attachment_order_is_not_a_change(self):
        cal = {"app": "google_calendar", "call_ended": {"action": "google_calendar-create-event", "args": {}}}
        old = {"integrations": [SLACK_SUMMARY, cal]}
        new = {"integrations": [cal, SLACK_SUMMARY]}
        assert not automations_changed(old, new)

    def test_a_legacy_config_reads_as_no_automations(self):
        assert not automations_changed({"integrations": ["slack_v2"]}, {"integrations": ["slack_v2"]})
