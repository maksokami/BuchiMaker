"""
Unit tests: models/dashboard.py

Tests for Summary, Aggregate, Filter, and Dashboard domain objects.
Uses in-memory DuckDB with real test data (no mocks for DuckDB).
"""

import json
import tempfile
from pathlib import Path

import pytest

from app.core.db import close_db, get_db, get_standby_db
from app.models.dashboard import Aggregate, Dashboard, Filter, Summary


@pytest.fixture(autouse=True)
def fresh_db_and_table():
    """Reset DuckDB and create a test base table before each test."""
    close_db()
    con = get_db()
    con.execute("""
        CREATE TABLE test_data AS
        SELECT * FROM (VALUES
            (1, 'alpha', 100, TRUE),
            (2, 'beta',  200, FALSE),
            (3, 'alpha', 150, TRUE),
            (4, 'gamma', 50,  FALSE)
        ) t(id, category, amount, active)
    """)
    yield
    close_db()


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

class TestSummary:
    """Tests for the Summary class."""

    def test_get_data_scalar(self):
        """Query returning a single column yields a scalar value."""
        s = Summary(name="total", query="SELECT COUNT(*) FROM test_data")
        con = get_db()
        result = s.get_data(con)
        assert result == 4

    def test_get_data_key_value(self):
        """Query returning two columns yields a dict."""
        s = Summary(
            name="kv",
            query="SELECT 'count' AS k, COUNT(*) AS v FROM test_data",
        )
        con = get_db()
        result = s.get_data(con)
        assert isinstance(result, dict)

    def test_get_data_invalid_query_returns_none(self):
        """Bad SQL returns None without raising."""
        s = Summary(name="bad", query="SELECT * FROM nonexistent_table")
        con = get_db()
        result = s.get_data(con)
        assert result is None


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

class TestAggregate:
    """Tests for the Aggregate class."""

    def test_get_data_returns_rows(self):
        """Multi-row query returns list of dicts."""
        a = Aggregate(
            name="by_category",
            query="SELECT category, SUM(amount) AS total FROM test_data GROUP BY 1",
        )
        con = get_db()
        rows = a.get_data(con)
        assert isinstance(rows, list)
        assert len(rows) == 3  # alpha, beta, gamma

    def test_column_aliases_applied(self):
        """column_aliases remaps SQL column names to display labels."""
        a = Aggregate(
            name="aliased",
            query="SELECT category, COUNT(*) AS cnt FROM test_data GROUP BY 1",
            column_aliases={"category": "Category", "cnt": "Count"},
        )
        con = get_db()
        rows = a.get_data(con)
        assert "Category" in rows[0]
        assert "Count" in rows[0]

    def test_invalid_query_returns_empty_list(self):
        """Bad SQL returns [] without raising."""
        a = Aggregate(name="bad", query="SELECT * FROM ghost")
        con = get_db()
        result = a.get_data(con)
        assert result == []


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

class TestFilter:
    """Tests for the Filter class."""

    def test_refresh_creates_view(self):
        """refresh() creates a DuckDB view for the filter."""
        f = Filter(
            filter_conditions=[{"field": "active", "operator": "=", "value": "true"}],
            source_table="test_data",
            filter_hash="abc123",
        )
        f.refresh()
        assert f.is_loaded
        rows = get_db().execute(f"SELECT COUNT(*) FROM {f.view_name}").fetchone()
        assert rows[0] == 2  # active=TRUE rows

    def test_is_fresh_initially_true(self):
        """Newly refreshed filter is considered fresh."""
        f = Filter([], "test_data", "hash1", refresh_frequency=10)
        f.refresh()
        assert f.is_fresh()

    def test_get_data_returns_rows(self):
        """get_data() returns filtered rows as list of dicts."""
        f = Filter([], "test_data", "nofilt")
        f.refresh()
        result = f.get_data()
        assert len(result["rows"]) == 4
        assert not result["truncated"]

    def test_get_data_truncates_large_payload(self):
        """get_data() truncates when payload exceeds truncate_mb=0."""
        f = Filter([], "test_data", "trunc")
        f.refresh()
        result = f.get_data(truncate_mb=0)
        # With 0 MB limit everything is truncated
        assert result["truncated"]

    def test_drop_removes_view(self):
        """drop() removes the view and sets is_loaded=False."""
        f = Filter([], "test_data", "drop_test")
        f.refresh()
        f.drop()
        assert not f.is_loaded
        with pytest.raises(Exception):
            get_db().execute(f"SELECT 1 FROM {f.view_name}").fetchall()

    def test_refresh_against_explicit_connection(self):
        """refresh(con=...) rebuilds the table against the given connection
        instead of the active one — used by SystemManager.global_refresh() to
        keep the standby slot's cached filter tables in sync before promotion
        (see ADR-016 in docs/backend_architecture.md)."""
        standby = get_standby_db()
        standby.execute("""
            CREATE TABLE test_data AS
            SELECT * FROM (VALUES
                (1, 'alpha', 100, TRUE),
                (2, 'beta',  200, FALSE)
            ) t(id, category, amount, active)
        """)
        f = Filter(
            filter_conditions=[{"field": "active", "operator": "=", "value": "true"}],
            source_table="test_data",
            filter_hash="standby_test",
        )
        f.refresh(con=standby)
        assert f.is_loaded
        # Not visible on the active connection — it was only built on standby.
        with pytest.raises(Exception):
            get_db().execute(f"SELECT 1 FROM {f.view_name}").fetchall()
        rows = standby.execute(f"SELECT COUNT(*) FROM {f.view_name}").fetchone()
        assert rows[0] == 1


# ---------------------------------------------------------------------------
# Dashboard (YAML loading)
# ---------------------------------------------------------------------------

class TestDashboard:
    """Tests for Dashboard.load_yaml() and related methods."""

    def _write_yaml(self, path: Path, content: str) -> str:
        """Helper to write a YAML file and return its path string."""
        path.write_text(content)
        return str(path)

    def test_load_valid_yaml(self, tmp_path):
        """A valid YAML file is parsed and dashboard attributes are set."""
        yaml_content = """
id: dash_001
title: Test Dashboard
base_view: "CREATE OR REPLACE VIEW dash_001_base AS SELECT * FROM test_data;"
totals:
  - name: total_count
    query: "SELECT COUNT(*) FROM dash_001_base"
aggregates:
  - name: by_cat
    query: "SELECT category, COUNT(*) FROM dash_001_base GROUP BY 1"
"""
        filepath = self._write_yaml(tmp_path / "dash.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)

        assert error == ""
        assert dash.id == "dash_001"
        assert dash.name == "Test Dashboard"
        assert len(dash.summaries) == 1
        assert len(dash.aggregates) == 1

    def test_load_missing_file(self, tmp_path):
        """Missing YAML file returns an error string."""
        dash = Dashboard()
        error = dash.load_yaml(str(tmp_path / "ghost.yaml"))
        assert "not found" in error.lower()

    def test_load_invalid_yaml_syntax(self, tmp_path):
        """Malformed YAML returns a user-friendly error with line info."""
        bad_yaml = "id: [unclosed bracket\ntitle: X"
        filepath = self._write_yaml(tmp_path / "bad.yaml", bad_yaml)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert error != ""
        assert "yaml" in error.lower() or "line" in error.lower()

    def test_load_rejects_duplicate_widget_id(self, tmp_path):
        """Two layout widgets sharing the same id are rejected, not silently loaded."""
        yaml_content = """
id: dash_dupe
title: Duplicate Widget Test
base_view: "CREATE OR REPLACE VIEW dash_dupe_base AS SELECT * FROM test_data;"
aggregates:
  - name: by_cat
    query: "SELECT category, COUNT(*) FROM dash_dupe_base GROUP BY 1"
layout:
  - bar_chart:
      id: dup_id
      title: "First"
      data: by_cat
      grid: {x: 0, y: 0, w: 4, h: 2}
  - bar_chart:
      id: dup_id
      title: "Second"
      data: by_cat
      grid: {x: 4, y: 0, w: 4, h: 2}
"""
        filepath = self._write_yaml(tmp_path / "dupe.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)

        assert error != ""
        assert "duplicate" in error.lower()
        assert "dup_id" in error

    def test_set_filters_creates_filter_object(self, tmp_path):
        """set_filters() creates a cached Filter and returns it."""
        yaml_content = """
id: dash_filt
title: Filter Test
base_view: "CREATE OR REPLACE VIEW dash_filt_base AS SELECT * FROM test_data;"
"""
        filepath = self._write_yaml(tmp_path / "f.yaml", yaml_content)
        dash = Dashboard()
        dash.load_yaml(filepath)

        f = dash.set_filters([{"field": "active", "operator": "=", "value": "true"}])
        assert f.filter_hash in dash.filters
        assert f.is_loaded

    def test_set_filters_deduplicates(self, tmp_path):
        """Same filter conditions reuse the existing Filter object."""
        yaml_content = (
            "id: d\ntitle: D\n"
            'base_view: "CREATE OR REPLACE VIEW d_base AS SELECT * FROM test_data;"\n'
        )
        filepath = self._write_yaml(tmp_path / "d.yaml", yaml_content)
        dash = Dashboard()
        dash.load_yaml(filepath)

        conditions = [{"field": "amount", "operator": ">", "value": "100"}]
        f1 = dash.set_filters(conditions)
        f2 = dash.set_filters(conditions)
        assert f1 is f2

    def test_trim_filters_removes_stale(self, tmp_path):
        """trim_filters() evicts filters not queried within the window."""
        yaml_content = (
            "id: d2\ntitle: D2\n"
            'base_view: "CREATE OR REPLACE VIEW d2_base AS SELECT * FROM test_data;"\n'
        )
        filepath = self._write_yaml(tmp_path / "d2.yaml", yaml_content)
        dash = Dashboard()
        dash.load_yaml(filepath)
        dash.settings["unallocate_frequency_filter"] = 0  # evict immediately

        dash.set_filters([])
        assert len(dash.filters) == 1

        count = dash.trim_filters()
        assert count == 1
        assert len(dash.filters) == 0

    def test_widget_count(self, tmp_path):
        """widget_count() returns the sum of summaries and aggregates."""
        yaml_content = """
id: wc
title: Widget Count
base_view: "CREATE OR REPLACE VIEW wc_base AS SELECT * FROM test_data;"
totals:
  - name: t1
    query: "SELECT COUNT(*) FROM wc_base"
  - name: t2
    query: "SELECT COUNT(*) FROM wc_base"
aggregates:
  - name: a1
    query: "SELECT COUNT(*) FROM wc_base"
"""
        filepath = self._write_yaml(tmp_path / "wc.yaml", yaml_content)
        dash = Dashboard()
        dash.load_yaml(filepath)
        assert dash.widget_count() == 3

    def test_load_mappings_and_layout(self, tmp_path):
        """Mappings and layout placeholders are correctly loaded."""
        yaml_content = """
id: dash_layout
title: Layout Dash
base_view: "CREATE OR REPLACE VIEW dash_layout_base AS SELECT * FROM test_data;"
mappings:
  - "filter_dpm": "dpm->>'$.id'"
  - "test2": "col3"
layout:
  - tile:
      id: title_1
      value: "my_total"
  - basic_table:
      id: table_1
      data: "my_aggr"
  - dropdown_single:
      id: combo_1
      mapping: "test2"
totals:
  - name: my_total
    query: "SELECT COUNT(*) FROM dash_layout_base"
aggregates:
  - name: my_aggr
    query: "SELECT COUNT(*) FROM dash_layout_base"
"""
        filepath = self._write_yaml(tmp_path / "layout.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert error == ""
        assert dash.mappings == {"filter_dpm": "dpm->>'$.id'", "test2": "col3"}
        assert len(dash.layout) == 3
        assert dash.layout[0]["type"] == "tile"
        assert dash.layout[0]["id"] == "title_1"
        assert dash.layout[1]["type"] == "basic_table"
        assert dash.layout[1]["config"]["data"] == "my_aggr"

    def test_load_layout_tile_validation(self, tmp_path):
        """Tile widgets reject aggregates."""
        yaml_content = """
id: dash_tile_err
title: Error Dash
base_view: "CREATE OR REPLACE VIEW dash_tile_err_base AS SELECT * FROM test_data;"
layout:
  - tile:
      id: title_1
      value: "my_aggr"
aggregates:
  - name: my_aggr
    query: "SELECT COUNT(*) FROM dash_tile_err_base"
"""
        filepath = self._write_yaml(tmp_path / "tile_err.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert "Validation error" in error
        assert "cannot use aggregate" in error

    def test_load_layout_basic_table_validation(self, tmp_path):
        """Basic table widgets reject totals."""
        yaml_content = """
id: dash_table_err
title: Error Dash
base_view: "CREATE OR REPLACE VIEW dash_table_err_base AS SELECT * FROM test_data;"
layout:
  - basic_table:
      id: table_1
      data: "my_total"
totals:
  - name: my_total
    query: "SELECT COUNT(*) FROM dash_table_err_base"
"""
        filepath = self._write_yaml(tmp_path / "table_err.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert "Validation error" in error
        assert "cannot use total" in error

    def test_load_layout_sankey_validation_data(self, tmp_path):
        """Sankey widgets reject totals in data field."""
        yaml_content = """
id: dash_sankey_err_data
title: Error Dash
base_view: "CREATE OR REPLACE VIEW dash_sankey_err_data_base AS SELECT * FROM test_data;"
layout:
  - sankey:
      id: sankey_1
      data: "my_total"
totals:
  - name: my_total
    query: "SELECT COUNT(*) FROM dash_sankey_err_data_base"
"""
        filepath = self._write_yaml(tmp_path / "sankey_err1.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert "Validation error" in error
        assert "cannot use total" in error

    def test_load_layout_horizontal_bar_chart_validation(self, tmp_path):
        """Horizontal bar chart widgets reject totals in data field."""
        yaml_content = """
id: dash_hbar_err
title: Error Dash
base_view: "CREATE OR REPLACE VIEW dash_hbar_err_base AS SELECT * FROM test_data;"
layout:
  - horizontal_bar_chart:
      id: hbar_1
      data: "my_total"
totals:
  - name: my_total
    query: "SELECT COUNT(*) FROM dash_hbar_err_base"
"""
        filepath = self._write_yaml(tmp_path / "hbar_err.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert "Validation error" in error
        assert "cannot use total" in error

    def test_load_layout_stacked_bar_chart_validation(self, tmp_path):
        """Stacked bar chart widgets reject totals in data field."""
        yaml_content = """
id: dash_sbar_err
title: Error Dash
base_view: "CREATE OR REPLACE VIEW dash_sbar_err_base AS SELECT * FROM test_data;"
layout:
  - stacked_bar_chart:
      id: sbar_1
      data: "my_total"
totals:
  - name: my_total
    query: "SELECT COUNT(*) FROM dash_sbar_err_base"
"""
        filepath = self._write_yaml(tmp_path / "sbar_err.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert "Validation error" in error
        assert "cannot use total" in error

    def test_load_layout_dropdown_single_validation_data(self, tmp_path):
        """dropdown_single rejects a total in its `data` (option-list) field."""
        yaml_content = """
id: dash_dd_single_err
title: Error Dash
base_view: "CREATE OR REPLACE VIEW dash_dd_single_err_base AS SELECT * FROM test_data;"
layout:
  - dropdown_single:
      id: dd_1
      mapping: "cat_alias"
      data: "my_total"
totals:
  - name: my_total
    query: "SELECT COUNT(*) FROM dash_dd_single_err_base"
"""
        filepath = self._write_yaml(tmp_path / "dd_single_err.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert "Validation error" in error
        assert "cannot use total" in error

    def test_load_layout_dropdown_multy_validation_data(self, tmp_path):
        """dropdown_multy rejects a total in its `data` (option-list) field."""
        yaml_content = """
id: dash_dd_multy_err
title: Error Dash
base_view: "CREATE OR REPLACE VIEW dash_dd_multy_err_base AS SELECT * FROM test_data;"
layout:
  - dropdown_multy:
      id: dd_1
      mapping: "cat_alias"
      data: "my_total"
totals:
  - name: my_total
    query: "SELECT COUNT(*) FROM dash_dd_multy_err_base"
"""
        filepath = self._write_yaml(tmp_path / "dd_multy_err.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert "Validation error" in error
        assert "cannot use total" in error


# ---------------------------------------------------------------------------
# Dashboard – base_view requirement
# ---------------------------------------------------------------------------

class TestDashboardBaseView:
    """Tests for the required `base_view` YAML field."""

    def _write_yaml(self, path: Path, content: str) -> str:
        path.write_text(content)
        return str(path)

    def test_base_view_required(self, tmp_path):
        """A dashboard with no base_view key is rejected."""
        yaml_content = """
id: no_base_view
title: No Base View
totals:
  - name: t1
    query: "SELECT COUNT(*) FROM test_data"
"""
        filepath = self._write_yaml(tmp_path / "nbv.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert "Validation error" in error
        assert "base_view" in error

    def test_base_view_empty_string_rejected(self, tmp_path):
        """An empty base_view value is treated the same as a missing one."""
        yaml_content = """
id: empty_base_view
title: Empty Base View
base_view: ""
"""
        filepath = self._write_yaml(tmp_path / "ebv.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert "Validation error" in error
        assert "base_view" in error

    def test_base_view_must_precede_aggregates(self, tmp_path):
        """base_view declared after `aggregates` in the file is rejected."""
        yaml_content = """
id: bv_after_agg
title: Base View After Aggregates
aggregates:
  - name: a1
    query: "SELECT COUNT(*) FROM bv_after_agg_base"
base_view: "CREATE OR REPLACE VIEW bv_after_agg_base AS SELECT * FROM test_data;"
"""
        filepath = self._write_yaml(tmp_path / "bv_after_agg.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert "Validation error" in error
        assert "base_view" in error
        assert "aggregates" in error

    def test_base_view_must_precede_totals(self, tmp_path):
        """base_view declared after `totals` in the file is rejected."""
        yaml_content = """
id: bv_after_tot
title: Base View After Totals
totals:
  - name: t1
    query: "SELECT COUNT(*) FROM bv_after_tot_base"
base_view: "CREATE OR REPLACE VIEW bv_after_tot_base AS SELECT * FROM test_data;"
"""
        filepath = self._write_yaml(tmp_path / "bv_after_tot.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert "Validation error" in error
        assert "base_view" in error
        assert "totals" in error

    def test_base_view_must_be_create_view_statement(self, tmp_path):
        """A base_view that isn't a CREATE [OR REPLACE] VIEW statement is rejected."""
        yaml_content = """
id: bv_not_ddl
title: Not A View Statement
base_view: "SELECT * FROM test_data"
"""
        filepath = self._write_yaml(tmp_path / "bv_not_ddl.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert "Validation error" in error
        assert "CREATE" in error

    def test_base_view_creates_duckdb_view_and_sets_source_table(self, tmp_path):
        """A valid base_view is executed against DuckDB and becomes source_table."""
        yaml_content = """
id: bv_ok
title: Base View OK
base_view: "CREATE OR REPLACE VIEW bv_ok_base AS SELECT * FROM test_data;"
"""
        filepath = self._write_yaml(tmp_path / "bv_ok.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert error == ""
        assert dash.source_table == "bv_ok_base"
        rows = get_db().execute("SELECT COUNT(*) FROM bv_ok_base").fetchone()
        assert rows[0] == 4

    def test_base_view_lowercase_create_view_supported(self, tmp_path):
        """Lowercase 'create view' (without OR REPLACE) is also accepted."""
        yaml_content = """
id: bv_lower
title: Lowercase Create View
base_view: "create view bv_lower_base as select * from test_data;"
"""
        filepath = self._write_yaml(tmp_path / "bv_lower.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert error == ""
        assert dash.source_table == "bv_lower_base"

    def test_total_not_using_base_view_rejected(self, tmp_path):
        """A total that queries the raw table instead of the view is rejected."""
        yaml_content = """
id: bv_total_bad
title: Total Bypasses Base View
base_view: "CREATE OR REPLACE VIEW bv_total_bad_base AS SELECT * FROM test_data;"
totals:
  - name: bad_total
    query: "SELECT COUNT(*) FROM test_data"
"""
        filepath = self._write_yaml(tmp_path / "bv_total_bad.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert "Validation error" in error
        assert "bad_total" in error
        assert "bv_total_bad_base" in error

    def test_aggregate_not_using_base_view_rejected(self, tmp_path):
        """An aggregate that queries the raw table instead of the view is rejected."""
        yaml_content = """
id: bv_agg_bad
title: Aggregate Bypasses Base View
base_view: "CREATE OR REPLACE VIEW bv_agg_bad_base AS SELECT * FROM test_data;"
aggregates:
  - name: bad_agg
    query: "SELECT category, COUNT(*) FROM test_data GROUP BY 1"
"""
        filepath = self._write_yaml(tmp_path / "bv_agg_bad.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert "Validation error" in error
        assert "bad_agg" in error
        assert "bv_agg_bad_base" in error

    def test_drop_base_view_removes_duckdb_view(self, tmp_path):
        """drop_base_view() removes the DuckDB view created at load time."""
        yaml_content = """
id: bv_drop
title: Drop Base View
base_view: "CREATE OR REPLACE VIEW bv_drop_base AS SELECT * FROM test_data;"
"""
        filepath = self._write_yaml(tmp_path / "bv_drop.yaml", yaml_content)
        dash = Dashboard()
        dash.load_yaml(filepath)
        dash.drop_base_view()
        with pytest.raises(Exception):
            get_db().execute("SELECT 1 FROM bv_drop_base").fetchall()

    def test_recreate_base_view_against_explicit_connection(self, tmp_path):
        """recreate_base_view(con) (re-)executes the stored base_view DDL
        against the given connection instead of the active one — used by
        SystemManager.global_refresh() to keep the standby slot's base_views
        in sync before promotion (see ADR-016 in docs/backend_architecture.md)."""
        yaml_content = """
id: bv_recreate
title: Recreate Base View
base_view: "CREATE OR REPLACE VIEW bv_recreate_base AS SELECT * FROM test_data;"
"""
        filepath = self._write_yaml(tmp_path / "bv_recreate.yaml", yaml_content)
        dash = Dashboard()
        dash.load_yaml(filepath)

        standby = get_standby_db()
        # A fresh standby connection/file doesn't have the view (or even the
        # underlying table) until it's explicitly (re)built there.
        with pytest.raises(Exception):
            standby.execute(f"SELECT 1 FROM {dash.source_table}").fetchall()

        standby.execute("""
            CREATE TABLE test_data AS
            SELECT * FROM (VALUES (1, 'alpha', 100, TRUE)) t(id, category, amount, active)
        """)
        dash.recreate_base_view(standby)
        rows = standby.execute(f"SELECT COUNT(*) FROM {dash.source_table}").fetchall()
        assert rows[0][0] == 1

    def test_recreate_base_view_without_load_yaml_raises(self):
        """recreate_base_view() on a Dashboard that never loaded a YAML
        (no base_view_sql stored yet) raises rather than silently no-oping."""
        dash = Dashboard()
        with pytest.raises(RuntimeError):
            dash.recreate_base_view()


# ---------------------------------------------------------------------------
# Dashboard – input_filter, button_filter, dropdown_multy layout parsing
# ---------------------------------------------------------------------------

class TestDashboardFilterWidgetParsing:
    """Tests for input_filter, button_filter, and dropdown_multy layout widget parsing."""

    def _write_yaml(self, path: Path, content: str) -> str:
        path.write_text(content)
        return str(path)

    def test_input_filter_parsed_into_layout(self, tmp_path):
        """input_filter widget is loaded into dashboard layout."""
        yaml_content = """
id: dash_input_filter
title: Input Filter Dash
base_view: "CREATE OR REPLACE VIEW dash_input_filter_base AS SELECT * FROM test_data;"
mappings:
  - "search_field": "category"
layout:
  - input_filter:
      id: cat_search
      label: "Category Search"
      mapping: "search_field"
      filter:
        - operator: "has"
        - value: ""
      grid:
        x: 0
        y: 0
        w: 2
        h: 1
"""
        filepath = self._write_yaml(tmp_path / "if.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert error == ""
        assert len(dash.layout) == 1
        widget = dash.layout[0]
        assert widget["type"] == "input_filter"
        assert widget["id"] == "cat_search"
        assert widget["config"]["mapping"] == "search_field"

    def test_button_filter_parsed_into_layout(self, tmp_path):
        """button_filter widget is loaded into dashboard layout."""
        yaml_content = """
id: dash_button_filter
title: Button Filter Dash
base_view: "CREATE OR REPLACE VIEW dash_button_filter_base AS SELECT * FROM test_data;"
mappings:
  - "active_field": "active"
layout:
  - button_filter:
      id: active_toggle
      label: "Active Only"
      mapping: "active_field"
      filter:
        - operator: "eq"
        - value: "true"
      grid:
        x: 0
        y: 0
        w: 1
        h: 1
"""
        filepath = self._write_yaml(tmp_path / "bf.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert error == ""
        assert len(dash.layout) == 1
        widget = dash.layout[0]
        assert widget["type"] == "button_filter"
        assert widget["id"] == "active_toggle"

    def test_dropdown_multy_parsed_into_layout(self, tmp_path):
        """dropdown_multy widget is loaded as a filter placeholder."""
        yaml_content = """
id: dash_combo
title: Combo Dash
base_view: "CREATE OR REPLACE VIEW dash_combo_base AS SELECT * FROM test_data;"
mappings:
  - "cat_field": "category"
layout:
  - dropdown_multy:
      id: cat_multi
      mapping: "cat_field"
      grid:
        x: 0
        y: 0
        w: 2
        h: 1
"""
        filepath = self._write_yaml(tmp_path / "cm.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert error == ""
        assert len(dash.layout) == 1
        assert dash.layout[0]["type"] == "dropdown_multy"

    def test_widget_start_end_time_parsed_into_layout(self, tmp_path):
        yaml_content = """
id: dash_start_end
title: Time Filter Dash
base_view: "CREATE OR REPLACE VIEW dash_start_end_base AS SELECT * FROM test_data;"
mappings:
  - "time_field": "snapshot_datetime"
layout:
  - widget_start_end_time:
      id: time_range
      mapping: "time_field"
      grid: {x: 0, y: 0, w: 2, h: 1}
"""
        filepath = self._write_yaml(tmp_path / "se.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert error == ""
        assert len(dash.layout) == 1
        assert dash.layout[0]["type"] == "widget_start_end_time"
        assert dash.layout[0]["id"] == "time_range"

    def test_button_datetime_filter_parsed_into_layout(self, tmp_path):
        yaml_content = """
id: dash_btn_time
title: Button Time Filter Dash
base_view: "CREATE OR REPLACE VIEW dash_btn_time_base AS SELECT * FROM test_data;"
mappings:
  - "time_field": "snapshot_datetime"
layout:
  - button_datetime_filter:
      id: time_toggle
      mapping: "time_field"
      toggle_group: "A"
      filter: "172800"
      grid: {x: 0, y: 0, w: 1, h: 1}
"""
        filepath = self._write_yaml(tmp_path / "bt.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert error == ""
        assert len(dash.layout) == 1
        assert dash.layout[0]["type"] == "button_datetime_filter"
        assert dash.layout[0]["id"] == "time_toggle"
        assert dash.layout[0]["config"]["toggle_group"] == "A"

    def test_mixed_layout_all_filter_widgets_included(self, tmp_path):
        """A layout with tile, input_filter, button_filter, dropdown_multy all parsed."""
        yaml_content = """
id: mixed_dash
title: Mixed
base_view: "CREATE OR REPLACE VIEW mixed_dash_base AS SELECT * FROM test_data;"
totals:
  - name: cnt
    query: "SELECT COUNT(*) FROM mixed_dash_base"
mappings:
  - "cat_map": "category"
  - "active_map": "active"
layout:
  - tile:
      id: tile_1
      value: "cnt"
      grid: {x: 0, y: 0, w: 1, h: 1}
  - input_filter:
      id: search_1
      mapping: "cat_map"
      grid: {x: 1, y: 0, w: 2, h: 1}
  - button_filter:
      id: btn_1
      mapping: "active_map"
      filter:
        - operator: "eq"
        - value: "true"
      grid: {x: 3, y: 0, w: 1, h: 1}
  - dropdown_multy:
      id: combo_1
      mapping: "cat_map"
      grid: {x: 4, y: 0, w: 2, h: 1}
"""
        filepath = self._write_yaml(tmp_path / "mixed.yaml", yaml_content)
        dash = Dashboard()
        error = dash.load_yaml(filepath)
        assert error == ""
        assert len(dash.layout) == 4
        types = [w["type"] for w in dash.layout]
        assert "tile" in types
        assert "input_filter" in types
        assert "button_filter" in types
        assert "dropdown_multy" in types


# ---------------------------------------------------------------------------
# Dashboard – parse_widget_filters
# ---------------------------------------------------------------------------

class TestParseWidgetFilters:
    """Tests for Dashboard.parse_widget_filters()."""

    def _make_dash(self, tmp_path) -> Dashboard:
        yaml_content = """
id: fw_dash
title: Filter Widget Dash
base_view: "CREATE OR REPLACE VIEW fw_dash_base AS SELECT * FROM test_data;"
mappings:
  - "cat_alias": "category"
  - "amt_alias": "amount"
layout:
  - input_filter:
      id: cat_widget
      label: "Category"
      mapping: "cat_alias"
      grid: {x: 0, y: 0, w: 2, h: 1}
  - input_filter:
      id: amt_widget
      label: "Amount"
      mapping: "amt_alias"
      grid: {x: 2, y: 0, w: 2, h: 1}
"""
        path = tmp_path / "fw.yaml"
        path.write_text(yaml_content)
        dash = Dashboard()
        dash.load_yaml(str(path))
        return dash

    def test_simple_eq_filter(self, tmp_path):
        """Plain string value implies eq operator."""
        dash = self._make_dash(tmp_path)
        conditions, fhash = dash.parse_widget_filters({"cat_widget": "alpha"})
        assert len(conditions) == 1
        assert conditions[0]["column"] == "category"
        assert conditions[0]["operator"] == "eq"
        assert conditions[0]["value"] == "alpha"
        assert len(fhash) == 32

    def test_explicit_operator_dict(self, tmp_path):
        """Operator-value dict is correctly parsed."""
        dash = self._make_dash(tmp_path)
        conditions, _ = dash.parse_widget_filters({
            "cat_widget": {"operator": "has", "value": "alp"}
        })
        assert conditions[0]["operator"] == "has"
        assert conditions[0]["value"] == "alp"

    def test_multiple_filters_stable_hash(self, tmp_path):
        """Same filters in different order produce the same hash."""
        dash = self._make_dash(tmp_path)
        _, h1 = dash.parse_widget_filters(
            {"cat_widget": "alpha", "amt_widget": "100"}
        )
        _, h2 = dash.parse_widget_filters(
            {"amt_widget": "100", "cat_widget": "alpha"}
        )
        assert h1 == h2

    def test_unknown_widget_skipped(self, tmp_path):
        """Unknown widget IDs are silently skipped."""
        dash = self._make_dash(tmp_path)
        conditions, _ = dash.parse_widget_filters({"nonexistent": "val"})
        assert conditions == []

    def test_invalid_operator_raises(self, tmp_path):
        """An unrecognised operator raises ValueError."""
        dash = self._make_dash(tmp_path)
        with pytest.raises(ValueError, match="Unknown operator"):
            dash.parse_widget_filters({
                "cat_widget": {"operator": "BAD", "value": "x"}
            })

    def test_empty_filters_stable_hash(self, tmp_path):
        """Empty filter dict → empty conditions, 32-char hash."""
        dash = self._make_dash(tmp_path)
        conditions, fhash = dash.parse_widget_filters({})
        assert conditions == []
        assert len(fhash) == 32

    def test_different_values_different_hash(self, tmp_path):
        """Different filter values produce different hashes."""
        dash = self._make_dash(tmp_path)
        _, h1 = dash.parse_widget_filters({"cat_widget": "alpha"})
        _, h2 = dash.parse_widget_filters({"cat_widget": "beta"})
        assert h1 != h2

    def test_multiple_filter_conditions_per_widget(self, tmp_path):
        """A list of dicts for a single widget is parsed correctly."""
        dash = self._make_dash(tmp_path)
        conditions, _ = dash.parse_widget_filters({
            "cat_widget": [
                {"operator": "prefix", "value": "a"},
                {"operator": "suffix", "value": "a"}
            ]
        })
        assert len(conditions) == 2
        ops = [c["operator"] for c in conditions]
        assert "prefix" in ops
        assert "suffix" in ops


# ---------------------------------------------------------------------------
# Dashboard – _build_sql_where
# ---------------------------------------------------------------------------

class TestBuildSqlWhere:
    """Tests for Dashboard._build_sql_where()."""

    def test_empty_conditions_returns_1_eq_1(self):
        w, params = Dashboard._build_sql_where([])
        assert w == "1=1"
        assert params == []

    def test_eq_string_uses_placeholder_not_literal(self):
        w, params = Dashboard._build_sql_where([
            {"column": "category", "operator": "eq", "value": "alpha"}
        ])
        assert "category = ?" in w
        assert "'alpha'" not in w  # value must never be embedded as a literal
        assert params == ["alpha"]

    def test_eq_numeric_unquoted(self):
        w, params = Dashboard._build_sql_where([
            {"column": "amount", "operator": "eq", "value": "100"}
        ])
        assert "amount = 100" in w
        assert "'100'" not in w
        assert params == []  # numeric values are embedded as literals, not bound

    def test_has_operator_like_placeholder(self):
        w, params = Dashboard._build_sql_where([
            {"column": "category", "operator": "has", "value": "alp"}
        ])
        assert "category LIKE ?" in w
        assert params == ["%alp%"]

    def test_prefix_operator_placeholder(self):
        w, params = Dashboard._build_sql_where([
            {"column": "category", "operator": "prefix", "value": "al"}
        ])
        assert "category LIKE ?" in w
        assert params == ["al%"]

    def test_suffix_operator_placeholder(self):
        w, params = Dashboard._build_sql_where([
            {"column": "category", "operator": "suffix", "value": "ha"}
        ])
        assert "category LIKE ?" in w
        assert params == ["%ha"]

    def test_multiple_conditions_joined_with_and(self):
        w, params = Dashboard._build_sql_where([
            {"column": "category", "operator": "eq", "value": "alpha"},
            {"column": "amount", "operator": "gt", "value": "50"},
        ])
        assert " AND " in w
        assert params == ["alpha"]

    def test_last_minutes_operator(self):
        w, params = Dashboard._build_sql_where([
            {"column": "snap_time", "operator": "last_minutes", "value": "120"}
        ])
        assert "snap_time >= CURRENT_TIMESTAMP - INTERVAL '120' MINUTE" in w
        assert params == []

    def test_eq_string_with_quote_is_not_embedded_as_literal(self):
        """Regression test for the SQL-injection gap: a value containing a
        single quote must never reach the SQL string itself — it must be
        bound as a parameter instead."""
        w, params = Dashboard._build_sql_where([
            {"column": "category", "operator": "eq", "value": "x' OR '1'='1"}
        ])
        assert "'" not in w
        assert params == ["x' OR '1'='1"]


# ---------------------------------------------------------------------------
# Dashboard – _build_widget_map
# ---------------------------------------------------------------------------

class TestBuildWidgetMap:
    """Tests for Dashboard._build_widget_map()."""

    def _make_dash(self, tmp_path) -> Dashboard:
        yaml_content = """
id: wm_dash
title: Widget Map Dash
base_view: "CREATE OR REPLACE VIEW wm_dash_base AS SELECT * FROM test_data;"
totals:
  - name: total_count
    query: "SELECT COUNT(*) FROM wm_dash_base"
aggregates:
  - name: by_cat
    query: "SELECT category, COUNT(*) FROM wm_dash_base GROUP BY 1"
  - name: flow_links
    query: "SELECT 'root' AS source, category AS target, COUNT(*) AS value FROM wm_dash_base GROUP BY 1, 2"
mappings:
  - "cat_alias": "category"
layout:
  - tile:
      id: tile_count
      label: "Count"
      value: "total_count"
      grid: {x: 0, y: 0, w: 1, h: 1}
  - basic_table:
      id: table_cats
      data: by_cat
      grid: {x: 0, y: 1, w: 4, h: 2}
  - bar_chart:
      id: chart_raw
      data: test_data
      grid: {x: 4, y: 1, w: 4, h: 2}
  - sankey:
      id: sankey_flow
      title: "Flow"
      data: flow_links
      grid: {x: 0, y: 5, w: 4, h: 2}
  - area_chart:
      id: area_trend
      title: "Trend"
      data: by_cat
      grid: {x: 4, y: 5, w: 4, h: 2}
  - horizontal_bar_chart:
      id: hbar_cats
      title: "By Category"
      data: by_cat
      grid: {x: 0, y: 7, w: 4, h: 2}
  - stacked_bar_chart:
      id: sbar_cats
      title: "By Category"
      data: by_cat
      grid: {x: 4, y: 7, w: 4, h: 2}
  - input_filter:
      id: search_cat
      mapping: "cat_alias"
      grid: {x: 0, y: 3, w: 2, h: 1}
  - dropdown_single:
      id: dd_single_cat
      mapping: "cat_alias"
      data: by_cat
      grid: {x: 2, y: 3, w: 2, h: 1}
  - dropdown_multy:
      id: dd_multy_cat
      mapping: "cat_alias"
      data: test_data
      grid: {x: 4, y: 3, w: 2, h: 1}
  - dropdown_single:
      id: dd_single_no_data
      mapping: "cat_alias"
      grid: {x: 6, y: 3, w: 2, h: 1}
"""
        path = tmp_path / "wm.yaml"
        path.write_text(yaml_content)
        d = Dashboard()
        d.load_yaml(str(path))
        return d

    def test_tile_mapped_to_total(self, tmp_path):
        d = self._make_dash(tmp_path)
        wm = {e["widget_id"]: e for e in d._build_widget_map()}
        assert "tile_count" in wm
        assert wm["tile_count"]["data_type"] == "total"
        assert wm["tile_count"]["data_key"] == "total_count"

    def test_basic_table_mapped_to_aggregate(self, tmp_path):
        d = self._make_dash(tmp_path)
        wm = {e["widget_id"]: e for e in d._build_widget_map()}
        assert "table_cats" in wm
        assert wm["table_cats"]["data_type"] == "aggregate"
        assert wm["table_cats"]["data_key"] == "by_cat"

    def test_bar_chart_with_raw_datasource(self, tmp_path):
        d = self._make_dash(tmp_path)
        wm = {e["widget_id"]: e for e in d._build_widget_map()}
        assert "chart_raw" in wm
        assert wm["chart_raw"]["data_type"] == "raw"
        assert wm["chart_raw"]["data_key"] == "test_data"

    def test_filter_widgets_excluded_from_widget_map(self, tmp_path):
        d = self._make_dash(tmp_path)
        wm = {e["widget_id"]: e for e in d._build_widget_map()}
        assert "search_cat" not in wm

    def test_sankey_mapped_to_aggregate_via_data_field(self, tmp_path):
        """Sankey widgets use the same `data` field as every other
        aggregate-backed widget — regression test for the fix that dropped
        the old `nodes: [agg1, agg2]` schema, which _build_widget_map()
        never actually read (sankey widgets never got a widget_map entry)."""
        d = self._make_dash(tmp_path)
        wm = {e["widget_id"]: e for e in d._build_widget_map()}
        assert "sankey_flow" in wm
        assert wm["sankey_flow"]["data_type"] == "aggregate"
        assert wm["sankey_flow"]["data_key"] == "flow_links"

    def test_area_chart_mapped_to_aggregate(self, tmp_path):
        d = self._make_dash(tmp_path)
        wm = {e["widget_id"]: e for e in d._build_widget_map()}
        assert "area_trend" in wm
        assert wm["area_trend"]["data_type"] == "aggregate"
        assert wm["area_trend"]["data_key"] == "by_cat"

    def test_horizontal_bar_chart_mapped_to_aggregate(self, tmp_path):
        d = self._make_dash(tmp_path)
        wm = {e["widget_id"]: e for e in d._build_widget_map()}
        assert "hbar_cats" in wm
        assert wm["hbar_cats"]["data_type"] == "aggregate"
        assert wm["hbar_cats"]["data_key"] == "by_cat"

    def test_stacked_bar_chart_mapped_to_aggregate(self, tmp_path):
        d = self._make_dash(tmp_path)
        wm = {e["widget_id"]: e for e in d._build_widget_map()}
        assert "sbar_cats" in wm
        assert wm["sbar_cats"]["data_type"] == "aggregate"
        assert wm["sbar_cats"]["data_key"] == "by_cat"

    def test_dropdown_single_data_mapped_to_aggregate(self, tmp_path):
        """dropdown_single's `data` field populates its option list from an
        aggregate, independent of `mapping` (the filter column)."""
        d = self._make_dash(tmp_path)
        wm = {e["widget_id"]: e for e in d._build_widget_map()}
        assert "dd_single_cat" in wm
        assert wm["dd_single_cat"]["data_type"] == "aggregate"
        assert wm["dd_single_cat"]["data_key"] == "by_cat"

    def test_dropdown_multy_data_mapped_to_raw_source(self, tmp_path):
        """dropdown_multy's `data` field can also point at a raw datasource
        table (not just an aggregate)."""
        d = self._make_dash(tmp_path)
        wm = {e["widget_id"]: e for e in d._build_widget_map()}
        assert "dd_multy_cat" in wm
        assert wm["dd_multy_cat"]["data_type"] == "raw"
        assert wm["dd_multy_cat"]["data_key"] == "test_data"

    def test_dropdown_without_data_excluded_from_widget_map(self, tmp_path):
        """A dropdown with only `mapping` (no `data`) stays out of the
        widget_map entirely — same as before this field was supported."""
        d = self._make_dash(tmp_path)
        wm = {e["widget_id"]: e for e in d._build_widget_map()}
        assert "dd_single_no_data" not in wm

    def test_dropdown_multy_data_source_included_in_raw_sources_needed(self, tmp_path):
        d = self._make_dash(tmp_path)
        assert "test_data" in d._raw_sources_needed()


# ---------------------------------------------------------------------------
# Dashboard – get_data_for_filters
# ---------------------------------------------------------------------------

class TestGetDataForFilters:
    """Tests for Dashboard.get_data_for_filters()."""

    def _make_dash(self, tmp_path) -> Dashboard:
        yaml_content = """
id: gdf_dash
title: Get Data Dash
base_view: "CREATE OR REPLACE VIEW gdf_dash_base AS SELECT * FROM test_data;"
totals:
  - name: total_count
    query: "SELECT COUNT(*) FROM gdf_dash_base"
aggregates:
  - name: by_cat
    query: "SELECT category, SUM(amount) AS total FROM gdf_dash_base GROUP BY 1"
"""
        path = tmp_path / "gdf.yaml"
        path.write_text(yaml_content)
        d = Dashboard()
        d.load_yaml(str(path))
        return d

    def test_no_filter_returns_all_rows_total(self, tmp_path):
        d = self._make_dash(tmp_path)
        result = d.get_data_for_filters(conditions=[], row_limit=100)
        assert result["totals"]["total_count"] == 4

    def test_aggregates_returned(self, tmp_path):
        d = self._make_dash(tmp_path)
        result = d.get_data_for_filters(conditions=[], row_limit=100)
        assert "by_cat" in result["aggregates"]
        assert len(result["aggregates"]["by_cat"]) == 3  # alpha, beta, gamma

    def test_row_limit_applied_to_aggregates(self, tmp_path):
        d = self._make_dash(tmp_path)
        result = d.get_data_for_filters(conditions=[], row_limit=1)
        assert len(result["aggregates"]["by_cat"]) == 1

    def test_widget_map_in_result(self, tmp_path):
        d = self._make_dash(tmp_path)
        result = d.get_data_for_filters(conditions=[], row_limit=100)
        assert "widget_map" in result
        assert isinstance(result["widget_map"], list)

    def test_source_tables_in_result(self, tmp_path):
        d = self._make_dash(tmp_path)
        result = d.get_data_for_filters(conditions=[], row_limit=100)
        assert "source_tables" in result
        assert "gdf_dash_base" in result["source_tables"]

    def test_raw_dict_in_result(self, tmp_path):
        d = self._make_dash(tmp_path)
        result = d.get_data_for_filters(conditions=[], row_limit=100)
        assert "raw" in result
        assert isinstance(result["raw"], dict)
