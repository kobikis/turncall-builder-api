# Setup: connecting GitHub

How to create the token that lets an [[Agent Backend]] push to your own
repository. Design and reasoning: [ADR-0013](./adr/0013-github-integration.md).

Two things have to exist: an **encryption key** on the server (once, by whoever
runs the builder) and a **token** per user.

## 1. Server: the encryption key

Tokens are stored encrypted, so the builder refuses to store one at all until
this is set — it will not fall back to plaintext.

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put it in `.env`:

```
GITHUB_TOKEN_ENCRYPTION_KEY=<the 44-character key>
```

**Set it once and leave it.** Rotating it invalidates every stored token and
every user has to reconnect. Decryption fails loudly rather than silently
returning junk, so you will know — but everyone is disconnected until they act.

## 2. Each user: a fine-grained token

**Settings → Developer settings → Personal access tokens → Fine-grained tokens
→ Generate new token**

| Field | Value |
|---|---|
| Token name | `turncall-builder` |
| Expiration | 90 days is a reasonable default — the connection records it |
| Resource owner | your account, or the organisation that owns the repos |
| Repository access | **Only select repositories** → the ones agents will push to |

Then **Permissions → Repository permissions → Contents: Read and write**.

That is the only permission needed. `Metadata: Read-only` is added
automatically and is required. Leave everything else at *No access* — the
builder never asks for more, and `assert_writable` checks exactly this when you
link a repo.

Generate it and copy the token. GitHub shows it once.

## 3. Connect it

```bash
curl -X POST http://localhost:8000/github/connection \
  -H 'Content-Type: application/json' \
  --cookie 'tc_builder_session=<your session>' \
  -d '{"token":"github_pat_..."}'
```

The response carries your GitHub login and the token's expiry. It never returns
the token again.

Then link an agent and push:

```bash
# what this token can push to
curl --cookie '...' http://localhost:8000/github/repos

# point an agent's backend at one — path is optional, and is what lets one
# repository hold several agents under agents/<slug>/
curl -X PUT http://localhost:8000/agents/<agent-id>/github \
  -H 'Content-Type: application/json' -H 'X-Workspace-Id: <workspace>' \
  --cookie '...' \
  -d '{"owner":"kobikis","repo":"my-agents","branch":"main","path":"agents/sushi"}'

curl -X POST --cookie '...' -H 'X-Workspace-Id: <workspace>' \
  http://localhost:8000/agents/<agent-id>/github/push
```

## Gotchas

**Organisation repositories may need approval.** If you pick an org as the
resource owner and the token appears to have no access, look at
**Org settings → Third-party Access → Personal access tokens** — the request may
be sitting there pending. Personal accounts have no such step.

**Read access is not enough.** `/github/repos` only lists repositories the token
can *push* to, so a repo missing from that list means the permission is `Read`
rather than `Read and write`, or the org has not approved the token yet.

**Expiry is silent until it is not.** The connection stores the expiry, but
nothing warns you yet — that is the console's job and it is not built. When a
token lapses, pushes start failing with "GitHub rejected this token"; reconnect
with a new one.

**A push that returns 409 is working correctly.** It means the repository has
changes the builder did not make, so nothing was pushed. Reconcile them in your
own repository — the builder will never force past it.
