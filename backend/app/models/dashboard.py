"""
Dashboard domain objects: Filter, Summary, Aggregate, Dashboard.

These classes map directly to the spec in classes.md and the YAML schema
in dashboards/template.yaml.  They own the DuckDB virtual tables that
sit on top of the base connector tables.

Lifecycle:
  Dashboard.load_yaml()  →  executes the required `base_view` DDL, then
                            creates Summary/Aggregate definitions (which must
                            all query that view)
  Dashboard.set_filters()  →  creates or reuses a Filter object
  Filter.refresh()  →  rebuilds the filtered DuckDB view
  Dashboard.get_data()  →  queries all Summaries and Aggregates via the Filter view
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from app.core.db import get_db, get_write_lock
from app.core.logging import get_logger
from app.core.validator import sanitize_name

_logger = get_logger("buchimaker.dashboard")

# Matches "CREATE [OR REPLACE] VIEW <name> AS ..." at the start of the
# base_view DDL, capturing the view name so it can become source_table.
_BASE_VIEW_RE = re.compile(
    r"^CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+([A-Za-z_][A-Za-z0-9_]*)\s+AS\s",
    re.IGNORECASE,
)

# Maximum payload bytes before truncation (700 MB → bytes); configurable at runtime
_DEFAULT_TRUNCATE_MB = 700

# Operator alias → SQL operator mapping
_OP_MAP: Dict[str, str] = {
    "eq": "=",
    "neq": "!=",
    "gt": ">",
    "lt": "<",
    "gte": ">=",
    "lte": "<=",
    # button_filter/input_filter docs (template.yaml) use ge/le rather than
    # gte/lte — kept as aliases of the same operators so both spellings work.
    "ge": ">=",
    "le": "<=",
    # String operators resolved at query-build time
    "has": "LIKE",
    "prefix": "LIKE",
    "suffix": "LIKE",
    # Multi-value operators (dropdown_multy) resolved at query-build time
    "in": "IN",
    "not_in": "NOT IN",
    # Datetime operators
    "last_minutes": "last_minutes",
}

# Widget types that consume aggregates
_AGGREGATE_WIDGETS = {
    "bar_chart", "horizontal_bar_chart", "stacked_bar_chart", "area_chart", "line_chart", "pie_chart", "sankey",
    "basic_table", "widget_type",
}
# Widget types that consume totals (single values)
_TOTAL_WIDGETS = {
    "tile", "radial_gauge_full", "radial_gauge_semi",
}
# Widget types that are filter controls — excluded from widget_map
_FILTER_WIDGETS = {
    "button_filter", "input_filter", "dropdown_multy", "dropdown_single", "button_url",
    "widget_start_end_time", "button_datetime_filter",
}


# ---------------------------------------------------------------------------
# Summary (key:value single-row query)
# ---------------------------------------------------------------------------

class Summary:
    """Represents a single-value DuckDB query result for tiles/gauges.

    Attributes:
        id: UUID of this summary.
        name: Human-readable name (used as the key in API responses).
        query: SQL that must return exactly one row with two columns (key, value)
               OR one column (the value is used directly).
    """

    def __init__(self, name: str, query: str, unfiltered: bool = False):
        """Initialise a Summary.

        Args:
            name: Name of this total/summary (unique within the dashboard).
            query: SQL query that returns one row.
            unfiltered: If True, active dashboard filters are never applied
                to this summary — it always reflects the full datasource.
        """
        self.id: str = str(uuid.uuid4())
        self.name: str = name
        self.query: str = query.rstrip(";")
        self.unfiltered: bool = unfiltered

    def get_data(self, con) -> Any:
        """Execute the summary query and return the scalar/dict result.

        Args:
            con: Active DuckDB connection.

        Returns:
            A scalar value if the query returns one column, or a dict if two
            columns (key, value) are returned.  Returns None on error.
        """
        try:
            result = con.execute(self.query).fetchall()
            if not result:
                return None
            row = result[0]
            if len(row) == 1:
                return row[0]
            return {str(row[0]): row[1]}
        except Exception as exc:
            _logger.warning("summary_query_failed", name=self.name, error=str(exc))
            return None


# ---------------------------------------------------------------------------
# Aggregate (multi-row query → table-like result)
# ---------------------------------------------------------------------------

class Aggregate:
    """Represents a multi-row DuckDB query result for charts and tables.

    Attributes:
        id: UUID of this aggregate.
        name: Human-readable name.
        query: SQL query that returns a table.
        column_aliases: Optional mapping from SQL column names to display names.
    """

    def __init__(self, name: str, query: str,
                 column_aliases: Optional[Dict[str, str]] = None,
                 unfiltered: bool = False):
        """Initialise an Aggregate.

        Args:
            name: Unique name within the dashboard.
            query: SQL query returning multiple rows.
            column_aliases: Optional {sql_col: display_label} mapping.
            unfiltered: If True, active dashboard filters are never applied
                to this aggregate — it always reflects the full datasource.
                Used for dropdown option-list aggregates so a dropdown's own
                selection doesn't shrink its own option list.
        """
        self.id: str = str(uuid.uuid4())
        self.name: str = name
        self.query: str = query.rstrip(";")
        self.column_aliases: Dict[str, str] = column_aliases or {}
        self.unfiltered: bool = unfiltered

    def get_data(self, con) -> List[Dict[str, Any]]:
        """Execute the aggregate query and return rows as a list of dicts.

        Args:
            con: Active DuckDB connection.

        Returns:
            List of row dicts, with column aliases applied where defined.
        """
        try:
            cursor = con.execute(self.query)
            cols = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            result = []
            for row in rows:
                record = {}
                for col, val in zip(cols, row):
                    display = self.column_aliases.get(col, col)
                    record[display] = val
                result.append(record)
            return result
        except Exception as exc:
            _logger.warning("aggregate_query_failed", name=self.name, error=str(exc))
            return []


# ---------------------------------------------------------------------------
# Filter (cached filtered view of the base table)
# ---------------------------------------------------------------------------

class Filter:
    """A cached DuckDB view representing a specific combination of UI filters.

    Because filters are shared across users (same filter hash → same object),
    this object is reused when multiple users apply identical filter sets.

    Attributes:
        id: UUID (also used as the DuckDB view name prefix).
        name: Human-readable label built from the filter hash.
        filter_conditions: List of raw filter dicts as sent by the frontend.
        source_table: Base DuckDB table/view name to filter.
        view_name: DuckDB view name for this filtered dataset.
        is_loaded: True after the view has been created in DuckDB.
        last_refreshed: Epoch of last DuckDB view refresh.
        last_queried: Epoch of last time get_data() was called.
        summaries: Dashboard summaries scoped to this filter view.
        aggregates: Dashboard aggregates scoped to this filter view.
    """

    def __init__(self, filter_conditions: List[Dict[str, Any]],
                 source_table: str, filter_hash: str,
                 refresh_frequency: int = 10):
        """Initialise a Filter object.

        Args:
            filter_conditions: List of {"field", "operator", "value"} dicts.
            source_table: Name of the base DuckDB table to filter.
            filter_hash: Hex digest uniquely identifying this filter combination.
            refresh_frequency: Staleness threshold in minutes.
        """
        self.id: str = str(uuid.uuid4())
        self.name: str = f"filter_{filter_hash[:8]}"
        self.filter_conditions = filter_conditions
        self.source_table = source_table
        self.view_name: str = f"__filter_{filter_hash}"
        self.filter_hash: str = filter_hash
        self.refresh_frequency: int = refresh_frequency
        self.is_loaded: bool = False
        self.last_refreshed: int = 0
        self.last_queried: int = 0
        self.summaries: List[Summary] = []
        self.aggregates: List[Aggregate] = []

    def is_fresh(self) -> bool:
        """Check whether the filter view is within the refresh window.

        Returns:
            True if age since last_refreshed < refresh_frequency (minutes).
        """
        age_minutes = (time.time() - self.last_refreshed) / 60
        return age_minutes < self.refresh_frequency

    def _build_where_clause(self) -> str:
        """Translate filter_conditions into a SQL WHERE clause.

        Returns:
            SQL WHERE clause string (without the WHERE keyword), or "1=1"
            if no conditions are set.
        """
        if not self.filter_conditions:
            return "1=1"
        parts = []
        for cond in self.filter_conditions:
            field = cond["field"].split(".")[-1]  # strip "table." prefix
            op = cond["operator"]
            val = cond["value"]
            # Numeric check: if it looks like a number, no quotes
            try:
                float(val)
                parts.append(f"{field} {op} {val}")
            except ValueError:
                parts.append(f"{field} {op} '{val}'")
        return " AND ".join(parts)

    def refresh(self, con: Optional[Any] = None) -> None:
        """Rebuild the filtered DuckDB table from the base table.

        Should be called at startup and every refresh_frequency minutes.

        Args:
            con: DuckDB connection to build the table against. Defaults to
                the active connection (``get_db()``); callers rebuilding
                data ahead of a promotion (see
                ``SystemManager.global_refresh``) pass the standby
                connection explicitly so the filter table exists in the new
                active slot immediately after promotion.
        """
        _con = con if con is not None else get_db()
        where = self._build_where_clause()
        with get_write_lock():
            _con.execute(f"DROP TABLE IF EXISTS {self.view_name}")
            _con.execute(
                f"CREATE TABLE {self.view_name} AS "
                f"SELECT * FROM {self.source_table} WHERE {where}"
            )
        self.last_refreshed = int(time.time())
        self.is_loaded = True
        _logger.info("filter_refreshed", view=self.view_name)

    def get_data(self, truncate_mb: int = _DEFAULT_TRUNCATE_MB
                 ) -> Dict[str, Any]:
        """Return the filtered dataset, truncating if too large.

        Args:
            truncate_mb: Maximum uncompressed byte size in MB before truncation.

        Returns:
            Dict with keys: "rows" (list of dicts), "truncated" (bool),
            "truncated_at_mb" (int or None).
        """
        self.last_queried = int(time.time())
        con = get_db()
        try:
            cursor = con.execute(f"SELECT * FROM {self.view_name}")
            cols = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
            records = [dict(zip(cols, r)) for r in rows]

            # Rough size check via JSON serialisation
            payload = json.dumps(records)
            size_mb = len(payload.encode()) / (1024 * 1024)
            if size_mb > truncate_mb:
                # Truncate to first N rows that fit
                truncated_records: List[Dict] = []
                running = 0
                for rec in records:
                    chunk = len(json.dumps(rec).encode())
                    if running + chunk > truncate_mb * 1024 * 1024:
                        break
                    truncated_records.append(rec)
                    running += chunk
                return {
                    "rows": truncated_records,
                    "truncated": True,
                    "truncated_at_mb": truncate_mb,
                }
            return {"rows": records, "truncated": False, "truncated_at_mb": None}
        except Exception as exc:
            _logger.error("filter_get_data_failed", view=self.view_name, error=str(exc))
            return {"rows": [], "truncated": False, "truncated_at_mb": None}

    def drop(self) -> None:
        """Remove the DuckDB table from memory."""
        con = get_db()
        with get_write_lock():
            con.execute(f"DROP TABLE IF EXISTS {self.view_name}")
        self.is_loaded = False
        _logger.info("filter_dropped", view=self.view_name)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class Dashboard:
    """Top-level dashboard object loaded from a YAML definition file.

    Manages its own set of Filter objects (one per unique filter combination)
    and the global list of Summary / Aggregate definitions.

    Attributes:
        id: Dashboard ID from the YAML ``id`` field.
        name: Dashboard title.
        description: Optional description.
        prompt: Optional LLM system prompt.
        source_table: Name of the DuckDB view created from the YAML's
            required ``base_view`` DDL. Every total/aggregate query must
            select from it, which is what the filter CTE substitution in
            :meth:`get_data_for_filters` keys off of.
        filters: Active Filter objects keyed by hash.
        summaries: Summary definitions (unfiltered by default).
        aggregates: Aggregate definitions.
        grid_columns: Column count for the frontend's GridStack layout —
            12 (default) or 24. From the YAML's optional ``grid_columns``
            field; every widget's ``grid.x + grid.w`` must fit within it.
        settings: Per-dashboard settings dict.
        yaml_path: Filesystem path of the loaded YAML file.
        loaded_at: Epoch when the YAML was last loaded.
    """

    def __init__(self):
        """Create an empty Dashboard.  Call :meth:`load_yaml` to populate."""
        self.id: str = ""
        self.name: str = ""
        self.description: Optional[str] = None
        self.prompt: Optional[str] = None
        self.source_table: str = ""
        self.base_view_sql: str = ""
        self.filters: Dict[str, Filter] = {}
        self.summaries: List[Summary] = []
        self.aggregates: List[Aggregate] = []
        self.mappings: Dict[str, str] = {}
        self.layout: List[Dict[str, Any]] = []
        self.grid_columns: int = 12
        self.settings: Dict[str, Any] = {
            "refresh_frequency_filter": 10,
            "unallocate_frequency_filter": 30,
        }
        self.yaml_path: Optional[str] = None
        self.loaded_at: int = 0

    # -- YAML loading --------------------------------------------------------

    def load_yaml(self, filepath: str) -> str:
        """Load or reload a dashboard definition from a YAML file.

        Preserves last_queried timestamps on existing Filter objects so that
        the cache management logic is not confused after a hot-reload.

        Args:
            filepath: Absolute path to the dashboard YAML file.

        Returns:
            Empty string on success, or a user-friendly error message with
            line number information on parse failure.
        """
        path = Path(filepath)
        if not path.exists():
            return f"Dashboard file not found: {filepath}"

        try:
            raw = path.read_text(encoding="utf-8")
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            line = f" (line {mark.line + 1})" if mark else ""
            return f"YAML syntax error{line}: {exc}"

        # Save last_queried from existing filters before wiping them
        old_queried = {
            h: f.last_queried for h, f in self.filters.items()
        }
        self._drop_all_filters()

        self.id = str(data.get("id", ""))
        try:
            sanitize_name(self.id, "dashboard id")
        except ValueError as exc:
            return f"Validation error: {exc}"
            
        self.name = str(data.get("title", "Unnamed Dashboard"))
        self.description = data.get("description")
        self.prompt = data.get("prompt")
        self.yaml_path = str(filepath)
        self.loaded_at = int(time.time())

        # Parse settings
        self.settings["refresh_frequency_filter"] = int(
            data.get("refresh_frequency_filter", 10)
        )
        self.settings["unallocate_frequency_filter"] = int(
            data.get("unallocate_frequency_filter", 30)
        )

        # Grid column count for the frontend's GridStack layout. Optional —
        # defaults to 12 (GridStack's own default) so dashboards written
        # before this field existed keep laying out exactly as before.
        raw_grid_columns = data.get("grid_columns", 12)
        try:
            grid_columns = int(raw_grid_columns)
        except (TypeError, ValueError):
            return f"Validation error: 'grid_columns' must be an integer, got {raw_grid_columns!r}."
        if grid_columns not in (12, 24):
            return f"Validation error: 'grid_columns' must be 12 or 24, got {grid_columns}."
        self.grid_columns = grid_columns

        # -- base_view (required) --------------------------------------------
        # Every dashboard must define a `base_view`: a `CREATE [OR REPLACE]
        # VIEW <name> AS SELECT ...` statement that flattens the dashboard's
        # data source(s) into one queryable shape.  It must appear before
        # `aggregates`/`totals` in the YAML, and every total/aggregate query
        # must select from it — that's what lets filters apply reliably via
        # a single WHERE clause (see ADR-011 in docs/backend_architecture.md).
        top_level_keys = list(data.keys())
        base_view_sql = data.get("base_view")
        if not base_view_sql or not str(base_view_sql).strip():
            return (
                "Validation error: 'base_view' is required and must define a "
                "\"CREATE [OR REPLACE] VIEW <name> AS SELECT ...\" statement."
            )

        base_view_idx = top_level_keys.index("base_view")
        for later_key in ("aggregates", "totals"):
            if later_key in top_level_keys and top_level_keys.index(later_key) < base_view_idx:
                return (
                    f"Validation error: 'base_view' must be defined before "
                    f"'{later_key}' in the YAML file."
                )

        base_view_sql = str(base_view_sql).strip()
        view_match = _BASE_VIEW_RE.match(base_view_sql)
        if not view_match:
            return (
                "Validation error: 'base_view' must be a "
                "\"CREATE [OR REPLACE] VIEW <name> AS SELECT ...\" statement."
            )

        view_name = view_match.group(1)
        try:
            sanitize_name(view_name, "base_view name")
        except ValueError as exc:
            return f"Validation error: {exc}"

        self.base_view_sql = base_view_sql
        try:
            self.recreate_base_view(get_db())
        except Exception as exc:
            return f"base_view creation failed: {exc}"

        self.source_table = view_name

        def _uses_base_view(query: str) -> bool:
            return re.search(rf"\b{re.escape(view_name)}\b", query, re.IGNORECASE) is not None

        # Parse totals → Summary objects
        self.summaries = []
        for item in data.get("totals", []):
            try:
                sanitize_name(item["name"], "total name")
                query = item["query"]
                if not _uses_base_view(query):
                    return (
                        f"Validation error: total '{item['name']}' must query the "
                        f"base_view '{view_name}', not the original data source."
                    )
                self.summaries.append(Summary(
                    name=item["name"],
                    query=query,
                    unfiltered=bool(item.get("unfiltered", False)),
                ))
            except (KeyError, ValueError) as exc:
                _logger.warning("summary_parse_error", error=str(exc))

        # Parse aggregates → Aggregate objects
        self.aggregates = []
        for item in data.get("aggregates", []):
            try:
                sanitize_name(item["name"], "aggregate name")
                query = item["query"]
                if not _uses_base_view(query):
                    return (
                        f"Validation error: aggregate '{item['name']}' must query the "
                        f"base_view '{view_name}', not the original data source."
                    )
                self.aggregates.append(Aggregate(
                    name=item["name"],
                    query=query,
                    column_aliases=item.get("column_aliases", {}),
                    unfiltered=bool(item.get("unfiltered", False)),
                ))
            except (KeyError, ValueError) as exc:
                _logger.warning("aggregate_parse_error", error=str(exc))

        # Parse mappings
        self.mappings = {}
        for mapping_item in data.get("mappings", []):
            if isinstance(mapping_item, dict):
                for k, v in mapping_item.items():
                    self.mappings[k] = str(v)

        total_names = {s.name for s in self.summaries}
        aggregate_names = {a.name for a in self.aggregates}

        # Parse layout with validation for supported widgets
        self.layout = []
        seen_widget_ids: set = set()
        seen_toggle_group_defaults: Dict[str, str] = {}
        for widget in data.get("layout", []):
            if not isinstance(widget, dict):
                continue

            widget_type = next(iter(widget.keys()), None)
            if not widget_type:
                continue

            widget_config = widget[widget_type]
            if not isinstance(widget_config, dict):
                continue

            parsed_widget = {
                "type": widget_type,
                "id": str(widget_config.get("id", uuid.uuid4())),
                "grid": widget_config.get("grid", {}),
                "config": widget_config
            }

            try:
                sanitize_name(parsed_widget["id"], f"widget '{widget_type}' id")
            except ValueError as exc:
                return f"Validation error: {exc}"

            if parsed_widget["id"] in seen_widget_ids:
                return (
                    f"Validation error: duplicate widget id '{parsed_widget['id']}' "
                    f"in layout. Widget ids must be unique within a dashboard."
                )
            seen_widget_ids.add(parsed_widget["id"])

            # A widget positioned/sized past the dashboard's declared
            # grid_columns would render off-grid (GridStack clips or wraps
            # it unpredictably), so reject it at load time instead. Only
            # checked when both x and w are present and numeric — a
            # missing/malformed grid is left to whatever leniency already
            # existed for that below (unvalidated elsewhere in this file).
            grid_cfg = parsed_widget["grid"]
            grid_x, grid_w = grid_cfg.get("x"), grid_cfg.get("w")
            if isinstance(grid_x, (int, float)) and isinstance(grid_w, (int, float)):
                if grid_x + grid_w > self.grid_columns:
                    return (
                        f"Validation error: layout.{widget_type} '{parsed_widget['id']}' "
                        f"grid.x ({grid_x}) + grid.w ({grid_w}) = {grid_x + grid_w} exceeds "
                        f"this dashboard's grid_columns ({self.grid_columns})."
                    )

            # Validation rules based on template.yaml comments
            if widget_type in _TOTAL_WIDGETS:
                val = widget_config.get("value")
                if val and val in aggregate_names:
                    return f"Validation error: layout.{widget_type} '{parsed_widget['id']}' cannot use aggregate '{val}'. It accepts only Totals."
            
            elif widget_type in _AGGREGATE_WIDGETS:
                data_val = widget_config.get("data")
                if data_val and data_val in total_names:
                    return f"Validation error: layout.{widget_type} '{parsed_widget['id']}' cannot use total '{data_val}'. It accepts only Aggregates or raw data source."

            elif widget_type == "input_filter":
                # input_filter: free-text filter widget.
                # Supports operators: eq, has, prefix, suffix (string matching).
                # Requires a 'mapping' field referencing an entry in the dashboard mappings.
                mapping = widget_config.get("mapping", "")
                if not mapping:
                    _logger.warning(
                        "input_filter_missing_mapping",
                        widget_id=parsed_widget["id"],
                    )
                # Placeholder – no strict rejection; filter is applied at query time.

            elif widget_type == "button_filter":
                # button_filter: toggle button filter widget.
                # Supports operators: ge, le, eq, neq for numbers;
                #   ge, prefix, suffix, has, eq, neq for strings.
                # Requires a 'mapping' field referencing a key in the dashboard mappings.
                #
                # 'toggle_group' is OPTIONAL (unlike button_datetime_filter,
                # where it's required): omitted, the button is a standalone
                # on/off toggle; set, it joins a mutually-exclusive radio-like
                # group — only one button sharing the same toggle_group value
                # can be active at a time. The exclusivity itself is enforced
                # entirely on the frontend (DashboardView's toggleGroups map),
                # same mechanism button_datetime_filter uses, so no extra
                # backend validation is needed here beyond passing the field
                # through in widget_config (already handled generically above).
                #
                # 'default_active' (bool, optional) makes this button's fixed
                # {operator, value} condition apply on first dashboard load,
                # before any click — e.g. the "ALL" button in a toggle_group
                # should read as selected by default. Applied entirely on the
                # frontend (DashboardView.seedDefaultFilters), same
                # pass-through-only treatment as toggle_group above. At most
                # one button per toggle_group should set it; if more than one
                # does, the frontend has no defined tie-break order, so we
                # only warn here rather than reject the dashboard.
                mapping = widget_config.get("mapping", "")
                if not mapping:
                    _logger.warning(
                        "button_filter_missing_mapping",
                        widget_id=parsed_widget["id"],
                    )
                if widget_config.get("default_active"):
                    toggle_group = widget_config.get("toggle_group", "")
                    if toggle_group:
                        prior = seen_toggle_group_defaults.get(toggle_group)
                        if prior:
                            _logger.warning(
                                "button_filter_duplicate_default_active",
                                widget_id=parsed_widget["id"],
                                toggle_group=toggle_group,
                                already_default=prior,
                            )
                        else:
                            seen_toggle_group_defaults[toggle_group] = parsed_widget["id"]
                # Placeholder – no strict rejection; filter is applied at query time.

            elif widget_type == "dropdown_single":
                # dropdown_single: single-select dropdown filter widget.
                # `mapping` is the DB column the selection filters on (applied
                # at query time). `data` is optional and points at an
                # aggregate or raw source whose rows populate the option
                # list — same `data` contract as the aggregate-backed
                # widgets above, so it can't point at a total.
                data_val = widget_config.get("data")
                if data_val and data_val in total_names:
                    return f"Validation error: layout.{widget_type} '{parsed_widget['id']}' cannot use total '{data_val}'. It accepts only Aggregates or raw data source."

            elif widget_type == "dropdown_multy":
                # dropdown_multy: multi-select dropdown filter widget.
                # `mapping` is the DB column the selection filters on (applied
                # at query time). `data` is optional and points at an
                # aggregate or raw source whose rows populate the option
                # list — same `data` contract as the aggregate-backed
                # widgets above, so it can't point at a total.
                data_val = widget_config.get("data")
                if data_val and data_val in total_names:
                    return f"Validation error: layout.{widget_type} '{parsed_widget['id']}' cannot use total '{data_val}'. It accepts only Aggregates or raw data source."

            elif widget_type == "widget_start_end_time":
                mapping = widget_config.get("mapping", "")
                if not mapping:
                    _logger.warning(
                        "widget_start_end_time_missing_mapping",
                        widget_id=parsed_widget["id"],
                    )

            elif widget_type == "button_datetime_filter":
                mapping = widget_config.get("mapping", "")
                if not mapping:
                    _logger.warning(
                        "button_datetime_filter_missing_mapping",
                        widget_id=parsed_widget["id"],
                    )
                toggle_group = widget_config.get("toggle_group", "")
                if not toggle_group:
                    _logger.warning(
                        "button_datetime_filter_missing_toggle_group",
                        widget_id=parsed_widget["id"],
                    )

            # Placeholders for other widget types (pie_chart, bar_chart, etc)
            # They are added to the layout without strict validation for now
            self.layout.append(parsed_widget)

        _logger.info(
            "dashboard_loaded",
            id=self.id,
            name=self.name,
            summaries=len(self.summaries),
            aggregates=len(self.aggregates),
        )
        return ""

    # -- Filter management ---------------------------------------------------

    @staticmethod
    def _hash_filters(conditions: List[Dict[str, Any]]) -> str:
        """Compute a deterministic hash for a list of filter conditions.

        Args:
            conditions: List of filter condition dicts.

        Returns:
            32-character hex digest.
        """
        canonical = json.dumps(sorted(
            [json.dumps(c, sort_keys=True) for c in conditions]
        ))
        return hashlib.md5(canonical.encode(), usedforsecurity=False).hexdigest()

    # ------------------------------------------------------------------
    # New widget-id-keyed filter processing
    # ------------------------------------------------------------------

    def parse_widget_filters(
        self,
        raw_filters: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], str]:
        """Translate a widget-id-keyed filter dict into canonical conditions.

        Each key is a widget ID that must exist in the dashboard layout as a
        filter widget.  The widget's ``mapping`` field resolves to the actual
        DB column (via ``self.mappings``).  The value is either a plain string
        (equality) or a ``{operator, value}`` dict.

        Only filter-type widgets are processed; unknown widget IDs are ignored.

        Args:
            raw_filters: Dict of ``{widget_id: value_or_operator_value}``.

        Returns:
            Tuple of (sorted_conditions_list, filter_hash).

        Raises:
            ValueError: If a widget_id refers to a non-filter widget or the
                operator is unknown.
        """
        # Build a quick lookup: widget_id → widget config (for filter widgets only)
        filter_widget_configs: Dict[str, Dict[str, Any]] = {}
        for widget in self.layout:
            wtype = widget.get("type", "")
            if wtype in _FILTER_WIDGETS and wtype != "button_url":
                wid = widget.get("id", "")
                if wid:
                    filter_widget_configs[wid] = widget.get("config", {})

        conditions: List[Dict[str, Any]] = []
        for widget_id, filter_value in raw_filters.items():
            # Security: validate widget_id characters
            try:
                sanitize_name(widget_id, "filter widget_id")
            except ValueError as exc:
                raise ValueError(str(exc)) from exc

            cfg = filter_widget_configs.get(widget_id)
            if cfg is None:
                # Unknown widget ID → silently skip (could be a query-param from
                # an external system that doesn't know the dashboard layout)
                _logger.warning("filter_widget_not_found", widget_id=widget_id)
                continue

            mapping_key = cfg.get("mapping", "")
            # Resolve mapping alias → actual DB column expression
            db_column = self.mappings.get(mapping_key, mapping_key)
            if not db_column:
                _logger.warning("filter_mapping_missing", widget_id=widget_id)
                continue

            # A widget can send multiple conditions as a top-level list (e.g.
            # widget_start_end_time sends one gte dict and one lte dict). This
            # is distinct from a single condition whose *value* is itself a
            # list (dropdown_multy's `in`/`not_in`, handled below).
            items = filter_value if isinstance(filter_value, list) else [filter_value]

            for item in items:
                # Determine operator and value
                if isinstance(item, dict):
                    operator_alias = str(item.get("operator", "eq"))
                    raw_value = item.get("value", "")
                else:
                    operator_alias = "eq"
                    raw_value = item

                if operator_alias not in _OP_MAP:
                    raise ValueError(
                        f"Unknown operator '{operator_alias}' for widget '{widget_id}'. "
                        f"Allowed: {list(_OP_MAP)}"
                    )

                if operator_alias in ("in", "not_in"):
                    # Multi-value condition (dropdown_multy): keep as a list
                    # of strings instead of collapsing to one scalar — a
                    # single str(list) here would compare the column against
                    # a Python list's repr, matching nothing.
                    raw_list = raw_value if isinstance(raw_value, list) else [raw_value]
                    value: Any = [str(v) for v in raw_list if str(v) != ""]
                    if not value:
                        continue  # nothing selected -> no condition to apply
                else:
                    value = str(raw_value)

                conditions.append({
                    "column": db_column,
                    "operator": operator_alias,
                    "value": value,
                    "widget_id": widget_id,
                })

        # Sort for deterministic hashing. `value` may be a list (in/not_in)
        # or a str (everything else) — comparing those types directly would
        # raise TypeError, so sort on a canonical JSON form instead.
        conditions.sort(key=lambda c: (c["column"], c["operator"], json.dumps(c["value"], sort_keys=True)))
        filter_hash = hashlib.md5(
            json.dumps(conditions, sort_keys=True).encode(),
            usedforsecurity=False
        ).hexdigest()
        return conditions, filter_hash

    @staticmethod
    def _build_sql_where(conditions: List[Dict[str, Any]]) -> Tuple[str, List[Any]]:
        """Build a parameterized SQL WHERE clause from parsed filter conditions.

        String-typed values are never embedded as SQL literals — they're
        bound via ``?`` placeholders to eliminate injection through filter
        *values* (columns come from developer-controlled YAML mappings, not
        request input, so they're safe to embed directly). Numeric values are
        embedded as literals since they're pre-validated by ``float()`` below
        and can't carry SQL syntax.

        Args:
            conditions: List produced by :meth:`parse_widget_filters`.

        Returns:
            Tuple of (SQL WHERE clause string without the ``WHERE`` keyword,
            ordered list of parameters to bind to its ``?`` placeholders).
            Clause is ``1=1`` with an empty params list when there are no
            conditions.
        """
        if not conditions:
            return "1=1", []
        parts = []
        params: List[Any] = []
        for cond in conditions:
            col = cond["column"]
            op_alias = cond["operator"]
            val = cond["value"]

            if op_alias in ("in", "not_in"):
                vals = val if isinstance(val, list) else [val]
                if not vals:
                    # Empty selection: IN matches nothing, NOT IN matches everything.
                    parts.append("1=0" if op_alias == "in" else "1=1")
                    continue
                sql_kw = "IN" if op_alias == "in" else "NOT IN"
                all_numeric = True
                for v in vals:
                    try:
                        float(v)
                    except (TypeError, ValueError):
                        all_numeric = False
                        break
                if all_numeric:
                    placeholders = ", ".join(vals)
                    parts.append(f"{col} {sql_kw} ({placeholders})")
                else:
                    placeholders = ", ".join(["?"] * len(vals))
                    parts.append(f"{col} {sql_kw} ({placeholders})")
                    params.extend(vals)
                continue

            # Numeric detection (no quotes needed)
            try:
                float(val)
                is_numeric = True
            except (TypeError, ValueError):
                is_numeric = False

            if op_alias == "last_minutes":
                if is_numeric:
                    parts.append(f"{col} >= CURRENT_TIMESTAMP - INTERVAL '{val}' MINUTE")
                else:
                    parts.append("1=1")
            elif op_alias == "has":
                parts.append(f"{col} LIKE ?")
                params.append(f"%{val}%")
            elif op_alias == "prefix":
                parts.append(f"{col} LIKE ?")
                params.append(f"{val}%")
            elif op_alias == "suffix":
                parts.append(f"{col} LIKE ?")
                params.append(f"%{val}")
            elif is_numeric:
                sql_op = _OP_MAP[op_alias]
                parts.append(f"{col} {sql_op} {val}")
            else:
                sql_op = _OP_MAP[op_alias]
                parts.append(f"{col} {sql_op} ?")
                params.append(val)
        return " AND ".join(parts), params

    def _build_widget_map(self) -> List[Dict[str, str]]:
        """Analyse the layout and produce a widget_map for the frontend.

        Returns:
            List of {widget_id, data_type, data_key} dicts for data widgets.
        """
        widget_map: List[Dict[str, str]] = []
        total_names = {s.name for s in self.summaries}
        aggregate_names = {a.name for a in self.aggregates}

        for widget in self.layout:
            wtype = widget.get("type", "")
            wid = widget.get("id", "")
            cfg = widget.get("config", {})

            if not wid:
                continue

            if wtype in ("dropdown_single", "dropdown_multy"):
                # Still filter widgets (excluded below), but their optional
                # `data` field populates the option list from an aggregate
                # or raw source — same `data` contract as the aggregate-
                # backed widgets, independent of `mapping` (which stays the
                # column the widget filters on).
                data_ref = str(cfg.get("data", ""))
                if data_ref in aggregate_names:
                    widget_map.append({"widget_id": wid, "data_type": "aggregate", "data_key": data_ref})
                elif data_ref:
                    widget_map.append({"widget_id": wid, "data_type": "raw", "data_key": data_ref})
                continue

            if wtype in _FILTER_WIDGETS:
                continue

            if wtype in _TOTAL_WIDGETS:
                value_ref = str(cfg.get("value", ""))
                if value_ref in total_names:
                    widget_map.append({"widget_id": wid, "data_type": "total", "data_key": value_ref})
                # If value is a literal (not in total_names), skip it — frontend handles it directly

            elif wtype in _AGGREGATE_WIDGETS:
                data_ref = str(cfg.get("data", ""))
                if data_ref in aggregate_names:
                    widget_map.append({"widget_id": wid, "data_type": "aggregate", "data_key": data_ref})
                elif data_ref:  # Raw datasource reference
                    widget_map.append({"widget_id": wid, "data_type": "raw", "data_key": data_ref})

        return widget_map

    def _raw_sources_needed(self) -> List[str]:
        """Return the list of raw data source names referenced by data widgets.

        These are widget ``data`` references that are NOT aggregate names
        (i.e., they point directly at a datasource table).
        """
        aggregate_names = {a.name for a in self.aggregates}
        needed: List[str] = []
        for widget in self.layout:
            wtype = widget.get("type", "")
            cfg = widget.get("config", {})

            if wtype in ("dropdown_single", "dropdown_multy"):
                data_ref = str(cfg.get("data", ""))
                if data_ref and data_ref not in aggregate_names and data_ref not in needed:
                    needed.append(data_ref)
                continue

            if wtype in _FILTER_WIDGETS or wtype in _TOTAL_WIDGETS:
                continue
            if wtype not in _AGGREGATE_WIDGETS:
                continue
            data_ref = str(cfg.get("data", ""))
            if data_ref and data_ref not in aggregate_names:
                if data_ref not in needed:
                    needed.append(data_ref)
        return needed

    def _widget_unfiltered_data_refs(self) -> set:
        """Return `data` refs (aggregate name OR raw source name) that at
        least one referencing widget flags `unfiltered: true` on itself.

        This is distinct from `Aggregate.unfiltered` (set on the aggregate's
        own YAML entry under `aggregates:`) — a dropdown's `unfiltered: true`
        lives on the *widget* in `layout:` and says "my own option list
        shouldn't shrink to my current selection", even when the aggregate it
        pulls from has no `unfiltered` flag of its own (e.g. `filter_data_env`
        is a perfectly normal filterable aggregate; `dropdown_env` just wants
        an unfiltered view of it for its option list). Both need to be
        checked — see the two call sites in get_data_for_filters.
        """
        refs: set = set()
        for widget in self.layout:
            cfg = widget.get("config", {})
            data_ref = str(cfg.get("data", ""))
            if data_ref and cfg.get("unfiltered"):
                refs.add(data_ref)
        return refs

    def get_data_for_filters(
        self,
        conditions: List[Dict[str, Any]],
        row_limit: int = 1000,
    ) -> Dict[str, Any]:
        """Query DuckDB for all widget data, applying the given filter conditions.

        This replaces the old view-based approach.  No DuckDB views are created;
        filters are applied inline in each query via a ``WHERE`` clause.

        Args:
            conditions: Sorted list produced by :meth:`parse_widget_filters`.
            row_limit: Maximum rows to return per result set.
        Returns:
            Dict with keys: totals, aggregates, raw, widget_map, source_tables.
        """
        con = get_db()
        where, where_params = self._build_sql_where(conditions)

        # -- Totals ----------------------------------------------------------
        totals: Dict[str, Any] = {}
        for s in self.summaries:
            # Apply WHERE only if this summary references the source_table
            # and isn't flagged `unfiltered: true` in the YAML.
            query = s.query
            filtered = bool(self.source_table and where != "1=1") and not s.unfiltered
            if filtered:
                # Inject CTE so the summary runs against the filtered subset.
                query = (
                    f"WITH __filtered AS "
                    f"(SELECT * FROM {self.source_table} WHERE {where}) "
                    + query.replace(self.source_table, "__filtered")
                )
            # Execute the (possibly filtered) query directly
            try:
                cursor = con.execute(query, where_params if filtered else [])
                row = cursor.fetchone()
                if row is None:
                    totals[s.name] = None
                elif len(row) == 1:
                    totals[s.name] = row[0]
                else:
                    cols = [desc[0] for desc in cursor.description]
                    totals[s.name] = dict(zip(cols, row))
            except Exception as exc:
                _logger.warning("total_query_failed", name=s.name, error=str(exc))
                totals[s.name] = None

        # -- Aggregates -------------------------------------------------------
        widget_unfiltered_refs = self._widget_unfiltered_data_refs()
        aggregates: Dict[str, List[Dict[str, Any]]] = {}
        for a in self.aggregates:
            query = a.query
            limited_query = f"SELECT * FROM ({query}) __agg LIMIT {row_limit}"
            filtered = (
                bool(self.source_table and where != "1=1")
                and not a.unfiltered
                and a.name not in widget_unfiltered_refs
            )
            if filtered:
                limited_query = (
                    f"WITH __filtered AS "
                    f"(SELECT * FROM {self.source_table} WHERE {where}) "
                    + limited_query.replace(self.source_table, "__filtered")
                )
            # Execute the limited and potentially filtered aggregate
            try:
                cursor = con.execute(limited_query, where_params if filtered else [])
                cols = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()
                result_rows = []
                for row in rows:
                    record: Dict[str, Any] = {}
                    for col, val in zip(cols, row):
                        display = a.column_aliases.get(col, col)
                        record[display] = val
                    result_rows.append(record)
                aggregates[a.name] = result_rows
            except Exception as exc:
                _logger.warning("aggregate_query_failed", name=a.name, error=str(exc))
                aggregates[a.name] = []

        # -- Raw datasource data (only if widgets need it) -------------------
        raw: Dict[str, List[Dict[str, Any]]] = {}
        for source_name in self._raw_sources_needed():
            try:
                filtered = where != "1=1" and source_name not in widget_unfiltered_refs
                raw_query = (
                    f"SELECT * FROM {source_name} "
                    f"WHERE {where} LIMIT {row_limit}"
                    if filtered
                    else f"SELECT * FROM {source_name} LIMIT {row_limit}"
                )
                cursor = con.execute(raw_query, where_params if filtered else [])
                cols = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()
                raw[source_name] = [dict(zip(cols, r)) for r in rows]
            except Exception as exc:
                _logger.warning("raw_source_query_failed", source=source_name, error=str(exc))
                raw[source_name] = []

        # -- Widget map -------------------------------------------------------
        widget_map = self._build_widget_map()

        # Collect datasource names used (for Redis index)
        source_tables = [self.source_table] if self.source_table else []
        for name in self._raw_sources_needed():
            if name not in source_tables:
                source_tables.append(name)

        return {
            "totals": totals,
            "aggregates": aggregates,
            "raw": raw,
            "widget_map": widget_map,
            "source_tables": source_tables,
        }

    def get_widget_page(
        self,
        widget_id: str,
        conditions: List[Dict[str, Any]],
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Fetch a single paginated page from a table-type layout widget.

        This method bypasses Redis entirely.  It always queries DuckDB directly
        using ``LIMIT`` and ``OFFSET`` — a query pattern DuckDB is highly
        optimised for.

        The widget must be a data widget (``basic_table``, ``bar_chart``, etc.)
        with a ``data`` config field that references either:

        - An **aggregate** name (defined in the dashboard YAML ``aggregates``
          section) — in this case the aggregate's SQL is used as the inner query.
        - A **raw datasource** table name — queried directly as
          ``SELECT * FROM <table>``.

        Args:
            widget_id: Layout widget ID (must match a non-filter widget with a
                ``data`` field).
            conditions: Sorted filter conditions from
                :meth:`parse_widget_filters`.
            limit: Page size (rows to return).
            offset: Zero-based row offset.

        Returns:
            Dict with keys: ``data_key``, ``rows``, ``total_count``, ``has_more``.
            ``total_count`` is an exact ``COUNT(*)`` over the same filtered
            query (minus ``LIMIT``/``OFFSET``); ``has_more`` is derived from it
            (``offset + len(rows) < total_count``), not a length heuristic.

        Raises:
            ValueError: If ``widget_id`` is not found or is a filter-type widget
                or has no ``data`` config field.
        """
        # -- Resolve widget → data_key ----------------------------------------
        widget_cfg: Optional[Dict[str, Any]] = None
        for w in self.layout:
            if w.get("id") == widget_id:
                widget_cfg = w
                break

        if widget_cfg is None:
            raise ValueError(f"Widget '{widget_id}' not found in dashboard layout.")

        wtype = widget_cfg.get("type", "")
        if wtype in _FILTER_WIDGETS:
            raise ValueError(
                f"Widget '{widget_id}' is a filter widget (type='{wtype}'). "
                "Pagination is only supported for data widgets."
            )

        data_key = str(widget_cfg.get("config", {}).get("data", ""))
        if not data_key:
            raise ValueError(
                f"Widget '{widget_id}' has no 'data' config field. "
                "Only widgets backed by an aggregate or raw datasource can be paginated."
            )

        # -- Determine base query ---------------------------------------------
        aggregate_names = {a.name: a for a in self.aggregates}
        where, where_params = self._build_sql_where(conditions)
        filtered = False

        widget_own_unfiltered = bool(widget_cfg.get("config", {}).get("unfiltered"))
        if data_key in aggregate_names:
            # Aggregate-backed widget: use the aggregate query as a subquery
            agg = aggregate_names[data_key]
            inner_query = agg.query
            filtered = bool(self.source_table and where != "1=1") and not agg.unfiltered and not widget_own_unfiltered
            if filtered:
                inner_query = (
                    f"WITH __filtered AS "
                    f"(SELECT * FROM {self.source_table} WHERE {where}) "
                    + inner_query.replace(self.source_table, "__filtered")
                )
            paged_query = (
                f"SELECT * FROM ({inner_query}) __agg "
                f"LIMIT {limit} OFFSET {offset}"
            )
            count_query = f"SELECT COUNT(*) FROM ({inner_query}) __agg"
            column_aliases = agg.column_aliases
        else:
            # Raw datasource table
            filtered = where != "1=1" and not widget_own_unfiltered
            if filtered:
                paged_query = (
                    f"SELECT * FROM {data_key} WHERE {where} "
                    f"LIMIT {limit} OFFSET {offset}"
                )
                count_query = f"SELECT COUNT(*) FROM {data_key} WHERE {where}"
            else:
                paged_query = f"SELECT * FROM {data_key} LIMIT {limit} OFFSET {offset}"
                count_query = f"SELECT COUNT(*) FROM {data_key}"
            column_aliases = {}

        # -- Execute ----------------------------------------------------------
        con = get_db()
        try:
            cursor = con.execute(paged_query, where_params if filtered else [])
            col_names = [desc[0] for desc in cursor.description]
            raw_rows = cursor.fetchall()
            total_count = con.execute(
                count_query, where_params if filtered else []
            ).fetchone()[0]
        except Exception as exc:
            _logger.error(
                "widget_page_query_failed",
                widget_id=widget_id,
                data_key=data_key,
                error=str(exc),
            )
            raise ValueError(f"DuckDB query failed for widget '{widget_id}': {exc}") from exc

        # Apply column aliases (aggregate only)
        rows: List[Dict[str, Any]] = []
        for raw_row in raw_rows:
            record: Dict[str, Any] = {}
            for col, val in zip(col_names, raw_row):
                display = column_aliases.get(col, col)
                record[display] = val
            rows.append(record)

        return {
            "data_key": data_key,
            "rows": rows,
            "total_count": total_count,
            "has_more": offset + len(rows) < total_count,
        }

    def export_widget_csv(
        self,
        widget_id: str,
        conditions: List[Dict[str, Any]],
        limit: int = 100000,
    ):
        """Yields CSV lines for a widget's data.
        
        Args:
            widget_id: Layout widget ID.
            conditions: Sorted filter conditions.
            limit: Maximum number of rows to export.
        
        Yields:
            CSV chunks as string.
        """
        import csv
        import io

        # -- Resolve widget → data_key ----------------------------------------
        widget_cfg: Optional[Dict[str, Any]] = None
        for w in self.layout:
            if w.get("id") == widget_id:
                widget_cfg = w
                break

        if widget_cfg is None:
            raise ValueError(f"Widget '{widget_id}' not found in dashboard layout.")

        wtype = widget_cfg.get("type", "")
        if wtype in _FILTER_WIDGETS:
            raise ValueError(
                f"Widget '{widget_id}' is a filter widget (type='{wtype}'). "
                "Export is only supported for data widgets."
            )

        data_key = str(widget_cfg.get("config", {}).get("data", ""))
        if not data_key:
            raise ValueError(
                f"Widget '{widget_id}' has no 'data' config field. "
                "Only widgets backed by an aggregate or raw datasource can be exported."
            )

        # -- Determine base query ---------------------------------------------
        aggregate_names = {a.name: a for a in self.aggregates}
        where, where_params = self._build_sql_where(conditions)
        filtered = False

        widget_own_unfiltered = bool(widget_cfg.get("config", {}).get("unfiltered"))
        if data_key in aggregate_names:
            agg = aggregate_names[data_key]
            inner_query = agg.query
            filtered = bool(self.source_table and where != "1=1") and not agg.unfiltered and not widget_own_unfiltered
            if filtered:
                inner_query = (
                    f"WITH __filtered AS "
                    f"(SELECT * FROM {self.source_table} WHERE {where}) "
                    + inner_query.replace(self.source_table, "__filtered")
                )
            query = f"SELECT * FROM ({inner_query}) __agg LIMIT {limit}"
            column_aliases = agg.column_aliases
        else:
            filtered = where != "1=1" and not widget_own_unfiltered
            if filtered:
                query = f"SELECT * FROM {data_key} WHERE {where} LIMIT {limit}"
            else:
                query = f"SELECT * FROM {data_key} LIMIT {limit}"
            column_aliases = {}

        # -- Execute ----------------------------------------------------------
        con = get_db()
        try:
            cursor = con.execute(query, where_params if filtered else [])
            col_names = [desc[0] for desc in cursor.description]
        except Exception as exc:
            _logger.error(
                "widget_export_query_failed",
                widget_id=widget_id,
                data_key=data_key,
                error=str(exc),
            )
            raise ValueError(f"DuckDB query failed for widget '{widget_id}': {exc}") from exc

        headers = [column_aliases.get(col, col) for col in col_names]

        def _generator():
            # Yield headers
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(headers)
            yield output.getvalue()

            # Yield rows in chunks
            try:
                # Using fetch_record_batch as required by spec
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", category=DeprecationWarning)
                    reader = cursor.fetch_record_batch(5000)
                    
                for batch in reader:
                    output = io.StringIO()
                    writer = csv.writer(output)
                    
                    rows = []
                    for row_dict in batch.to_pylist():
                        rows.append(tuple(row_dict[col] for col in col_names))
                    
                    writer.writerows(rows)
                    yield output.getvalue()
            except (AttributeError, Exception):
                # Fallback to fetchmany if pyarrow is not available or errors out
                while True:
                    chunk = cursor.fetchmany(5000)
                    if not chunk:
                        break
                    output = io.StringIO()
                    writer = csv.writer(output)
                    writer.writerows(chunk)
                    yield output.getvalue()

        return _generator()

    def set_filters(self, conditions: List[Dict[str, Any]]) -> "Filter":
        """Apply a set of filter conditions to the dashboard.

        Reuses an existing Filter object if the same condition set was
        applied before (hash-based deduplication), or creates a new one.

        Args:
            conditions: List of {"field", "operator", "value"} dicts.

        Returns:
            The active Filter object for this condition set.
        """
        h = self._hash_filters(conditions)
        if h in self.filters:
            f = self.filters[h]
            if not f.is_fresh():
                f.refresh()
            return f

        f = Filter(
            filter_conditions=conditions,
            source_table=self.source_table,
            filter_hash=h,
            refresh_frequency=self.settings["refresh_frequency_filter"],
        )
        f.summaries = list(self.summaries)
        f.aggregates = list(self.aggregates)
        f.refresh()
        self.filters[h] = f
        return f

    def trim_filters(self) -> int:
        """Remove filters not queried within the unallocate window.

        Returns:
            Number of filters removed.
        """
        threshold = self.settings["unallocate_frequency_filter"] * 60
        to_remove = [
            h for h, f in self.filters.items()
            if (time.time() - f.last_queried) > threshold
        ]
        for h in to_remove:
            self.filters[h].drop()
            del self.filters[h]
        if to_remove:
            _logger.info("filters_trimmed", count=len(to_remove))
        return len(to_remove)

    def refresh_filters(self) -> None:
        """Re-execute all cached filter views to pick up fresh base data."""
        for f in self.filters.values():
            f.refresh()

    # -- Data retrieval -------------------------------------------------------

    def get_dashboard_data(
        self,
        filter_hash: Optional[str] = None,
        truncate_mb: int = _DEFAULT_TRUNCATE_MB,
    ) -> Dict[str, Any]:
        """Return data for all widgets in the dashboard.

        Uses the filter identified by filter_hash if provided, otherwise the
        unfiltered base source_table.

        Args:
            filter_hash: Hash of the desired filter (from set_filters).
            truncate_mb: Payload size cap in MB.

        Returns:
            Dict with: totals, aggregates, filters_data, truncated, truncated_at_mb.
        """
        con = get_db()

        # Determine which table/view to query
        query_table = self.source_table
        active_filter: Optional[Filter] = None
        if filter_hash and filter_hash in self.filters:
            active_filter = self.filters[filter_hash]
            query_table = active_filter.view_name

        # Summaries
        totals: Dict[str, Any] = {}
        for s in self.summaries:
            totals[s.name] = s.get_data(con)

        # Aggregates
        aggregates: Dict[str, List[Dict[str, Any]]] = {}
        for a in self.aggregates:
            aggregates[a.name] = a.get_data(con)

        # Filtered base data
        filters_result = {"rows": [], "truncated": False, "truncated_at_mb": None}
        if active_filter:
            filters_result = active_filter.get_data(truncate_mb)

        return {
            "dashboard_id": self.id,
            "filter_hash": filter_hash,
            "totals": totals,
            "aggregates": aggregates,
            "filters_data": filters_result["rows"],
            "truncated": filters_result["truncated"],
            "truncated_at_mb": filters_result["truncated_at_mb"],
        }

    def get_dashboard_definition(self) -> Dict[str, Any]:
        """Return the dashboard definition for frontend UI construction.

        Returns:
            Dict with id, name, description, summaries, aggregates.
        """
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "source_table": self.source_table,
            "summaries": [{"id": s.id, "name": s.name} for s in self.summaries],
            "aggregates": [{"id": a.id, "name": a.name} for a in self.aggregates],
            "mappings": self.mappings,
            "layout": self.layout,
            "grid_columns": self.grid_columns,
            "settings": self.settings,
        }

    def list_virtual_tables(self) -> List[str]:
        """Return names of all current DuckDB virtual tables for this dashboard.

        Returns:
            List of view/table name strings.
        """
        tables = []
        for f in self.filters.values():
            if f.is_loaded:
                tables.append(f.view_name)
        return tables

    def _drop_all_filters(self) -> None:
        """Drop all filter views and clear the filter cache."""
        for f in self.filters.values():
            f.drop()
        self.filters.clear()

    def drop_base_view(self) -> None:
        """Drop this dashboard's base_view from DuckDB, if one was created."""
        if not self.source_table:
            return
        con = get_db()
        with get_write_lock():
            con.execute(f"DROP VIEW IF EXISTS {self.source_table}")
        _logger.info("base_view_dropped", view=self.source_table)

    def recreate_base_view(self, con: Optional[Any] = None) -> None:
        """(Re-)execute this dashboard's stored ``base_view`` DDL against ``con``.

        Args:
            con: DuckDB connection to create the view against. Defaults to
                the active connection (``get_db()``); callers rebuilding
                data ahead of a promotion (see
                ``SystemManager.global_refresh``) pass the standby
                connection explicitly so the view exists in the new active
                slot immediately after promotion — otherwise the view (and
                anything selecting from it) would only exist in whichever
                slot happened to be active when the dashboard was loaded.

        Raises:
            RuntimeError: If ``base_view_sql`` hasn't been set yet (i.e.
                ``load_yaml`` was never called successfully).
        """
        if not self.base_view_sql:
            raise RuntimeError(
                f"Dashboard '{self.id}' has no base_view_sql to recreate."
            )
        _con = con if con is not None else get_db()
        with get_write_lock():
            _con.execute(self.base_view_sql.rstrip(";"))

    def widget_count(self) -> int:
        """Return total number of declared widgets (summaries + aggregates).

        Returns:
            Integer count.
        """
        return len(self.summaries) + len(self.aggregates)
