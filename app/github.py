"""GitHub connections and pushing an Agent Backend to a Linked repo (ADR-0013).

A connection is one User's fine-grained personal access token, stored encrypted
because it is the first credential the builder must read back rather than hash.
A Linked repo is an owner, repository, branch and optional path the user chose —
nothing is ever created on their behalf.

Pushing is deliberately not `git push` of the local repo. A Linked repo may hold
other agents under other paths, so a push clones the target, writes this agent's
subtree, commits on top of whatever is there, and pushes fast-forward only. It
never force-pushes and never rewrites anything outside its own path.

Divergence — someone edited this agent's files on GitHub — is detected by
comparing the remote subtree against a hash of what we last pushed. On a
mismatch the push refuses and reports it. Overwriting is recoverable via git,
but silently discarding someone's fix is the worst outcome available here.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

_API = "https://api.github.com"
_UA = {"User-Agent": "turncall-builder", "X-GitHub-Api-Version": "2022-11-28"}


class GitHubError(RuntimeError):
    """A GitHub operation failed with something the user can act on."""


class DivergedError(GitHubError):
    """The remote subtree changed since we last pushed it."""


@dataclass(frozen=True)
class TokenIdentity:
    """Who a token belongs to, and when it stops working."""

    login: str
    expires_at: datetime | None


@dataclass(frozen=True)
class PushResult:
    commit_sha: str
    tree_hash: str


# --- token storage -----------------------------------------------------------


def _fernet() -> Fernet:
    key = os.environ.get("GITHUB_TOKEN_ENCRYPTION_KEY", "")
    if not key:
        raise GitHubError(
            "GITHUB_TOKEN_ENCRYPTION_KEY is not set — GitHub connections are "
            "disabled. Generate one with: python -c \"from cryptography.fernet "
            'import Fernet; print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise GitHubError(
            "GITHUB_TOKEN_ENCRYPTION_KEY is not a valid Fernet key (44 url-safe "
            "base64 characters)."
        ) from exc


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """Raises if the key changed — rotating it invalidates every stored token."""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise GitHubError(
            "Stored GitHub token could not be decrypted — GITHUB_TOKEN_"
            "ENCRYPTION_KEY has changed. Reconnect GitHub to store it again."
        ) from exc


# --- GitHub API --------------------------------------------------------------


async def _get(token: str, path: str, **params: Any) -> tuple[Any, httpx.Headers]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{_API}{path}",
            headers={**_UA, "Authorization": f"Bearer {token}"},
            params=params or None,
        )
    if r.status_code == 401:
        raise GitHubError("GitHub rejected this token. It may be revoked or expired.")
    if r.status_code == 403:
        raise GitHubError(
            "GitHub refused the request. The token may not have access to this "
            "resource, or you have hit a rate limit."
        )
    if r.status_code >= 400:
        raise GitHubError(f"GitHub returned {r.status_code}: {r.text[:200]}")
    return r.json(), r.headers


async def identify(token: str) -> TokenIdentity:
    """Who the token belongs to, and its expiry if GitHub reports one.

    Fine-grained tokens carry an expiry; classic ones may not. The expiry is
    what lets the console warn before pushes start failing quietly.
    """
    data, headers = await _get(token, "/user")
    raw = headers.get("github-authentication-token-expiration", "")
    expires: datetime | None = None
    if raw:
        try:
            expires = datetime.fromisoformat(raw.strip().replace(" UTC", "+00:00"))
        except ValueError:
            expires = None
    return TokenIdentity(login=data.get("login", ""), expires_at=expires)


async def list_repos(token: str) -> list[dict[str, Any]]:
    """Repositories the token can push to, newest first.

    Filtered to `permissions.push`: offering a repo the user cannot write to
    would produce a connection that looks fine and fails on first push.
    """
    data, _ = await _get(token, "/user/repos", per_page=100, sort="updated")
    return [
        {
            "owner": r["owner"]["login"],
            "name": r["name"],
            "full_name": r["full_name"],
            "default_branch": r.get("default_branch") or "main",
            "private": r.get("private", False),
        }
        for r in data
        if (r.get("permissions") or {}).get("push")
    ]


async def list_branches(token: str, owner: str, repo: str) -> list[str]:
    data, _ = await _get(token, f"/repos/{owner}/{repo}/branches", per_page=100)
    return [b["name"] for b in data]


async def assert_writable(token: str, owner: str, repo: str) -> None:
    """Check push access at link time rather than discovering it on first push.

    A link that looks connected and fails later is the silent-success failure
    this codebase keeps getting bitten by.
    """
    data, _ = await _get(token, f"/repos/{owner}/{repo}")
    if not (data.get("permissions") or {}).get("push"):
        raise GitHubError(
            f"This token cannot push to {owner}/{repo}. Grant it "
            "'Contents: Read and write' on that repository."
        )


# --- the agent config, as a file ---------------------------------------------

# Redacted by *key name*, recursively, rather than by known path. A path list
# silently stops covering a config that grows a new secret field; matching the
# name means a future one is caught by default. Over-redacting a config file is
# recoverable; publishing an agent's HMAC secret to GitHub is not.
_SECRET_KEYS = {
    "webhook_secret",
    "api_key",
    "secret",
    "secret_access_key",
    "session_token",
    "access_key_id",
    "token",
    "password",
    "authorization",
    # MCP servers document these as carrying credentials.
    "headers",
    "env",
}

REDACTED = "***"


def redact_config(value: Any) -> Any:
    """Deep copy with every secret-named key masked.

    Masked rather than dropped: a reader reconstructing the agent needs to know
    a value existed and must be supplied, which a missing key does not say.
    """
    if isinstance(value, dict):
        return {
            k: (REDACTED if k.lower() in _SECRET_KEYS and v not in (None, "") else redact_config(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_config(v) for v in value]
    return value


def agent_config_file(config: dict[str, Any]) -> str:
    """The agent's configuration as it is pushed: redacted, sorted, stable.

    Sorted keys and a trailing newline so an unchanged config produces an empty
    diff — otherwise every push would look like a change.
    """
    return json.dumps(redact_config(config), indent=2, sort_keys=True) + "\n"


# --- pushing -----------------------------------------------------------------

# Never pushed: local git internals, and the secrets file the scaffold keeps out
# of git on purpose. .env.example IS pushed — it is what makes a clone runnable.
_NEVER_PUSH = {".git", ".env", "events.db", "__pycache__"}


def _collect(service_dir: str) -> dict[str, bytes]:
    """The agent's files as {relative path: bytes}, minus what must not leave."""
    out: dict[str, bytes] = {}
    for root, dirs, names in os.walk(service_dir):
        dirs[:] = [d for d in dirs if d not in _NEVER_PUSH]
        for name in names:
            if name in _NEVER_PUSH:
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, service_dir)
            out[rel] = open(full, "rb").read()
    return out


def tree_hash(files: dict[str, bytes]) -> str:
    """Content hash of a file set, order-independent.

    Stored after a push and compared against the remote subtree before the next
    one, so an upstream edit is detected instead of silently overwritten.
    """
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(path.encode())
        h.update(b"\0")
        h.update(hashlib.sha256(files[path]).digest())
    return h.hexdigest()


def _read_subtree(checkout: str, path: str) -> dict[str, bytes]:
    root = os.path.join(checkout, path) if path else checkout
    return _collect(root) if os.path.isdir(root) else {}


async def _git(args: list[str], cwd: str, token: str = "") -> tuple[int, str]:
    """Run git, with the token scrubbed from anything we return or log."""
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    out, _ = await proc.communicate()
    text = out.decode(errors="replace")
    if token:
        text = text.replace(token, "***")
    return proc.returncode or 0, text


def _remote(token: str, owner: str, repo: str) -> str:
    # x-access-token is GitHub's documented username for token auth over HTTPS.
    return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"


async def push_backend(
    *,
    service_dir: str,
    token: str,
    owner: str,
    repo: str,
    branch: str,
    path: str = "",
    last_tree_hash: str | None = None,
    message: str | None = None,
    agent_config: dict[str, Any] | None = None,
) -> PushResult:
    """Publish this agent's files into owner/repo at branch:path.

    Clones the target, refuses if the subtree changed upstream since our last
    push, replaces it with the local files, and pushes fast-forward only.
    Everything outside `path` is left exactly as it was.

    `agent_config` is written as `agent.json` alongside the code, secrets
    redacted — so the repository describes the whole agent rather than only its
    backend service.

    Raises DivergedError when the remote subtree no longer matches what we
    pushed, and GitHubError for anything else.
    """
    local = _collect(service_dir)
    if not local:
        raise GitHubError(f"Nothing to push — {service_dir} is empty.")

    # The generated backend contains no model, prompt or voice — those live in
    # the agent config, which is not code. Without this file the repo cannot
    # reconstruct the agent, and a model change produces an empty diff.
    if agent_config:
        local["agent.json"] = agent_config_file(agent_config).encode()

    remote = _remote(token, owner, repo)
    workdir = tempfile.mkdtemp(prefix="turncall-push-")
    checkout = os.path.join(workdir, "repo")
    try:
        rc, out = await _git(
            ["clone", "--depth", "1", "--branch", branch, remote, checkout],
            workdir,
            token,
        )
        if rc != 0:
            # A branch that does not exist yet is a normal case: start it from
            # whatever the repo's default is rather than failing the user.
            rc, out = await _git(["clone", "--depth", "1", remote, checkout], workdir, token)
            if rc != 0:
                raise GitHubError(f"Could not clone {owner}/{repo}: {out.strip()[:300]}")
            rc, out = await _git(["checkout", "-b", branch], checkout, token)
            if rc != 0:
                raise GitHubError(f"Could not create branch {branch}: {out.strip()[:300]}")

        # Divergence: has anyone touched our subtree since we last wrote it?
        existing = _read_subtree(checkout, path)
        if last_tree_hash and existing and tree_hash(existing) != last_tree_hash:
            raise DivergedError(
                f"{owner}/{repo}@{branch}"
                + (f":{path}" if path else "")
                + " has changes the builder did not make. Nothing was pushed — "
                "reconcile them in your repository first."
            )

        target = os.path.join(checkout, path) if path else checkout
        # Replace only our subtree. Everything else in the repo is untouched.
        if path and os.path.isdir(target):
            shutil.rmtree(target)
        os.makedirs(target, exist_ok=True)
        for rel, blob in local.items():
            dest = os.path.join(target, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(blob)

        await _git(["add", "-A", "--", path or "."], checkout, token)
        rc, out = await _git(["diff", "--cached", "--quiet"], checkout, token)
        if rc == 0:
            # Nothing changed — report the current head rather than an empty commit.
            _, head = await _git(["rev-parse", "HEAD"], checkout, token)
            return PushResult(commit_sha=head.strip()[:40], tree_hash=tree_hash(local))

        await _git(["config", "user.email", "builder@turncall.local"], checkout, token)
        await _git(["config", "user.name", "TurnCall Builder"], checkout, token)
        rc, out = await _git(
            ["commit", "-q", "-m", message or "turncall: sync agent backend"],
            checkout,
            token,
        )
        if rc != 0:
            raise GitHubError(f"Commit failed: {out.strip()[:300]}")

        # No --force, ever. A rejected push means the branch moved under us.
        rc, out = await _git(["push", "origin", f"HEAD:{branch}"], checkout, token)
        if rc != 0:
            raise DivergedError(
                f"Push to {owner}/{repo}@{branch} was rejected — the branch has "
                f"moved since it was cloned. Nothing was force-pushed. {out.strip()[:200]}"
            )

        _, head = await _git(["rev-parse", "HEAD"], checkout, token)
        return PushResult(commit_sha=head.strip()[:40], tree_hash=tree_hash(local))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
