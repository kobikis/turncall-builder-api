"""The last 24 hours of an agent, bucketed by hour.

Two things carry the logic and both are worth pinning: which hour a call lands
in, and when to stop paging. TurnCall's /v1/calls has no date filter, so the
window is walked newest-first until a page leaves it — get that wrong and the
ribbon either misses calls or fetches the agent's entire history.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.routers.agent_activity import HOURS, MAX_PAGES, PAGE, _activity, _bucket

NOW = datetime(2026, 9, 28, 15, 30, tzinfo=UTC)


def _at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


class TestBucket:
    def test_now_is_the_last_bucket(self):
        # The ribbon reads left to right as time passing, so the current hour
        # is at the right edge where the eye ends up.
        assert _bucket(_at(0), NOW) == HOURS - 1

    def test_earlier_today_walks_left(self):
        assert _bucket(_at(3), NOW) == HOURS - 4
        assert _bucket(_at(23), NOW) == 0

    def test_outside_the_window_is_no_bucket(self):
        assert _bucket(_at(24), NOW) is None
        assert _bucket(_at(200), NOW) is None

    def test_a_future_timestamp_is_no_bucket(self):
        # Clock skew between services is ordinary; a call from the future must
        # not land in a bucket and inflate the count.
        assert _bucket(_at(-2), NOW) is None

    def test_a_naive_timestamp_is_read_as_utc(self):
        naive = (NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat()
        assert _bucket(naive, NOW) == HOURS - 2

    @pytest.mark.parametrize("value", [None, "", "yesterday", "2026-13-45"])
    def test_an_unreadable_timestamp_is_skipped_not_raised(self, value):
        # One malformed row must not cost the whole workspace its activity.
        assert _bucket(value, NOW) is None


class _Client:
    """A TurnCall client serving canned pages of calls."""

    def __init__(self, pages: list[list[dict[str, Any]]]):
        self.pages = pages
        self.requested = 0

    async def list_calls(self, api_key: str, page: int = 1, limit: int = 50):
        self.requested += 1
        rows = self.pages[page - 1] if page <= len(self.pages) else []
        return {"success": True, "data": rows, "total": 0, "page": page, "limit": limit}


@pytest.fixture
def client(monkeypatch):
    def install(pages):
        fake = _Client(pages)
        monkeypatch.setattr("app.runtime.get_client", lambda: fake)
        return fake

    return install


def _calls(count: int, hours_ago: float) -> list[dict[str, Any]]:
    return [{"started_at": _at(hours_ago)} for _ in range(count)]


class TestActivity:
    @pytest.mark.asyncio
    async def test_counts_calls_into_the_hour_they_started(self, client):
        client([_calls(2, 0) + _calls(3, 5)])
        result = await _activity("tc_key", NOW)

        assert result["calls_24h"] == 5
        assert result["hours"][HOURS - 1] == 2
        assert result["hours"][HOURS - 6] == 3
        assert result["available"] is True

    @pytest.mark.asyncio
    async def test_stops_at_a_short_page(self, client):
        # Fewer rows than asked for means the agent has no more calls at all.
        fake = client([_calls(3, 1), _calls(50, 2)])
        await _activity("tc_key", NOW)
        assert fake.requested == 1

    @pytest.mark.asyncio
    async def test_stops_once_a_page_falls_out_of_the_window(self, client):
        # A full page whose oldest row predates the window: everything after it
        # is older still, so paging on would fetch the agent's whole history.
        fake = client([_calls(PAGE - 1, 2) + _calls(1, 30), _calls(PAGE, 40)])
        result = await _activity("tc_key", NOW)

        assert fake.requested == 1
        assert result["calls_24h"] == PAGE - 1
        assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_says_so_when_the_window_outruns_the_page_budget(self, client):
        # Every page full and still inside 24h: the count is a floor, and the
        # payload has to admit that rather than under-report in silence.
        fake = client([_calls(PAGE, 1) for _ in range(MAX_PAGES + 2)])
        result = await _activity("tc_key", NOW)

        assert fake.requested == MAX_PAGES
        assert result["truncated"] is True
        assert result["calls_24h"] == PAGE * MAX_PAGES

    @pytest.mark.asyncio
    async def test_a_quiet_agent_is_zeroes_not_absence(self, client):
        client([[]])
        result = await _activity("tc_key", NOW)

        assert result["hours"] == [0] * HOURS
        assert result["calls_24h"] == 0
        # Distinct from the unavailable case, which is the point: "nobody rang"
        # and "we could not ask" are different statements.
        assert result["available"] is True
