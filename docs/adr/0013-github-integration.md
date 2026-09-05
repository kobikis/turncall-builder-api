# Pushing an Agent Backend to the user's GitHub

An [[Agent Backend]] is generated code the user cannot currently take with them:
it lives in the builder's Docker environment and is only reachable through the
console. This records how it gets to their GitHub, and why each choice was made.

**Status: accepted, shipping in stages.** The scaffold changes are in. The
token-based connection is designed and not yet built; it needs no operator
setup, so it is no longer blocked on anything.

## The point is ownership

Three motivations were on the table: *ownership* (take the code and run it on
your own infrastructure), *visibility* (read and version it while the builder
keeps running it), and *collaboration* (engineers edit it upstream and those
edits matter).

Ownership is the one worth building for. The other two are side effects of it,
and it is the only one that answers the actual complaint: the generated backend
is a black box running on someone else's machine.

That choice has a consequence the others don't: **a clone has to actually run.**
Hence `.env.example` and the run-it-yourself README, shipped ahead of any
GitHub work because they are worth having regardless.

## Most of the hard part already exists

`generator.py` already treats every Agent Backend as a git repo. It runs
`git init` on first materialize, commits `builder: generate` on every
regeneration, and commits `user edits before regen` *before* overwriting — so
"nothing in a generated repo is ever lost, only layered in `git log`".

Pushing is therefore `git remote add` plus `git push`, and GitHub receives a
faithful history that already distinguishes generated commits from the user's
own edits. The expensive part of this feature was built long before anyone asked
for it.

## A fine-grained token first; a GitHub App later if it earns it

An earlier draft chose a GitHub App, reasoning by analogy with ADR-0016 in the
engine, where per-agent static AWS keys were gated off in favour of an assumed
role. **That analogy does not transfer.** There, the better credential was just
a different field — no ceremony. Here the App costs an app registration, a
private key for the operator to hold, a callback endpoint and an installation
flow, and it blocks the entire feature on a manual step only the operator can
perform. A token ships today.

**Fine-grained** personal access tokens also close most of the gap that
argument rested on. They are scoped to selected repositories, can be limited to
`contents: write`, and carry a mandatory expiry. The objection was really to
classic tokens — all repositories, no expiry — which is not what this uses.

So: a user pastes a fine-grained token scoped to the repositories they intend to
push to. Three conditions make that defensible rather than merely convenient.

### 1. It is encrypted at rest

builder-api hashes passwords with argon2id and, today, **stores no third-party
secret at all**. A token is the first credential it must be able to read back —
you cannot hash something you need to use. It is therefore a new storage class,
not an incremental change.

It is encrypted with a key from the environment, in the same spirit as the
engine's `API_KEY_HASH_SECRET`: a database dump on its own is useless without
the process's key. Plaintext would mean one dump yields write access to every
connected user's repositories.

### 2. Its scope is checked, not assumed

On connect, verify the token actually has `contents: write` on the repository
being linked, and reject it with a clear message otherwise. A token that looks
connected and fails on first push is the silent-success failure this codebase
keeps getting bitten by.

### 3. Expiry is surfaced before it bites

This is the real cost of choosing a token, and it is a UX obligation rather than
a footnote. Fine-grained tokens expire on a schedule the user picked, and an
expired one means pushes start failing quietly. The connection stores the expiry
and the console warns ahead of it. A GitHub App would not have this problem.

### Migration

Nothing about [[Linked repo]] depends on which credential pushed. Swapping in an
App later changes how a token is obtained and nothing else — so this is a "for
now" that does not have to be undone, only added to.

## Push-only, and never force

Options were a one-time eject, ongoing pushes, or two-way sync.

**Ongoing, push-only.** It is nearly free given the local history, and it keeps
the pushed repo current as tools change and the backend regenerates.

**The local repo is the source of truth, and a push must never force.** If
someone edits on GitHub, the local repo has no idea; a force push would silently
destroy their fix, which is the worst outcome available here. A divergent push
fails, surfaces the error in the console with a link to the repo, and offers
nothing destructive. Two-way sync is a real feature and a separate decision —
worth making only once we know whether anyone actually edits upstream.

## Per-user connections, with an owner per repo

A workspace-level connection was proposed first, on the grounds that a per-user
one breaks when that person leaves. That objection does not survive contact
with the failure mode: if the connecting user goes, the local repo keeps
generating and the GitHub repo keeps existing under its owner's account. Only
the *sync* stops, and another user can relink. An inconvenience, not a loss.

Per-user is also the more honest model. A GitHub App installation is authorized
by a person against an account they control; a "workspace connection" would in
practice have been the first admin's connection with the attribution hidden.
Commits pushed under a user's own installation are attributed to them on
GitHub, which is better provenance than a shared machine identity.

So: each [[User]] connects their own GitHub, and each [[Linked repo]] records
which user's connection owns it — the one that first linked it, and the one
whose token subsequent pushes use.

**When that connection goes away** — disconnected, revoked, or the user
removed from the workspace — pushes for repos it owns fail with a distinct
state: *the connection that owns this repo is gone*. Any other connected user
can take it over. Nothing is deleted and nothing is force-pushed.

**Open: whether an editor may push to a personal account.** The generated
backend is the business's tool logic, and a per-user connection means anyone who
can push could send it to a GitHub account only they control. That may be
exactly right (it is their code) or may want restricting to organisations the
workspace recognises. Not decided; worth deciding before a workspace has members
who are not its owner.

## Smaller decisions, taken rather than debated

- **Deleting an agent leaves its GitHub repo alone.** Deleting a user's
  repository is irreversible and not something this product should do. The
  [[Linked repo]] is simply forgotten.

## The user picks the destination; we never create it

An earlier draft had the first push *create* `turncall-agent-<slug>` under the
user's account, with an explicit button as the mitigation for a product
reaching into someone's GitHub uninvited. Mintlify's Git settings solve this
better by not having the problem: the user chooses an **organisation**, an
existing **repository**, a **branch**, and an optional **path within the repo**.

That is the model here too, and it is strictly better:

- Nothing is created in anyone's account. The trust concern evaporates instead
  of being mitigated.
- The user keeps naming, visibility, branch protection and whatever CI the repo
  already has.
- **The path field is the important one.** It replaces an assumption made
  earlier — one repo per Agent Backend — with the user's choice. A workspace
  with twenty agents can put them all in one repo under `agents/<slug>/`, or use
  a repo each. Mintlify needs this less than we do: a docs deployment is one
  site, while a workspace is many agents.
- **A branch is a field, not an assumption.** It also enables a better default
  than committing to trunk: push to a branch and let the user open a pull
  request against their own main.

Two concerns stay separate, as they do in Mintlify's UI. *Which organisations
has the App been installed on, and what can it see* is a property of the
[[GitHub connection]]. *Which org, repo, branch and path does this agent push
to* is a property of the [[Linked repo]]. Conflating them was a mistake in the
first draft — the first is set up once, the second is set per agent.

## Consequences

- **`.env` never leaves.** It is gitignored and stays that way; `.env.example`
  carries the variable names with empty values. A test asserts no secret value
  reaches the committed template, because that file is the one place a
  credential could silently escape.
- **A new secret to hold, and a new kind.** The encryption key becomes an
  operator credential in builder-api's environment alongside the provider keys —
  and rotating it invalidates every stored token, so it is set once and left
  alone. This is also the first user-supplied third-party credential the builder
  persists; the threat model changes from "leaks our keys" to "leaks our users'
  write access to their own repositories".
- **Divergence is visible, not resolved.** The console will show a failed push;
  resolving it is the user's job in their own repo. That is deliberate — the
  alternative is us guessing whose version wins.
- **Vocabulary**: [[GitHub connection]] and [[Linked repo]] in `CONTEXT.md`.
  "Publish" was avoided (it means agent versioning in TurnCall) and so was
  "export" (it implies one-time, and this is continuous).
- **Pushing into a shared repo means the local repo is no longer the whole
  story.** With a path inside a monorepo, a push touches one subtree of a
  repository that has other content and its own history. The push must be a
  commit on top of whatever is there, never anything that rewrites the rest —
  which the never-force rule already covers, but the stakes are higher when the
  repo is not exclusively ours.
- **A [[Linked repo]] outlives the connection that made it.** The repo row keeps
  working as a link and a clone target even when its owning connection is gone;
  only pushing stops.

## Status

Accepted. `.env.example` and the README shipped. The token-based connection is
unblocked — it requires no GitHub App registration and no operator setup, which
is the point of choosing it first.
