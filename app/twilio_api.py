"""The Twilio REST calls the Builder makes, all on an account's incoming numbers.

Listing them is how a Twilio card is validated (the account's own list
endpoint, the same "checked, not proven to work" wording as the provider cards)
and how the add-number form offers numbers to pick instead of typing a SID.
Finding one is how a release checks which account a number is on (#111).
"""

from __future__ import annotations

from typing import Any

import httpx

_HOST = "https://api.twilio.com"
_BASE = f"{_HOST}/2010-04-01"


class TwilioRejected(Exception):
    """Twilio answered 401/403 for this Account SID / Auth Token pair."""


async def list_numbers(account_sid: str, auth_token: str, *, page_size: int = 1000) -> list[dict[str, Any]]:
    """The account's incoming numbers: [{sid, phone_number, friendly_name}].

    Raises TwilioRejected on bad credentials, httpx.HTTPError otherwise.
    """
    url = f"{_BASE}/Accounts/{account_sid}/IncomingPhoneNumbers.json"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, auth=(account_sid, auth_token), params={"PageSize": page_size})
    if r.status_code in (401, 403, 404):  # 404: an AC that is not this token's account
        raise TwilioRejected(f"Twilio answered {r.status_code}")
    r.raise_for_status()
    return [
        {"sid": n["sid"], "phone_number": n["phone_number"], "friendly_name": n.get("friendly_name")}
        for n in r.json().get("incoming_phone_numbers", [])
    ]


async def find_number(account_sid: str, auth_token: str, e164: str) -> dict[str, Any] | None:
    """The account's incoming number that is exactly `e164`, as
    {sid, phone_number, friendly_name}; None when the account doesn't hold it.

    Twilio's `PhoneNumber` filter matches partially (kobikis/turncall#344):
    asking for +1312555010 also lists +13125550100. So the filter only narrows
    the list, each number is compared exactly, and every page is read.

    Raises TwilioRejected on bad credentials, httpx.HTTPError otherwise.
    """
    url: str | None = f"{_BASE}/Accounts/{account_sid}/IncomingPhoneNumbers.json"
    params: dict[str, Any] | None = {"PhoneNumber": e164, "PageSize": 1000}
    async with httpx.AsyncClient(timeout=15) as client:
        while url:
            r = await client.get(url, auth=(account_sid, auth_token), params=params)
            if r.status_code in (401, 403, 404):
                raise TwilioRejected(f"Twilio answered {r.status_code}")
            r.raise_for_status()
            page = r.json()
            for n in page.get("incoming_phone_numbers") or []:
                if n.get("phone_number") == e164:
                    return {
                        "sid": n["sid"],
                        "phone_number": n["phone_number"],
                        "friendly_name": n.get("friendly_name"),
                    }
            # The next page's path, filter included; only ever on Twilio's host.
            next_uri = page.get("next_page_uri")
            url = f"{_HOST}{next_uri}" if next_uri and next_uri.startswith("/") else None
            params = None
    return None


def checked_url(account_sid: str) -> str:
    return f"GET {_BASE}/Accounts/{account_sid}/IncomingPhoneNumbers.json"
