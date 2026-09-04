# Design: Login + RBAC + Multi-tenancy (builder-api)

Turns the single-tenant builder into a multi-tenant product businesses log into.
Full decision record: ADR-0011. Domain terms: `CONTEXT.md` (Workspace, User,
Membership, Role, Login session). This doc is the build surface for `/to-spec`.

## Model (all in builder-api; TurnCall stays identity-free)

- **Workspace** = the tenant (a company). Flat. Owns Agents, Numbers, Sessions.
- **User** logs in (email+password or Google), belongs to 1+ Workspaces.
- **Membership** = (user, workspace, role). RBAC unit.
- **Role**: `admin` / `editor` / `viewer`.
- A TurnCall `project` stays per-agent plumbing; not the tenant.

### Role capabilities
| Action | admin | editor | viewer |
|---|:--:|:--:|:--:|
| Create/edit/delete Agents, Numbers, KB | ✅ | ✅ | ❌ |
| Run test calls (WebRTC) | ✅ | ✅ | ❌ |
| View calls & analytics | ✅ | ✅ | ✅ |
| Manage members & billing | ✅ | ❌ | ❌ |
| Delete Workspace | ✅ | ❌ | ❌ |

## New tables

- `users` — id, email (unique, verified), password_hash (nullable → Google-only),
  google_sub (nullable), created_at.
- `workspaces` — id, name, created_at.
- `memberships` — id, user_id, workspace_id, role; unique(user_id, workspace_id).
- `user_sessions` — token (pk, opaque random), user_id, expires_at, created_at.
- Add `workspace_id` (FK) to `sessions`, `agent_backends`, `phone_numbers`.

## Auth flows

- **Signup** (email+pw or Google): create User + Workspace + admin Membership
  atomically. Password hashed argon2id.
- **Google**: OpenID Connect auth-code (Authlib). Verify ID token → link by
  verified email (same User across methods) or create.
- **Invite**: admin invites email + role → invitee accepts (signup or login) →
  Membership created. Invite-only join; no open self-join.
- **Login** → insert `user_sessions` row → `Set-Cookie` httpOnly/Secure/SameSite=Lax.
- **Logout / kick / re-role** → delete/update rows → effective next request.
- **New Workspace / switcher**: a User can create additional Workspaces and switch.

## Request pipeline

Middleware on every builder-api endpoint:
```
cookie → user_sessions → User
X-Workspace-Id header → Workspace
→ resolve Membership(user, workspace)  (403 if none)
→ inject (user, workspace, role); role gates the action
```
Scope all Workspace-owned queries by the resolved workspace_id.

## builder→TurnCall boundary

- TurnCall project/key creation now requires the **platform key** (only builder-api
  holds it; env `turncall_api_key`). Close the unauthenticated dev path. *(One
  change inside the TurnCall repo.)*
- WebRTC test-call credentials proxied through builder-api: check session +
  Membership (viewer denied), return a **short-lived, single-agent-scoped** TurnCall
  key. Browser never holds the platform key, never persists any TurnCall key.
  TTL mechanism (expiry column vs create-then-revoke vs connect ticket) = spec
  detail.

## Migration (one Alembic revision)

1. Create `users`, `workspaces`, `memberships`, `user_sessions`.
2. Add nullable `workspace_id` to `sessions`, `agent_backends`, `phone_numbers`.
3. Insert "Default" Workspace; backfill all existing rows to it.
4. Set `workspace_id` NOT NULL.
5. Optionally seed admin User (`INITIAL_ADMIN_EMAIL`, no password) + admin Membership
   in Default. Unset (the self-hosted default) seeds none; the first authenticated
   user claims Default.
6. Claim via first "Sign in with Google" as that email.

## Frontend (builder-web)

- Login/signup screen (email+pw + "Sign in with Google").
- Auth gate on the app; unauthenticated → login.
- Workspace switcher + "New Workspace"; send `X-Workspace-Id` on every request.
- Members page (admin): invite, list, change role, remove.
- Hide/disable edit + test-call controls for `viewer`.

## Out of scope (V1)

SSO/SAML, SCIM, per-resource sharing, billing integration, email deliverability
for invites beyond a basic send. Note where deferred; don't build.
