"""The one Twilio REST call the Builder makes: list an account's numbers.

It is how a Twilio card is validated (the account's own list endpoint, the same
"checked, not proven to work" wording as the provider cards) and how the
add-number form offers numbers to pick instead of typing a SID.
"""

from __future__ import annotations

from typing import Any

import httpx

_BASE = "https://api.twilio.com/2010-04-01"


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


def checked_url(account_sid: str) -> str:
    return f"GET {_BASE}/Accounts/{account_sid}/IncomingPhoneNumbers.json"
