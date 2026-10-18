"""The Telnyx REST calls the Builder makes itself: finding one of an account's
numbers, which is how a release checks that a Telnyx number is on the
Workspace's account (#113), and listing them all, which is how connecting a
Telnyx account checks the Workspace's bound numbers against it (#114, #116).
Everything else on Telnyx goes through TurnCall.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

import httpx

from .settings import load_settings

logger = logging.getLogger("turncall_builder")

_BASE = "https://api.telnyx.com/v2"
_PAGE_SIZE = 250  # Telnyx's maximum

# How long a check may take (#116-#118). A check runs inside one interactive
# request (connecting, applying, a release), where the admin sees only a busy
# button, and every request and wait it makes, across every page, counts
# against one overall deadline. The deadlines are settings
# (TELNYX_CONNECT_CHECK_DEADLINE_S, default 20; TELNYX_RELEASE_CHECK_DEADLINE_S,
# default 10): the defaults sit under the 30-60 s request timeouts load
# balancers in front of a deployment commonly have, which builder-api can't
# know, so past them the admin couldn't tell whether a connect saved. An
# operator behind a shorter one lowers them; one with large Telnyx accounts
# raises them. Connecting reads every page of the account, so it gets more.
REQUEST_TIMEOUT_S = 15.0  # one request, and never past the check's deadline
# The longest Retry-After worth waiting for inside a check. A longer one fails
# at once with the wait in the sentence: waiting less than Telnyx asked only
# earns another 429.
RETRY_AFTER_CAP_S = 10.0
# A 429 without Retry-After, or a 5xx: retried after these, at most twice.
BACKOFF_S = (1.0, 2.0)

_clock = time.monotonic  # the tests' seams
_sleep = asyncio.sleep


class TelnyxRejected(Exception):
    """Telnyx answered 401/403 for this API key."""


class TelnyxMalformed(ValueError):
    """Telnyx answered, with a body that isn't the number list asked for."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Telnyx answered {status} with an unreadable body")
        self.status = status
        self.body = body


class TelnyxRetryLater(Exception):
    """The check gave up rather than wait: Telnyx asked for longer than
    RETRY_AFTER_CAP_S, or the next wait or request would pass the check's
    deadline. `status` is the answer that asked for a wait (None when the
    deadline ran out between pages), `wait_s` how long it asked for."""

    def __init__(self, status: int | None, wait_s: float | None) -> None:
        super().__init__(f"Telnyx answered {status}; wait {wait_s}s")
        self.status = status
        self.wait_s = wait_s


class _Deadline:
    def __init__(self, seconds: float) -> None:
        self._ends = _clock() + seconds

    def left(self) -> float:
        return self._ends - _clock()


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


def _retry_after(response: httpx.Response) -> float | None:
    """Telnyx's Retry-After in seconds; None when it gives none (or a date,
    which it isn't known to send: the backoff applies then)."""
    try:
        return max(float(response.headers["retry-after"]), 0.0)
    except (KeyError, ValueError):
        return None


async def _page(
    client: httpx.AsyncClient, api_key: str, params: dict[str, Any], deadline: _Deadline
) -> tuple[list[dict[str, Any]], int]:
    """One page of the account's numbers, and how many pages there are. A
    429 or 5xx is retried while the wait fits the cap and the deadline."""
    headers = {"Authorization": f"Bearer {api_key}"}
    for attempt in range(len(BACKOFF_S) + 1):
        left = deadline.left()
        if left <= 0:
            raise TelnyxRetryLater(None, None)
        r = await client.get(
            f"{_BASE}/phone_numbers", params=params, headers=headers,
            timeout=min(REQUEST_TIMEOUT_S, left),
        )
        if r.status_code in (401, 403):
            raise TelnyxRejected(f"Telnyx answered {r.status_code}")
        if _retryable(r.status_code):
            asked = _retry_after(r)
            if asked is not None and asked > RETRY_AFTER_CAP_S:
                raise TelnyxRetryLater(r.status_code, asked)
            if attempt < len(BACKOFF_S):
                wait = asked if asked is not None else BACKOFF_S[attempt]
                if wait >= deadline.left():
                    raise TelnyxRetryLater(r.status_code, wait)
                await _sleep(wait)
                continue
            if asked is not None:
                raise TelnyxRetryLater(r.status_code, asked)
        r.raise_for_status()
        try:
            page = r.json()
            numbers = [n for n in page.get("data") or [] if isinstance(n.get("phone_number"), str)]
            total = int((page.get("meta") or {}).get("total_pages") or 1)
        except (ValueError, TypeError, AttributeError) as exc:
            raise TelnyxMalformed(r.status_code, r.text) from exc
        return numbers, total
    raise AssertionError("unreachable: the last attempt returns or raises")


async def find_number(
    api_key: str, e164: str, *, deadline_s: float | None = None
) -> dict[str, Any] | None:
    """The account's number that is exactly `e164`, as {id, phone_number};
    None when the account doesn't hold it.

    Telnyx's `filter[phone_number]` takes digits only (a `+` matches nothing)
    and matches partially: asking for 1312555010 also lists +13125550100. So
    the filter only narrows the list, each number is compared exactly, and
    every page is read.

    Raises TelnyxRejected on a refused key, TelnyxMalformed on a body it
    can't read, TelnyxRetryLater when it gave up waiting within `deadline_s`
    (TELNYX_RELEASE_CHECK_DEADLINE_S by default), httpx.HTTPError otherwise;
    `lookup_failure` says which.
    """
    params: dict[str, Any] = {
        "filter[phone_number]": e164.lstrip("+"), "page[size]": _PAGE_SIZE, "page[number]": 1,
    }
    deadline = _Deadline(deadline_s or load_settings().telnyx_release_check_deadline_s)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        while True:
            numbers, total = await _page(client, api_key, params, deadline)
            for n in numbers:
                if n["phone_number"] == e164:
                    return {"id": str(n.get("id") or ""), "phone_number": e164}
            if params["page[number]"] >= total:
                return None
            params["page[number]"] += 1


async def list_numbers(api_key: str, *, deadline_s: float | None = None) -> set[str]:
    """Every number on the account, as E.164: one request per 250 numbers,
    however many of them the caller is asking about (#116).

    The trade-off: an account of tens of thousands of numbers takes a request
    per 250 of them even when only a handful are being checked, where looking
    each up with `find_number` would take a request apiece. Not worth a second
    path while accounts that size are hypothetical.

    Raises as `find_number` does; `deadline_s` is
    TELNYX_CONNECT_CHECK_DEADLINE_S by default."""
    params: dict[str, Any] = {"page[size]": _PAGE_SIZE, "page[number]": 1}
    held: set[str] = set()
    deadline = _Deadline(deadline_s or load_settings().telnyx_connect_check_deadline_s)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        while True:
            numbers, total = await _page(client, api_key, params, deadline)
            held.update(n["phone_number"] for n in numbers)
            if params["page[number]"] >= total:
                return held
            params["page[number]"] += 1


# Everything a lookup raises that `lookup_failure` has a sentence for.
LOOKUP_ERRORS = (TelnyxRejected, TelnyxRetryLater, httpx.HTTPError, ValueError)

_BODY_LOGGED = 300  # characters of an unexpected answer kept in the log


def lookup_failure(exc: Exception, what: str, *, new_key: bool = False) -> str:
    """The sentence for a lookup of `what` (a number, or the Workspace's
    numbers) that failed, for a 502 (#114).

    Which one depends on whether retrying can help: Telnyx not answering, or
    answering 429 or 5xx (already retried, or not worth waiting for within the
    check), can pass; any other answer it
    gave, or a body that isn't the number list, won't, and is logged.
    `new_key`: the key being connected, not yet saved, rather than the
    Workspace's stored one."""
    if isinstance(exc, TelnyxRejected):
        if new_key:
            return (
                f"Telnyx rejected this API key while checking {what} on its account, so "
                "nothing was saved: check the key and try again."
            )
        return (
            f"Telnyx rejected the workspace's Telnyx API key while checking {what}: "
            "update it on the Integrations page."
        )
    account = "this Telnyx account" if new_key else "the workspace's Telnyx account"
    if isinstance(exc, httpx.TransportError):
        return f"Telnyx didn't answer when looking up {what} on {account}; retry in a moment."
    if isinstance(exc, TelnyxRetryLater):
        if exc.wait_s is None:
            return (
                f"Telnyx is answering too slowly when looking up {what} on {account}; "
                "retry in a moment."
            )
        seconds = max(math.ceil(exc.wait_s), 1)
        later = f"try again in about {seconds} second{'' if seconds == 1 else 's'}."
        if exc.status == 429:
            return f"Telnyx is rate-limiting {account}; {later}"
        return f"Telnyx answered {exc.status} when looking up {what}; {later}"
    status: int | None = None
    body = ""
    if isinstance(exc, httpx.HTTPStatusError):
        status, body = exc.response.status_code, exc.response.text
    elif isinstance(exc, TelnyxMalformed):
        status, body = exc.status, exc.body
    if status is not None and _retryable(status):
        return f"Telnyx answered {status} when looking up {what}; retry in a moment."
    logger.warning(
        "telnyx lookup of %s: unexpected answer (status %s): %s",
        what, status, body[:_BODY_LOGGED] or exc,
    )
    answered = f"answered {status}" if status is not None else "gave an answer the builder couldn't read"
    return (
        f"Telnyx {answered} when looking up {what}; this needs a fix in TurnCall's "
        "builder, not a retry."
    )
