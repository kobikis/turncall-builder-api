# Agent Backends on Kubernetes

ADR-0004 runs every Agent Backend as its own docker compose, through the host's
Docker socket, and called that acceptable for a local tool and not something to
ship. A Kubernetes node has no Docker socket to mount, so the builder could not
run in a cluster at all.

## Decision

**`BACKEND_RUNTIME=kubernetes` swaps the runner, nothing else** (`app/backends/
kube.py`). The generated repo stays the source of truth on a volume, committed
to git as before; each operation `generator` did with compose is the same
operation on objects in the builder's own namespace:

| compose | Kubernetes |
|---|---|
| `up -d --build` / `up -d` | apply a ConfigMap (`app.py`, `requirements.txt`), a Secret (`.env`), a PVC, a Service and a Deployment, then wait for the pod to be ready |
| `down` | delete them, the volume included |
| `docker ps` | Deployments with a ready replica, reported as docker names containers so the live-status check reads both |
| `host.docker.internal:<port>` | `turncall-agent-<port>.<ns>.svc.cluster.local:8000` |

**No image is built in the cluster.** The scaffold's requirements are fixed
(ADR-0004's hybrid codegen), so every backend runs one prebuilt runtime image
and gets its code from the ConfigMap. Building per agent would need a builder
service and a registry in every install for a dependency list that never
varies. A dependency a user adds to their repo is installed at start, onto the
backend's volume.

**The pod is named for the agent; the address is named for the port.** The
Deployment takes the agent's slug (`hotel-receptionist-12d9bda3`), so `kubectl
get pods` reads as a list of agents. The Service, ConfigMap, Secret and volume
take the registry port (`turncall-agent-<port>`): the Service is the address
TurnCall stores in an agent's tool and call-init URLs, the port is unique and
never changes, and `backend_url(port)` is called with it everywhere.

**The API server is called with httpx and the pod's service account**, using
server-side apply. No client library and no `kubectl` in the image. The Role is
namespaced and covers only the five kinds a backend is made of.

**The console reads a backend through builder-api**
(`/agents/{id}/backend/events|automations`). It used to fetch
`localhost:<port>` directly, which a browser cannot reach inside a cluster; the
relay also puts the backend's unauthenticated reads behind a member's login. It
is the path in both runtimes.

## Consequences

- One builder-api replica: the generated repos are on a ReadWriteOnce volume,
  and builds run as tasks in the process.
- A backend's event feed survives restarts (it is on its volume), where under
  compose it survived only until the container was removed.
- A build reports failed when the pod is not ready within
  `AGENT_READY_TIMEOUT_SECONDS`, as a failed `compose up` did.
- A private runtime image works: the chart's (kobikis/turncall-charts) `imagePullSecrets` reach every
  backend's pod (`AGENT_IMAGE_PULL_SECRETS`).
- Compose remains the default and is unchanged.
