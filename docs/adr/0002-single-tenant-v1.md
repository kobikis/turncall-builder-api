# Single-tenant V1, TurnCall key in env

> **Superseded by ADR-0011** — the builder is now multi-tenant (Workspaces, Users,
> RBAC, login). Kept for history; the reasoning below is why V1 deferred tenancy.


V1 is a single-tenant tool: one TurnCall project, its API key (`tc_…`) in the
backend `.env`, no login on the Composer app, and DB rows (sessions, events)
not scoped to any user.

Chosen over building accounts/multi-tenancy now because multi-tenancy is a large
tax — app auth, per-user encrypted TurnCall keys, row scoping, RBAC — on top of
the thing actually being validated: the prompt→agent loop. Reversible but with
schema impact, so it's recorded. Add tenancy in V2 if this becomes a product;
the loop is what V1 proves.
