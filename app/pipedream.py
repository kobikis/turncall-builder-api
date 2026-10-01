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
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.secret_box import SecretBox

_API = "https://api.pipedream.com/v1"
# The auth handshake is a redirect out and back; ten minutes is generous for a
# consent screen and short enough that a leaked link is worthless.
_STATE_MAX_AGE = 600
_STATE_SALT = "pipedream-connect"

# Pipedream exposes one MCP base URL for every app and routes entirely by header.
MCP_URL = "https://remote.mcp.pipedream.net/v3"


@dataclass(frozen=True)
class CatalogInput:
    """One input an end-of-call Action needs from the Workspace.

    `name` is Pipedream's own prop name and never reaches a person; `label` is
    what they are actually asked for.
    """

    name: str
    label: str
    placeholder: str = ""
    # An Action that lists the values this input accepts for a connected
    # account. Set = the Console offers a picker instead of a text field, which
    # is the only defence against an opaque identifier: a Google calendar ID
    # differing from the right one by a single character is accepted by the
    # form, refused by Google, and shows up as an entry that never appears.
    options_action: str = ""
    # Where the value and the human-readable name live in each returned item.
    option_value: str = "id"
    option_label: str = "name"
    # The key holding the list, when the Action returns an object rather than
    # one. Not a guess: `google_calendar-list-calendars` answers with a list and
    # `slack_v2-list-channels` answers with `{"channels": [...]}`, so two apps
    # were already enough to need this.
    option_root: str = ""


@dataclass(frozen=True)
class CatalogAction:
    """An Action that makes sense to run when a call ends (ADR-0021).

    Not every tool does. `list-channels` is useful mid-call and meaningless
    afterwards, and an Action whose required inputs cannot be known from a
    `call.ended` payload cannot be offered at all — it would configure cleanly
    and then fail every night.

    `summary_arg` is the input the summary rides in. It belongs here rather than
    in an agent's config because it is a fact about the Action, identical for
    every Workspace that ever picks it: asking a person to supply a constant is
    not configuration.
    """

    id: str
    label: str
    summary_arg: str
    # The component's own name for its app prop, which is NOT the app slug:
    # slack_v2-post-message declares `slack`. Sending the credential under the
    # slug leaves the prop unset, so the action runs unauthenticated and Slack
    # answers `not_authed` — a 200 from Pipedream with nothing posted.
    app_prop: str
    inputs: tuple[CatalogInput, ...] = ()
    # Props filled from the `call.ended` payload rather than from the form:
    # prop name -> a format template over the payload. This is what lets an
    # Action be offered whose inputs a Workspace could not possibly type — a
    # calendar entry for the call has to start when the call did. Like
    # `summary_arg` it is a fact about the Action, identical everywhere, so it
    # never reaches the config or the Console.
    derived: tuple[tuple[str, str], ...] = ()
    # How the summary wrapper is marked up. Slack renders `*bold*`; a calendar
    # description renders it as punctuation. ADR-0021 flagged this as due the
    # moment a second Action existed.
    summary_style: str = "plain"


@dataclass(frozen=True)
class CatalogApp:
    """An app this build can connect, and the tools an agent may actually call.

    `slug` is Pipedream's own app slug, which is also the prefix of every tool
    name it generates. Slack's is `slack_v2` — the registry package was versioned
    and the slug went with it, so `slack` is not the name of anything current.

    `tools` is the mid-call allowlist; `actions` is the end-of-call menu. They
    are different questions (ADR-0021) and an app may have one and not the other.
    """

    slug: str
    name: str
    tools: tuple[str, ...]
    actions: tuple[CatalogAction, ...] = ()


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
            actions=(
                CatalogAction(
                    id="slack_v2-post-message",
                    label="Post the call summary",
                    summary_arg="text",
                    app_prop="slack",
                    summary_style="slack",
                    inputs=(
                        CatalogInput(
                            name="channel",
                            label="Channel",
                            placeholder="#sales-calls",
                            options_action="slack_v2-list-channels",
                            option_root="channels",
                            option_label="name",
                        ),
                    ),
                ),
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
            actions=(
                # `create-event` needs a start and an end, which is why this was
                # once left empty: a `call.ended` payload says when a *call*
                # finished, not when a meeting should be. But it says exactly
                # when the call ran, and an entry for the call itself is a real
                # thing to want — so the times come from the payload and the
                # Workspace gets the same choice every other app has.
                #
                # Scheduling a *follow-up* still is not offered: nothing in the
                # payload knows when it should be. That needs takeaway fields
                # bound to inputs, which is a separate change.
                CatalogAction(
                    id="google_calendar-create-event",
                    label="Add the call to a calendar",
                    # The event body. `summary` is Google's name for the title,
                    # which is why the summary does not ride in it.
                    summary_arg="description",
                    app_prop="googleCalendar",
                    inputs=(
                        # "Calendar" with a placeholder of `primary` reads as
                        # an invitation to name the calendar, and Google answers
                        # a display name with 404 Not Found — into a log nobody
                        # is watching. Ask for the thing it actually wants.
                        CatalogInput(
                            name="calendarId",
                            label="Calendar",
                            placeholder="primary, or …@group.calendar.google.com",
                            options_action="google_calendar-list-calendars",
                            option_label="summary",
                        ),
                        CatalogInput(
                            name="summary",
                            label="Event title",
                            placeholder="Customer call",
                        ),
                    ),
                    derived=(
                        ("eventStartDate", "{started_at}"),
                        ("eventEndDate", "{ended_at}"),
                    ),
                ),
            ),
        ),
    )
}


@dataclass(frozen=True)
class CallEnded:
    """The one Action an Automation runs when a call ends (ADR-0021).

    Its arguments are bound here rather than at runtime because nothing is
    choosing: the generated handler just runs it. `args` is an open dict — the
    action's own shape, never a Slack-shaped field — so a second catalog app
    needs no change here.
    """

    action: str
    args: dict[str, Any]
    # Which of the action's inputs receives the summary the handler composes.
    # Named rather than assumed because it is the action's own prop: Slack calls
    # it `text`, and nothing says the next catalog app will agree.
    summary_arg: str


@dataclass(frozen=True)
class Integration:
    """An attached app and when it runs (ADR-0021)."""

    app: str
    mid_call: bool
    call_ended: CallEnded | None


def parse_integrations(value: Any) -> list[Integration]:
    """Read `config.integrations` in either shape.

    A bare slug is the pre-ADR-0021 form and means mid-call only. An end-of-call
    trigger is something a Workspace opts into, never something an upgrade turns
    on underneath it — so widening the reader is the whole migration.

    Unreadable entries are skipped, not raised on: validation belongs at the API
    edge (`_validate_agent_config`), and this runs against configs that are
    already stored, where a 500 deep in the mapper would be the worse failure.
    """
    out: list[Integration] = []
    for entry in value or []:
        if isinstance(entry, str):
            out.append(Integration(app=entry, mid_call=True, call_ended=None))
            continue
        if not isinstance(entry, dict):
            continue
        app = entry.get("app")
        if not isinstance(app, str) or not app:
            continue
        raw = entry.get("call_ended")
        call_ended = None
        if isinstance(raw, dict) and isinstance(raw.get("action"), str) and raw["action"]:
            args = raw.get("args")
            summary_arg = raw.get("summary_arg")
            call_ended = CallEnded(
                action=raw["action"],
                args=args if isinstance(args, dict) else {},
                summary_arg=summary_arg if isinstance(summary_arg, str) else "",
            )
        out.append(
            Integration(
                app=app, mid_call=bool(entry.get("mid_call")), call_ended=call_ended
            )
        )
    return out


def mid_call_apps(value: Any) -> list[str]:
    """The attached apps that get an `mcp_servers` entry — the in-call half.

    An end-of-call-only attachment returns nothing here, which is the point: it
    must not hand the model a tool it never asked to expose.
    """
    return [i.app for i in parse_integrations(value) if i.mid_call]


def automations(value: Any) -> list[Integration]:
    """The attached apps with an end-of-call Automation — the generated half."""
    return [i for i in parse_integrations(value) if i.call_ended]


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


_BOX = SecretBox(
    env="PIPEDREAM_ENCRYPTION_KEY",
    error=PipedreamError,
    disabled="integrations are disabled",
    changed=(
        "The stored Pipedream client secret could not be decrypted — "
        "PIPEDREAM_ENCRYPTION_KEY has changed. Restart with the original "
        "key, or set PIPEDREAM_CLIENT_SECRET again to re-store it."
    ),
)


def encrypt_secret(secret: str) -> str:
    return _BOX.encrypt(secret)


def decrypt_secret(ciphertext: str) -> str:
    """Raises if the key changed — rotating it orphans the stored credential."""
    return _BOX.decrypt(ciphertext)


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
    stable by construction (changing it is what re-stores the credential).

    A deployment where every Workspace brings its own Pipedream project need not
    set any PIPEDREAM_* at all, so the Integrations key — required for those
    projects' sealed secrets anyway — signs the state there instead."""
    key = os.environ.get("PIPEDREAM_ENCRYPTION_KEY", "") or os.environ.get(
        "INTEGRATIONS_ENCRYPTION_KEY", ""
    )
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
            "Pipedream rejected our credentials. Check the workspace's Pipedream "
            "project on Integrations, or PIPEDREAM_CLIENT_ID / PIPEDREAM_CLIENT_SECRET."
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
            "Pipedream refused to issue an access token. Check the workspace's "
            "Pipedream project on Integrations, or PIPEDREAM_CLIENT_ID / "
            "PIPEDREAM_CLIENT_SECRET."
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


def _action_error(result: Any) -> str | None:
    """What the third party refused, if it did.

    Pipedream answers 200 whether or not the Action worked: a run Google
    rejected is a successful HTTP call whose observations carry the error.
    Reading only the status code is why a calendar entry that never appeared
    looked exactly like one that did.

    The generated Agent Backend carries its own copy of this. It is the same
    knowledge, but it runs in a different repo with no import path back here.
    """
    if not isinstance(result, dict):
        return None
    for observation in result.get("os") or []:
        if not isinstance(observation, dict) or observation.get("k") != "error":
            continue
        err = observation.get("err") or {}
        data = err.get("data") or {}
        return str(data.get("error") or err.get("message") or "the action failed")
    return None


async def run_action(
    cred: Credential,
    *,
    workspace_id: str,
    action_id: str,
    app_prop: str,
    account_id: str,
    props: dict[str, Any] | None = None,
) -> Any:
    """Run one Pipedream Action as this Workspace and return its `ret`.

    The account rides under the component's own prop name, never the app slug —
    the wrong key runs the Action with no credential and the third party answers
    a cheerful 200 with nothing done.
    """
    token = (await access_token(cred)).token
    body = await _request(
        "POST",
        f"{_API}/connect/{cred.project_id}/actions/run",
        cred=cred,
        token=token,
        json={
            "id": action_id,
            "external_user_id": workspace_id,
            "configured_props": {
                app_prop: {"authProvisionId": account_id},
                **(props or {}),
            },
        },
    )
    refused = _action_error(body)
    if refused:
        raise PipedreamError(f"{action_id} was refused: {refused}")
    return body.get("ret") if isinstance(body, dict) else None


async def input_options(
    cred: Credential, *, workspace_id: str, app: str, prop: str
) -> list[dict[str, str]]:
    """The values a catalog input accepts, for this Workspace's connected app.

    Fetched per account and never cached: which calendars exist is the
    Workspace's business and changes without telling us.
    """
    known = CATALOG.get(app)
    if known is None:
        raise PipedreamError(f"{app} is not an app this build can connect.")
    field = next(
        (
            (action, i)
            for action in known.actions
            for i in action.inputs
            if i.name == prop and i.options_action
        ),
        None,
    )
    if field is None:
        raise PipedreamError(f"{prop} is typed in, not chosen from a list.")
    action, spec = field

    accounts = await list_accounts(cred, workspace_id=workspace_id, app=app)
    if not accounts:
        raise PipedreamError(f"No {known.name} account is connected yet.")

    items = await run_action(
        cred,
        workspace_id=workspace_id,
        action_id=spec.options_action,
        app_prop=action.app_prop,
        account_id=accounts[0]["id"],
    )
    if spec.option_root and isinstance(items, dict):
        items = items.get(spec.option_root)
    if not isinstance(items, list):
        raise PipedreamError(f"{spec.options_action} returned no list to choose from.")
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get(spec.option_value)
        if not isinstance(value, str) or not value:
            continue
        label = item.get(spec.option_label)
        out.append(
            {"value": value, "label": label if isinstance(label, str) and label else value}
        )
    # Sorted here rather than in the Console: every caller wants the same order,
    # and a Slack workspace can answer with a hundred channels.
    # ponytail: whatever one call returns. Paging is a change if a Workspace
    # ever has more channels than Pipedream returns in one go.
    return sorted(out, key=lambda o: o["label"].lower())


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
