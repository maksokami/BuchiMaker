"""
Minimal OIDC Authorization Code + PKCE client, driven by runtime settings.

Deliberately not built on a startup-registered client (e.g. Authlib's
Starlette integration) because the identity-provider connection is
runtime-configurable through the Settings > SSO tab — every call here
re-reads ``general_settings.sso`` and re-resolves the provider's discovery
document, rather than assuming a fixed provider fixed at process start.

Flow, matching ``app.api.auth``:
  1. ``build_authorize_url()`` — PKCE pair + state/nonce, flow state
     stashed in Redis (``app.core.session_store``), browser redirected to
     the provider's authorization endpoint.
  2. ``exchange_code()`` — provider's callback hands back ``code`` +
     ``state``; the flow state is popped (one-shot) and used to complete
     the token exchange and verify the ID token's signature/issuer/
     audience/expiry via the provider's published JWKS.
  3. ``fetch_userinfo()`` — merged into the verified ID-token claims,
     since some IdP mapper configurations (Keycloak group mappers, in
     particular) only populate certain claims in the userinfo response.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

from app.core.logging import get_logger
from app.core.session_store import session_store

_logger = get_logger("buchimaker.oidc")

_DISCOVERY_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}
_DISCOVERY_TTL_SECONDS = 600
_JWK_CLIENTS: Dict[str, PyJWKClient] = {}

_HTTP_TIMEOUT = httpx.Timeout(10.0)


class OIDCError(Exception):
    """Raised for any failure in the discovery/exchange/verification flow."""


@dataclass
class SSOConfig:
    issuer_url: str
    client_id: str
    client_secret: str
    scopes: str
    redirect_uri: str


def config_from_settings(sso: Dict[str, Any]) -> Optional[SSOConfig]:
    """Build an :class:`SSOConfig` from ``general_settings.sso``, or None if incomplete."""
    if not (sso.get("issuer_url") and sso.get("client_id") and sso.get("client_secret") and sso.get("redirect_uri")):
        return None
    return SSOConfig(
        issuer_url=sso["issuer_url"].rstrip("/"),
        client_id=sso["client_id"],
        client_secret=sso["client_secret"],
        scopes=sso.get("scopes") or "openid profile email",
        redirect_uri=sso["redirect_uri"],
    )


async def discover(issuer_url: str) -> Dict[str, Any]:
    """Fetch (and briefly cache) the provider's OpenID Connect discovery document.

    Args:
        issuer_url: Base issuer URL, e.g. ``https://keycloak.example.com/realms/buchimaker``.

    Returns:
        The parsed discovery document.

    Raises:
        OIDCError: If the document can't be fetched or parsed.
    """
    issuer_url = issuer_url.rstrip("/")
    cached = _DISCOVERY_CACHE.get(issuer_url)
    if cached and (time.time() - cached[0]) < _DISCOVERY_TTL_SECONDS:
        return cached[1]

    url = f"{issuer_url}/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            doc = resp.json()
    except Exception as exc:
        raise OIDCError(f"Could not fetch OIDC discovery document from {url}: {exc}") from exc

    for required in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if required not in doc:
            raise OIDCError(f"Discovery document at {url} is missing '{required}'.")

    _DISCOVERY_CACHE[issuer_url] = (time.time(), doc)
    return doc


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii").rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    return verifier, challenge


async def build_authorize_url(cfg: SSOConfig, next_path: str) -> str:
    """Start a login: stash PKCE/nonce flow state and return the redirect URL.

    Args:
        cfg: Resolved SSO connection settings.
        next_path: Frontend path to return to after a successful login.

    Returns:
        The fully-formed authorization-endpoint URL to redirect the browser to.

    Raises:
        OIDCError: If the flow state couldn't be persisted (Redis down) or
            discovery failed.
    """
    doc = await discover(cfg.issuer_url)
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(16)

    if not session_store.create_flow_state(
        state, {"code_verifier": verifier, "nonce": nonce, "next_path": next_path or "/"}
    ):
        raise OIDCError("Could not persist login flow state (session store unavailable).")

    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": cfg.scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{doc['authorization_endpoint']}?{urlencode(params)}"


async def exchange_code(cfg: SSOConfig, code: str, state: str) -> Dict[str, Any]:
    """Complete the callback: exchange ``code`` for tokens and return verified claims.

    Args:
        cfg: Resolved SSO connection settings.
        code: Authorization code from the provider's callback query string.
        state: State from the provider's callback query string.

    Returns:
        Merged ID-token + userinfo claims (userinfo takes precedence for
        overlapping keys, since it's the more authoritative live source).

    Raises:
        OIDCError: On a missing/expired flow state, a token-exchange
            failure, or ID-token verification failure (bad signature,
            issuer, audience, nonce, or expiry).
    """
    flow = session_store.pop_flow_state(state)
    if flow is None:
        raise OIDCError("Login flow expired or was already used. Please sign in again.")

    doc = await discover(cfg.issuer_url)

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            token_resp = await client.post(
                doc["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": cfg.redirect_uri,
                    "client_id": cfg.client_id,
                    "client_secret": cfg.client_secret,
                    "code_verifier": flow["code_verifier"],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            token_resp.raise_for_status()
            tokens = token_resp.json()
    except Exception as exc:
        raise OIDCError(f"Token exchange with the identity provider failed: {exc}") from exc

    id_token = tokens.get("id_token")
    if not id_token:
        raise OIDCError("Identity provider did not return an id_token.")

    jwks_uri = doc["jwks_uri"]
    jwk_client = _JWK_CLIENTS.setdefault(jwks_uri, PyJWKClient(jwks_uri))
    try:
        signing_key = jwk_client.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=cfg.client_id,
            issuer=cfg.issuer_url,
        )
    except Exception as exc:
        raise OIDCError(f"ID token verification failed: {exc}") from exc

    if claims.get("nonce") != flow["nonce"]:
        raise OIDCError("ID token nonce mismatch — possible replay.")

    access_token = tokens.get("access_token")
    if access_token and "userinfo_endpoint" in doc:
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                userinfo_resp = await client.get(
                    doc["userinfo_endpoint"],
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if userinfo_resp.status_code == 200:
                    claims = {**claims, **userinfo_resp.json()}
        except Exception as exc:
            _logger.warning("oidc_userinfo_fetch_failed", error=str(exc))

    claims["_next_path"] = flow.get("next_path", "/")
    return claims


def end_session_url(discovery_doc: Dict[str, Any], client_id: str, post_logout_redirect_uri: str) -> Optional[str]:
    """Return the provider's RP-initiated logout URL, or None if unsupported."""
    endpoint = discovery_doc.get("end_session_endpoint")
    if not endpoint:
        return None
    params = {"client_id": client_id, "post_logout_redirect_uri": post_logout_redirect_uri}
    return f"{endpoint}?{urlencode(params)}"


def resolve_claim(claims: Dict[str, Any], claim_path: str) -> Any:
    """Resolve a (possibly dotted) claim path, e.g. 'realm_access.roles'.

    Args:
        claims: Merged ID-token + userinfo claims.
        claim_path: A top-level claim name, or a dotted path into nested
            objects (e.g. Keycloak's ``realm_access.roles``).

    Returns:
        The resolved value (scalar, list, or None if any segment is missing).
    """
    value: Any = claims
    for segment in claim_path.split("."):
        if isinstance(value, dict):
            value = value.get(segment)
        else:
            return None
    return value
