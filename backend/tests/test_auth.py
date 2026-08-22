"""
Tests for OIDC session auth and role-based access control.

Covers:
  - app.core.roles.resolve_role_from_claims — the claim -> role matcher.
  - require_authentication's anonymous_access short-circuit.
  - require_role gating across the four roles, end-to-end via TestClient
    with a synthetic session (no real identity provider needed — the
    session store is what require_authentication actually reads).
  - GET/PUT /system/access and /system/sso persistence round-trips.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.auth import Role
from app.core.general_settings import general_settings
from app.core.roles import resolve_role_from_claims
from app.core.session_store import session_store
from app.models.system_manager import system_manager


# ---------------------------------------------------------------------------
# resolve_role_from_claims — pure unit tests, no app/session needed
# ---------------------------------------------------------------------------


class TestResolveRoleFromClaims:
    def test_no_mappings_denies(self):
        assert resolve_role_from_claims({"groups": ["admins"]}, []) == Role.DENY

    def test_no_match_denies(self):
        mappings = [{"claim": "groups", "value": "admins", "role": "Administrator"}]
        assert resolve_role_from_claims({"groups": ["users"]}, mappings) == Role.DENY

    def test_list_claim_membership_match(self):
        mappings = [{"claim": "groups", "value": "admins", "role": "Administrator"}]
        assert resolve_role_from_claims({"groups": ["users", "admins"]}, mappings) == Role.ADMINISTRATOR

    def test_scalar_claim_exact_match(self):
        mappings = [{"claim": "department", "value": "data-eng", "role": "Data Admin"}]
        assert resolve_role_from_claims({"department": "data-eng"}, mappings) == Role.DATA_ADMIN

    def test_dotted_claim_path(self):
        mappings = [{"claim": "realm_access.roles", "value": "viewer", "role": "Viewer"}]
        claims = {"realm_access": {"roles": ["offline_access", "viewer"]}}
        assert resolve_role_from_claims(claims, mappings) == Role.VIEWER

    def test_first_match_wins(self):
        mappings = [
            {"claim": "groups", "value": "admins", "role": "Administrator"},
            {"claim": "groups", "value": "admins", "role": "Viewer"},
        ]
        assert resolve_role_from_claims({"groups": ["admins"]}, mappings) == Role.ADMINISTRATOR

    def test_explicit_deny_mapping(self):
        mappings = [{"claim": "groups", "value": "contractors", "role": "Deny"}]
        assert resolve_role_from_claims({"groups": ["contractors"]}, mappings) == Role.DENY

    def test_missing_claim_path_segment_no_match(self):
        mappings = [{"claim": "realm_access.roles", "value": "admin", "role": "Administrator"}]
        assert resolve_role_from_claims({}, mappings) == Role.DENY


# ---------------------------------------------------------------------------
# End-to-end: anonymous_access + session-backed role gating
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(system_manager, "load_persisted_state", lambda: None)
    monkeypatch.setattr(system_manager, "start_refresh_thread", lambda: None)
    monkeypatch.setattr(system_manager, "stop_refresh_thread", lambda: None)
    monkeypatch.setattr(system_manager, "start_audit_writer", lambda: None)
    monkeypatch.setattr(system_manager, "stop_audit_writer", lambda: None)
    yield


class _FakeSessionRedis:
    """In-memory stand-in for the session store's Redis client (str-mode)."""

    def __init__(self):
        self._store: dict = {}

    def get(self, key):
        return self._store.get(key)

    def setex(self, key, ttl, value):
        self._store[key] = value

    def delete(self, *keys):
        for k in keys:
            self._store.pop(k, None)


@pytest.fixture()
def fake_session_store(monkeypatch):
    fake = _FakeSessionRedis()
    monkeypatch.setattr(session_store, "_client", fake)
    return fake


@pytest.fixture()
def client():
    from app.app import create_app
    app = create_app()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _session_cookie(role: str, subject: str = "user1", email: str = "user@example.com", name: str = "Test User") -> str:
    session_id = session_store.create_session(subject=subject, email=email, name=name, role=role)
    assert session_id is not None
    return session_id


def _login_as(client: TestClient, role: str, **kwargs) -> None:
    """Set the session cookie directly on the client (avoids the deprecated
    per-request `cookies=` kwarg)."""
    client.cookies.set("buchi_session", _session_cookie(role, **kwargs))


class TestAnonymousAccess:
    def test_defaults_to_anonymous_administrator(self, client):
        resp = client.get("/auth/me")
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_anonymous"] is True
        assert body["name"] == "Anonymous"
        assert body["role"] == "Administrator"

    def test_disabling_anonymous_access_requires_session(self, client, fake_session_store):
        general_settings.anonymous_access = False
        try:
            resp = client.get("/auth/me")
            assert resp.status_code == 401
            resp = client.get("/api/v1/dashboards")
            assert resp.status_code == 401
        finally:
            general_settings.anonymous_access = True

    def test_valid_session_is_honored_when_anonymous_disabled(self, client, fake_session_store):
        general_settings.anonymous_access = False
        try:
            _login_as(client, "Viewer")
            resp = client.get("/auth/me")
            assert resp.status_code == 200
            body = resp.json()
            assert body["is_anonymous"] is False
            assert body["role"] == "Viewer"
            assert body["email"] == "user@example.com"
        finally:
            general_settings.anonymous_access = True

    def test_anonymous_access_ignores_any_existing_session(self, client, fake_session_store):
        # anonymous_access=True short-circuits unconditionally, per spec —
        # even a valid session cookie for a real role is ignored.
        _login_as(client, "Deny")
        resp = client.get("/auth/me")
        assert resp.status_code == 200
        assert resp.json()["role"] == "Administrator"
        assert resp.json()["is_anonymous"] is True


class TestRoleGating:
    @pytest.fixture(autouse=True)
    def _require_login(self):
        general_settings.anonymous_access = False
        yield
        general_settings.anonymous_access = True

    def test_deny_role_blocked_everywhere_except_auth_me(self, client, fake_session_store):
        _login_as(client, "Deny")
        assert client.get("/auth/me").status_code == 200
        assert client.get("/api/v1/dashboards").status_code == 403
        assert client.get("/api/v1/system/access").status_code == 403

    def test_viewer_can_list_dashboards_not_settings(self, client, fake_session_store):
        _login_as(client, "Viewer")
        assert client.get("/api/v1/dashboards").status_code == 200
        assert client.get("/api/v1/system/access").status_code == 403
        assert client.get("/api/v1/system/data-sources").status_code == 403
        assert client.get("/api/v1/widgets").status_code == 403

    def test_data_admin_can_manage_data_not_access_or_widgets(self, client, fake_session_store):
        _login_as(client, "Data Admin")
        assert client.get("/api/v1/dashboards").status_code == 200
        assert client.get("/api/v1/system/data-sources").status_code == 200
        assert client.get("/api/v1/system/access").status_code == 403
        assert client.get("/api/v1/widgets").status_code == 403
        assert client.get("/api/v1/system/audit-logs").status_code == 403

    def test_administrator_can_reach_everything_tested(self, client, fake_session_store):
        _login_as(client, "Administrator")
        assert client.get("/api/v1/dashboards").status_code == 200
        assert client.get("/api/v1/system/data-sources").status_code == 200
        assert client.get("/api/v1/system/access").status_code == 200
        assert client.get("/api/v1/widgets").status_code == 200

    def test_no_cookie_at_all_is_401_not_403(self, client, fake_session_store):
        resp = client.get("/api/v1/dashboards")
        assert resp.status_code == 401

    def test_invalid_role_string_in_session_denies(self, client, fake_session_store):
        # Defense in depth: a corrupted/legacy session role string should
        # never silently grant access.
        _login_as(client, "not-a-real-role")
        assert client.get("/api/v1/dashboards").status_code == 403


# ---------------------------------------------------------------------------
# Settings persistence round-trips (exercised as the anonymous Administrator,
# matching how a fresh install's first admin would interact with them)
# ---------------------------------------------------------------------------


class TestAccessAndSsoSettingsPersistence:
    def test_access_settings_round_trip(self, client):
        resp = client.put(
            "/api/v1/system/access",
            json={
                "anonymous_access": False,
                "role_mappings": [{"claim": "groups", "value": "admins", "role": "Administrator"}],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["anonymous_access"] is False
        assert resp.json()["role_mappings"] == [{"claim": "groups", "value": "admins", "role": "Administrator"}]

        # Restore anonymous_access so later tests aren't affected by leaked state.
        general_settings.anonymous_access = True

        resp = client.get("/api/v1/system/access")
        assert resp.status_code == 200
        assert resp.json()["role_mappings"] == [{"claim": "groups", "value": "admins", "role": "Administrator"}]

    def test_access_settings_rejects_invalid_role(self, client):
        resp = client.put(
            "/api/v1/system/access",
            json={"role_mappings": [{"claim": "groups", "value": "x", "role": "Not A Role"}]},
        )
        assert resp.status_code == 422

    def test_sso_settings_round_trip_and_secret_masking(self, client):
        resp = client.put(
            "/api/v1/system/sso",
            json={
                "issuer_url": "https://keycloak.example.com/realms/buchimaker",
                "client_id": "buchimaker",
                "client_secret": "super-secret-value",
                "redirect_uri": "http://localhost:3000/auth/callback",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["client_secret_set"] is True
        assert "client_secret" not in body  # never echoed back

        resp = client.get("/api/v1/system/sso")
        assert resp.json()["issuer_url"] == "https://keycloak.example.com/realms/buchimaker"
        assert resp.json()["client_secret_set"] is True

        # Saving again with a blank secret must not clear the stored one.
        resp = client.put("/api/v1/system/sso", json={"client_id": "buchimaker-2"})
        assert resp.status_code == 200
        assert resp.json()["client_secret_set"] is True
        assert resp.json()["client_id"] == "buchimaker-2"

    def test_sso_test_endpoint_rejects_unreachable_issuer(self, client):
        resp = client.post("/api/v1/system/sso/test", json={"issuer_url": "https://this-host-does-not-exist.invalid"})
        assert resp.status_code == 200  # the endpoint reports failure in the body, not via HTTP status
        assert resp.json()["ok"] is False
