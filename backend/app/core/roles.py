"""
System roles and the per-endpoint permission gate.

Four roles exist, in descending order of privilege:
  - ADMINISTRATOR – unrestricted; the only role that can reach Settings'
    General/Access/SSO/AI/Widgets tabs and the audit-log page.
  - DATA_ADMIN – dashboards, data sources, and DB SQL queries; nothing
    else in Settings.
  - VIEWER – read-only access to dashboards; no Settings, no audit log.
  - DENY – no API access at all beyond ``/auth/*`` and ``/healthz``. This
    is also the role assigned to any OIDC user whose claims don't match a
    configured mapping (see ``app/core/general_settings.py``).

Apply ``require_role(...)`` as an additional route-level dependency
alongside the router-level ``require_authentication`` (see ``app/app.py``
and ``app/core/auth.py``) wherever an endpoint needs more than Viewer.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import Depends, HTTPException, status

from app.core.auth import Principal, Role, require_authentication
from app.core.oidc import resolve_claim

# Any authenticated, non-Deny role — the floor for "can use the app at all".
ANY_AUTHENTICATED = (Role.VIEWER, Role.DATA_ADMIN, Role.ADMINISTRATOR)
DATA_ADMIN_OR_ABOVE = (Role.DATA_ADMIN, Role.ADMINISTRATOR)
ADMIN_ONLY = (Role.ADMINISTRATOR,)


def resolve_role_from_claims(claims: Dict[str, Any], mappings: List[Dict[str, Any]]) -> Role:
    """Resolve the system role for a set of OIDC claims via the configured mappings.

    Evaluated in order; the first mapping whose claim value matches wins.
    A claim value that resolves to a list (e.g. Keycloak's ``groups`` or
    ``realm_access.roles``) matches if the configured value is a member of
    it; a scalar claim matches on exact equality. No match at all — or an
    empty mapping list — means Deny, per spec (there is no configurable
    default role for unmapped users).

    Args:
        claims: Merged ID-token + userinfo claims from a completed login.
        mappings: ``general_settings.role_mappings`` — list of
            ``{"claim": ..., "value": ..., "role": ...}``.

    Returns:
        The resolved Role, or Role.DENY if nothing matched.
    """
    for mapping in mappings:
        claim_path = mapping.get("claim")
        expected = mapping.get("value")
        if not claim_path or expected is None:
            continue
        actual = resolve_claim(claims, claim_path)
        matched = actual == expected if not isinstance(actual, list) else expected in actual
        if matched:
            try:
                return Role(mapping.get("role"))
            except ValueError:
                continue
    return Role.DENY


def require_role(*allowed: Role):
    """Build a FastAPI dependency that 403s unless the caller has one of ``allowed``.

    Args:
        *allowed: Roles permitted to call the decorated endpoint.

    Returns:
        An async dependency yielding the validated ``Principal``, for
        handlers that also want the caller's identity.
    """

    async def _dependency(
        principal: Principal = Depends(require_authentication),
    ) -> Principal:
        if principal.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{principal.role.value}' is not permitted to perform this action.",
            )
        return principal

    return _dependency
