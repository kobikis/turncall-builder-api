"""The Telnyx REST call the Builder makes itself: finding one of an account's
numbers, which is how a release checks that a Telnyx number is on the
Workspace's account (#113). Everything else on Telnyx goes through TurnCall.
"""

from __future__ import annotations

from typing import Any

import httpx

_BASE = "https://api.telnyx.com/v2"
_PAGE_SIZE = 250  # Telnyx's maximum


class TelnyxRejected(Exception):
    """Telnyx answered 401/403 for this API key."""


async def find_number(api_key: str, e164: str) -> dict[str, Any] | None:
    """The account's number that is exactly `e164`, as {id, phone_number};
    None when the account doesn't hold it.

    Telnyx's `filter[phone_number]` takes digits only (a `+` matches nothing)
    and matches partially: asking for 1312555010 also lists +13125550100. So
    the filter only narrows the list, each number is compared exactly, and
    every page is read.

    Raises TelnyxRejected on a refused key, httpx.HTTPError otherwise.
    """
    params: dict[str, Any] = {
        "filter[phone_number]": e164.lstrip("+"), "page[size]": _PAGE_SIZE, "page[number]": 1,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            r = await client.get(f"{_BASE}/phone_numbers", params=params, headers=headers)
            if r.status_code in (401, 403):
                raise TelnyxRejected(f"Telnyx answered {r.status_code}")
            r.raise_for_status()
            page = r.json()
            for n in page.get("data") or []:
                if n.get("phone_number") == e164:
                    return {"id": str(n.get("id") or ""), "phone_number": n["phone_number"]}
            total = (page.get("meta") or {}).get("total_pages") or 1
            if params["page[number]"] >= total:
                return None
            params["page[number]"] += 1
