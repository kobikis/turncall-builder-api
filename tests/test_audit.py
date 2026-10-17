"""What an audit entry records (ADR-0028): the diff, and what it never shows."""

from __future__ import annotations

import pytest

from app.audit import MASK, before_unknown, diff, is_secret, secret_changed
from app.settings import parse_audit_retention


def test_an_update_lists_only_what_changed_by_json_pointer():
    before = {"name": "A", "llm": {"model": "gpt-4o", "temperature": 0.5}, "tools": ["end_call"]}
    after = {"name": "A", "llm": {"model": "gpt-4o-mini", "temperature": 0.5}, "tools": ["end_call", "transfer"]}
    assert diff(before, after) == {
        "/llm/model": {"before": "gpt-4o", "after": "gpt-4o-mini"},
        "/tools/1": {"before": None, "after": "transfer"},
    }


def test_a_creation_and_a_deletion_list_every_field():
    assert diff(None, {"a": 1, "b": {"c": 2}}) == {
        "/a": {"before": None, "after": 1},
        "/b/c": {"before": None, "after": 2},
    }
    assert diff({"a": 1}, None) == {"/a": {"before": 1, "after": None}}


def test_names_with_slash_or_tilde_are_escaped():
    assert list(diff(None, {"metadata": {"a/b": 1, "x~y": 2}})) == ["/metadata/a~1b", "/metadata/x~0y"]


@pytest.mark.parametrize(
    "key",
    ["api_key", "webhook_secret", "server_url_secret", "auth_token", "client_secret",
     "headers", "env", "password", "token", "secret_access_key", "Authorization"],
)
def test_secret_names_are_masked(key):
    assert is_secret(key)
    changes = diff({key: "old-value"}, {key: "new-value"})
    assert changes == {f"/{key}": {"before": MASK, "after": MASK, "secret": True}}
    assert "value" not in repr(changes)


def test_a_secret_subtree_is_masked_whole_and_unchanged_is_silent():
    before = {"mcp": [{"headers": {"Authorization": "Bearer a"}}]}
    assert diff(before, before) == {}
    after = {"mcp": [{"headers": {"Authorization": "Bearer b", "X-Other": "c"}}]}
    assert diff(before, after) == {"/mcp/0/headers": {"before": MASK, "after": MASK, "secret": True}}


def test_a_secret_set_or_cleared_shows_null_on_the_empty_side():
    assert diff({"api_key": None}, {"api_key": "k"}) == {"/api_key": {"before": None, "after": MASK, "secret": True}}
    assert diff({"api_key": "k"}, {}) == {"/api_key": {"before": MASK, "after": None, "secret": True}}
    assert diff({"api_key": ""}, {"api_key": None}) == {}


def test_a_plain_field_holding_the_mask_text_is_not_marked_secret():
    """Only the explicit flag says a value was masked: `***` typed into a
    greeting is that greeting."""
    assert diff({"greeting": "hi"}, {"greeting": MASK}) == {"/greeting": {"before": "hi", "after": MASK}}


def test_before_unknown_keeps_the_secret_flag():
    assert before_unknown({"api_key": "k", "name": "n"}) == {
        "/api_key": {"after": MASK, "secret": True, "before_unknown": True},
        "/name": {"after": "n", "before_unknown": True},
    }


def test_non_secret_names_are_not_masked():
    for key in ("name", "region", "account_sid", "model", "keywords"):
        assert not is_secret(key)


def test_secret_changed():
    assert secret_changed(False, True) == {"/value": {"before": None, "after": MASK, "secret": True}}
    assert secret_changed(True, False) == {"/value": {"before": MASK, "after": None, "secret": True}}


@pytest.mark.parametrize(("raw", "days"), [(None, None), ("", None), ("  ", None), ("1", 1), ("365", 365), ("36500", 36500)])
def test_retention_setting(raw, days):
    assert parse_audit_retention(raw) == days


@pytest.mark.parametrize("raw", ["0", "-1", "36501", "a year", "7.5"])
def test_a_bad_retention_setting_refuses_to_start(raw):
    with pytest.raises(ValueError, match="AUDIT_LOG_RETENTION_DAYS"):
        parse_audit_retention(raw)
