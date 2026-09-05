# Pushing an Agent Backend to the user's GitHub

An [[Agent Backend]] is generated code the user cannot currently take with them:
it lives in the builder's Docker environment and is only reachable through the
console. This records how it gets to their GitHub, and why each choice was made.

**Status: accepted, shipping in stages.** The scaffold changes are in. The App
integration is not built — it cannot be verified without a registered GitHub
App, which only the operator can create.

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

## A GitHub App, not a token

A personal access token is the fast path and puts a long-lived credential to the
user's repositories in builder-api's database. A GitHub App issues short-lived
installation tokens, scoped to the repositories the user selects, revocable from
GitHub's side without touching this system.

This is the same decision as ADR-0016 in the engine, where per-agent static AWS
keys were gated off by default in favour of an assumed role. The reasoning
transfers exactly: prefer the credential that expires and that the user can
revoke without asking us.

The cost is real — app registration, a private key to hold, installation
callbacks — and it is why this ships after the scaffold changes rather than with
them.

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
- **A new secret to hold.** The GitHub App's private key becomes an operator
  credential in builder-api's environment, alongside the provider keys.
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

Accepted. `.env.example` and the README shipped; the App integration is blocked
on a registered GitHub App.
