# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

**Layout: single-context.** One `CONTEXT.md` at the repo root, and ADRs in `docs/adr/`.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root — the project's glossary / ubiquitous language.
- **`docs/adr/`** — read the ADRs that touch the area you're about to work in (e.g. `docs/adr/0017-*`, `docs/adr/0018-*`). These record past architectural decisions and their trade-offs.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## File structure

```
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-llm-driven-composer.md
│   ├── 0002-single-tenant-v1.md
│   └── …
└── app/
```

Note: the sibling `turncall` engine repo puts its ADRs in `adr/` at the root, not `docs/adr/`. When working across both, use each repo's own path.

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal — either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0005 (builder is control-plane only) — but worth reopening because…_
