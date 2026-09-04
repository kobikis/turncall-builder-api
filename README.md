<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo/wordmark-dark.svg">
    <img src="docs/logo/wordmark-light.svg" alt="TurnCall" width="260">
  </picture>

  <h1>Builder · API</h1>

  <h3>Describe a voice agent in plain English. Get a working one.</h3>

  <p>
    The backend that turns a conversation into a deployed TurnCall agent.<br>
    It asks follow-up questions until the design is unambiguous, then builds it.
  </p>

  <p>
    <a href="LICENSE.md"><img src="https://img.shields.io/badge/license-FSL--1.1--ALv2-orange?style=flat" alt="FSL-1.1-ALv2"></a>
    <a href="https://github.com/kobikis/turncall-builder-api/actions/workflows/ci.yml"><img src="https://github.com/kobikis/turncall-builder-api/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
    <a href="#"><img src="https://img.shields.io/badge/python-3.12+-blue?style=flat&logo=python&logoColor=white" alt="Python 3.12+"></a>
    <a href="#"><img src="https://img.shields.io/badge/FastAPI-009688?style=flat&logo=fastapi&logoColor=white" alt="FastAPI"></a>
    <a href="https://github.com/kobikis/turncall"><img src="https://img.shields.io/badge/engine-TurnCall_(MIT)-green?style=flat" alt="TurnCall engine"></a>
    <a href="https://docs.turncall.io"><img src="https://img.shields.io/badge/docs-docs.turncall.io-0B7285?style=flat&logo=readthedocs&logoColor=white" alt="Docs"></a>
  </p>

  <p>
    <a href="#quickstart">Quickstart</a> ·
    <a href="ROADMAP.md">Roadmap</a> ·
    <a href="CONTEXT.md">Glossary</a> ·
    <a href="docs/adr/">Decisions</a> ·
    <a href="https://docs.turncall.io">Engine docs</a> ·
    <a href="CONTRIBUTING.md">Contributing</a> ·
    <a href="#license">License</a>
  </p>
</div>

---

## How it works

```
You: "a receptionist for my dental clinic that can book appointments"
     ↓
Builder asks: opening hours? what to do out of hours? transfer to a human when?
     ↓
Generates a TurnCall agent config — editable before you commit to it
     ↓
Creates it in TurnCall, binds a number, streams the events its calls produce
```

Frontend lives in
[`turncall-builder-web`](https://github.com/kobikis/turncall-builder-web).
The voice runtime it targets is the MIT-licensed
[TurnCall engine](https://github.com/kobikis/turncall) — its docs live at
[docs.turncall.io](https://docs.turncall.io).

## Quickstart

Everything runs in Docker — no host Python. The builder reuses TurnCall's
Postgres, so start TurnCall first.

**Against TurnCall's local-storage stack** (`turncall-local-*`, from `make docker-up-local` in the `turncall/` repo):

```bash
make docker-up-local     # create builder db + build/start API + migrate
make docker-down-local   # stop the builder API
```

**Against TurnCall's default stack** (`localstack-*`):

```bash
make docker-up           # same, targeting the localstack-* containers
make docker-down
```

Then: `make docker-logs` to tail, `make test` to run tests (also in Docker).
The API listens on `127.0.0.1:8000` (loopback only — see ADR-0002).

**Skip the login (local dev):** `make seed-guest` creates a `guest` / `guest`
account (admin of its own workspace, so it can create agents right away). Log in
with those at http://localhost:5173. Override with `GUEST_EMAIL` / `GUEST_PASSWORD`.
Refuses to run when `TURNCALL_ENV=production`.

> After a TurnCall reset, re-provision the platform key + DBs with `make turncall-setup`.

## Contributing

Bug fixes and docs: open a PR. Features: open an issue first.

Sign your commits (`git commit -s`) — CI enforces it. **No CLA, no forms.**
Contributing here also grants the right to relicense your work under commercial
terms, which is what lets it be included in this licence's Apache 2.0
conversion. See [CONTRIBUTING.md](CONTRIBUTING.md); the MIT
[engine](https://github.com/kobikis/turncall) carries no such grant.

Security issues: see [SECURITY.md](SECURITY.md), not the public issue tracker.

## License

**FSL-1.1-ALv2** — see [LICENSE.md](LICENSE.md). Use, modify and redistribute
for any purpose other than competing with us; each release converts to Apache
2.0 two years after it ships.

TurnCall is open core: this builder is source-available, while the
[TurnCall engine](https://github.com/kobikis/turncall) it drives is MIT. The
reasoning is recorded in [adr/0015](https://github.com/kobikis/turncall/blob/master/adr/0015-open-core-licensing.md).
