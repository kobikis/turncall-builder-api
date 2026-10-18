"""telnyx_api's listing, retries and deadlines (#116-#118), against a mock
transport and a fake clock: nothing reaches Telnyx, and nothing really waits."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from app import telnyx_api
from app.settings import load_settings, parse_deadline

E164 = "+13125550100"
_CLIENT = httpx.AsyncClient  # before any test patches it


def _serve(monkeypatch, handler):
    """Route telnyx_api's requests to `handler`; returns the requests seen."""
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        telnyx_api.httpx, "AsyncClient",
        lambda **kw: _CLIENT(transport=httpx.MockTransport(record), **kw),
    )
    return seen


def _page(numbers, total_pages=1):
    return httpx.Response(200, json={
        "data": [{"id": n[-4:], "phone_number": n} for n in numbers],
        "meta": {"total_pages": total_pages},
    })


def _script(*responses):
    """Answer each request with the next response."""
    queue = list(responses)
    return lambda request: queue.pop(0)


def _deadlines(monkeypatch, **over):
    monkeypatch.setattr(telnyx_api, "load_settings", lambda: replace(load_settings(), **over))


# --- listing the account (#116) ------------------------------------------------


def test_listing_reads_every_page_unfiltered(monkeypatch, telnyx_clock):
    pages = [["+13125550100", "+13125550101"], ["+13125550102"], ["+13125550103"]]
    seen = _serve(
        monkeypatch,
        lambda r: _page(pages[int(r.url.params["page[number]"]) - 1], total_pages=3),
    )
    held = asyncio.run(telnyx_api.list_numbers("KEY"))
    assert held == {n for page in pages for n in page}
    assert [r.url.params["page[number]"] for r in seen] == ["1", "2", "3"]
    assert all(r.url.params["page[size]"] == "250" for r in seen)
    assert all("filter[phone_number]" not in r.url.params for r in seen)
    assert seen[0].headers["authorization"] == "Bearer KEY"


@pytest.mark.parametrize("status", [401, 403])
def test_listing_names_a_refused_key(monkeypatch, telnyx_clock, status):
    _serve(monkeypatch, lambda r: httpx.Response(status))
    with pytest.raises(telnyx_api.TelnyxRejected):
        asyncio.run(telnyx_api.list_numbers("bad"))


def test_listing_an_unreadable_body_is_malformed(monkeypatch, telnyx_clock):
    _serve(monkeypatch, lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(telnyx_api.TelnyxMalformed):
        asyncio.run(telnyx_api.list_numbers("KEY"))


# --- retries (#116, #117) --------------------------------------------------------


@pytest.mark.parametrize("call", [
    lambda: telnyx_api.list_numbers("KEY"),
    lambda: telnyx_api.find_number("KEY", E164),
])
def test_a_short_retry_after_is_waited_and_retried(monkeypatch, telnyx_clock, call):
    seen = _serve(monkeypatch, _script(
        httpx.Response(429, headers={"Retry-After": "3"}), _page([E164]),
    ))
    assert asyncio.run(call())
    assert (len(seen), telnyx_clock.waits) == (2, [3.0])


def test_a_long_retry_after_fails_at_once_with_the_wait(monkeypatch, telnyx_clock):
    seen = _serve(monkeypatch, _script(httpx.Response(429, headers={"Retry-After": "30"})))
    with pytest.raises(telnyx_api.TelnyxRetryLater) as raised:
        asyncio.run(telnyx_api.list_numbers("KEY"))
    assert (raised.value.status, raised.value.wait_s) == (429, 30.0)
    assert (len(seen), telnyx_clock.waits) == (1, [])
    assert telnyx_api.lookup_failure(raised.value, "the workspace's Telnyx numbers") == (
        "Telnyx is rate-limiting the workspace's Telnyx account; try again in about 30 seconds."
    )


@pytest.mark.parametrize("first", [
    httpx.Response(429), httpx.Response(502), httpx.Response(503),
])
def test_no_retry_after_backs_off_one_then_two_seconds(monkeypatch, telnyx_clock, first):
    seen = _serve(monkeypatch, _script(first, first, _page([E164])))
    assert asyncio.run(telnyx_api.find_number("KEY", E164))
    assert (len(seen), telnyx_clock.waits) == (3, [1.0, 2.0])


def test_retries_run_out_after_two(monkeypatch, telnyx_clock):
    seen = _serve(monkeypatch, lambda r: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(telnyx_api.list_numbers("KEY"))
    assert len(seen) == 3


def test_a_last_429_with_a_short_wait_says_the_wait(monkeypatch, telnyx_clock):
    _serve(monkeypatch, lambda r: httpx.Response(429, headers={"Retry-After": "4"}))
    with pytest.raises(telnyx_api.TelnyxRetryLater) as raised:
        asyncio.run(telnyx_api.find_number("KEY", E164))
    assert (raised.value.status, raised.value.wait_s) == (429, 4.0)
    assert telnyx_clock.waits == [4.0, 4.0]


# --- the deadline (#117, #118) -------------------------------------------------


def _slow_pages(monkeypatch, clock, pages, seconds_each, last=None):
    """Pages that each take `seconds_each`; `last` answers the final one."""

    def handler(request):
        index = int(request.url.params["page[number]"])
        clock.advance(seconds_each)
        if last is not None and index == pages:
            return last
        return _page([f"+1312555{index:04d}"], total_pages=pages)

    return _serve(monkeypatch, handler)


def test_the_deadline_runs_out_across_pages(monkeypatch, telnyx_clock):
    """20 s by default: three 8 s pages pass it, so the fourth is never asked."""
    seen = _slow_pages(monkeypatch, telnyx_clock, pages=5, seconds_each=8)
    with pytest.raises(telnyx_api.TelnyxRetryLater) as raised:
        asyncio.run(telnyx_api.list_numbers("KEY"))
    assert (raised.value.status, raised.value.wait_s) == (None, None)
    assert len(seen) == 3
    # A request never outlives the check: the third had 4 s left.
    assert seen[2].extensions["timeout"]["read"] == pytest.approx(4.0)


def test_a_wait_past_the_deadline_is_not_waited(monkeypatch, telnyx_clock):
    """Two 8 s pages leave 4 s: a Retry-After of 5 fits the cap, not the check."""
    seen = _slow_pages(
        monkeypatch, telnyx_clock, pages=3, seconds_each=8,
        last=httpx.Response(429, headers={"Retry-After": "5"}),
    )
    with pytest.raises(telnyx_api.TelnyxRetryLater) as raised:
        asyncio.run(telnyx_api.list_numbers("KEY"))
    assert (raised.value.status, raised.value.wait_s) == (429, 5.0)
    assert (len(seen), telnyx_clock.waits) == (3, [])


def test_the_connect_check_deadline_is_a_setting(monkeypatch, telnyx_clock):
    _deadlines(monkeypatch, telnyx_connect_check_deadline_s=40.0)
    seen = _slow_pages(monkeypatch, telnyx_clock, pages=4, seconds_each=8)
    assert len(asyncio.run(telnyx_api.list_numbers("KEY"))) == 4  # 32 s, under 40
    assert len(seen) == 4

    _deadlines(monkeypatch, telnyx_connect_check_deadline_s=10.0)
    seen = _slow_pages(monkeypatch, telnyx_clock, pages=4, seconds_each=8)
    with pytest.raises(telnyx_api.TelnyxRetryLater):
        asyncio.run(telnyx_api.list_numbers("KEY"))
    assert len(seen) == 2


def test_the_release_check_deadline_is_a_setting(monkeypatch, telnyx_clock):
    """10 s by default; 5 s here, so two 3 s pages run it out."""
    _deadlines(monkeypatch, telnyx_release_check_deadline_s=5.0)

    def handler(request):
        telnyx_clock.advance(3)
        return _page([], total_pages=3)

    seen = _serve(monkeypatch, handler)
    with pytest.raises(telnyx_api.TelnyxRetryLater):
        asyncio.run(telnyx_api.find_number("KEY", E164))
    assert len(seen) == 2


@pytest.mark.parametrize("raw, seconds", [(None, 20.0), ("", 20.0), (" 45 ", 45.0), ("7.5", 7.5)])
def test_a_deadline_setting_is_read_in_seconds(raw, seconds):
    assert parse_deadline("TELNYX_CONNECT_CHECK_DEADLINE_S", raw, 20.0) == seconds


@pytest.mark.parametrize("raw", ["0", "0.5", "601", "soon"])
def test_a_deadline_setting_out_of_range_refuses_to_start(raw):
    with pytest.raises(ValueError, match="TELNYX_RELEASE_CHECK_DEADLINE_S"):
        parse_deadline("TELNYX_RELEASE_CHECK_DEADLINE_S", raw, 10.0)


def test_the_settings_read_both_deadlines(monkeypatch):
    monkeypatch.setenv("TELNYX_CONNECT_CHECK_DEADLINE_S", "25")
    monkeypatch.setenv("TELNYX_RELEASE_CHECK_DEADLINE_S", "")
    load_settings.cache_clear()
    try:
        settings = load_settings()
        assert settings.telnyx_connect_check_deadline_s == 25.0
        assert settings.telnyx_release_check_deadline_s == 10.0
    finally:
        load_settings.cache_clear()
