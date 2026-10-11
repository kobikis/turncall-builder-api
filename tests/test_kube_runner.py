"""The Kubernetes runtime for Agent Backends (ADR-0025), against a fake API server."""

import json

import httpx
import pytest

from app.backends import kube, registry


class FakeApiServer:
    """Records requests; answers applies, reads and deletes like the API server."""

    def __init__(self, ready_after: int = 0, deployments: list | None = None):
        self.requests: list[httpx.Request] = []
        self.ready_after = ready_after
        self.deployments = deployments or []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "PATCH":
            return httpx.Response(200, json=json.loads(request.content))
        if request.method == "DELETE":
            return httpx.Response(404 if path.endswith("-data") else 200, json={})
        if path.endswith("/deployments"):
            return httpx.Response(200, json={"items": self.deployments})
        reads = sum(1 for r in self.requests if r.method == "GET")
        ready = 1 if reads > self.ready_after else 0
        status = {"observedGeneration": 2, "updatedReplicas": 1, "replicas": 1,
                  "readyReplicas": ready}
        return httpx.Response(200, json={"metadata": {"generation": 2}, "status": status})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler), base_url="https://k8s"
        )


@pytest.fixture
def api(monkeypatch):
    server = FakeApiServer()
    monkeypatch.setattr(kube, "_client", server.client)
    monkeypatch.setattr(kube, "namespace", lambda: "builder")
    monkeypatch.setattr(kube.asyncio, "sleep", _no_sleep)
    return server


async def _no_sleep(_):
    return None


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "turncall-agent-hotel-ab12cd34"
    d.mkdir()
    (d / "app.py").write_text("app = 1\n")
    (d / "requirements.txt").write_text("fastapi\n")
    (d / ".env").write_text("# comment\nAGENT_ID=a1\nCALL_INIT_SECRET=s=1\n")
    return d


def _applied(server) -> dict[str, dict]:
    return {
        json.loads(r.content)["kind"]: json.loads(r.content)
        for r in server.requests
        if r.method == "PATCH"
    }


@pytest.mark.asyncio
async def test_apply_creates_the_backend_and_waits_until_ready(api, repo):
    api.ready_after = 2
    ok, logs = await kube.apply(str(repo), 9001)

    assert ok, logs
    objs = _applied(api)
    assert set(objs) == {"ConfigMap", "Secret", "PersistentVolumeClaim", "Service", "Deployment"}
    assert objs["ConfigMap"]["data"] == {"app.py": "app = 1\n", "requirements.txt": "fastapi\n"}
    # .env is the Secret, a value holding '=' intact, comments dropped
    assert objs["Secret"]["stringData"] == {"AGENT_ID": "a1", "CALL_INIT_SECRET": "s=1"}
    assert objs["Deployment"]["metadata"]["annotations"] == {
        "turncall.dev/agent-slug": "hotel-ab12cd34"
    }
    # the pod is named for the agent; the Service keeps the port-based address
    assert objs["Deployment"]["metadata"]["name"] == "hotel-ab12cd34"
    assert objs["Service"]["metadata"]["name"] == "turncall-agent-9001"
    assert any(r.url.path.endswith("/deployments/hotel-ab12cd34") for r in api.requests
               if r.method == "GET")
    patch = next(r for r in api.requests if r.method == "PATCH")
    assert patch.headers["content-type"] == "application/apply-patch+yaml"
    assert patch.url.params["fieldManager"] == "turncall-builder"
    assert patch.url.path == "/api/v1/namespaces/builder/configmaps/turncall-agent-9001-code"


@pytest.mark.asyncio
async def test_a_changed_env_rolls_the_pod(api, repo):
    def checksum():
        dep = kube.manifests(str(repo), 9001)[-1]
        return dep["spec"]["template"]["metadata"]["annotations"]["checksum"]

    before = checksum()
    (repo / ".env").write_text("AGENT_ID=a1\nCALL_INIT_SECRET=rebound\n")
    assert checksum() != before


@pytest.mark.asyncio
async def test_a_pod_that_never_gets_ready_fails_the_build(api, repo, monkeypatch):
    api.ready_after = 10**6
    monkeypatch.setattr(kube, "READY_TIMEOUT_S", 0.05)
    ok, logs = await kube.apply(str(repo), 9001)
    assert not ok
    assert "not ready" in logs


@pytest.mark.asyncio
async def test_an_unreachable_api_server_fails_without_raising(monkeypatch, repo):
    def unreachable(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(kube, "namespace", lambda: "builder")
    monkeypatch.setattr(
        kube, "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(unreachable),
                                  base_url="https://k8s"),
    )
    ok, logs = await kube.apply(str(repo), 9001)
    assert not ok and "refused" in logs
    assert await kube.running_names() is None


@pytest.mark.asyncio
async def test_teardown_deletes_everything_and_tolerates_missing(api):
    ok, _ = await kube.teardown("/generated/turncall-agent-hotel-ab12cd34", 9001)
    assert ok
    deleted = {r.url.path.rsplit("/", 1)[1] for r in api.requests if r.method == "DELETE"}
    assert deleted == {
        "hotel-ab12cd34", "turncall-agent-9001", "turncall-agent-9001-code",
        "turncall-agent-9001-env", "turncall-agent-9001-data",
    }


def _deployment(obj: str, slug: str, ready: int) -> dict:
    return {
        "metadata": {"name": slug, "annotations": {"turncall.dev/agent-slug": slug},
                     "labels": {"app.kubernetes.io/instance": obj}},
        "status": {"readyReplicas": ready},
    }


@pytest.mark.asyncio
async def test_running_names_read_like_docker_container_names(api):
    api.deployments = [
        _deployment("turncall-agent-9001", "hotel-ab12cd34", 1),
        _deployment("turncall-agent-9002", "clinic-cd34ef56", 0),
    ]
    names = await kube.running_names()
    assert names == {"turncall-agent-hotel-ab12cd34-k8s"}
    # the registry's live-status check matches on this prefix
    assert any(n.startswith("turncall-agent-hotel-ab12cd34-") for n in names)


@pytest.mark.asyncio
async def test_reap_removes_only_backends_with_no_registry_row(api):
    api.deployments = [
        _deployment("turncall-agent-9001", "hotel-ab12cd34", 1),
        _deployment("turncall-agent-9002", "gone-cd34ef56", 1),
    ]
    assert await kube.reap_orphans({"hotel-ab12cd34"}) == ["gone-cd34ef56"]
    deleted = {r.url.path.rsplit("/", 1)[1] for r in api.requests if r.method == "DELETE"}
    assert deleted == {
        "gone-cd34ef56", "turncall-agent-9002", "turncall-agent-9002-code",
        "turncall-agent-9002-env", "turncall-agent-9002-data",
    }


def test_backend_url_is_the_service_on_kubernetes(monkeypatch):
    monkeypatch.setattr(kube, "ENABLED", True)
    monkeypatch.setattr(kube, "namespace", lambda: "builder")
    assert (
        registry.backend_url(9001)
        == "http://turncall-agent-9001.builder.svc.cluster.local:8000"
    )
    monkeypatch.setattr(kube, "ENABLED", False)
    assert registry.backend_url(9001) == "http://host.docker.internal:9001"


def test_runtime_image_carries_the_scaffold_requirements():
    from pathlib import Path

    from app.backends import scaffold

    reqs = scaffold.render(slug="x", port=9001, tools=[])["requirements.txt"].split()
    dockerfile = (Path(__file__).parent.parent / "agent-runtime" / "Dockerfile").read_text()
    assert all(f'"{r}"' in dockerfile for r in reqs), reqs


def test_a_long_agent_name_keeps_its_id_within_63_characters():
    long = "/generated/turncall-agent-" + "very-long-hotel-receptionist-name-" * 3 + "ab12cd34"
    name = kube.deployment_name(long)
    assert len(name) <= 63 and name.endswith("-ab12cd34")
    assert kube.deployment_name("/generated/turncall-agent-hotel-ab12cd34") == "hotel-ab12cd34"


def test_a_private_runtime_image_gets_its_pull_secrets(repo, monkeypatch):
    def pod(m):
        return m[-1]["spec"]["template"]["spec"]

    assert pod(kube.manifests(str(repo), 9001))["imagePullSecrets"] == []
    monkeypatch.setattr(kube, "PULL_SECRETS", ["ghcr"])
    assert pod(kube.manifests(str(repo), 9001))["imagePullSecrets"] == [{"name": "ghcr"}]
