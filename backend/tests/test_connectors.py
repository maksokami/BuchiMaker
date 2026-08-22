"""
Unit tests: connectors/base.py

Tests for CSVConnector and JSONConnector using real DuckDB in-memory
connections (no mocks for DuckDB itself – it's fast enough).
BigQueryConnector is tested for its ImportError guard only.
"""

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from app.connectors.base import (
    BigQueryConnector,
    CSVConnector,
    ConnectorRegistry,
    JSONConnector,
    ParquetConnector,
)
from app.core.db import close_db, get_db


@pytest.fixture(autouse=True)
def fresh_db():
    """Ensure a clean DuckDB state between tests."""
    close_db()
    yield
    close_db()


# ---------------------------------------------------------------------------
# CSVConnector
# ---------------------------------------------------------------------------

class TestCSVConnector:
    """Tests for CSVConnector."""

    def test_load_valid_csv(self, tmp_path):
        """A valid CSV file is registered as a DuckDB view."""
        csv_file = tmp_path / "sales.csv"
        csv_file.write_text("id,amount\n1,100\n2,200\n")

        connector = CSVConnector(name="sales", title="Sales", filepath=str(csv_file))
        connector.load()

        assert connector.last_updated > 0
        rows = get_db().execute("SELECT COUNT(*) FROM sales").fetchone()
        assert rows[0] == 2

    def test_load_missing_file_raises(self, tmp_path):
        """FileNotFoundError is raised for a missing CSV file."""
        connector = CSVConnector(
            name="missing", title="Missing", filepath=str(tmp_path / "nope.csv")
        )
        with pytest.raises(FileNotFoundError, match="nope.csv"):
            connector.load()

    def test_source_type_tag(self):
        """CSVConnector has source_type = 'csv'."""
        assert CSVConnector.source_type == "csv"

    def test_to_info_returns_dict(self, tmp_path):
        """to_info() returns a DataSourceInfo-compatible dict."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("x\n1\n")
        connector = CSVConnector(name="data", title="Data", filepath=str(csv_file))
        connector.load()
        info = connector.to_info()
        assert info["source_type"] == "csv"
        assert info["name"] == "data"
        assert info["last_updated"] > 0

    def test_drop_removes_view(self, tmp_path):
        """drop() removes the DuckDB view."""
        csv_file = tmp_path / "d.csv"
        csv_file.write_text("v\n1\n")
        connector = CSVConnector(name="d", title="D", filepath=str(csv_file))
        connector.load()
        connector.drop()
        with pytest.raises(Exception):
            get_db().execute("SELECT * FROM d").fetchall()

    def test_filepath_with_apostrophe_loads_safely(self, tmp_path):
        """A legitimate path containing an apostrophe must not break the
        SQL statement (proves the fix doesn't regress valid inputs)."""
        csv_file = tmp_path / "o'brien.csv"
        csv_file.write_text("id,amount\n1,100\n")
        connector = CSVConnector(name="quoted_path", title="Quoted", filepath=str(csv_file))
        connector.load()
        rows = get_db().execute("SELECT COUNT(*) FROM quoted_path").fetchone()
        assert rows[0] == 1

    def test_filepath_injection_payload_does_not_execute_sql(self, tmp_path):
        """A filename crafted to look like a SQL injection payload must be
        bound as literal path data, never executed as SQL."""
        con = get_db()
        con.execute("CREATE TABLE decoy AS SELECT 1 AS untouched")

        payload_file = tmp_path / "x'); DROP TABLE decoy; --.csv"
        payload_file.write_text("id\n1\n")

        connector = CSVConnector(
            name="csv_injected", title="Injected", filepath=str(payload_file)
        )
        connector.load()

        assert con.execute("SELECT COUNT(*) FROM decoy").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM csv_injected").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# JSONConnector
# ---------------------------------------------------------------------------

class TestJSONConnector:
    """Tests for JSONConnector."""

    def test_load_valid_json(self, tmp_path):
        """A valid JSON file is registered as a DuckDB view."""
        json_file = tmp_path / "records.json"
        json_file.write_text(json.dumps([{"id": 1, "val": "a"}, {"id": 2, "val": "b"}]))

        connector = JSONConnector(
            name="records", title="Records", filepath=str(json_file)
        )
        connector.load()

        rows = get_db().execute("SELECT COUNT(*) FROM records").fetchone()
        assert rows[0] == 2

    def test_load_missing_file_raises(self, tmp_path):
        """FileNotFoundError is raised for a missing JSON file."""
        connector = JSONConnector(
            name="nope", title="Nope", filepath=str(tmp_path / "ghost.json")
        )
        with pytest.raises(FileNotFoundError, match="ghost.json"):
            connector.load()

    def test_source_type_tag(self):
        """JSONConnector has source_type = 'json'."""
        assert JSONConnector.source_type == "json"

    def test_filepath_with_apostrophe_loads_safely(self, tmp_path):
        """A legitimate path containing an apostrophe must not break the
        SQL statement (proves the fix doesn't regress valid inputs)."""
        json_file = tmp_path / "o'brien.json"
        json_file.write_text(json.dumps([{"id": 1}]))
        connector = JSONConnector(name="json_quoted", title="Quoted", filepath=str(json_file))
        connector.load()
        rows = get_db().execute("SELECT COUNT(*) FROM json_quoted").fetchone()
        assert rows[0] == 1

    def test_filepath_injection_payload_does_not_execute_sql(self, tmp_path):
        """A filename crafted to look like a SQL injection payload must be
        bound as literal path data, never executed as SQL."""
        con = get_db()
        con.execute("CREATE TABLE decoy_json AS SELECT 1 AS untouched")

        payload_file = tmp_path / "x'); DROP TABLE decoy_json; --.json"
        payload_file.write_text(json.dumps([{"id": 1}]))

        connector = JSONConnector(
            name="json_injected", title="Injected", filepath=str(payload_file)
        )
        connector.load()

        assert con.execute("SELECT COUNT(*) FROM decoy_json").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM json_injected").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# ParquetConnector
# ---------------------------------------------------------------------------

class TestParquetConnector:
    """Tests for ParquetConnector."""

    def test_load_valid_parquet(self, tmp_path):
        """A valid Parquet file is registered as a DuckDB view."""
        # Using DuckDB to create a dummy parquet file
        parquet_file = tmp_path / "records.parquet"
        con = get_db()
        con.execute(f"COPY (SELECT 1 AS id, 'a' AS val UNION ALL SELECT 2, 'b') TO '{parquet_file}' (FORMAT PARQUET)")

        connector = ParquetConnector(
            name="pq_records", title="PQ Records", filepath=str(parquet_file)
        )
        connector.load()

        rows = con.execute("SELECT COUNT(*) FROM pq_records").fetchone()
        assert rows[0] == 2

    def test_source_type_tag(self):
        """ParquetConnector has source_type = 'parquet'."""
        assert ParquetConnector.source_type == "parquet"

    def test_filepath_with_apostrophe_loads_safely(self, tmp_path):
        """A legitimate path containing an apostrophe must not break the
        SQL statement (proves the fix doesn't regress valid inputs)."""
        con = get_db()
        base_file = tmp_path / "base.parquet"
        con.execute(
            f"COPY (SELECT 1 AS id, 'a' AS val) TO '{base_file}' (FORMAT PARQUET)"
        )
        quoted_file = tmp_path / "o'brien.parquet"
        shutil.copy(base_file, quoted_file)

        connector = ParquetConnector(
            name="pq_quoted", title="Quoted", filepath=str(quoted_file)
        )
        connector.load()
        rows = con.execute("SELECT COUNT(*) FROM pq_quoted").fetchone()
        assert rows[0] == 1

    def test_filepath_injection_payload_does_not_execute_sql(self, tmp_path):
        """A filename crafted to look like a SQL injection payload must be
        bound as literal path data, never executed as SQL."""
        con = get_db()
        con.execute("CREATE TABLE decoy_pq AS SELECT 1 AS untouched")

        base_file = tmp_path / "base2.parquet"
        con.execute(
            f"COPY (SELECT 1 AS id, 'a' AS val) TO '{base_file}' (FORMAT PARQUET)"
        )
        payload_file = tmp_path / "x'); DROP TABLE decoy_pq; --.parquet"
        shutil.copy(base_file, payload_file)

        connector = ParquetConnector(
            name="pq_injected", title="Injected", filepath=str(payload_file)
        )
        connector.load()

        assert con.execute("SELECT COUNT(*) FROM decoy_pq").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM pq_injected").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# BigQueryConnector – offline guard
# ---------------------------------------------------------------------------

class TestBigQueryConnectorOffline:
    """Tests for BigQueryConnector without live GCP access."""

    def test_load_raises_import_error_without_deps(self, monkeypatch):
        """Should raise ImportError if dependencies are missing."""
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if "google" in name:
                raise ImportError("No module named 'google'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        connector = BigQueryConnector(
            name="bq_test",
            title="BQ",
            project_id="proj",
            dataset_id="ds",
            table_id="tbl",
            credentials_path="/fake/creds.json",
        )
        with pytest.raises(ImportError, match="Missing bigquery dependency"):
            connector.load()

    def test_default_query_built_correctly(self):
        """Default query includes project, dataset, and table."""
        connector = BigQueryConnector(
            name="bq",
            title="BQ",
            project_id="my_proj",
            dataset_id="my_ds",
            table_id="my_tbl",
            credentials_path="/creds.json",
        )
        assert "my_proj.my_ds.my_tbl" in connector.query


# ---------------------------------------------------------------------------
# ConnectorRegistry
# ---------------------------------------------------------------------------

class TestConnectorRegistry:
    """Tests for ConnectorRegistry."""

    def test_built_in_types_registered(self):
        """csv, json, and bigquery are registered by default."""
        types = ConnectorRegistry.list_types()
        assert "csv" in types
        assert "json" in types
        assert "parquet" in types
        assert "bigquery" in types

    def test_get_returns_class(self):
        """get() returns the connector class for a known type."""
        cls = ConnectorRegistry.get("csv")
        assert cls is CSVConnector

    def test_get_returns_none_for_unknown(self):
        """get() returns None for an unregistered type."""
        cls = ConnectorRegistry.get("oracle")
        assert cls is None


# ---------------------------------------------------------------------------
# BaseConnector – identifier validation (defense in depth)
# ---------------------------------------------------------------------------

class TestConnectorNameValidation:
    """``name`` becomes a raw SQL identifier, so it must be validated at
    construction time even when a connector is built outside the API's
    Pydantic layer (e.g. restored from a saved config file)."""

    def test_rejects_sql_injection_in_name(self, tmp_path):
        csv_file = tmp_path / "ok.csv"
        csv_file.write_text("id\n1\n")
        with pytest.raises(ValueError):
            CSVConnector(
                name="sales; DROP TABLE users; --",
                title="Sales",
                filepath=str(csv_file),
            )

    def test_accepts_safe_name(self, tmp_path):
        csv_file = tmp_path / "ok2.csv"
        csv_file.write_text("id\n1\n")
        connector = CSVConnector(name="sales_2024", title="Sales", filepath=str(csv_file))
        assert connector.name == "sales_2024"


# ---------------------------------------------------------------------------
# File connectors – filepath sandboxing (defense in depth)
#
# The autouse `sandbox_data_dir` fixture (tests/conftest.py) points
# `settings.data_dir` at this test's own `tmp_path`, so paths inside
# `tmp_path` are "inside the data directory" and everything else is not.
# ---------------------------------------------------------------------------

class TestConnectorFilepathSandboxing:
    """``filepath`` must resolve inside the configured data directory —
    registering a source that points elsewhere on the container filesystem
    must be rejected, not just SQL-injection-safe."""

    def test_csv_rejects_path_outside_data_dir(self, tmp_path):
        outside_dir = Path(tempfile.mkdtemp())
        try:
            outside_file = outside_dir / "secret.csv"
            outside_file.write_text("id\n1\n")
            with pytest.raises(ValueError, match="data directory"):
                CSVConnector(name="secret", title="Secret", filepath=str(outside_file))
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)

    def test_json_rejects_path_outside_data_dir(self, tmp_path):
        outside_dir = Path(tempfile.mkdtemp())
        try:
            outside_file = outside_dir / "secret.json"
            outside_file.write_text(json.dumps([{"id": 1}]))
            with pytest.raises(ValueError, match="data directory"):
                JSONConnector(name="secret", title="Secret", filepath=str(outside_file))
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)

    def test_parquet_rejects_path_outside_data_dir(self, tmp_path):
        outside_dir = Path(tempfile.mkdtemp())
        try:
            outside_file = outside_dir / "secret.parquet"
            get_db().execute(
                f"COPY (SELECT 1 AS id) TO '{outside_file}' (FORMAT PARQUET)"
            )
            with pytest.raises(ValueError, match="data directory"):
                ParquetConnector(name="secret", title="Secret", filepath=str(outside_file))
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)

    def test_rejects_directory_traversal(self, tmp_path):
        """A `../` climb back into a sibling directory must be caught by
        path *resolution*, not a naive string-prefix check."""
        sibling_dir = tmp_path.parent / "sandbox_traversal_sibling"
        sibling_dir.mkdir(exist_ok=True)
        try:
            secret_file = sibling_dir / "secret.csv"
            secret_file.write_text("id\n1\n")
            traversal_path = tmp_path / ".." / sibling_dir.name / "secret.csv"
            with pytest.raises(ValueError, match="data directory"):
                CSVConnector(name="secret", title="Secret", filepath=str(traversal_path))
        finally:
            shutil.rmtree(sibling_dir, ignore_errors=True)

    def test_accepts_path_inside_data_dir(self, tmp_path):
        inside_file = tmp_path / "orders.csv"
        inside_file.write_text("id\n1\n")
        connector = CSVConnector(name="orders", title="Orders", filepath=str(inside_file))
        assert connector.filepath == str(inside_file)

    def test_accepts_nested_path_inside_data_dir(self, tmp_path):
        nested_dir = tmp_path / "nested"
        nested_dir.mkdir()
        inside_file = nested_dir / "orders.csv"
        inside_file.write_text("id\n1\n")
        connector = CSVConnector(name="orders", title="Orders", filepath=str(inside_file))
        assert connector.filepath == str(inside_file)
