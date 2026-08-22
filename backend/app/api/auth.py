"""
API router: /auth  –  OIDC login/logout and the current-caller identity.

Endpoints
---------
GET  /auth/login     – start the OIDC Authorization Code + PKCE flow
GET  /auth/callback  – provider redirect target; completes the exchange
GET  /auth/logout    – destroy the session, optionally via provider RP-logout
GET  /auth/me        – the caller's resolved identity (sidebar/route guard)

Deliberately mounted with no ``require_authentication`` dependency (see
``app/app.py``, matching the health router's pattern) so:
  - An admin can test a real login round trip from the SSO tab before
    flipping "Allow Anonymous Access" off.
  - The frontend can always call ``/auth/me`` to decide whether to show
    the app shell or redirect to ``/auth/login``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from app.core import oidc
from app.core.auth import SESSION_COOKIE_NAME, require_authentication
from app.core.general_settings import general_settings
from app.core.logging import get_logger
from app.core.roles import resolve_role_from_claims
from app.core.session_store import SESSION_TTL_SECONDS, session_store
from app.models.schemas import AuthMeResponse

router = APIRouter(prefix="/auth", tags=["auth"])
_logger = get_logger("buchimaker.api.auth")


def _safe_next_path(next_path: str | None) -> str:
    """Only ever redirect back to a same-site relative path (open-redirect guard)."""
    if not next_path or not next_path.startswith("/") or next_path.startswith("//"):
        return "/"
    return next_path


def _cookie_kwargs(request: Request) -> dict:
    return dict(
        httponly=True,
        samesite="lax",
        secure=(request.url.scheme == "https"),
        path="/",
    )


@router.get(
    "/login",
    summary="Start OIDC login",
    description="Redirects to the configured identity provider's authorization endpoint.",
    responses={302: {"description": "Redirect to the identity provider."}},
)
async def login(request: Request, next: str | None = Query(default="/")):
    cfg = oidc.config_from_settings(general_settings.sso)
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SSO is not fully configured. Set issuer, client ID, secret, and redirect URI in Settings > SSO.",
        )
    try:
        url = await oidc.build_authorize_url(cfg, _safe_next_path(next))
    except oidc.OIDCError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


@router.get(
    "/callback",
    summary="OIDC provider callback",
    description="Completes the token exchange, resolves the caller's role, and creates a session.",
    responses={302: {"description": "Redirect back into the app."}},
)
async def callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
):
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Identity provider returned an error: {error} ({error_description or 'no description'}).",
        )
    if not code or not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing code or state.")

    cfg = oidc.config_from_settings(general_settings.sso)
    if cfg is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SSO is not fully configured.")

    try:
        claims = await oidc.exchange_code(cfg, code, state)
    except oidc.OIDCError as exc:
        _logger.warning("oidc_callback_failed", error=str(exc))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    role = resolve_role_from_claims(claims, general_settings.role_mappings)
    subject = claims.get("sub", "unknown")
    name = claims.get("name") or claims.get("preferred_username") or claims.get("email") or subject
    email = claims.get("email")

    session_id = session_store.create_session(subject=subject, email=email, name=name, role=role.value)
    if session_id is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Session store unavailable.")

    next_path = _safe_next_path(claims.get("_next_path"))
    response = RedirectResponse(next_path, status_code=status.HTTP_302_FOUND)
    response.set_cookie(SESSION_COOKIE_NAME, session_id, max_age=SESSION_TTL_SECONDS, **_cookie_kwargs(request))
    _logger.info("oidc_login_succeeded", subject=subject, role=role.value)
    return response


@router.get(
    "/logout",
    summary="Log out",
    description="Destroys the local session and, if supported, redirects through the provider's RP-initiated logout.",
    responses={302: {"description": "Redirect back into the app (or via the provider's logout endpoint)."}},
)
async def logout(request: Request):
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        session_store.destroy_session(session_id)

    redirect_url = "/"
    cfg = oidc.config_from_settings(general_settings.sso)
    if cfg is not None:
        try:
            doc = await oidc.discover(cfg.issuer_url)
            end_session = oidc.end_session_url(doc, cfg.client_id, str(request.base_url))
            if end_session:
                redirect_url = end_session
        except oidc.OIDCError:
            pass  # fall back to a local redirect — logout must never get stuck

    response = RedirectResponse(redirect_url, status_code=status.HTTP_302_FOUND)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@router.get(
    "/me",
    response_model=AuthMeResponse,
    summary="Get the current caller's identity",
    description="Polled by the frontend at startup to decide what to render and to drive the sidebar.",
)
async def me(request: Request):
    principal = await require_authentication(request)
    sso = general_settings.sso
    sso_configured = oidc.config_from_settings(sso) is not None
    return AuthMeResponse(
        is_anonymous=principal.is_anonymous,
        name=principal.name,
        email=principal.email,
        role=principal.role.value,
        sso_configured=sso_configured,
    )
