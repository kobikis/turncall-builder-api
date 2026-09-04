"""Auth endpoints: signup / login / logout / me (ADR-0011).

Identity foundation only — email + password. A successful auth issues a
server-side login session delivered as an httpOnly/Secure/SameSite=Lax cookie.
Google login (#32) and Workspace/RBAC scoping (#31) build on this."""

from __future__ import annotations

from typing import Any

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from .. import auth_store, runtime
from ..settings import load_settings

router = APIRouter(prefix="/auth")

COOKIE_NAME = "tc_builder_session"
# localhost is a secure context, so Secure cookies still reach a dev SPA over http.
_COOKIE_KW = dict(httponly=True, secure=True, samesite="lax", path="/")

# Google OIDC (#32). Registered once if configured; Authlib fetches Google's
# metadata + JWKS on first use and verifies the id_token signature/aud/nonce.
_settings = load_settings()
oauth = OAuth()
if _settings.google_client_id:
    oauth.register(
        name="google",
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_id=_settings.google_client_id,
        client_secret=_settings.google_client_secret,
        client_kwargs={"scope": "openid email"},
    )


async def _exchange(request: Request) -> dict[str, Any]:
    """Exchange the callback code for the verified Google claims. Authlib checks
    the id_token signature (JWKS), audience, and nonce. Isolated as its own
    function so the OIDC provider is mocked here in tests."""
    token = await oauth.google.authorize_access_token(request)
    return token.get("userinfo") or {}


class SignupBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=1024)
    workspace_name: str | None = Field(default=None, max_length=200)


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(auth_store.SESSION_TTL.total_seconds()),
        **_COOKIE_KW,
    )


async def current_user(
    tc_builder_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    """Resolve the current User from the session cookie, or 401. Used as a
    dependency by endpoints that require login."""
    if not tc_builder_session:
        raise HTTPException(status_code=401, detail="not authenticated")
    user = await auth_store.resolve_session(runtime.get_pool(), tc_builder_session)
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


@router.post("/signup")
async def signup(body: SignupBody, response: Response) -> dict[str, Any]:
    if "@" not in body.email:
        raise HTTPException(status_code=422, detail="invalid email")
    pool = runtime.get_pool()
    name = body.workspace_name or f"{body.email.split('@')[0]}'s Workspace"
    try:
        user = await auth_store.signup(
            pool, email=body.email, password=body.password, workspace_name=name
        )
    except auth_store.DuplicateEmail as exc:
        raise HTTPException(status_code=409, detail="email already registered") from exc
    token = await auth_store.create_login_session(pool, user["id"])
    _set_session_cookie(response, token)
    return {"success": True, "data": {"user": {"id": user["id"], "email": user["email"]}}}


@router.post("/login")
async def login(body: LoginBody, response: Response) -> dict[str, Any]:
    pool = runtime.get_pool()
    user = await auth_store.get_user_by_email(pool, body.email)
    # Always run argon2 (against a dummy hash if the email is unknown) so response
    # time doesn't reveal whether the account exists. Same 401 either way.
    pw_hash = (user or {}).get("password_hash") or auth_store._DUMMY_HASH
    verified = auth_store.verify_password(pw_hash, body.password)
    if not user or not user["password_hash"] or not verified:
        raise HTTPException(status_code=401, detail="invalid email or password")
    token = await auth_store.create_login_session(pool, user["id"])
    _set_session_cookie(response, token)
    return {"success": True, "data": {"user": {"id": user["id"], "email": user["email"]}}}


@router.post("/logout")
async def logout(
    response: Response, tc_builder_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    if tc_builder_session:
        await auth_store.delete_session(runtime.get_pool(), tc_builder_session)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"success": True, "data": {}}


@router.get("/me")
async def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return {"success": True, "data": {"user": user}}


@router.get("/google/login")
async def google_login(request: Request) -> Response:
    """Kick off the OIDC authorization-code flow — redirect to Google's consent."""
    if not _settings.google_client_id:
        raise HTTPException(status_code=503, detail="google login not configured")
    return await oauth.google.authorize_redirect(request, _settings.google_redirect_uri)


@router.get("/google/callback")
async def google_callback(request: Request) -> Response:
    """OIDC callback: verify the Google identity, link/create the User by verified
    email, and issue the same login-session cookie as password auth."""
    if not _settings.google_client_id:
        raise HTTPException(status_code=503, detail="google login not configured")
    try:
        userinfo = await _exchange(request)
    except OAuthError as exc:
        raise HTTPException(status_code=401, detail="google authentication failed") from exc
    email = (userinfo.get("email") or "").strip().lower()
    # Only a Google-verified email may claim/create an account — an unverified one
    # could be an address the Google user doesn't actually control.
    if not email or not userinfo.get("email_verified"):
        raise HTTPException(status_code=401, detail="google email not verified")
    pool = runtime.get_pool()
    name = f"{email.split('@')[0]}'s Workspace"
    user = await auth_store.upsert_google_user(pool, email=email, workspace_name=name)
    token = await auth_store.create_login_session(pool, user["id"])
    # Land back on the SPA with the session cookie set (JSON body kept for tests).
    redirect = RedirectResponse(url=_settings.public_base_url or "/", status_code=303)
    _set_session_cookie(redirect, token)
    return redirect
