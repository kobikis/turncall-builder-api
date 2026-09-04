# Security Policy

## Reporting a vulnerability

**Please do not open a public issue.** Report privately through GitHub's
private vulnerability reporting on this repository ("Security" tab → "Report a
vulnerability"). The report is visible only to maintainers.

## What to expect

- **Acknowledgement**: within 7 days.
- **Assessment**: within 30 days.
- **Disclosure**: we aim to fix and publish an advisory within **90 days**.
- **No bug bounty.** We'll credit you in the advisory unless you'd prefer not.

## Supported versions

Only the latest release receives security fixes.

## Scope

This is the TurnCall agent builder. It holds workspace membership, Google OAuth
identities, and TurnCall API keys on behalf of its users, so we're particularly
interested in: cross-workspace data access, authentication or session bypass,
privilege escalation within a workspace, and anything that leaks a stored
TurnCall API key.

Vulnerabilities in the TurnCall engine itself belong in that repository's
[security policy](https://github.com/kobikis/turncall/security/policy).
