"""
Authentication gate: anonymous access vs. OIDC-backed session cookie.

Controlled by ``general_settings.anonymous_access`` (Settings > Access
tab, "Allow Anonymous Access"):
  - True  — no login required; every request is treated as the anonymous
    Administrator. This is the default for a fresh install (seeded from
    the legacy ``ANONYMOUS_USER`` env var on first run — see
    ``app/core/general_settings.py``).
  - False — every request must carry a valid session cookie, created by
    completing the OIDC Authorization Code + PKCE flow at ``/auth/login``
    (``app/api/auth.py``). The backend performs that exchange itself (the
    "BFF" pattern) via ``app/core/oidc.py`` — the browser never sees a
    token, only the opaque session cookie.

Apply ``require_authentication`` as a FastAPI dependency on routers that
should be gated (see app/app.py); it is not global middleware, so routes
that intentionally stay public (health checks, the /auth/* endpoints
themselves) can omit it. For endpoints that need more than "any logged-in,
non-Deny user", layer ``app.core.roles.require_role(...)`` on top.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request, status

from app.core.general_settings import general_settings
from app.core.session_store import session_store

SESSION_COOKIE_NAME = "buchi_session"


class Role(str, enum.Enum):
    """System roles, ordered from least to most privileged for display."""

    DENY = "Deny"
    VIEWER = "Viewer"
    DATA_ADMIN = "Data Admin"
    ADMINISTRATOR = "Administrator"

    @classmethod
    def _missing_(cls, value: object):
        # Tolerate case/whitespace drift in OIDC role-mapping values and
        # persisted YAML (e.g. "administrator" or " Viewer ").
        if isinstance(value, str):
            normalized = value.strip().lower()
            for member in cls:
                if member.value.lower() == normalized:
                    return member
        return None


@dataclass(frozen=True)
class Principal:
    """The caller's resolved identity for this request."""

    subject: str
    name: str
    email: Optional[str]
    role: Role
    is_anonymous: bool


ANONYMOUS_PRINCIPAL = Principal(
    subject="anonymous", name="Anonymous", email=None, role=Role.ADMINISTRATOR, is_anonymous=True
)


async def require_authentication(request: Request) -> Principal:
    """FastAPI dependency resolving the caller's identity for this request.

    Returns:
        The anonymous Administrator principal when
        ``general_settings.anonymous_access`` is True (unconditionally —
        any existing session cookie is ignored in this mode, per spec: the
        sidebar always shows "Anonymous / Administrator" while it's on).
        Otherwise the session's resolved Principal.

    Raises:
        HTTPException: 401 if no valid session cookie is present.
    """
    if general_settings.anonymous_access:
        request.state.principal = ANONYMOUS_PRINCIPAL
        return ANONYMOUS_PRINCIPAL

    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    session = session_store.get_session(session_id) if session_id else None
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Sign in at /auth/login.",
        )

    try:
        role = Role(session.role)
    except ValueError:
        role = Role.DENY

    principal = Principal(
        subject=session.subject,
        name=session.name,
        email=session.email,
        role=role,
        is_anonymous=False,
    )
    request.state.principal = principal
    return principal
