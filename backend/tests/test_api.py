"""
Integration tests: API endpoints via FastAPI TestClient.

Tests cover /healthz, /api/v1/system/*, and /api/v1/dashboards/*.
Uses TestClient (synchronous HTTPX-based transport) with a fresh
DuckDB state per test.

The dashboard data endpoint is POST /api/v1/dashboards/{id}/data and
combines filter resolution + data retrieval in a single call.
"""

import gzip
import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.db import close_db, get_db
from app.models.system_manager import system_manager


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    """Reset DuckDB and system_manager state before each test.

    Patches load_persisted_state to prevent YAML state files from populating
    the system_manager during TestClient lifespan startup.
    """
    monkeypatch.setattr(system_manager, "load_persisted_state", lambda: None)
    monkeypatch.setattr(system_manager, "start_refresh_thread", lambda: None)
    monkeypatch.setattr(system_manager, "stop_refresh_thread", lambda: None)
    monkeypatch.setattr(system_manager, "start_audit_writer", lambda: None)
    monkeypatch.setattr(system_manager, "stop_audit_writer", lambda: None)
    close_db()
    system_manager.dashboards.clear()
    system_manager.data_sources.clear()
    yield
    close_db()
    system_manager.dashboards.clear()
    system_manager.data_sources.clear()


@pytest.fixture()
def client():
    """Provide a TestClient with the full BuchiMaker app."""
    from app.app import create_app
    app = create_app()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


class _FakeRedis:
    """Minimal in-memory stand-in for the subset of the redis-py client API
    that RedisCache uses. The test environment has no reachable Redis
    server (real connections fail DNS resolution), so cache hit/miss and
    invalidation behavior can't be exercised against a live Redis — this
    fake lets tests verify RedisCache's own logic instead."""

    def __init__(self):
        self._store: dict = {}
        self._sets: dict = {}

    def get(self, key):
        return self._store.get(key)

    def setex(self, key, ttl, value):
        self._store[key] = value

    def set(self, key, value):
        self._store[key] = value

    def sadd(self, key, member):
        self._sets.setdefault(key, set()).add(member)

    def expire(self, key, ttl):
        pass

    def smembers(self, key):
        return self._sets.get(key, set())

    def delete(self, *keys):
        count = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                count += 1
            self._sets.pop(k, None)
        return count

    def scan_iter(self, match=None):
        import fnmatch
        for k in list(self._store.keys()):
            if match is None or fnmatch.fnmatch(k, match):
                yield k

    def flushdb(self):
        self._store.clear()
        self._sets.clear()


@pytest.fixture()
def fake_redis(monkeypatch):
    """Inject an in-memory fake Redis client into the redis_cache singleton
    for the test's duration; monkeypatch restores the original (disconnected)
    client automatically afterward."""
    from app.core.redis_client import redis_cache
    fake = _FakeRedis()
    monkeypatch.setattr(redis_cache, "_client", fake)
    return fake


@pytest.fixture()
def csv_file(tmp_path):
    """Create a small CSV test file."""
    p = tmp_path / "orders.csv"
    p.write_text("id,product,amount\n1,apple,10\n2,banana,20\n3,apple,15\n")
    return str(p)


@pytest.fixture()
def dashboard_yaml(tmp_path, csv_file):
    """Create a minimal valid dashboard YAML with filter widgets."""
    content = f"""
id: api_test_dash
title: API Test Dashboard
base_view: "CREATE OR REPLACE VIEW api_test_dash_base AS SELECT * FROM orders;"
totals:
  - name: total_orders
    query: "SELECT COUNT(*) FROM api_test_dash_base"
aggregates:
  - name: by_product
    query: "SELECT product, SUM(amount) AS revenue FROM api_test_dash_base GROUP BY 1"
mappings:
  - "product_filter": "product"
  - "amount_filter": "amount"
layout:
  - tile:
      id: tile_total
      label: "Total Orders"
      value: "total_orders"
      grid:
        x: 0
        y: 0
        w: 2
        h: 1
  - basic_table:
      id: table_products
      title: "Products"
      data: by_product
      grid:
        x: 0
        y: 1
        w: 6
        h: 3
  - input_filter:
      id: product_search
      label: "Search Product"
      mapping: "product_filter"
      filter:
        - operator: "has"
        - value: ""
      grid:
        x: 0
        y: 4
        w: 2
        h: 1
  - dropdown_single:
      id: product_select
      label: "Product"
      mapping: "product_filter"
      grid:
        x: 2
        y: 4
        w: 2
        h: 1
"""
    p = tmp_path / "api_test.yaml"
    p.write_text(content)
    return str(p)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    """Tests for GET /healthz."""

    def test_healthz_returns_200(self, client):
        """Healthy system returns 200 with status=ok."""
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["duckdb_ok"] is True

    def test_healthz_includes_version(self, client):
        """Response includes the application version string."""
        resp = client.get("/healthz")
        body = resp.json()
        assert "version" in body
        assert body["version"] != ""


# ---------------------------------------------------------------------------
# System settings
# ---------------------------------------------------------------------------

class TestSystemSettingsAPI:
    """Tests for GET/PUT /api/v1/system/settings."""

    def test_get_default_settings(self, client):
        """GET /settings returns settings."""
        from app.core.general_settings import general_settings
        general_settings.auto_refresh = None
        general_settings.redis_host = None
        general_settings.redis_port = None
        general_settings.redis_password = None
        general_settings.redis_user = None
        general_settings.redis_tls_enabled = None
        resp = client.get("/api/v1/system/settings")
        assert resp.status_code == 200
        assert resp.json() == {
            "auto_refresh": None,
            "redis_host": None,
            "redis_port": None,
            "redis_password_set": False,
            "redis_user": None,
            "redis_tls_enabled": None,
            "duckdb_active_file": "./db/duck.db",
            "syslog": {
                "enabled": False,
                "host": None,
                "port": None,
                "tls_enabled": False,
                "cert_path": None,
                "key_path": None,
                "ca_cert_path": None,
            },
            "row_limit": 1000,
            "redis_ttl_seconds": 1800,
            "sql_api_enabled": False,
        }

    def test_get_settings_reports_password_set_without_leaking_value(self, client):
        """GET /settings never echoes the real password, only whether one is set."""
        from app.core.general_settings import general_settings
        general_settings.redis_password = "super-secret"
        try:
            resp = client.get("/api/v1/system/settings")
            assert resp.status_code == 200
            body = resp.json()
            assert "redis_password" not in body
            assert body["redis_password_set"] is True
            assert "super-secret" not in resp.text
        finally:
            general_settings.redis_password = None

    def test_update_settings(self, client):
        """PUT /api/v1/system/settings updates globals."""
        resp = client.put("/api/v1/system/settings", json={
            "auto_refresh": 60,
            "redis_host": "remote_redis",
            "row_limit": 100,
            "redis_ttl_seconds": 1800,
            "sql_api_enabled": True
        })
        assert resp.status_code == 200
        assert resp.json() == {
            "auto_refresh": 60,
            "redis_host": "remote_redis",
            "redis_port": None,
            "redis_password_set": False,
            "redis_user": None,
            "redis_tls_enabled": None,
            "duckdb_active_file": "./db/duck.db",
            "syslog": {
                "enabled": False,
                "host": None,
                "port": None,
                "tls_enabled": False,
                "cert_path": None,
                "key_path": None,
                "ca_cert_path": None,
            },
            "row_limit": 100,
            "redis_ttl_seconds": 1800,
            "sql_api_enabled": True,
        }

        # We can also check general_settings directly:
        from app.core.general_settings import general_settings
        assert general_settings.auto_refresh == 60

    def test_update_settings_omitting_password_keeps_existing(self, client):
        """PUT /settings without redis_password leaves the existing password unchanged."""
        from app.core.general_settings import general_settings
        general_settings.redis_password = "keep-me"
        try:
            resp = client.put("/api/v1/system/settings", json={"auto_refresh": 30})
            assert resp.status_code == 200
            assert resp.json()["redis_password_set"] is True
            assert general_settings.redis_password == "keep-me"
        finally:
            general_settings.redis_password = None

    def test_update_settings_auto_refresh_disabled(self, client):
        """PUT /settings accepts 'disabled' for auto_refresh."""
        resp = client.put("/api/v1/system/settings", json={"auto_refresh": "disabled"})
        assert resp.status_code == 200
        assert resp.json()["auto_refresh"] == "disabled"

    def test_update_settings_auto_refresh_cron(self, client):
        """PUT /settings accepts a list of valid cron expressions."""
        cron_list = ["*/15 * * * *", "0 3 * * 1-5"]
        resp = client.put("/api/v1/system/settings", json={"auto_refresh": cron_list})
        assert resp.status_code == 200
        assert resp.json()["auto_refresh"] == cron_list

    def test_update_settings_auto_refresh_invalid_string(self, client):
        """PUT /settings rejects arbitrary strings for auto_refresh."""
        resp = client.put("/api/v1/system/settings", json={"auto_refresh": "weird stuff"})
        assert resp.status_code == 422
        
    def test_update_settings_auto_refresh_invalid_cron(self, client):
        """PUT /settings rejects lists with invalid cron expressions."""
        resp = client.put("/api/v1/system/settings", json={"auto_refresh": ["not a cron", "0 * * * *"]})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Redis cache reset
# ---------------------------------------------------------------------------

class TestCacheResetAPI:
    """Tests for POST /api/v1/system/cache/reset."""

    def test_reset_redis_cache(self, client, fake_redis):
        """POST /cache/reset flushes all keys and returns 200."""
        fake_redis._store["db:1:abc"] = b"payload"
        fake_redis._sets["ds:orders:keys"] = {"db:1:abc"}

        resp = client.post("/api/v1/system/cache/reset")

        assert resp.status_code == 200
        assert resp.json() == {"status": "success", "message": "Redis cache cleared."}
        assert fake_redis._store == {}
        assert fake_redis._sets == {}

    def test_reset_redis_cache_unavailable(self, client, monkeypatch):
        """POST /cache/reset returns 503 when Redis is unreachable."""
        from app.core.redis_client import redis_cache
        monkeypatch.setattr(redis_cache, "_client", None)

        resp = client.post("/api/v1/system/cache/reset")

        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

class TestDataSourcesAPI:
    """Tests for /api/v1/system/data-sources."""

    def test_list_empty(self, client):
        """Initially no data sources are registered."""
        resp = client.get("/api/v1/system/data-sources")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_register_csv_source(self, client, csv_file):
        """POST /data-sources/csv registers and loads the CSV file."""
        resp = client.post("/api/v1/system/data-sources/csv", json={
            "name": "orders",
            "title": "Orders",
            "filepath": csv_file,
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["source_type"] == "csv"
        assert body["name"] == "orders"
        assert body["last_updated"] > 0

    def test_register_csv_missing_file(self, client, tmp_path):
        """POST with a non-existent filepath *inside* the data dir returns 400."""
        resp = client.post("/api/v1/system/data-sources/csv", json={
            "name": "ghost",
            "title": "Ghost",
            "filepath": str(tmp_path / "missing.csv"),
        })
        assert resp.status_code == 400

    def test_register_csv_path_outside_data_dir_rejected(self, client):
        """POST with a filepath outside the configured data dir returns 422,
        not 400 — this is a validation failure, not a missing-file error."""
        resp = client.post("/api/v1/system/data-sources/csv", json={
            "name": "ghost",
            "title": "Ghost",
            "filepath": "/totally/missing/file.csv",
        })
        assert resp.status_code == 422

    def test_register_csv_path_traversal_rejected(self, client, tmp_path):
        """A `../` traversal attempt out of the data dir is rejected."""
        resp = client.post("/api/v1/system/data-sources/csv", json={
            "name": "ghost",
            "title": "Ghost",
            "filepath": str(tmp_path / ".." / "escaped.csv"),
        })
        assert resp.status_code == 422

    def test_register_csv_invalid_name(self, client, csv_file):
        """Name with special characters is rejected with 422."""
        resp = client.post("/api/v1/system/data-sources/csv", json={
            "name": "bad!name",
            "title": "Bad",
            "filepath": csv_file,
        })
        assert resp.status_code == 422

    def test_delete_data_source(self, client, csv_file):
        """DELETE removes a registered source."""
        client.post("/api/v1/system/data-sources/csv", json={
            "name": "orders",
            "title": "Orders",
            "filepath": csv_file,
        })
        resp = client.delete("/api/v1/system/data-sources/orders")
        assert resp.status_code == 204

    def test_delete_removes_from_list(self, client, csv_file):
        """After DELETE, the source no longer appears in GET /data-sources."""
        client.post("/api/v1/system/data-sources/csv", json={
            "name": "orders",
            "title": "Orders",
            "filepath": csv_file,
        })
        client.delete("/api/v1/system/data-sources/orders")
        resp = client.get("/api/v1/system/data-sources")
        assert resp.status_code == 200
        names = [ds["name"] for ds in resp.json()]
        assert "orders" not in names

    def test_delete_nonexistent_source(self, client):
        """DELETE of unknown source returns 404."""
        resp = client.delete("/api/v1/system/data-sources/ghost")
        assert resp.status_code == 404

    def test_list_connector_types(self, client):
        """GET /connector-types returns at least csv, json, bigquery."""
        resp = client.get("/api/v1/system/connector-types")
        assert resp.status_code == 200
        types = resp.json()
        assert "csv" in types
        assert "json" in types
        assert "bigquery" in types

    def test_list_after_registration_returns_source(self, client, csv_file):
        """GET /data-sources includes the newly registered CSV source."""
        client.post("/api/v1/system/data-sources/csv", json={
            "name": "orders",
            "title": "Orders",
            "filepath": csv_file,
        })
        resp = client.get("/api/v1/system/data-sources")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["name"] == "orders"
        assert body[0]["source_type"] == "csv"
        assert body[0]["last_updated"] > 0
        assert "id" in body[0]
        assert "title" in body[0]

    def test_register_csv_overwrite_same_name(self, client, csv_file):
        """Registering again with the same name overwrites the existing view."""
        client.post("/api/v1/system/data-sources/csv", json={
            "name": "orders",
            "title": "Orders v1",
            "filepath": csv_file,
        })
        resp = client.post("/api/v1/system/data-sources/csv", json={
            "name": "orders",
            "title": "Orders v2",
            "filepath": csv_file,
        })
        assert resp.status_code == 201
        assert resp.json()["title"] == "Orders v2"
        # Still only one source
        list_resp = client.get("/api/v1/system/data-sources")
        assert len(list_resp.json()) == 1

    # ------------------------------------------------------------------
    # JSON connector
    # ------------------------------------------------------------------

    def test_register_json_source(self, client, tmp_path):
        """POST /data-sources/json registers and loads a JSON array file."""
        p = tmp_path / "inventory.json"
        p.write_text('[{"id": 1, "item": "apple", "qty": 100}, {"id": 2, "item": "banana", "qty": 50}]')
        resp = client.post("/api/v1/system/data-sources/json", json={
            "name": "inventory",
            "title": "Warehouse Inventory",
            "filepath": str(p),
            "description": "Daily inventory snapshot",
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["source_type"] == "json"
        assert body["name"] == "inventory"
        assert body["title"] == "Warehouse Inventory"
        assert body["description"] == "Daily inventory snapshot"
        assert body["last_updated"] > 0
        assert "id" in body

    def test_register_json_ndjson_source(self, client, tmp_path):
        """POST /data-sources/json accepts JSON-Lines (NDJSON) format."""
        p = tmp_path / "events.ndjson"
        p.write_text('{"event": "click", "ts": 1}\n{"event": "view", "ts": 2}\n')
        resp = client.post("/api/v1/system/data-sources/json", json={
            "name": "events",
            "title": "Events",
            "filepath": str(p),
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["source_type"] == "json"
        assert body["name"] == "events"

    def test_register_json_missing_file(self, client, tmp_path):
        """POST /data-sources/json with a non-existent file inside the data
        dir returns 400."""
        resp = client.post("/api/v1/system/data-sources/json", json={
            "name": "ghost",
            "title": "Ghost",
            "filepath": str(tmp_path / "missing.json"),
        })
        assert resp.status_code == 400

    def test_register_json_path_outside_data_dir_rejected(self, client):
        """POST /data-sources/json with a filepath outside the configured
        data dir returns 422, not 400."""
        resp = client.post("/api/v1/system/data-sources/json", json={
            "name": "ghost",
            "title": "Ghost",
            "filepath": "/totally/missing/data.json",
        })
        assert resp.status_code == 422

    def test_register_json_invalid_name(self, client, tmp_path):
        """JSON source name with special characters is rejected with 422."""
        p = tmp_path / "data.json"
        p.write_text('[{"x": 1}]')
        resp = client.post("/api/v1/system/data-sources/json", json={
            "name": "bad!name",
            "title": "Bad",
            "filepath": str(p),
        })
        assert resp.status_code == 422

    def test_register_json_appears_in_list(self, client, tmp_path):
        """A registered JSON source appears in GET /data-sources."""
        p = tmp_path / "data.json"
        p.write_text('[{"col": "val"}]')
        client.post("/api/v1/system/data-sources/json", json={
            "name": "my_json",
            "title": "My JSON",
            "filepath": str(p),
        })
        resp = client.get("/api/v1/system/data-sources")
        assert resp.status_code == 200
        names = [ds["name"] for ds in resp.json()]
        assert "my_json" in names

    def test_delete_json_source(self, client, tmp_path):
        """DELETE removes a registered JSON source."""
        p = tmp_path / "data.json"
        p.write_text('[{"col": "val"}]')
        client.post("/api/v1/system/data-sources/json", json={
            "name": "temp_json",
            "title": "Temp",
            "filepath": str(p),
        })
        resp = client.delete("/api/v1/system/data-sources/temp_json")
        assert resp.status_code == 204

    # ------------------------------------------------------------------
    # BigQuery connector — dependency guard
    # ------------------------------------------------------------------

    def test_register_bigquery_without_deps_returns_501(self, monkeypatch):
        """POST /data-sources/bigquery returns 501 if google-cloud-bigquery is missing.

        The API handler catches ImportError raised inside register_data_source and
        converts it to HTTP 501. We simulate this by patching register_data_source.
        """
        from app.models.system_manager import system_manager as sm

        def _raise_import(*args, **kwargs):
            raise ImportError(
                "BigQueryConnector requires 'google-cloud-bigquery' and 'pandas'. "
                "Install them with: pip install google-cloud-bigquery pandas"
            )

        monkeypatch.setattr(sm, "register_data_source", _raise_import)

        from app.app import create_app
        app = create_app()
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.post("/api/v1/system/data-sources/bigquery", json={
                "name": "bq_events",
                "title": "Analytics Events",
                "project_id": "my-project",
                "dataset_id": "analytics",
                "table_id": "events",
                "credentials_path": "/run/secrets/bq_sa.json",
            })
        assert resp.status_code == 501
        detail = resp.json().get("detail", "").lower()
        assert "google-cloud-bigquery" in detail or "bigqueryconnector" in detail

    def test_register_bigquery_invalid_name(self, client):
        """BigQuery source name with special characters is rejected with 422."""
        resp = client.post("/api/v1/system/data-sources/bigquery", json={
            "name": "bad!bq",
            "title": "Bad BQ",
            "project_id": "proj",
            "dataset_id": "ds",
            "table_id": "tbl",
            "credentials_path": "/run/secrets/sa.json",
        })
        assert resp.status_code == 422

    def test_register_bigquery_missing_required_fields(self, client):
        """POST /data-sources/bigquery with missing required fields returns 422."""
        resp = client.post("/api/v1/system/data-sources/bigquery", json={
            "name": "bq_events",
            "title": "Events",
            # missing project_id, dataset_id, table_id, credentials_path
        })
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Dashboards — lifecycle
# ---------------------------------------------------------------------------

class TestDashboardsAPI:
    """Tests for /api/v1/dashboards lifecycle (load, list, get, delete)."""

    def _load_orders_source(self, client, csv_file):
        """Helper: register the orders CSV source."""
        client.post("/api/v1/system/data-sources/csv", json={
            "name": "orders",
            "title": "Orders",
            "filepath": csv_file,
        })

    def test_list_dashboards_empty(self, client):
        """Initially no dashboards are loaded."""
        resp = client.get("/api/v1/dashboards")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_load_dashboard_yaml(self, client, csv_file, dashboard_yaml):
        """POST /dashboards/load registers a dashboard from YAML."""
        self._load_orders_source(client, csv_file)
        resp = client.post("/api/v1/dashboards/load", json={"filepath": dashboard_yaml})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_load_missing_yaml_returns_422(self, client):
        """POST with non-existent YAML path returns 422."""
        resp = client.post("/api/v1/dashboards/load", json={"filepath": "/ghost.yaml"})
        assert resp.status_code == 422

    def test_get_dashboard_definition(self, client, csv_file, dashboard_yaml):
        """GET /dashboards/{id} returns the dashboard definition."""
        self._load_orders_source(client, csv_file)
        client.post("/api/v1/dashboards/load", json={"filepath": dashboard_yaml})
        resp = client.get("/api/v1/dashboards/api_test_dash")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "api_test_dash"
        assert "summaries" in body
        assert "layout" in body

    def test_get_dashboard_definition_includes_input_filter_widget(
        self, client, csv_file, dashboard_yaml
    ):
        """GET /dashboards/{id} layout includes the input_filter widget."""
        self._load_orders_source(client, csv_file)
        client.post("/api/v1/dashboards/load", json={"filepath": dashboard_yaml})
        resp = client.get("/api/v1/dashboards/api_test_dash")
        assert resp.status_code == 200
        layout = resp.json()["layout"]
        types = [w["type"] for w in layout]
        assert "input_filter" in types
        # Find the input_filter and check its id
        input_filter = next(w for w in layout if w["type"] == "input_filter")
        assert input_filter["id"] == "product_search"
        assert input_filter["config"]["mapping"] == "product_filter"

    def test_get_unknown_dashboard_returns_404(self, client):
        """GET unknown dashboard ID returns 404."""
        resp = client.get("/api/v1/dashboards/does_not_exist")
        assert resp.status_code == 404

    def test_delete_dashboard(self, client, csv_file, dashboard_yaml):
        """DELETE removes the dashboard."""
        self._load_orders_source(client, csv_file)
        client.post("/api/v1/dashboards/load", json={"filepath": dashboard_yaml})
        resp = client.delete("/api/v1/dashboards/api_test_dash")
        assert resp.status_code == 204
        assert "api_test_dash" not in system_manager.dashboards

    def test_invalid_dashboard_id_rejected(self, client):
        """Dashboard ID with special characters returns 422."""
        resp = client.get("/api/v1/dashboards/bad!id")
        assert resp.status_code == 422

    def test_virtual_tables_empty_initially(self, client, csv_file, dashboard_yaml):
        """New dashboard with no active filters has no virtual tables."""
        self._load_orders_source(client, csv_file)
        client.post("/api/v1/dashboards/load", json={"filepath": dashboard_yaml})
        resp = client.get("/api/v1/dashboards/api_test_dash/virtual-tables")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# global_refresh() active/standby symmetry (see ADR-016 in
# docs/backend_architecture.md)
# ---------------------------------------------------------------------------

class TestGlobalRefreshBaseViewSymmetry:
    """Regression tests: a dashboard's base_view must survive a
    global_refresh() promotion cycle.

    global_refresh() loads fresh connector tables into the *standby*
    DuckDB slot and promotes it to active. Dashboards' base_views are
    normally only (re)created against get_db() (whichever slot is active
    at load time), so without recreating them against the standby
    connection first, a promotion would silently leave the new active
    slot without any dashboard's base_view — every total/aggregate query
    would start failing right after a refresh until the dashboard was
    reloaded.
    """

    def _load_orders_source(self, client, csv_file):
        client.post("/api/v1/system/data-sources/csv", json={
            "name": "orders",
            "title": "Orders",
            "filepath": csv_file,
        })

    def test_base_view_queryable_after_second_global_refresh(
        self, client, csv_file, dashboard_yaml
    ):
        """A dashboard loaded before a second global_refresh() can still be
        queried afterwards, against the newly-promoted active connection."""
        self._load_orders_source(client, csv_file)
        resp = client.post("/api/v1/dashboards/load", json={"filepath": dashboard_yaml})
        assert resp.status_code == 200

        dash = system_manager.dashboards["api_test_dash"]
        assert dash.source_table  # base_view was created against the first active slot

        # Triggers another load-into-standby + promote cycle. Before the
        # fix, base_views were never recreated against the standby
        # connection, so the dashboard's view would only exist in whatever
        # slot is demoted to standby by this call.
        system_manager.global_refresh(rate_limit=False)

        resp = client.post("/api/v1/dashboards/api_test_dash/data", json={"filters": {}})
        assert resp.status_code == 200
        assert resp.json()["totals"]["total_orders"] == 3


# ---------------------------------------------------------------------------
# POST /data — unified filter + data endpoint
# ---------------------------------------------------------------------------

class TestDashboardDataAPI:
    """Tests for POST /api/v1/dashboards/{id}/data."""

    def _setup(self, client, csv_file, dashboard_yaml):
        """Register data source and load dashboard."""
        client.post("/api/v1/system/data-sources/csv", json={
            "name": "orders",
            "title": "Orders",
            "filepath": csv_file,
        })
        client.post("/api/v1/dashboards/load", json={"filepath": dashboard_yaml})

    def _post_data(self, client, filters=None, dashboard_body=None):
        """Helper: POST to /data and return the parsed (decompressed) JSON body."""
        body = {}
        if dashboard_body:
            body["dashboard"] = dashboard_body
        if filters is not None:
            body["filters"] = filters
        resp = client.post(
            "/api/v1/dashboards/api_test_dash/data",
            json=body,
            headers={"Accept-Encoding": "gzip"},
        )
        return resp

    def _parse_response(self, resp):
        """Decompress gzip response and parse JSON."""
        content = resp.content
        # TestClient may or may not decompress automatically
        try:
            return json.loads(content)
        except Exception:
            return json.loads(gzip.decompress(content))

    def test_post_data_no_filter_returns_200(self, client, csv_file, dashboard_yaml):
        """POST /data with no filters returns 200 with totals and aggregates."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = self._post_data(client, filters={})
        assert resp.status_code == 200
        body = self._parse_response(resp)
        assert body["dashboard_id"] == "api_test_dash"
        assert "totals" in body
        assert "aggregates" in body
        assert "widget_map" in body
        assert "filter_hash" in body

    def test_post_data_totals_correct(self, client, csv_file, dashboard_yaml):
        """totals['total_orders'] reflects all 3 rows without filters."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = self._post_data(client, filters={})
        assert resp.status_code == 200
        body = self._parse_response(resp)
        assert body["totals"]["total_orders"] == 3

    def test_post_data_with_equality_filter(self, client, csv_file, dashboard_yaml):
        """Filtering by product_search with eq reduces totals."""
        self._setup(client, csv_file, dashboard_yaml)
        # Filter for apple using the input_filter widget with eq operator
        resp = self._post_data(client, filters={
            "product_search": {"operator": "eq", "value": "apple"}
        })
        assert resp.status_code == 200
        body = self._parse_response(resp)
        # 2 rows are apple
        assert body["totals"]["total_orders"] == 2

    def test_post_data_with_has_operator(self, client, csv_file, dashboard_yaml):
        """'has' operator performs substring match."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = self._post_data(client, filters={
            "product_search": {"operator": "has", "value": "app"}
        })
        assert resp.status_code == 200
        body = self._parse_response(resp)
        # "apple" contains "app" — 2 rows
        assert body["totals"]["total_orders"] == 2

    def test_post_data_with_simple_string_filter(self, client, csv_file, dashboard_yaml):
        """Simple string value (no operator dict) implies eq."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = self._post_data(client, filters={"product_search": "banana"})
        assert resp.status_code == 200
        body = self._parse_response(resp)
        assert body["totals"]["total_orders"] == 1

    def test_post_data_widget_map_populated(self, client, csv_file, dashboard_yaml):
        """widget_map contains entries for tile and basic_table widgets."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = self._post_data(client, filters={})
        assert resp.status_code == 200
        body = self._parse_response(resp)
        wm = {e["widget_id"]: e for e in body["widget_map"]}
        # Tile should map to total
        assert "tile_total" in wm
        assert wm["tile_total"]["data_type"] == "total"
        assert wm["tile_total"]["data_key"] == "total_orders"
        # basic_table should map to aggregate
        assert "table_products" in wm
        assert wm["table_products"]["data_type"] == "aggregate"
        assert wm["table_products"]["data_key"] == "by_product"
        # Filter widgets should NOT appear in widget_map
        assert "product_search" not in wm
        assert "product_select" not in wm

    def test_post_data_row_limit_respected(self, client, csv_file, dashboard_yaml):
        """row_limit in response reflects configured limit."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = self._post_data(client, filters={})
        assert resp.status_code == 200
        body = self._parse_response(resp)
        assert "row_limit" in body
        assert body["row_limit"] >= 1

    def test_post_data_dashboard_id_mismatch_returns_400(
        self, client, csv_file, dashboard_yaml
    ):
        """Body 'dashboard' field that doesn't match path returns 400."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = client.post(
            "/api/v1/dashboards/api_test_dash/data",
            json={"dashboard": "wrong_id", "filters": {}},
        )
        assert resp.status_code == 400

    def test_post_data_unknown_dashboard_returns_404(self, client):
        """POST /data for non-existent dashboard returns 404."""
        resp = client.post("/api/v1/dashboards/ghost_dash/data", json={})
        assert resp.status_code == 404

    def test_post_data_invalid_dashboard_id_returns_422(self, client):
        """Dashboard ID with special characters returns 422."""
        resp = client.post("/api/v1/dashboards/bad!id/data", json={})
        assert resp.status_code == 422

    def test_post_data_unknown_filter_widget_is_skipped(
        self, client, csv_file, dashboard_yaml
    ):
        """Filters referencing unknown widget IDs are silently skipped."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = self._post_data(client, filters={"nonexistent_widget": "value"})
        assert resp.status_code == 200
        body = self._parse_response(resp)
        # Should return full unfiltered data (3 rows)
        assert body["totals"]["total_orders"] == 3

    def test_post_data_invalid_operator_returns_422(
        self, client, csv_file, dashboard_yaml
    ):
        """An unrecognised operator returns 422."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = self._post_data(client, filters={
            "product_search": {"operator": "BAD_OP", "value": "x"}
        })
        assert resp.status_code == 422

    def test_post_data_response_has_filter_hash(self, client, csv_file, dashboard_yaml):
        """Each response includes a stable filter_hash."""
        self._setup(client, csv_file, dashboard_yaml)
        # Two identical requests should produce the same hash
        r1 = self._post_data(client, filters={"product_search": "apple"})
        r2 = self._post_data(client, filters={"product_search": "apple"})
        assert r1.status_code == 200
        assert r2.status_code == 200
        b1 = self._parse_response(r1)
        b2 = self._parse_response(r2)
        assert b1["filter_hash"] == b2["filter_hash"]

    def test_post_data_different_filters_produce_different_hash(
        self, client, csv_file, dashboard_yaml
    ):
        """Different filter values produce different hashes."""
        self._setup(client, csv_file, dashboard_yaml)
        r1 = self._post_data(client, filters={"product_search": "apple"})
        r2 = self._post_data(client, filters={"product_search": "banana"})
        b1 = self._parse_response(r1)
        b2 = self._parse_response(r2)
        assert b1["filter_hash"] != b2["filter_hash"]

    def test_post_data_cache_hit_sets_from_cache_true(
        self, client, csv_file, dashboard_yaml, fake_redis
    ):
        """A second identical request is served from cache with
        from_cache=true in the body and X-Cache: HIT — regression test for
        the bug where from_cache was hardcoded false even on a real hit."""
        self._setup(client, csv_file, dashboard_yaml)
        r1 = self._post_data(client, filters={})
        assert r1.headers.get("x-cache") == "MISS"
        b1 = self._parse_response(r1)
        assert b1["from_cache"] is False

        r2 = self._post_data(client, filters={})
        assert r2.headers.get("x-cache") == "HIT"
        b2 = self._parse_response(r2)
        assert b2["from_cache"] is True
        assert b2["totals"] == b1["totals"]

    def test_dashboard_reload_invalidates_cache(
        self, client, csv_file, dashboard_yaml, fake_redis
    ):
        """Hot-reloading a dashboard busts its previously cached /data
        responses — regression test for the bug where stale cached data
        survived a YAML reload indefinitely."""
        self._setup(client, csv_file, dashboard_yaml)
        r1 = self._post_data(client, filters={})
        assert r1.headers.get("x-cache") == "MISS"

        r2 = self._post_data(client, filters={})
        assert r2.headers.get("x-cache") == "HIT"  # confirms it was actually cached

        reload_resp = client.post(
            "/api/v1/dashboards/load", json={"filepath": dashboard_yaml}
        )
        assert reload_resp.status_code == 200

        r3 = self._post_data(client, filters={})
        assert r3.headers.get("x-cache") == "MISS"  # stale cache must not survive the reload

    def test_post_data_filter_value_with_quote_does_not_error(
        self, client, csv_file, dashboard_yaml
    ):
        """A filter value containing a single quote must not break the SQL
        query — regression test for the SQL-injection gap where filter
        values were embedded as unescaped string literals."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = self._post_data(client, filters={"product_search": "O'Brien"})
        assert resp.status_code == 200
        body = self._parse_response(resp)
        assert body["totals"]["total_orders"] == 0  # no product literally matches

    def test_post_data_filter_value_injection_payload_does_not_bypass_filter(
        self, client, csv_file, dashboard_yaml
    ):
        """A classic `' OR 1=1 --` payload must be treated as a literal
        substring to search for, not as SQL — it must not return all rows."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = self._post_data(
            client,
            filters={"product_search": {
                "operator": "has",
                "value": "nonexistent' OR 1=1 -- ",
            }},
        )
        assert resp.status_code == 200
        body = self._parse_response(resp)
        assert body["totals"]["total_orders"] == 0  # must NOT match all 3 rows

    def test_post_data_empty_body_allowed(self, client, csv_file, dashboard_yaml):
        """POST /data with an entirely empty body is valid (no filters)."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = client.post("/api/v1/dashboards/api_test_dash/data", json={})
        assert resp.status_code == 200

    def test_post_data_query_param_filter_applied(
        self, client, csv_file, dashboard_yaml
    ):
        """Simple filter as URL query param f[widget_id]=value is applied."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = client.post(
            "/api/v1/dashboards/api_test_dash/data?f[product_search]=apple",
            json={},
        )
        assert resp.status_code == 200
        body = self._parse_response(resp)
        assert body["totals"]["total_orders"] == 2

    def test_post_data_query_param_overrides_body_filter(
        self, client, csv_file, dashboard_yaml
    ):
        """Query-param filter is pinned/locked — it wins over a body filter
        with the same key, since it represents a shareable link's fixed
        filter that must not be silently overridable by widget state."""
        self._setup(client, csv_file, dashboard_yaml)
        # Query param says apple, body says banana — apple should win (pinned)
        resp = client.post(
            "/api/v1/dashboards/api_test_dash/data?f[product_search]=apple",
            json={"filters": {"product_search": "banana"}},
        )
        assert resp.status_code == 200
        body = self._parse_response(resp)
        assert body["totals"]["total_orders"] == 2  # apple: 2 rows


# ---------------------------------------------------------------------------
# Table pagination — POST /{dashboard_id}/widgets/{widget_id}/page
# ---------------------------------------------------------------------------

class TestTablePaginationAPI:
    """Tests for POST /api/v1/dashboards/{id}/widgets/{widget_id}/page."""

    _PAGE_URL = "/api/v1/dashboards/api_test_dash/widgets/{widget_id}/page"

    def _setup(self, client, csv_file, dashboard_yaml):
        """Register data source and load dashboard."""
        client.post("/api/v1/system/data-sources/csv", json={
            "name": "orders",
            "title": "Orders",
            "filepath": csv_file,
        })
        client.post("/api/v1/dashboards/load", json={"filepath": dashboard_yaml})

    def _parse_response(self, resp):
        """Decompress gzip response if needed and return parsed JSON."""
        content = resp.content
        try:
            return json.loads(content)
        except Exception:
            return json.loads(gzip.decompress(content))

    def test_basic_page_returns_200(self, client, csv_file, dashboard_yaml):
        """POST /page returns 200 with rows for a valid data widget."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = client.post(
            self._PAGE_URL.format(widget_id="table_products"),
            json={"limit": 10, "offset": 0},
        )
        assert resp.status_code == 200
        body = self._parse_response(resp)
        assert body["widget_id"] == "table_products"
        assert body["dashboard_id"] == "api_test_dash"
        assert body["data_key"] == "by_product"
        assert isinstance(body["rows"], list)
        assert "has_more" in body
        assert isinstance(body["total_count"], int)

    def test_page_offset_zero_returns_all_rows(self, client, csv_file, dashboard_yaml):
        """offset=0 with large limit returns all aggregate rows."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = client.post(
            self._PAGE_URL.format(widget_id="table_products"),
            json={"limit": 100, "offset": 0},
        )
        assert resp.status_code == 200
        body = self._parse_response(resp)
        # 2 distinct products (apple, banana) from 3 orders rows, GROUP BY product
        assert len(body["rows"]) == 2
        assert body["total_count"] == 2
        assert body["has_more"] is False  # fewer rows than limit=100

    def test_page_offset_beyond_data_returns_empty(self, client, csv_file, dashboard_yaml):
        """offset beyond total rows returns empty rows list, but total_count still reflects the real total."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = client.post(
            self._PAGE_URL.format(widget_id="table_products"),
            json={"limit": 10, "offset": 9999},
        )
        assert resp.status_code == 200
        body = self._parse_response(resp)
        assert body["rows"] == []
        assert body["total_count"] == 2
        assert body["has_more"] is False

    def test_total_count_consistent_across_pages(self, client, csv_file, dashboard_yaml):
        """total_count is the same regardless of which page is requested."""
        self._setup(client, csv_file, dashboard_yaml)
        first = self._parse_response(client.post(
            self._PAGE_URL.format(widget_id="table_products"),
            json={"limit": 1, "offset": 0},
        ))
        second = self._parse_response(client.post(
            self._PAGE_URL.format(widget_id="table_products"),
            json={"limit": 1, "offset": 1},
        ))
        assert first["total_count"] == second["total_count"] == 2
        assert first["has_more"] is True   # offset(0) + len(rows)(1) < total_count(2)
        assert second["has_more"] is False  # offset(1) + len(rows)(1) == total_count(2)

    def test_page_with_filter_applied(self, client, csv_file, dashboard_yaml):
        """Filters are applied before pagination — only matching rows returned."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = client.post(
            self._PAGE_URL.format(widget_id="table_products"),
            json={
                "filters": {"product_search": "apple"},
                "limit": 10,
                "offset": 0,
            },
        )
        assert resp.status_code == 200
        body = self._parse_response(resp)
        # by_product aggregated — apple should be the only group
        products = [r.get("product", r.get("Product", "")) for r in body["rows"]]
        assert all("apple" in p.lower() for p in products)

    def test_has_more_true_when_limit_exactly_filled(self, client, csv_file, dashboard_yaml):
        """has_more=True when returned rows == limit."""
        self._setup(client, csv_file, dashboard_yaml)
        # 2 distinct products (apple, banana); limit=1 should trigger has_more
        resp = client.post(
            self._PAGE_URL.format(widget_id="table_products"),
            json={"limit": 1, "offset": 0},
        )
        assert resp.status_code == 200
        body = self._parse_response(resp)
        assert len(body["rows"]) == 1
        assert body["total_count"] == 2
        assert body["has_more"] is True

    def test_limit_and_offset_reflected_in_response(self, client, csv_file, dashboard_yaml):
        """Response echoes back the limit and offset values."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = client.post(
            self._PAGE_URL.format(widget_id="table_products"),
            json={"limit": 42, "offset": 7},
        )
        assert resp.status_code == 200
        body = self._parse_response(resp)
        assert body["limit"] == 42
        assert body["offset"] == 7

    def test_filter_widget_rejected_with_400(self, client, csv_file, dashboard_yaml):
        """Requesting page for a filter widget returns 400."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = client.post(
            self._PAGE_URL.format(widget_id="product_search"),
            json={"limit": 10, "offset": 0},
        )
        assert resp.status_code == 400
        assert "filter widget" in resp.json()["detail"].lower()

    def test_tile_widget_rejected_with_400(self, client, csv_file, dashboard_yaml):
        """Requesting page for a tile widget (no 'data' field) returns 400."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = client.post(
            self._PAGE_URL.format(widget_id="tile_total"),
            json={"limit": 10, "offset": 0},
        )
        assert resp.status_code == 400

    def test_unknown_widget_returns_400(self, client, csv_file, dashboard_yaml):
        """Widget ID not in layout returns 400."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = client.post(
            self._PAGE_URL.format(widget_id="ghost_widget"),
            json={"limit": 10, "offset": 0},
        )
        assert resp.status_code == 400

    def test_unknown_dashboard_returns_404(self, client):
        """Non-existent dashboard returns 404."""
        resp = client.post(
            "/api/v1/dashboards/ghost_dash/widgets/table_1/page",
            json={"limit": 10, "offset": 0},
        )
        assert resp.status_code == 404

    def test_invalid_dashboard_id_returns_422(self, client):
        """Dashboard ID with special characters returns 422."""
        resp = client.post(
            "/api/v1/dashboards/bad!id/widgets/table_1/page",
            json={"limit": 10, "offset": 0},
        )
        assert resp.status_code == 422

    def test_invalid_widget_id_returns_422(self, client, csv_file, dashboard_yaml):
        """Widget ID with special characters returns 422."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = client.post(
            self._PAGE_URL.format(widget_id="bad!widget"),
            json={"limit": 10, "offset": 0},
        )
        assert resp.status_code == 422

    def test_limit_too_large_returns_422(self, client, csv_file, dashboard_yaml):
        """limit > 5000 is rejected by Pydantic validation."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = client.post(
            self._PAGE_URL.format(widget_id="table_products"),
            json={"limit": 9999, "offset": 0},
        )
        assert resp.status_code == 422

    def test_empty_body_uses_defaults(self, client, csv_file, dashboard_yaml):
        """Empty body uses limit=100, offset=0 defaults."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = client.post(
            self._PAGE_URL.format(widget_id="table_products"),
            json={},
        )
        assert resp.status_code == 200
        body = self._parse_response(resp)
        assert body["limit"] == 100
        assert body["offset"] == 0

    def test_response_header_x_cache_bypass(self, client, csv_file, dashboard_yaml):
        """X-Cache: BYPASS header confirms Redis was not consulted."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = client.post(
            self._PAGE_URL.format(widget_id="table_products"),
            json={"limit": 10, "offset": 0},
        )
        assert resp.status_code == 200
        assert resp.headers.get("X-Cache") == "BYPASS"


# ---------------------------------------------------------------------------
# CSV Export — POST /{dashboard_id}/widgets/{widget_id}/export
# ---------------------------------------------------------------------------

class TestTableExportAPI:
    """Tests for POST /api/v1/dashboards/{id}/widgets/{widget_id}/export."""

    _EXPORT_URL = "/api/v1/dashboards/api_test_dash/widgets/{widget_id}/export"

    def _setup(self, client, csv_file, dashboard_yaml):
        """Register data source and load dashboard."""
        client.post("/api/v1/system/data-sources/csv", json={
            "name": "orders",
            "title": "Orders",
            "filepath": csv_file,
        })
        client.post("/api/v1/dashboards/load", json={"filepath": dashboard_yaml})

    def test_basic_export_returns_200_and_csv(self, client, csv_file, dashboard_yaml):
        """POST /export returns 200 with text/csv."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = client.post(
            self._EXPORT_URL.format(widget_id="table_products"),
            json={},
        )
        assert resp.status_code == 200
        assert resp.headers.get("Content-Type") == "text/csv; charset=utf-8"
        
        # Check attachment header
        cd = resp.headers.get("Content-Disposition")
        assert cd and 'attachment; filename="export_api_test_dash_table_products.csv"' in cd
        
        # Check CSV content
        lines = resp.text.strip().split("\n")
        assert len(lines) >= 2  # header + at least one row
        assert "product,revenue" in lines[0] or "product,amount" in lines[0] or "revenue" in lines[0]

    def test_export_with_filter_applied(self, client, csv_file, dashboard_yaml):
        """Filters are applied before export."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = client.post(
            self._EXPORT_URL.format(widget_id="table_products"),
            json={
                "filters": {"product_search": "apple"},
            },
        )
        assert resp.status_code == 200
        content = resp.text.lower()
        assert "apple" in content
        assert "banana" not in content

    def test_export_filter_widget_rejected(self, client, csv_file, dashboard_yaml):
        """Requesting export for a filter widget returns 400."""
        self._setup(client, csv_file, dashboard_yaml)
        resp = client.post(
            self._EXPORT_URL.format(widget_id="product_search"),
            json={},
        )
        assert resp.status_code == 400
        assert "filter widget" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# SQL API tests
# ---------------------------------------------------------------------------

class TestSQLAPI:
    """Tests for POST /api/v1/system/sql."""

    @pytest.fixture(autouse=True)
    def _enable_sql_api(self):
        """sql_api_enabled defaults to False; opt in for this class's tests."""
        from app.core.general_settings import general_settings
        original_state = general_settings.sql_api_enabled
        general_settings.sql_api_enabled = True
        yield
        general_settings.sql_api_enabled = original_state

    def test_sql_select_success(self, client):
        """A simple SELECT query works and returns compressed TSV."""
        resp = client.post(
            "/api/v1/system/sql",
            json={"query": "SELECT 1 as num"},
            headers={"Accept-Encoding": "gzip"}
        )
        assert resp.status_code == 200
        # httpx TestClient might automatically decompress the response based on headers
        try:
            content = gzip.decompress(resp.content).decode("utf-8")
        except OSError:
            # Already decompressed
            content = resp.content.decode("utf-8")
        
        lines = content.strip().split("\n")
        assert len(lines) == 2
        assert lines[0] == "num"
        assert lines[1] == "1"

    def test_sql_non_select_fails(self, client):
        """Non-SELECT queries are rejected."""
        resp = client.post(
            "/api/v1/system/sql",
            json={"query": "DROP TABLE dummy"},
        )
        assert resp.status_code == 400
        assert "Only SELECT queries are allowed" in resp.json()["detail"]

    def test_sql_stacked_statement_rejected_and_not_executed(self, client):
        """A SELECT followed by a chained statement is rejected outright, and the
        chained statement never runs (regression test for the DROP-TABLE bypass)."""
        con = get_db()
        con.execute("CREATE TABLE marker AS SELECT 1 AS x")

        resp = client.post(
            "/api/v1/system/sql",
            json={"query": "SELECT 1; DROP TABLE marker"},
        )
        assert resp.status_code == 400
        assert "Exactly one SQL statement is allowed" in resp.json()["detail"]

        # The DROP must never have executed.
        assert con.execute("SELECT * FROM marker").fetchall() == [(1,)]

    def test_sql_file_read_functions_rejected(self, client):
        """SELECT queries calling file-reading table functions are rejected,
        even though a plain SELECT prefix check would let them through."""
        for query in [
            "SELECT * FROM read_csv_auto('/etc/passwd')",
            "select * from READ_PARQUET('/app/data/x.parquet')",
            "SELECT * FROM read_json_auto('/etc/shadow')",
            "SELECT * FROM glob('/etc/*')",
        ]:
            resp = client.post("/api/v1/system/sql", json={"query": query})
            assert resp.status_code == 400, query
            assert "disallowed file-reading function" in resp.json()["detail"]

    def test_sql_no_forced_row_limit(self, client):
        """Results are no longer capped at 20 rows; more than 20 rows come back."""
        resp = client.post(
            "/api/v1/system/sql",
            json={"query": "SELECT * FROM range(30) AS t(num)"},
        )
        assert resp.status_code == 200
        try:
            content = gzip.decompress(resp.content).decode("utf-8")
        except OSError:
            content = resp.content.decode("utf-8")
        lines = content.strip().split("\n")
        assert len(lines) == 31  # header + 30 rows

    def test_sql_user_supplied_limit_respected(self, client):
        """A user-written LIMIT clause is honored as-is, not stripped/replaced."""
        resp = client.post(
            "/api/v1/system/sql",
            json={"query": "SELECT * FROM range(30) AS t(num) LIMIT 5"},
        )
        assert resp.status_code == 200
        try:
            content = gzip.decompress(resp.content).decode("utf-8")
        except OSError:
            content = resp.content.decode("utf-8")
        lines = content.strip().split("\n")
        assert len(lines) == 6  # header + 5 rows

    def test_sql_api_disabled_fails(self, client):
        """When sql_api_enabled is False, returns 403."""
        from app.core.general_settings import general_settings
        # Temporarily disable the API
        original_state = general_settings.sql_api_enabled
        general_settings.sql_api_enabled = False
        try:
            resp = client.post(
                "/api/v1/system/sql",
                json={"query": "SELECT 1 as num"},
            )
            assert resp.status_code == 403
            assert "SQL API is disabled" in resp.json()["detail"]
        finally:
            general_settings.sql_api_enabled = original_state
