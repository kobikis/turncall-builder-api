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

# Pipedream exposes one MCP base URL for every app and routes entirely by header.
MCP_URL = "https://remote.mcp.pipedream.net/v3"


@dataclass(frozen=True)
class CatalogApp:
    """An app this build can connect, and the tools an agent may actually call.

    `slug` is Pipedream's own app slug, which is also the prefix of every tool
    name it generates. Slack's is `slack_v2` — the registry package was versioned
    and the slug went with it, so `slack` is not the name of anything current.
    """

    slug: str
    name: str
    tools: tuple[str, ...]


# v1 catalog (ADR-0018): the two apps that were actually asked for. ADR-0017's
# raw `mcp_servers` form stays the escape hatch for anything else.
#
# The `tools` tuple is a pinned allowlist, and it is not optional. Pipedream
# generates one MCP tool per action in an app's registry: 50 for Slack, 17 for
# Google Calendar (measured against PipedreamHQ/pipedream). TurnCall caps a
# server at 50 tools and an agent at 100, so unfiltered Slack alone lands exactly
# on one cap and eats half the other — and every advertised tool rides in every
# LLM request for the length of the call, which costs latency and tool-selection
# quality, not just tokens.
CATALOG = {
    app.slug: app
    for app in (
        CatalogApp(
            slug="slack_v2",
            name="Slack",
            tools=(
                # Channels, users and groups from one tool, threads included.
                "slack_v2-post-message",
                "slack_v2-list-channels",
                "slack_v2-find-user-by-email",
            ),
        ),
        CatalogApp(
            slug="google_calendar",
            name="Google Calendar",
            tools=(
                "google_calendar-create-event",
                "google_calendar-list-events",
                "google_calendar-get-event",
                "google_calendar-update-event",
                # Booking a meeting means knowing when someone is free.
                "google_calendar-query-free-busy-calendars",
            ),
        ),
    )
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
    known = CATALOG.get(slug)
    return {
        "id": raw.get("id"),
        "app": slug,
        "name": app.get("name") or (known.name if known else slug),
        "account_name": raw.get("name"),
        "healthy": raw.get("healthy", True),
        "created_at": raw.get("created_at"),
    }


# --- agent config ------------------------------------------------------------


def mcp_servers_for(
    apps: list[str], *, project_id: str, environment: str, external_user_id: str
) -> list[dict[str, Any]]:
    """The `mcp_servers` entries for a list of attached app slugs (ADR-0018).

    One Pipedream MCP URL serves every app and routes by header, so two attached
    apps are two entries sharing a URL and differing by `x-pd-app-slug`.

    No Authorization header: the access token lives an hour, so it is minted per
    call and injected through call-init (#27), not stored in agent config.

    An app with no pinned `tool_filter` is skipped rather than sent — an
    unfiltered server is a config that works in the demo and blows a tool cap in
    production.
    """
    servers = []
    for slug in apps:
        app = CATALOG.get(slug)
        if app is None or not app.tools:
            continue
        servers.append(
            {
                "name": f"pipedream_{slug}",
                "transport": "http",
                "url": MCP_URL,
                "headers": {
                    "x-pd-project-id": project_id,
                    "x-pd-environment": environment,
                    "x-pd-external-user-id": external_user_id,
                    "x-pd-app-slug": slug,
                },
                "tool_filter": list(app.tools),
            }
        )
    return servers


def authorize(servers: list[dict[str, Any]], token: str) -> list[dict[str, Any]]:
    """Return a NEW server list with a bearer token on every Pipedream entry.

    Identified by URL, not by name: the name is ours to choose and a hand-written
    server could borrow it, but only a Pipedream entry points at Pipedream's MCP.

    This is the one place a credential enters an agent config, and it happens per
    call — the token lives an hour, so it is minted for the call and travels with
    it. Nothing stored ever carries it.
    """
    return [
        {**s, "headers": {**s.get("headers", {}), "Authorization": f"Bearer {token}"}}
        if s.get("url") == MCP_URL
        else s
        for s in servers
    ]
