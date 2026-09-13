"""Choosing a catalog input's value from a list instead of typing it.

A Google calendar ID differing from the right one by a single character is
accepted by the form, refused by Google, and surfaces as a calendar entry that
never appears — that is what this exists to prevent. So the tests are about the
shapes two real Actions actually return, which are not the same shape, and about
every way the lookup can fail without costing the form.

These drive the real `pipedream.input_options` with only the network stubbed;
conftest's `mocks` fixture replaces every coroutine in the module, so it is
deliberately not used here.
"""

import asyncio

import pytest

from app import pipedream

CRED = pipedream.Credential(
    client_id="cid", client_secret="s", project_id="proj", environment="development"
)

# What the two Actions really answer with, trimmed. Measured, not guessed:
# list-calendars returns a bare list and list-channels returns an object.
CALENDARS = [
    {"id": "family025@group.calendar.google.com", "summary": "Family"},
    {"id": "kobi@example.com", "summary": "kobi@example.com"},
]
CHANNELS = {
    "channels": [
        {"id": "CKLED8TU2", "name": "general"},
        {"id": "C0C1BCDA0JH", "name": "calls"},
    ]
}


def _stub(monkeypatch, *, accounts=(("apn_1", "x"),), ret=None, boom=None):
    async def _accounts(cred, *, workspace_id, app=None):
        return [{"id": i, "app": app or a} for i, a in accounts]

    async def _run(cred, *, workspace_id, action_id, app_prop, account_id, props=None):
        if boom:
            raise pipedream.PipedreamError(boom)
        return ret

    monkeypatch.setattr(pipedream, "list_accounts", _accounts)
    monkeypatch.setattr(pipedream, "run_action", _run)


def _options(app, prop):
    """Sync on purpose: this suite has no async plugin, and one `asyncio.run`
    here beats adding a framework for twelve assertions."""
    return asyncio.run(
        pipedream.input_options(CRED, workspace_id="ws-1", app=app, prop=prop)
    )


class TestShapes:
    def test_a_bare_list_becomes_value_and_label(self, monkeypatch):
        _stub(monkeypatch, ret=CALENDARS)
        assert _options("google_calendar", "calendarId") == [
            {"value": "family025@group.calendar.google.com", "label": "Family"},
            {"value": "kobi@example.com", "label": "kobi@example.com"},
        ]

    def test_an_object_is_unwrapped_by_its_root_key(self, monkeypatch):
        """Slack answers `{"channels": [...]}`. Two apps were enough to need
        this, which is why it is declared per input rather than assumed."""
        _stub(monkeypatch, ret=CHANNELS)
        assert _options("slack_v2", "channel") == [
            {"value": "C0C1BCDA0JH", "label": "calls"},
            {"value": "CKLED8TU2", "label": "general"},
        ]

    def test_options_are_sorted_by_what_a_person_reads(self, monkeypatch):
        _stub(monkeypatch, ret=CHANNELS)
        labels = [o["label"] for o in _options("slack_v2", "channel")]
        assert labels == sorted(labels)

    def test_an_item_with_no_value_is_dropped(self, monkeypatch):
        """A half-formed entry must not become a blank row that configures
        cleanly and posts nowhere."""
        _stub(monkeypatch, ret=[*CALENDARS, {"summary": "Broken"}, "nonsense"])
        assert len(_options("google_calendar", "calendarId")) == 2

    def test_a_missing_label_falls_back_to_the_value(self, monkeypatch):
        _stub(monkeypatch, ret=[{"id": "abc"}])
        assert _options("google_calendar", "calendarId") == [
            {"value": "abc", "label": "abc"}
        ]


class TestFailures:
    """Every one of these costs the picker, never the form — the Console falls
    back to a text field, so the Workspace can still type an ID."""

    def test_an_unconnected_app_says_so(self, monkeypatch):
        _stub(monkeypatch, accounts=(), ret=CALENDARS)
        with pytest.raises(pipedream.PipedreamError, match="No Google Calendar account"):
            _options("google_calendar", "calendarId")

    def test_an_input_that_is_typed_in_is_refused(self, monkeypatch):
        _stub(monkeypatch, ret=CALENDARS)
        with pytest.raises(pipedream.PipedreamError, match="typed in"):
            _options("slack_v2", "text")

    def test_an_unknown_app_is_refused(self, monkeypatch):
        _stub(monkeypatch, ret=CALENDARS)
        with pytest.raises(pipedream.PipedreamError, match="not an app this build"):
            _options("notion", "database")

    def test_a_refusal_from_the_third_party_is_carried_through(self, monkeypatch):
        _stub(monkeypatch, boom="list-channels was refused: missing_scope")
        with pytest.raises(pipedream.PipedreamError, match="missing_scope"):
            _options("slack_v2", "channel")

    def test_an_unexpected_body_is_refused_rather_than_shown_empty(
        self, monkeypatch
    ):
        """An empty dropdown reads as "you have no calendars", which is a lie
        the Workspace cannot act on."""
        _stub(monkeypatch, ret={"unexpected": True})
        with pytest.raises(pipedream.PipedreamError, match="no list to choose from"):
            _options("google_calendar", "calendarId")


class TestActionErrors:
    def test_a_200_carrying_an_error_is_not_success(self):
        """Pipedream answers 200 whether or not the Action worked."""
        body = {"os": [{"k": "error", "err": {"data": {"error": "Not Found"}}}]}
        assert pipedream._action_error(body) == "Not Found"

    def test_a_clean_run_reports_nothing(self):
        assert pipedream._action_error({"os": [], "ret": []}) is None
