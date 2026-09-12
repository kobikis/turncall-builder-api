"""Pipedream Connect: the custody boundary for third-party credentials (ADR-0018).

The builder never holds a Slack or Google token. It holds one Pipedream client
credential per deployment, encrypted, and asks Pipedream for a short-lived access
token when it needs to act. The Workspace is Pipedream's `external_user_id`, so
one tenant cannot see or use another's Connected apps without us building
anything to stop them.

Pipedream is also the system of record for what is connected. Listing and
disconnecting go straight to their API rather than to a local mirror — a user can
disconnect from Pipedream's own UI, and a mirror would quietly disagree.

Everything here that touches the network is async and mocked in tests; the crypto
and state-signing helpers are pure and tested directly (test_pipedream_credential).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeTimedSerializer

_API = "https://api.pipedream.com/v1"
# The auth handshake is a redirect out and back; ten minutes is generous for a
# consent screen and short enough that a leaked link is worthless.
_STATE_MAX_AGE = 600
_STATE_SALT = "pipedream-connect"

# v1 catalog (ADR-0018): the two apps that were actually asked for. ADR-0017's
# raw `mcp_servers` form stays the escape hatch for anything else. The pinned
# per-app tool allowlist lands with the Console picker (#26), not here.
CATALOG = {
    "slack": "Slack",
    "google_calendar": "Google Calendar",
}


class PipedreamError(RuntimeError):
    """A Pipedream operation failed with something the user can act on."""


@dataclass(frozen=True)
class AccessToken:
    """A Pipedream access token and when it stops working."""

    token: str
    expires_at: float


@dataclass(frozen=True)
class Credential:
    """The deployment's Pipedream client credential, decrypted for use."""

    client_id: str
    client_secret: str
    project_id: str
    environment: str


# --- credential storage ------------------------------------------------------


def _fernet() -> Fernet:
    key = os.environ.get("PIPEDREAM_ENCRYPTION_KEY", "")
    if not key:
        raise PipedreamError(
            "PIPEDREAM_ENCRYPTION_KEY is not set — integrations are disabled. "
            'Generate one with: python -c "from cryptography.fernet import '
            'Fernet; print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise PipedreamError(
            "PIPEDREAM_ENCRYPTION_KEY is not a valid Fernet key (44 url-safe "
            "base64 characters)."
        ) from exc


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Raises if the key changed — rotating it orphans the stored credential."""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise PipedreamError(
            "The stored Pipedream client secret could not be decrypted — "
            "PIPEDREAM_ENCRYPTION_KEY has changed. Restart with the original "
            "key, or set PIPEDREAM_CLIENT_SECRET again to re-store it."
        ) from exc


# --- connect state -----------------------------------------------------------


def _serializer() -> URLSafeTimedSerializer:
    """Signed with the integration key, not the session secret.

    BUILDER_SESSION_SECRET defaults to a random per-process value because it only
    guards a ~30s handshake. This state has to survive a consent screen, a
    restart, and more than one worker — on that key every callback would come
    back indistinguishable from a forgery.
    """
    return URLSafeTimedSerializer(_state_key(), salt=_STATE_SALT)


def _state_key() -> str:
    """Reuses PIPEDREAM_ENCRYPTION_KEY: integrations already require it, and it is
    stable by construction (changing it is what re-stores the credential)."""
    key = os.environ.get("PIPEDREAM_ENCRYPTION_KEY", "")
    if not key:
        raise PipedreamError(
            "PIPEDREAM_ENCRYPTION_KEY is not set — integrations are disabled."
        )
    return key


def sign_state(*, workspace_id: str, app: str) -> str:
    """Carry (Workspace, app) across the redirect in a form Pipedream cannot
    forge. Nothing else Pipedream hands back to the callback is trusted."""
    return _serializer().dumps({"w": workspace_id, "a": app})


def load_state(state: str) -> tuple[str, str]:
    try:
        data = _serializer().loads(state, max_age=_STATE_MAX_AGE)
        return str(data["w"]), str(data["a"])
    except (BadSignature, KeyError, TypeError) as exc:
        raise PipedreamError("This connect link has expired or is invalid.") from exc


# --- API ---------------------------------------------------------------------

# Access tokens live an hour. Cached per credential so a burst of calls costs one
# grant; expiry is trimmed so we never present one mid-flight. The secret is part
# of the key — rotating it must not keep serving the token it superseded.
_token_cache: dict[tuple[str, str, str], AccessToken] = {}


async def _request(
    method: str, url: str, *, cred: Credential, token: str, **kw: Any
) -> Any:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "X-PD-Environment": cred.environment,
            },
            **kw,
        )
    if r.status_code == 401:
        raise PipedreamError(
            "Pipedream rejected our credentials. Check PIPEDREAM_CLIENT_ID / "
            "PIPEDREAM_CLIENT_SECRET."
        )
    if r.status_code == 404:
        raise PipedreamError("Pipedream could not find that resource.")
    if r.status_code >= 400:
        raise PipedreamError(f"Pipedream returned {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


async def access_token(cred: Credential) -> AccessToken:
    """A Pipedream access token, minted on demand and reused until it expires."""
    key = (cred.client_id, cred.client_secret, cred.environment)
    cached = _token_cache.get(key)
    if cached and cached.expires_at > time.time():
        return cached
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{_API}/oauth/token",
            json={
                "grant_type": "client_credentials",
                "client_id": cred.client_id,
                "client_secret": cred.client_secret,
            },
        )
    if r.status_code >= 400:
        raise PipedreamError(
            "Pipedream refused to issue an access token. Check "
            "PIPEDREAM_CLIENT_ID / PIPEDREAM_CLIENT_SECRET."
        )
    body = r.json()
    token = body.get("access_token") or ""
    if not token:
        raise PipedreamError("Pipedream returned no access token.")
    # 60s of slack so a token minted here is still valid when the call lands.
    minted = AccessToken(
        token=token,
        expires_at=time.time() + max(int(body.get("expires_in", 3600)) - 60, 0),
    )
    _token_cache[key] = minted
    return minted


async def connect_link(
    cred: Credential, *, workspace_id: str, app: str, callback_url: str
) -> str:
    """The hosted page where the admin authorizes `app` for this Workspace.

    The token carries our own signed state on both redirect URIs, so the callback
    learns which Workspace and which app from us, never from the query string.
    """
    state = sign_state(workspace_id=workspace_id, app=app)
    token = (await access_token(cred)).token
    body = await _request(
        "POST",
        f"{_API}/connect/{cred.project_id}/tokens",
        cred=cred,
        token=token,
        json={
            "external_user_id": workspace_id,
            "success_redirect_uri": f"{callback_url}?{urlencode({'state': state})}",
            "error_redirect_uri": f"{callback_url}?{urlencode({'state': state, 'error': '1'})}",
        },
    )
    url = body.get("connect_link_url")
    if not url:
        raise PipedreamError("Pipedream returned no connect link.")
    return f"{url}{'&' if '?' in url else '?'}{urlencode({'app': app})}"


async def list_accounts(
    cred: Credential, *, workspace_id: str, app: str | None = None
) -> list[dict[str, Any]]:
    """This Workspace's Connected apps, as Pipedream sees them."""
    token = (await access_token(cred)).token
    params = {"external_user_id": workspace_id}
    if app:
        params["app"] = app
    body = await _request(
        "GET",
        f"{_API}/connect/{cred.project_id}/accounts",
        cred=cred,
        token=token,
        params=params,
    )
    data = body.get("data")
    if not isinstance(data, list):
        raise PipedreamError("Pipedream returned an unexpected account list.")
    return [_account(a) for a in data if isinstance(a, dict)]


async def delete_account(cred: Credential, account_id: str) -> None:
    token = (await access_token(cred)).token
    await _request(
        "DELETE",
        f"{_API}/connect/{cred.project_id}/accounts/{account_id}",
        cred=cred,
        token=token,
    )


def _account(raw: dict[str, Any]) -> dict[str, Any]:
    """Public shape of a Connected app. Pipedream can return credentials on some
    endpoints — this allowlist is what stops them reaching a response body."""
    app = raw.get("app") or {}
    slug = app.get("name_slug") or app.get("slug") or ""
    return {
        "id": raw.get("id"),
        "app": slug,
        "name": app.get("name") or CATALOG.get(slug, slug),
        "account_name": raw.get("name"),
        "healthy": raw.get("healthy", True),
        "created_at": raw.get("created_at"),
    }
