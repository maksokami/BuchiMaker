"""
Redis-backed SSO session store (the BFF pattern).

The backend performs the OIDC Authorization Code + PKCE exchange with the
identity provider directly (see ``app.core.oidc``) and never hands a token
to the browser. Instead it creates an opaque session record here, keyed by
a random session ID that becomes the value of an httpOnly cookie
(``app.api.auth``). Every subsequent request resolves that cookie back to
this store to recover the caller's identity and role
(``app.core.auth.require_authentication``).

Also hosts the short-lived "flow state" used between ``/auth/login`` and
``/auth/callback`` (PKCE verifier, nonce, and the post-login redirect
path) — one-shot, deleted on first read, so a completed/replayed callback
can't be reused.

Both use the same Redis connection factory as the dashboard-data cache
(``app.core.redis_client.create_redis_connection``) but with
``decode_responses=True``, since session/flow records are JSON text, not
gzipped bytes.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from app.core.logging import get_logger
from app.core.redis_client import create_redis_connection

_logger = get_logger("buchimaker.session_store")

_SESSION_KEY = "sso_session:{session_id}"
_FLOW_KEY = "oidc_flow:{state}"

SESSION_TTL_SECONDS = 8 * 3600  # 8 hours — re-login required after this
FLOW_TTL_SECONDS = 5 * 60  # 5 minutes to complete the redirect round trip


@dataclass
class SessionData:
    """Identity resolved from a completed OIDC login, as stored in Redis."""

    subject: str
    email: Optional[str]
    name: str
    role: str
    issued_at: float


class SessionStore:
    """Thin Redis-backed store for SSO sessions and in-flight login state.

    Degrades gracefully: if Redis is unavailable, every method is a no-op
    returning ``None``/``False`` — callers (``app.core.auth``,
    ``app.api.auth``) treat that the same as "no session", which fails
    closed (401) rather than open.
    """

    def __init__(self) -> None:
        self._client = create_redis_connection(decode_responses=True)

    @property
    def available(self) -> bool:
        return self._client is not None

    def reconnect(self) -> None:
        self._client = create_redis_connection(decode_responses=True)

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def create_session(self, subject: str, email: Optional[str], name: str, role: str) -> Optional[str]:
        """Create a new session and return its opaque ID, or None if Redis is down."""
        if not self._client:
            return None
        session_id = secrets.token_urlsafe(32)
        data = SessionData(subject=subject, email=email, name=name, role=role, issued_at=time.time())
        try:
            self._client.setex(
                _SESSION_KEY.format(session_id=session_id),
                SESSION_TTL_SECONDS,
                json.dumps(asdict(data)),
            )
            return session_id
        except Exception as exc:
            _logger.warning("session_create_error", error=str(exc))
            return None

    def get_session(self, session_id: str) -> Optional[SessionData]:
        """Look up a session by ID. Returns None if missing, expired, or Redis is down."""
        if not self._client or not session_id:
            return None
        try:
            raw = self._client.get(_SESSION_KEY.format(session_id=session_id))
            if not raw:
                return None
            return SessionData(**json.loads(raw))
        except Exception as exc:
            _logger.warning("session_get_error", error=str(exc))
            return None

    def destroy_session(self, session_id: str) -> None:
        if not self._client or not session_id:
            return
        try:
            self._client.delete(_SESSION_KEY.format(session_id=session_id))
        except Exception as exc:
            _logger.warning("session_destroy_error", error=str(exc))

    # ------------------------------------------------------------------
    # OIDC login flow state (state -> PKCE verifier / nonce / next path)
    # ------------------------------------------------------------------

    def create_flow_state(self, state: str, data: Dict[str, Any]) -> bool:
        if not self._client:
            return False
        try:
            self._client.setex(_FLOW_KEY.format(state=state), FLOW_TTL_SECONDS, json.dumps(data))
            return True
        except Exception as exc:
            _logger.warning("flow_state_create_error", error=str(exc))
            return False

    def pop_flow_state(self, state: str) -> Optional[Dict[str, Any]]:
        """Read and delete the flow state for ``state`` — one-shot, replay-proof."""
        if not self._client or not state:
            return None
        key = _FLOW_KEY.format(state=state)
        try:
            raw = self._client.get(key)
            if not raw:
                return None
            self._client.delete(key)
            return json.loads(raw)
        except Exception as exc:
            _logger.warning("flow_state_pop_error", error=str(exc))
            return None


# Module-level singleton, matching app.core.redis_client.redis_cache.
session_store = SessionStore()
