"""Seed a default guest login for LOCAL DEVELOPMENT.

Creates a User + Workspace + admin Membership (via auth_store.signup) so you can
log into the builder without Google OAuth or signing up. Idempotent. Refuses to
run when TURNCALL_ENV == "production" — a known default password must never land
in a reachable environment.

    make seed-guest        # then log in with the printed credentials
"""

from __future__ import annotations

import asyncio
import os
import sys

from app import auth_store
from app.db import create_pool


async def main() -> None:
    if os.environ.get("TURNCALL_ENV") == "production":
        sys.exit("refusing to seed a default guest account in production")

    email = os.environ.get("GUEST_EMAIL", "guest@turncall.local")
    password = os.environ.get("GUEST_PASSWORD", "guest")

    pool = await create_pool()
    try:
        await auth_store.signup(
            pool, email=email, password=password, workspace_name="Guest Workspace"
        )
    except auth_store.DuplicateEmail:
        print(f"guest already exists: {email} (no-op)")
        return
    finally:
        await pool.close()

    print(f"seeded guest login — {email} / {password}")
    print("log in at http://localhost:5173")


if __name__ == "__main__":
    asyncio.run(main())
