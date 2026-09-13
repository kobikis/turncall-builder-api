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
        "summary_arg": "text",
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
        [entry] = parse_integrations([{"app": "slack_v2", "call_ended": {"action": "slack_v2-post-message", "summary_arg": "text"}}])
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
        assert "set to do nothing" in self._reject([{"app": "slack_v2"}])

    def test_an_unknown_app_is_refused(self):
        assert "not an app this build can connect" in self._reject([{"app": "notion", "mid_call": True}])

    def test_an_action_the_app_does_not_have_is_refused(self):
        """The handler logs and drops, so a wrong action looks exactly like a
        call that ended with nothing to say."""
        detail = self._reject(
            [{"app": "slack_v2", "call_ended": {"action": "slack_v2-delete-everything", "summary_arg": "text"}}]
        )
        assert "cannot do that when a call ends" in detail

    def test_another_apps_action_is_refused(self):
        detail = self._reject(
            [{"app": "slack_v2", "call_ended": {"action": "google_calendar-create-event", "summary_arg": "text"}}]
        )
        assert "cannot do that when a call ends" in detail

    def test_a_mid_call_tool_is_not_automatically_an_end_of_call_action(self):
        """`list-channels` is worth calling during a call and meaningless after
        one. The two menus are different questions."""
        detail = self._reject(
            [{"app": "slack_v2", "call_ended": {"action": "slack_v2-list-channels", "summary_arg": "text"}}]
        )
        assert "cannot do that when a call ends" in detail

    def test_the_calendar_action_is_offered_like_any_other(self):
        """Every catalog app gets the same choice. Calendar's `create-event`
        needs a start and an end; they come from the call, not the form."""
        cfg = _config(
            [
                {
                    "app": "google_calendar",
                    "call_ended": {
                        "action": "google_calendar-create-event",
                        "args": {"calendarId": "primary", "summary": "Customer call"},
                        "summary_arg": "description",
                    },
                }
            ]
        )
        # End-of-call only: the model is handed no calendar tool.
        assert "google_calendar" not in repr(cfg.get("mcp_servers") or [])

    def test_the_calendar_action_still_names_what_it_needs(self):
        detail = self._reject(
            [
                {
                    "app": "google_calendar",
                    "call_ended": {
                        "action": "google_calendar-create-event",
                        "args": {"summary": "Customer call"},
                        "summary_arg": "description",
                    },
                }
            ]
        )
        assert "needs a Calendar" in detail

    def test_a_missing_input_names_the_input(self):
        """"Needs a Channel" is actionable; a schema path is not."""
        detail = self._reject(
            [
                {
                    "app": "slack_v2",
                    "call_ended": {"action": "slack_v2-post-message", "args": {}, "summary_arg": "text"},
                }
            ]
        )
        assert "needs a Channel" in detail

    def test_a_refusal_never_leaks_the_validator(self):
        """The message is read by a person in a form, not by a developer in a
        traceback. Class names, union branch tags, the offending value echoed
        back, and a link to pydantic's docs are all noise they cannot act on —
        and the union that produced two errors for one mistake is gone."""
        detail = self._reject([{"app": "slack_v2"}])
        for leak in ("pydantic", "_AgentConfigShape", "_IntegrationShape",
                     "function-after", "type=", "input_value", "validation error"):
            assert leak not in detail, f"{leak!r} leaked into a user-facing message"

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
        a = {"app": "slack_v2", "call_ended": {"action": "slack_v2-post-message", "args": {"channel": "#c", "thread": "1"}, "summary_arg": "text"}}
        b = {"app": "slack_v2", "call_ended": {"action": "slack_v2-post-message", "args": {"thread": "1", "channel": "#c"}, "summary_arg": "text"}}
        assert not automations_changed({"integrations": [a]}, {"integrations": [b]})

    def test_attachment_order_is_not_a_change(self):
        # A second automation; the diff never validates, so any shape will do.
        cal = {"app": "google_calendar", "call_ended": {"action": "x", "args": {}, "summary_arg": "s"}}
        old = {"integrations": [SLACK_SUMMARY, cal]}
        new = {"integrations": [cal, SLACK_SUMMARY]}
        assert not automations_changed(old, new)

    def test_a_legacy_config_reads_as_no_automations(self):
        assert not automations_changed({"integrations": ["slack_v2"]}, {"integrations": ["slack_v2"]})


class TestNeedsIntegrations:
    """A request, not an attachment."""

    def _reject(self, config):
        with pytest.raises(HTTPException) as exc:
            _validate_agent_config({"name": "Bot", **config})
        assert exc.value.status_code == 422
        return str(exc.value.detail)

    def test_an_agent_can_record_what_it_needs(self):
        _validate_agent_config({"name": "Bot", "needs_integrations": ["google_calendar"]})

    def test_it_exposes_no_tool_by_itself(self):
        """Only `integrations` puts something in front of the model. Needing an
        app and having one are different states, and the difference is the
        Workspace's decision."""
        config = to_create_agent_request(
            {"name": "Bot", "system_prompt": "hi", "needs_integrations": ["google_calendar"]},
            pipedream_target=TARGET,
        )["config"]
        assert "mcp_servers" not in config

    def test_an_app_nobody_can_connect_is_refused(self):
        assert "not an app this build can connect" in self._reject(
            {"needs_integrations": ["notion"]}
        )

    def test_a_custom_tool_that_shadows_an_integration_is_refused(self):
        """The failure this backstops: a `book_table` tool described as saving to
        Google Calendar, which has no credentials and can only pretend."""
        detail = self._reject({
            "custom_tools": [{
                "name": "book_table",
                "description": "Saves a confirmed reservation to the restaurant's "
                "Google Calendar so floor staff can see it.",
            }]
        })
        assert "has no Google Calendar credentials" in detail
        assert "Integrations page" in detail

    def test_an_ordinary_custom_tool_is_untouched(self):
        _validate_agent_config({
            "name": "Bot",
            "custom_tools": [{
                "name": "create_reservation",
                "description": "Records a reservation in the restaurant's book.",
            }],
        })


CALENDAR_ENTRY = {
    "app": "google_calendar",
    "call_ended": {
        "action": "google_calendar-create-event",
        "args": {"calendarId": "primary", "summary": "Customer call"},
        "summary_arg": "description",
    },
}


def _run_automations(integrations, payload, tmp_path, monkeypatch):
    """Execute the generated backend's `on_call_ended` and report what it sent.

    The seam is the generated file, not the scaffold's internals: what matters
    is that a real `call.ended` body reaches Pipedream as the right props.
    """
    import asyncio

    from app.backends import scaffold

    monkeypatch.setenv("AGENT_ID", "agent-1")
    monkeypatch.setenv("WEBHOOK_SECRET", "w")
    monkeypatch.chdir(tmp_path)
    files = scaffold.render(
        slug="x-1", port=9001, tools=[], integrations=integrations, agent_id="agent-1"
    )
    ns: dict = {}
    exec(compile(files["app.py"], "app.py", "exec"), ns)  # noqa: S102 — our own scaffold

    sent = []

    async def _capture(action_id, app, app_prop, props):
        sent.append({"action": action_id, "app_prop": app_prop, "props": props})

    ns["run_pipedream_action"] = _capture
    asyncio.run(ns["on_call_ended"](payload))
    return sent


ENDED = {
    "from_number": "+15551234567",
    "to_number": "+15557654321",
    "duration_ms": 92_000,
    "ended_reason": "customer_ended_call",
    "summary": "They asked about pricing.",
    "started_at": "2026-09-13T10:00:00+00:00",
    "ended_at": "2026-09-13T10:01:32+00:00",
}


class TestCalendarAutomation:
    def test_the_event_is_the_call(self, tmp_path, monkeypatch):
        """The times a Workspace could not have typed come from the payload."""
        [sent] = _run_automations([CALENDAR_ENTRY], ENDED, tmp_path, monkeypatch)
        assert sent["action"] == "google_calendar-create-event"
        # The component declares `googleCalendar`, not the app slug — the wrong
        # key runs the action with no credential and Google answers 200.
        assert sent["app_prop"] == "googleCalendar"
        assert sent["props"]["eventStartDate"] == ENDED["started_at"]
        assert sent["props"]["eventEndDate"] == ENDED["ended_at"]
        assert sent["props"]["calendarId"] == "primary"
        assert sent["props"]["summary"] == "Customer call"
        assert "They asked about pricing." in sent["props"]["description"]

    def test_a_call_that_never_started_creates_no_event(self, tmp_path, monkeypatch):
        """An unanswered call has no start. An event spanning nothing is wrong,
        not merely blank — and `None` in an RFC3339 field is a 400 anyway."""
        payload = {**ENDED, "started_at": None, "ended_reason": "customer_did_not_answer"}
        assert _run_automations([CALENDAR_ENTRY], payload, tmp_path, monkeypatch) == []

    def test_one_skipped_automation_does_not_cost_the_others(self, tmp_path, monkeypatch):
        payload = {**ENDED, "started_at": None}
        sent = _run_automations(
            [CALENDAR_ENTRY, SLACK_SUMMARY], payload, tmp_path, monkeypatch
        )
        assert [s["action"] for s in sent] == ["slack_v2-post-message"]

    def test_the_summary_is_marked_up_for_where_it_lands(self, tmp_path, monkeypatch):
        """`*bold*` is Slack's. In a calendar description it is punctuation —
        ADR-0021 flagged this as due the moment a second Action existed."""
        sent = _run_automations(
            [CALENDAR_ENTRY, SLACK_SUMMARY], ENDED, tmp_path, monkeypatch
        )
        by_action = {s["action"]: s for s in sent}
        assert by_action["slack_v2-post-message"]["props"]["text"].startswith("*Call ended*")
        described = by_action["google_calendar-create-event"]["props"]["description"]
        assert described.startswith("Call ended —")
        # `customer_ended_call` has its own underscores; what must be gone is
        # the wrapper's, which is the last line in full.
        assert described.splitlines()[-1] == "92s · customer_ended_call"


class TestRedelivery:
    """TurnCall retries a delivery it did not get an answer to in time, and a
    Pipedream action is a third-party round trip — the first attempt timed out
    on TurnCall's side while the action was still running, and the retry ran it
    again. A second calendar entry for one call is not a cosmetic fault."""

    @staticmethod
    def _client(tmp_path, monkeypatch):
        import hashlib
        import hmac
        import json
        import time

        from fastapi.testclient import TestClient

        from app.backends import scaffold

        monkeypatch.setenv("AGENT_ID", "agent-1")
        monkeypatch.setenv("WEBHOOK_SECRET", "evsecret")
        monkeypatch.chdir(tmp_path)
        files = scaffold.render(
            slug="x-1", port=9001, tools=[], integrations=[SLACK_SUMMARY], agent_id="agent-1"
        )
        ns: dict = {}
        exec(compile(files["app.py"], "app.py", "exec"), ns)  # noqa: S102 — our own scaffold

        ran = []

        async def _capture(*args):
            ran.append(args[0])

        ns["run_pipedream_action"] = _capture
        client = TestClient(ns["app"])

        def post(event_id):
            body = json.dumps(
                {"event": "call.ended", "event_id": event_id, "payload": dict(ENDED)}
            )
            ts = str(int(time.time()))
            sig = "v1=" + hmac.new(
                b"evsecret", f"{ts}.{body}".encode(), hashlib.sha256
            ).hexdigest()
            return client.post(
                "/events",
                content=body,
                headers={"X-TurnCall-Signature": sig, "X-TurnCall-Timestamp": ts},
            )

        return post, ran

    def test_a_redelivery_runs_the_automation_once(self, tmp_path, monkeypatch):
        post, ran = self._client(tmp_path, monkeypatch)
        assert post("evt-1").status_code == 200
        assert post("evt-1").status_code == 200  # same event_id: TurnCall's retry
        assert ran == ["slack_v2-post-message"]

    def test_a_different_event_still_runs(self, tmp_path, monkeypatch):
        post, ran = self._client(tmp_path, monkeypatch)
        post("evt-1")
        post("evt-2")
        assert len(ran) == 2

    def test_a_redelivery_is_still_recorded(self, tmp_path, monkeypatch):
        """The log says what arrived, including the retry; `handled` says what
        was acted on. Collapsing the two would hide the redelivery entirely."""
        import sqlite3

        post, _ = self._client(tmp_path, monkeypatch)
        post("evt-1")
        post("evt-1")
        conn = sqlite3.connect(tmp_path / "events.db")
        assert conn.execute("select count(*) from events").fetchone()[0] == 2
        assert conn.execute("select count(*) from handled").fetchone()[0] == 1
