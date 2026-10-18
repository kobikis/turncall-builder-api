"""Env-backed settings (single-tenant, ADR-0002)."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from functools import lru_cache

logger = logging.getLogger("turncall_builder")


@dataclass(frozen=True)
class Settings:
    turncall_api_key: str
    turncall_base_url: str
    public_base_url: str
    # TurnCall's public (Twilio-reachable) base URL — for showing/verifying the
    # Twilio voice webhook in the console (should match TurnCall's PUBLIC_BASE_URL).
    turncall_public_url: str
    # Google OIDC (#32). Empty client_id disables the Google login routes.
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str
    # Signs the short-lived OAuth state/nonce cookie (Starlette SessionMiddleware),
    # NOT the login session (that's the opaque DB token). Random per-process default
    # is fine — it only guards the ~30s auth handshake.
    # ponytail: per-process default breaks multi-worker mid-handshake; set the env
    # var if you run >1 worker.
    session_secret: str
    # The platform credential the builder presents as X-Platform-Key when it
    # provisions TurnCall projects/keys (turncall#102). Must equal TurnCall's own
    # PLATFORM_API_KEY. Empty = don't send the header (works against an un-gated
    # TurnCall); once TurnCall is gated, a missing/mismatched value fails the
    # bootstrap with an actionable error (see turncall_client).
    platform_api_key: str
    # Pipedream Connect (ADR-0018). Empty client secret disables integrations —
    # the endpoints then 503 with what to set rather than failing at Pipedream.
    # The secret is stored encrypted (pipedream_credentials); env is what
    # configures it, and a restart is what applies a change.
    pipedream_client_id: str
    pipedream_client_secret: str
    pipedream_project_id: str
    # Where Pipedream sends the admin back — this API's own public
    # /integrations/callback, not the console's URL (PUBLIC_BASE_URL is the
    # console). Same shape and the same reason as google_redirect_uri.
    pipedream_redirect_uri: str
    # development is free and is where this stays until launch (ADR-0018).
    pipedream_environment: str
    # How long the audit log of people keeps an entry (ADR-0028); None = for
    # good. The deployment's setting, not a Workspace's: an admin must not be
    # able to shorten the trail that records what they did.
    audit_log_retention_days: int | None = None
    # The overall deadline of a Telnyx numbers check (#117, #118): every
    # request and wait it makes, across every page. See telnyx_api.
    telnyx_connect_check_deadline_s: float = 20.0
    telnyx_release_check_deadline_s: float = 10.0


def parse_audit_retention(raw: str | None) -> int | None:
    """AUDIT_LOG_RETENTION_DAYS: 1 to 36500 days; unset or empty keeps entries
    for good. Anything else refuses to start, rather than silently keeping too
    much or deleting too soon."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        days = int(raw)
    except ValueError:
        days = 0
    if not 1 <= days <= 36500:
        raise ValueError(
            f"AUDIT_LOG_RETENTION_DAYS must be a whole number of days from 1 to 36500, got {raw!r}"
        )
    return days


def parse_deadline(name: str, raw: str | None, default: float) -> float:
    """A check's deadline in seconds, from 1 to 600; unset or empty is
    `default`. Anything else refuses to start, rather than a check that gives
    up at once or never does."""
    raw = (raw or "").strip()
    if not raw:
        return default
    try:
        seconds = float(raw)
    except ValueError:
        seconds = 0.0
    if not 1 <= seconds <= 600:
        raise ValueError(f"{name} must be a number of seconds from 1 to 600, got {raw!r}")
    return seconds


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    # Cached so every caller shares one object — the random session_secret default
    # stays stable within the process instead of differing per call site.
    session_secret = os.environ.get("BUILDER_SESSION_SECRET")
    if not session_secret:
        # Fails closed under multiple workers (state mismatch → auth denied), but
        # silently — warn so it's diagnosable in prod.
        logger.warning(
            "BUILDER_SESSION_SECRET unset — using a random per-process key; "
            "Google login breaks across multiple workers. Set it to run >1 worker."
        )
    return Settings(
        turncall_api_key=os.environ.get("TURNCALL_API_KEY", ""),
        turncall_base_url=os.environ.get(
            "TURNCALL_BASE_URL", "https://api.turncall.com"
        ).rstrip("/"),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
        turncall_public_url=os.environ.get("TURNCALL_PUBLIC_URL", "").rstrip("/"),
        google_client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
        google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        google_redirect_uri=os.environ.get("GOOGLE_REDIRECT_URI", ""),
        session_secret=session_secret or secrets.token_urlsafe(32),
        platform_api_key=os.environ.get("PLATFORM_API_KEY", ""),
        pipedream_client_id=os.environ.get("PIPEDREAM_CLIENT_ID", ""),
        pipedream_client_secret=os.environ.get("PIPEDREAM_CLIENT_SECRET", ""),
        pipedream_project_id=os.environ.get("PIPEDREAM_PROJECT_ID", ""),
        pipedream_redirect_uri=os.environ.get("PIPEDREAM_REDIRECT_URI", ""),
        # Anything else would go straight into X-PD-Environment and be rejected
        # by Pipedream with nothing pointing back at this variable.
        pipedream_environment=(
            "production"
            if os.environ.get("PIPEDREAM_ENVIRONMENT", "").strip() == "production"
            else "development"
        ),
        audit_log_retention_days=parse_audit_retention(
            os.environ.get("AUDIT_LOG_RETENTION_DAYS")
        ),
        telnyx_connect_check_deadline_s=parse_deadline(
            "TELNYX_CONNECT_CHECK_DEADLINE_S",
            os.environ.get("TELNYX_CONNECT_CHECK_DEADLINE_S"),
            Settings.telnyx_connect_check_deadline_s,
        ),
        telnyx_release_check_deadline_s=parse_deadline(
            "TELNYX_RELEASE_CHECK_DEADLINE_S",
            os.environ.get("TELNYX_RELEASE_CHECK_DEADLINE_S"),
            Settings.telnyx_release_check_deadline_s,
        ),
    )
