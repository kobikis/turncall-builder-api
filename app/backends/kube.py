"""Agent Backends on Kubernetes (ADR-0025): generator's lifecycle without Docker.

`BACKEND_RUNTIME=kubernetes` swaps `docker compose` for objects in the builder's
own namespace, applied through the API server with the pod's service account:

- a ConfigMap with the backend's code (`app.py`, `requirements.txt`) and a
  Secret with its `.env` — the generated repo stays the source of truth, so a
  restart re-reads both, exactly as `docker compose up` re-reads the files;
- a Deployment on one shared runtime image (`AGENT_RUNTIME_IMAGE`) that
  carries the scaffold's fixed requirements, so nothing is built in-cluster;
- a volume for the backend's SQLite (`/data`), and a Service, which is what
  TurnCall and the builder reach instead of `host.docker.internal:<port>`.

The Deployment — so the pod — is named for the agent (its slug,
`hotel-receptionist-12d9bda3`), which is what `kubectl get pods` should show.
Everything else is named by the registry port (`turncall-agent-<port>`): the
Service is the address TurnCall already holds in the agent's tool and call-init
URLs, and `registry.backend_url(port)` keeps its signature.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import ssl
import time
from typing import Any

import httpx

ENABLED = os.environ.get("BACKEND_RUNTIME", "docker") == "kubernetes"
RUNTIME_IMAGE = os.environ.get("AGENT_RUNTIME_IMAGE", "turncall-agent-runtime:latest")
DATA_SIZE = os.environ.get("AGENT_DATA_SIZE", "1Gi")
READY_TIMEOUT_S = float(os.environ.get("AGENT_READY_TIMEOUT_SECONDS", "180"))

_SA = "/var/run/secrets/kubernetes.io/serviceaccount"
_MANAGER = "turncall-builder"
_SELECTOR = f"app.kubernetes.io/managed-by={_MANAGER}"
_SLUG = "turncall.dev/agent-slug"
_CODE_FILES = ("app.py", "requirements.txt")
# The scaffold's requirements are baked into the runtime image, so this is a
# no-op offline; a dependency the user added to their repo lands on the volume.
_COMMAND = (
    "pip install --user --no-cache-dir -q -r /code/requirements.txt"
    " && exec uvicorn --app-dir /code app:app --host 0.0.0.0 --port 8000"
)


def name(port: int) -> str:
    return f"turncall-agent-{port}"


def _slug(service_dir: str) -> str:
    return os.path.basename(service_dir.rstrip("/")).removeprefix("turncall-agent-")


def deployment_name(service_dir: str) -> str:
    """The agent's slug, within the 63 characters a pod's name may carry; a long
    name is cut before its agent-id suffix, which keeps it unique."""
    slug = _slug(service_dir)
    return slug if len(slug) <= 63 else f"{slug[:54].rstrip('-')}-{slug[-8:]}"


def namespace() -> str:
    env = os.environ.get("POD_NAMESPACE")
    if env:
        return env
    with open(f"{_SA}/namespace") as fh:
        return fh.read().strip()


def service_url(port: int) -> str:
    return f"http://{name(port)}.{namespace()}.svc.cluster.local:8000"


def _client() -> httpx.AsyncClient:
    """In-cluster API client. The token file is re-read each time: the kubelet
    rotates projected tokens."""
    with open(f"{_SA}/token") as fh:
        token = fh.read().strip()
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    return httpx.AsyncClient(
        base_url=f"https://{host}:{port}",
        headers={"Authorization": f"Bearer {token}"},
        verify=ssl.create_default_context(cafile=f"{_SA}/ca.crt"),
        timeout=15,
    )


def _path(kind: str, ns: str, obj_name: str = "") -> str:
    plural = {
        "ConfigMap": "/api/v1/namespaces/{ns}/configmaps",
        "Secret": "/api/v1/namespaces/{ns}/secrets",
        "Service": "/api/v1/namespaces/{ns}/services",
        "PersistentVolumeClaim": "/api/v1/namespaces/{ns}/persistentvolumeclaims",
        "Deployment": "/apis/apps/v1/namespaces/{ns}/deployments",
    }[kind].format(ns=ns)
    return f"{plural}/{obj_name}" if obj_name else plural


def _read_env(service_dir: str) -> dict[str, str]:
    path = os.path.join(service_dir, ".env")
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        lines = fh.read().splitlines()
    pairs = (ln.partition("=") for ln in lines if "=" in ln and not ln.lstrip().startswith("#"))
    return {k.strip(): v for k, _, v in pairs}


def _read_code(service_dir: str) -> dict[str, str]:
    code = {}
    for rel in _CODE_FILES:
        with open(os.path.join(service_dir, rel)) as fh:
            code[rel] = fh.read()
    return code


def manifests(service_dir: str, port: int) -> list[dict[str, Any]]:
    """The backend's objects, built from what is on disk now. Pure but for reads."""
    obj, slug = name(port), _slug(service_dir)
    labels = {
        "app.kubernetes.io/name": "turncall-agent",
        "app.kubernetes.io/instance": obj,
        "app.kubernetes.io/managed-by": _MANAGER,
    }
    meta = {"labels": labels, "annotations": {_SLUG: slug}}
    code, env = _read_code(service_dir), _read_env(service_dir)
    # A changed file or secret must roll the pod; the volumes alone never would.
    checksum = hashlib.sha256(json.dumps([code, env], sort_keys=True).encode()).hexdigest()
    selector = {"app.kubernetes.io/instance": obj}
    return [
        {"apiVersion": "v1", "kind": "ConfigMap",
         "metadata": {"name": f"{obj}-code", **meta}, "data": code},
        {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
         "metadata": {"name": f"{obj}-env", **meta}, "stringData": env},
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
         "metadata": {"name": f"{obj}-data", **meta},
         "spec": {"accessModes": ["ReadWriteOnce"],
                  "resources": {"requests": {"storage": DATA_SIZE}}}},
        {"apiVersion": "v1", "kind": "Service",
         "metadata": {"name": obj, **meta},
         "spec": {"selector": selector, "ports": [{"name": "http", "port": 8000}]}},
        {"apiVersion": "apps/v1", "kind": "Deployment",
         "metadata": {"name": deployment_name(service_dir), **meta},
         "spec": {
             "replicas": 1,
             # One writer for the SQLite on a ReadWriteOnce volume.
             "strategy": {"type": "Recreate"},
             "selector": {"matchLabels": selector},
             "template": {
                 "metadata": {"labels": labels, "annotations": {"checksum": checksum}},
                 "spec": {
                     "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                         "runAsGroup": 10001, "fsGroup": 10001},
                     "containers": [{
                         "name": "backend",
                         "image": RUNTIME_IMAGE,
                         "command": ["sh", "-c", _COMMAND],
                         "workingDir": "/data",  # events.db lands on the volume
                         "env": [{"name": "PYTHONUSERBASE", "value": "/data/.local"}],
                         "envFrom": [{"secretRef": {"name": f"{obj}-env"}}],
                         "ports": [{"name": "http", "containerPort": 8000}],
                         "readinessProbe": {"httpGet": {"path": "/health", "port": "http"},
                                            "periodSeconds": 3},
                         "volumeMounts": [{"name": "code", "mountPath": "/code"},
                                          {"name": "data", "mountPath": "/data"}],
                     }],
                     "volumes": [
                         {"name": "code", "configMap": {"name": f"{obj}-code"}},
                         {"name": "data",
                          "persistentVolumeClaim": {"claimName": f"{obj}-data"}},
                     ],
                 },
             },
         }},
    ]


def _ready(dep: dict[str, Any]) -> bool:
    status, gen = dep.get("status", {}), dep["metadata"].get("generation", 0)
    return (
        status.get("observedGeneration", 0) >= gen
        and status.get("updatedReplicas", 0) == 1
        and status.get("readyReplicas", 0) == 1
        and status.get("replicas", 0) == 1
    )


async def apply(service_dir: str, port: int) -> tuple[bool, str]:
    """Apply the backend's objects and wait for its pod to be ready — the
    counterpart of `docker compose up -d`, which also returns once it runs."""
    ns, dep = namespace(), deployment_name(service_dir)
    logs: list[str] = []
    try:
        objects = manifests(service_dir, port)
        async with _client() as k8s:
            for m in objects:
                r = await k8s.patch(
                    _path(m["kind"], ns, m["metadata"]["name"]),
                    params={"fieldManager": _MANAGER, "force": "true"},
                    headers={"Content-Type": "application/apply-patch+yaml"},
                    content=json.dumps(m),
                )
                r.raise_for_status()
                logs.append(f"applied {m['kind']}/{m['metadata']['name']}")
            deadline = time.monotonic() + READY_TIMEOUT_S
            while time.monotonic() < deadline:
                r = await k8s.get(_path("Deployment", ns, dep))
                r.raise_for_status()
                if _ready(r.json()):
                    logs.append(f"deployment/{dep} ready")
                    return True, "\n".join(logs)
                await asyncio.sleep(2)
        logs.append(f"deployment/{dep} not ready after {READY_TIMEOUT_S:.0f}s")
    except (httpx.HTTPError, OSError, KeyError) as exc:
        logs.append(f"kubernetes: {exc}")
    return False, "\n".join(logs)


async def _delete(k8s: httpx.AsyncClient, ns: str, dep: str, obj: str) -> list[str]:
    failed = []
    for kind, obj_name in (
        ("Deployment", dep), ("Service", obj), ("ConfigMap", f"{obj}-code"),
        ("Secret", f"{obj}-env"), ("PersistentVolumeClaim", f"{obj}-data"),
    ):
        r = await k8s.delete(_path(kind, ns, obj_name))
        if r.status_code not in (200, 202, 404):
            failed.append(f"{kind}/{obj_name}: {r.status_code} {r.text}")
    return failed


async def teardown(service_dir: str, port: int) -> tuple[bool, str]:
    """Remove the backend's objects, its volume included — `docker compose down`
    drops the container's events.db the same way. The repo stays on disk."""
    try:
        async with _client() as k8s:
            failed = await _delete(k8s, namespace(), deployment_name(service_dir), name(port))
    except (httpx.HTTPError, OSError, KeyError) as exc:
        return False, f"kubernetes: {exc}"
    return not failed, "\n".join(failed)


async def _deployments(k8s: httpx.AsyncClient, ns: str) -> list[dict[str, Any]]:
    r = await k8s.get(_path("Deployment", ns), params={"labelSelector": _SELECTOR})
    r.raise_for_status()
    return r.json()["items"]


async def running_names() -> set[str] | None:
    """Ready backends, named the way docker names their containers
    (`turncall-agent-<slug>-…`), so the registry's live-status check reads both
    runtimes alike. None when the API server cannot be asked."""
    try:
        async with _client() as k8s:
            items = await _deployments(k8s, namespace())
    except (httpx.HTTPError, OSError, KeyError):
        return None
    return {
        f"turncall-agent-{d['metadata'].get('annotations', {}).get(_SLUG, '')}-k8s"
        for d in items
        if d.get("status", {}).get("readyReplicas", 0) >= 1
    }


async def reap_orphans(live_slugs: set[str]) -> list[str]:
    """Delete backends whose registry row is gone. Best-effort, never raises."""
    removed: list[str] = []
    try:
        async with _client() as k8s:
            ns = namespace()
            for d in await _deployments(k8s, ns):
                slug = d["metadata"].get("annotations", {}).get(_SLUG, "")
                if slug in live_slugs:
                    continue
                obj = d["metadata"]["labels"]["app.kubernetes.io/instance"]
                if not await _delete(k8s, ns, d["metadata"]["name"], obj):
                    removed.append(d["metadata"]["name"])
    except (httpx.HTTPError, OSError, KeyError):
        pass
    return removed
