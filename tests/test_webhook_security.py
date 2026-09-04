"""Signature verification tests — must match TurnCall's signing exactly."""

import hashlib
import hmac

from app.webhook_security import verify_signature

SECRET = "s3cret"


def _sign(payload: str, ts: str, secret: str = SECRET) -> str:
    return "v1=" + hmac.new(
        secret.encode(), f"{ts}.{payload}".encode(), hashlib.sha256
    ).hexdigest()


def test_valid_signature_accepted():
    body, ts = '{"event":"call.ended"}', "1700000000"
    assert verify_signature(body, SECRET, _sign(body, ts), ts) is True


def test_tampered_body_rejected():
    ts = "1700000000"
    sig = _sign('{"event":"call.ended"}', ts)
    assert verify_signature('{"event":"hacked"}', SECRET, sig, ts) is False


def test_wrong_secret_rejected():
    body, ts = "{}", "1700000000"
    assert verify_signature(body, SECRET, _sign(body, ts, "other"), ts) is False


def test_missing_pieces_rejected():
    assert verify_signature("{}", "", "v1=abc", "1700000000") is False
    assert verify_signature("{}", SECRET, "", "1700000000") is False
