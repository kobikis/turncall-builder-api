"""Materialize an Agent Backend repo on disk and run it as docker compose (ADR-0004).

The builder container has the Docker CLI + a mounted docker socket, so `docker
compose up` here creates sibling containers on the host. service_dir is on a
host-bind-mounted volume, so the files are real on the host too.

Every materialize commits to git, and any user edits are committed *before* a
regeneration overwrites them — nothing in a generated repo is ever lost, only
layered in `git log`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

logger = logging.getLogger("turncall_builder")

# Commit as the builder regardless of host git config.
_GIT = ["git", "-c", "user.name=TurnCall Builder", "-c", "user.email=builder@turncall.local"]

_AGENT_CONTAINER_PREFIX = "turncall-agent-"


async def _run(cmd: list[str], cwd: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


def write_files(service_dir: str, files: dict[str, str]) -> None:
    os.makedirs(service_dir, exist_ok=True)
    for rel, content in files.items():
        path = os.path.join(service_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(rel) else None
        with open(path, "w") as fh:
            fh.write(content)


def set_env_value(service_dir: str, key: str, value: str) -> None:
    """Set KEY=value in the backend's .env (replacing any existing line)."""
    path = os.path.join(service_dir, ".env")
    lines: list[str] = []
    if os.path.exists(path):
        with open(path) as fh:
            lines = [
                line for line in fh.read().splitlines() if not line.startswith(f"{key}=")
            ]
    lines.append(f"{key}={value}")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


async def _commit_all(service_dir: str, message: str) -> None:
    """Best-effort `git add -A && git commit` — a no-op on a clean tree."""
    await _run(_GIT + ["add", "-A"], service_dir)
    await _run(_GIT + ["commit", "-q", "-m", message], service_dir)


async def materialize_and_run(service_dir: str, files: dict[str, str]) -> tuple[bool, str]:
    """Write the repo, preserve user edits in git, and `docker compose up -d --build`.

    Returns (ok, logs). ok reflects the compose step only (git steps are best
    effort — a missing git binary never blocks generation).
    """
    logs: list[str] = []

    # Preserve any user edits BEFORE overwriting (regen path).
    if os.path.isdir(os.path.join(service_dir, ".git")):
        await _commit_all(service_dir, "user edits before regen")
        logs.append("$ git commit (user edits before regen)")

    write_files(service_dir, files)

    rc, out = await _run(["git", "init", "-q"], service_dir)
    logs.append(f"$ git init\n{out}")
    await _commit_all(service_dir, "builder: generate")

    rc, out = await _run(["docker", "compose", "up", "-d", "--build"], service_dir)
    logs.append(f"$ docker compose up -d --build\n{out}")
    return rc == 0, "\n".join(logs)


# Short TTL so the console's status polling (every /agents + /agents/{id})
# doesn't fork `docker ps` on every request; 2s staleness is invisible to a UI.
_NAMES_TTL_S = 2.0
_names_cache: tuple[float, set[str] | None] | None = None


async def running_container_names() -> set[str] | None:
    """Names of all running containers, or None if docker is unreachable.
    Memoized for a couple of seconds."""
    global _names_cache
    now = time.monotonic()
    if _names_cache is not None and now - _names_cache[0] < _NAMES_TTL_S:
        return _names_cache[1]
    rc, out = await _run(["docker", "ps", "--format", "{{.Names}}"], "/")
    names = (
        None
        if rc != 0
        else {line.strip() for line in out.splitlines() if line.strip()}
    )
    _names_cache = (now, names)
    return names


def orphan_container_names(all_names: list[str], live_slugs: set[str]) -> list[str]:
    """Pure decision: which agent containers have no backing registry row.

    A container is `turncall-agent-<slug>-<slug>-1`, and every live backend's
    slug ends in the agent's id prefix (unique), so a container is an orphan
    when none of the live slugs appears in its name — its row was dropped (e.g.
    a builder DB reset on stack restart) while the container kept running and
    kept squatting on its host port.
    """
    return [
        n
        for n in all_names
        if n.startswith(_AGENT_CONTAINER_PREFIX)
        and not any(slug and slug in n for slug in live_slugs)
    ]


async def reap_orphan_agent_containers(live_slugs: set[str]) -> list[str]:
    """Force-remove agent backend containers whose registry row is gone, freeing
    the host ports they hold so the next generation can bind. Best-effort: docker
    failures are logged, never raised. Returns the names removed."""
    rc, out = await _run(["docker", "ps", "-a", "--format", "{{.Names}}"], "/")
    if rc != 0:
        logger.warning("orphan reap skipped: docker ps failed\n%s", out)
        return []
    all_names = [n.strip() for n in out.splitlines() if n.strip()]
    removed: list[str] = []
    for name in orphan_container_names(all_names, live_slugs):
        rc, out = await _run(["docker", "rm", "-f", name], "/")
        if rc == 0:
            logger.info("reaped orphan agent container: %s", name)
            removed.append(name)
        else:
            logger.warning("failed to reap orphan %s\n%s", name, out)
    return removed


async def restart(service_dir: str) -> tuple[bool, str]:
    """Recreate the container to pick up an .env/config change. No `--build` —
    the code is unchanged, so reuse the existing image (compose still builds it
    if it's missing). Code changes go through materialize_and_run, which builds.
    Avoids a multi-second image rebuild on every call-init bind + Start."""
    rc, out = await _run(["docker", "compose", "up", "-d"], service_dir)
    return rc == 0, out


async def teardown(service_dir: str) -> tuple[bool, str]:
    """Stop and remove the backend container. The repo stays on disk."""
    rc, out = await _run(["docker", "compose", "down"], service_dir)
    return rc == 0, out
