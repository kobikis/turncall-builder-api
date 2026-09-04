"""Verify TurnCall webhook signatures.

Mirrors TurnCall: signature is `v1=<hex>` where hex = HMAC-SHA256(secret,
"{timestamp}.{raw_body}"). A trust boundary — events failing this are dropped.
"""

from __future__ import annotations

import hashlib
import hmac


def verify_signature(
    payload: str, secret: str, signature_header: str, timestamp: str
) -> bool:
    if not (secret and signature_header and timestamp):
        return False
    expected = "v1=" + hmac.new(
        secret.encode(), f"{timestamp}.{payload}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)
