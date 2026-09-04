# Multi-tenant identity in the builder; TurnCall stays identity-free

Supersedes ADR-0002 (single-tenant V1). V1 proved the prompt→agent loop; this
turns the builder into a multi-tenant product businesses log into. The whole
identity model — humans, the company tenant, and RBAC — lives in **builder-api**.
TurnCall gains no concept of a user.

## Where identity lives

TurnCall stays **identity-free**: it knows only projects and machine API keys
(`tc_…`). It has projects + a `ProjectRole` already, so putting users there was the
obvious alternative — rejected so TurnCall can stay a headless engine, deployable
without the builder, with no login surface. The cost: builder-api runs a full
parallel identity system instead of reusing TurnCall's roles, and holds the
TurnCall keys server-side. Accepted.

## Decisions

- **Workspace = the tenant.** A company is one Workspace (flat; no
  Workspace-above-Workspace, matching Vapi/ElevenLabs). It owns the builder's
  Agents, Numbers, and Sessions. A TurnCall `project` stays per-agent plumbing for
  webhook isolation — NOT the tenant.
- **User / Membership / Role.** A User logs in and belongs to one-or-more
  Workspaces via a Membership carrying a Role: `admin` (everything + members,
  billing, delete Workspace), `editor` (create/edit/delete Agents, Numbers, KB; run
  test calls; view calls), `viewer` (read-only; **no test calls**, no edits).
  Enforced in builder-api; unrelated to TurnCall's `ProjectRole`.
- **Signup vs invite.** Signup creates User + Workspace + admin Membership
  atomically. Everyone else is invite-only (admin invites an email, picks a role).
  A User may own/join several Workspaces (workspace switcher + "New Workspace").
- **Login sessions, not JWT.** Opaque token in `user_sessions` (Postgres),
  delivered as an httpOnly/Secure/SameSite=Lax cookie, resolved per request.
  Server-side so it is instantly revocable on logout/kick/re-role — the reason
  stateless JWT was rejected. Named `user_sessions` to avoid colliding with the
  composer `sessions` table.
- **Workspace per request via `X-Workspace-Id` header.** Cookie → User; header →
  Workspace; middleware resolves the Membership and injects `(user, workspace,
  role)`, 403 if none. Chosen over path-nesting (`/workspaces/{id}/…`, bigger
  refactor) and over an active-workspace-on-session (breaks with multiple tabs).
- **Auth mechanics.** Passwords: argon2id (`argon2-cffi`). Google: OpenID Connect
  auth-code via Authlib. Accounts link by **verified email** — one email is one
  User across both methods.

## Securing the builder→TurnCall boundary

- **Platform-key gate.** TurnCall's project/key creation stops being unauthenticated
  — it requires one privileged platform credential that only builder-api holds
  (already `turncall_api_key` in its env). This is the one change that reaches into
  TurnCall; it adds no user model there.
- **Browser never holds a long-lived TurnCall key.** The WebRTC test-call
  credential fetch moves behind builder-api, which checks the Login session +
  Membership (viewer denied) and returns only a **short-lived, single-agent-scoped**
  key for that one peer connection — never persisted, never the platform key.

## Migration

One Alembic migration: create `users`/`workspaces`/`memberships`/`user_sessions`;
add nullable `workspace_id` to `sessions`/`agent_backends`/`phone_numbers`; insert
a "Default" Workspace; backfill all existing rows to it; set `workspace_id` NOT
NULL; optionally seed one admin User (`INITIAL_ADMIN_EMAIL`, no password) with an admin
Membership. First "Sign in with Google" as that email claims the seeded User and
lands in the Default Workspace with all existing data. TurnCall projects/keys are
untouched.
