"""
Pydantic request / response schemas for the BuchiMaker API.

These models form the public contract between the frontend and the backend.
All user-supplied name fields use the ``SafeName`` / ``SafeIdentifier``
annotated types so that Constitution security rules are enforced
automatically during Pydantic parsing.

Each model carries ``json_schema_extra`` examples so that Swagger UI
pre-populates request bodies and shows realistic response previews.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import get_settings
from app.core.validator import SafeIdentifier, SafeName, validate_path_within_base


# ---------------------------------------------------------------------------
# Data-source schemas
# ---------------------------------------------------------------------------


class DataSourceCSVCreate(BaseModel):
    """Request body for registering a CSV data source.

    The CSV file is loaded into DuckDB as a virtual view using
    ``read_csv_auto()``.  The ``name`` field becomes the SQL table name
    you reference in dashboard YAML queries.

    Attributes:
        name: SQL-safe identifier used in dashboard queries (e.g. ``orders``).
        title: Human-readable display title shown in the UI.
        filepath: Absolute path to the CSV file **inside the container**
            (or on the mounted volume).
        description: Optional free-text description.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "orders",
                "title": "Sales Orders",
                "filepath": "/app/data/orders.csv",
                "description": "Monthly sales order export from ERP",
            }
        }
    )

    name: SafeName = Field(
        description=(
            "SQL-safe short name used as the DuckDB table/view name. "
            "Only alphanumeric characters, spaces, dashes, and underscores. "
            "Example: `orders`"
        )
    )
    title: SafeName = Field(
        description="Human-readable display name shown in the UI. Example: `Sales Orders`"
    )
    filepath: str = Field(
        description=(
            "Absolute path to the CSV file inside the container or on the "
            "mounted data volume. Must resolve inside the configured "
            "`DATA_DIR` (default `/app/data`) — paths outside it are "
            "rejected. Example: `/app/data/orders.csv`"
        )
    )
    description: Optional[str] = Field(
        default=None,
        description="Optional free-text description. Example: `Monthly ERP export`",
    )

    @field_validator("filepath")
    @classmethod
    def _filepath_within_data_dir(cls, value: str) -> str:
        return validate_path_within_base(value, get_settings().data_dir)


class DataSourceJSONCreate(BaseModel):
    """Request body for registering a JSON data source.

    Supports both JSON arrays (``[{...}, {...}]``) and JSON-Lines format
    (one JSON object per line).  Uses DuckDB ``read_json_auto()``.

    Attributes:
        name: SQL-safe identifier used in dashboard queries.
        title: Human-readable display title.
        filepath: Absolute path to the JSON file inside the container.
        description: Optional free-text description.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "inventory",
                "title": "Warehouse Inventory",
                "filepath": "/app/data/inventory.json",
                "description": "Daily inventory snapshot",
            }
        }
    )

    name: SafeName = Field(
        description="SQL-safe short name used as the DuckDB view name. Example: `inventory`"
    )
    title: SafeName = Field(
        description="Human-readable display name. Example: `Warehouse Inventory`"
    )
    filepath: str = Field(
        description=(
            "Absolute path to the JSON or JSON-Lines file inside the container. "
            "Must resolve inside the configured `DATA_DIR` (default "
            "`/app/data`) — paths outside it are rejected. "
            "Example: `/app/data/inventory.json`"
        )
    )
    description: Optional[str] = Field(
        default=None,
        description="Optional description. Example: `Daily inventory snapshot`",
    )

    @field_validator("filepath")
    @classmethod
    def _filepath_within_data_dir(cls, value: str) -> str:
        return validate_path_within_base(value, get_settings().data_dir)


class DataSourceParquetCreate(BaseModel):
    """Request body for registering a Parquet data source.

    The Parquet file (or glob pattern) is loaded into DuckDB as a virtual view using
    ``read_parquet()``.

    Attributes:
        name: SQL-safe identifier used in dashboard queries.
        title: Human-readable display title.
        filepath: Absolute path or glob pattern to the Parquet file(s).
        description: Optional free-text description.
        hive_partitioning: Boolean to enable or disable Hive partitioning inference.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "sales_data",
                "title": "Daily Sales Data",
                "filepath": "/app/data/agg_daily/**/*.parquet",
                "description": "Daily aggregated sales",
                "hive_partitioning": True
            }
        }
    )

    name: SafeIdentifier = Field(
        description="SQL-safe short name used as the DuckDB view name. Example: `sales_data`"
    )
    title: SafeName = Field(
        description="Human-readable display name. Example: `Daily Sales Data`"
    )
    filepath: str = Field(
        description=(
            "Absolute path or glob pattern to the Parquet file(s). Must "
            "resolve inside the configured `DATA_DIR` (default "
            "`/app/data`) — paths outside it are rejected. "
            "Example: `/app/data/agg_daily/**/*.parquet`"
        )
    )
    description: Optional[str] = Field(
        default=None,
        description="Optional description. Example: `Daily aggregated sales`",
    )
    hive_partitioning: bool = Field(
        default=False,
        description="Enable or disable Hive partitioning inference."
    )

    @field_validator("filepath")
    @classmethod
    def _filepath_within_data_dir(cls, value: str) -> str:
        return validate_path_within_base(value, get_settings().data_dir)


class DataSourceBigQueryCreate(BaseModel):
    """Request body for registering a Google BigQuery data source.

    Data is fetched via the BigQuery client library and loaded into a
    DuckDB in-memory table.  Credentials must reference a service-account
    JSON key file on the container filesystem — **never hardcode secrets**.

    Requires optional dependencies: ``google-cloud-bigquery``, ``pandas``.

    Attributes:
        name: SQL-safe identifier used in dashboard queries.
        title: Human-readable display title.
        project_id: GCP project identifier.
        dataset_id: BigQuery dataset identifier.
        table_id: BigQuery table identifier.
        credentials_path: Container path to the service-account JSON key file.
        query: Optional override SQL. Defaults to ``SELECT * FROM <table>``.
        description: Optional free-text description.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "bq_events",
                "title": "Analytics Events",
                "project_id": "my-gcp-project",
                "dataset_id": "analytics",
                "table_id": "events",
                "credentials_path": "/run/secrets/bq_credentials.json",
                "query": "SELECT * FROM `my-gcp-project.analytics.events` WHERE date >= '2024-01-01'",
                "description": "Google Analytics 4 events from BigQuery",
            }
        }
    )

    name: SafeName = Field(description="SQL-safe short name. Example: `bq_events`")
    title: SafeName = Field(description="Display title. Example: `Analytics Events`")
    project_id: str = Field(description="GCP project ID. Example: `my-gcp-project`")
    dataset_id: str = Field(description="BigQuery dataset. Example: `analytics`")
    table_id: str = Field(description="BigQuery table. Example: `events`")
    credentials_path: str = Field(
        description=(
            "Container path to the service-account JSON key file. "
            "Example: `/run/secrets/bq_credentials.json`"
        )
    )
    query: Optional[str] = Field(
        default=None,
        description=(
            "Optional override SQL query. Defaults to `SELECT * FROM <project.dataset.table>`. "
            "Example: `SELECT id, name, amount FROM proj.ds.tbl WHERE active = TRUE`"
        ),
    )
    description: Optional[str] = Field(
        default=None,
        description="Optional free-text description.",
    )


class DataSourceInfo(BaseModel):
    """Response model for a registered data source.

    Attributes:
        id: UUID assigned to this connector instance.
        name: SQL-safe short name (also the DuckDB view/table name).
        title: Human-readable display title.
        description: Optional description.
        source_type: Connector type (csv, json, bigquery).
        filepath: File path backing this source, if applicable (csv/json/parquet). None for bigquery.
        last_updated: Unix epoch of last load, or 0 if never loaded.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "name": "orders",
                "title": "Sales Orders",
                "description": "Monthly sales order export",
                "source_type": "csv",
                "filepath": "/app/data/orders.csv",
                "last_updated": 1720483200,
            }
        }
    )

    id: str = Field(description="UUID of this connector instance.")
    name: str = Field(description="SQL-safe name used in DuckDB queries.")
    title: str = Field(description="Human-readable display title.")
    description: Optional[str] = Field(default=None, description="Optional description.")
    source_type: str = Field(description="Connector type: `csv`, `json`, or `bigquery`.")
    filepath: Optional[str] = Field(default=None, description="File path backing this source (csv/json/parquet). None for bigquery.")
    last_updated: int = Field(description="Unix epoch of last successful load. 0 = never loaded.")


# ---------------------------------------------------------------------------
# Dashboard data request / filter schemas
# ---------------------------------------------------------------------------


class FilterValue(BaseModel):
    """A filter value with an explicit operator.

    Used when the frontend needs to specify a non-equality comparison.

    Attributes:
        operator: String operator alias. One of: ``eq``, ``neq``, ``gt``,
            ``lt``, ``gte``, ``lte``, ``has``, ``prefix``, ``suffix``.
        value: The value to compare against.
    """

    model_config = ConfigDict(
        json_schema_extra={"example": {"operator": "has", "value": "ear-aa-1"}}
    )

    operator: str = Field(
        pattern=r"^(eq|neq|gt|lt|gte|lte|has|prefix|suffix)$",
        description=(
            "Operator alias. Allowed: `eq`, `neq`, `gt`, `lt`, `gte`, `lte`, "
            "`has` (substring), `prefix`, `suffix`."
        ),
    )
    value: str = Field(
        description="Value to compare against (always sent as a string)."
    )


class DashboardDataRequest(BaseModel):
    """Request body for ``POST /dashboards/{dashboard_id}/data``.

    Combines filter application and data retrieval in a single call.
    Filters are identified by **widget ID** (as defined in the dashboard YAML).

    Simple filters (scalar value, equality implied) may also be passed as
    URL query parameters – useful for pre-filtered deep-links.
    Body filters take precedence over query-parameter filters with the same key.

    Attributes:
        dashboard: Optional dashboard ID assertion (must match path param if provided).
        filters: Dict of ``widget_id`` → filter value.  The value is either
            a plain string (equality) or a ``FilterValue`` object with an explicit
            operator.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "dashboard": "sales-force_1",
                "filters": {
                    "dpm_list": "ear-aa-1232",
                    "input_filter_test": {"operator": "has", "value": "ear-aa-1"},
                    "org_Unit": "Digital section (323)",
                },
            }
        }
    )

    dashboard: Optional[str] = Field(
        default=None,
        description=(
            "Optional dashboard ID assertion.  If provided, it must match "
            "the `dashboard_id` path parameter."
        ),
    )
    filters: Dict[str, Any] = Field(
        default={},
        description=(
            "Dict of widget_id → filter value.  Value may be a plain string "
            "(equality) or ``{operator, value}`` for non-equality comparisons."
        ),
    )


# ---------------------------------------------------------------------------
# Dashboard schemas
# ---------------------------------------------------------------------------


class DashboardLoadRequest(BaseModel):
    """Request body for ``POST /dashboards/load``.

    The filepath may be absolute or relative to the configured
    ``DASHBOARDS_DIR`` container path.  Hot-reloading is supported —
    calling this endpoint again with the same file reloads the definition
    while preserving active filter caches.

    Attributes:
        filepath: Path to the dashboard YAML file.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "filepath": "/app/dashboards/sales_overview.yaml"
            }
        }
    )

    filepath: str = Field(
        description=(
            "Path to the dashboard YAML definition file. "
            "Can be absolute (`/app/dashboards/sales.yaml`) or relative to "
            "`DASHBOARDS_DIR`. Supports hot-reload: calling again reloads "
            "the definition while preserving active filter caches."
        )
    )


class DashboardLoadResponse(BaseModel):
    """Response body for ``POST /dashboards/load``.

    Attributes:
        status: Always ``ok`` on success.
        dashboards_loaded: Total number of dashboards currently registered.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"status": "ok", "dashboards_loaded": 3}
        }
    )

    status: str = Field(description="Always `ok` on success.")
    dashboards_loaded: int = Field(
        description="Total number of dashboards registered after this load."
    )


class DashboardInfo(BaseModel):
    """Lightweight summary of a registered dashboard.

    Returned by ``GET /dashboards`` (list endpoint).

    Attributes:
        id: Dashboard ID from the YAML ``id`` field.
        name: Dashboard display name from the YAML ``title`` field.
        description: Optional description from the YAML file.
        widget_count: Total number of declared widgets (totals + aggregates).
        filter_count: Number of currently active cached filter views.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "sales_overview",
                "name": "Sales Overview",
                "description": "Monthly sales performance dashboard",
                "widget_count": 5,
                "filter_count": 2,
                "filepath": "/app/dashboards/sales_overview.yaml"
            }
        }
    )

    id: str = Field(description="Dashboard ID from the YAML `id` field.")
    name: str = Field(description="Dashboard title from the YAML `title` field.")
    description: Optional[str] = Field(
        default=None, description="Optional description from the YAML file."
    )
    widget_count: int = Field(
        description="Total declared widgets (number of totals + aggregates)."
    )
    filter_count: int = Field(
        description="Number of currently cached DuckDB filter views."
    )
    filepath: Optional[str] = Field(
        default=None, description="The file path to the dashboard YAML definition."
    )


class DashboardDefinitionResponse(BaseModel):
    """Full dashboard definition returned by ``GET /dashboards/{dashboard_id}``.

    The frontend uses this to construct the grid layout, bind widget data,
    and know which totals / aggregates are available.

    Attributes:
        id: Dashboard ID.
        name: Dashboard title.
        description: Optional description.
        source_table: Primary DuckDB table/view name (the registered data source).
        summaries: Declared totals — single-value widgets.
        aggregates: Declared aggregates — multi-row chart/table data.
        settings: Per-dashboard operational settings.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "sales_overview",
                "name": "Sales Overview",
                "description": "Monthly KPI dashboard",
                "source_table": "orders",
                "summaries": [
                    {"id": "uuid-1", "name": "total_orders"},
                    {"id": "uuid-2", "name": "revenue"},
                ],
                "aggregates": [
                    {"id": "uuid-3", "name": "by_region"},
                    {"id": "uuid-4", "name": "by_product"},
                ],
                "mappings": {
                    "filter_dpm": "dpm->>'$.id'",
                    "test": "col1->>'$.llp'"
                },
                "layout": [
                    {
                        "type": "tile",
                        "id": "title_1",
                        "grid": {"x": 0, "y": 0, "w": 1, "h": 1, "min_w": 1, "min_h": 1},
                        "config": {"label": "Total Resources", "value": "total_orders"}
                    }
                ],
                "grid_columns": 12,
                "settings": {
                    "refresh_frequency_filter": 10,
                    "unallocate_frequency_filter": 30,
                },
            }
        }
    )

    id: str = Field(description="Dashboard ID from the YAML `id` field.")
    name: str = Field(description="Dashboard display title.")
    description: Optional[str] = Field(default=None, description="Optional description.")
    source_table: str = Field(
        description="Primary DuckDB table/view name from the registered data source."
    )
    summaries: List[Dict[str, str]] = Field(
        description=(
            "List of declared totals. Each entry: `{id: uuid, name: total_name}`. "
            "Query results available in `GET /data` → `totals` dict."
        )
    )
    aggregates: List[Dict[str, str]] = Field(
        description=(
            "List of declared aggregates. Each entry: `{id: uuid, name: agg_name}`. "
            "Query results available in `GET /data` → `aggregates` dict."
        )
    )
    mappings: Dict[str, str] = Field(
        default={},
        description="Mappings of UI filter fields to DB column expressions."
    )
    layout: List[Dict[str, Any]] = Field(
        default=[],
        description="Layout configuration for widgets, containing widget types and their settings."
    )
    grid_columns: int = Field(
        default=12,
        description=(
            "Column count for the frontend's GridStack layout. From the YAML's "
            "optional `grid_columns` field — 12 (default) or 24. Every widget's "
            "`grid.x + grid.w` fits within this."
        )
    )
    settings: Dict[str, Any] = Field(
        description=(
            "Per-dashboard operational settings. Keys: "
            "`refresh_frequency_filter` (int, minutes), "
            "`unallocate_frequency_filter` (int, minutes)."
        )
    )


class DashboardDataResponse(BaseModel):
    """Response body for ``POST /dashboards/{dashboard_id}/data``.

    Contains all widget data for the dashboard, optionally filtered.
    The payload is GZIP-compressed before being stored in Redis and
    returned to the client (client should accept ``Content-Encoding: gzip``).

    The ``widget_map`` field helps the frontend bind data to widgets:
    each entry maps a layout widget ID to the data key it should read
    from ``totals`` / ``aggregates`` / ``raw``.

    Attributes:
        dashboard_id: ID of the queried dashboard.
        filter_hash: MD5 hex digest of the resolved filter set.
        from_cache: True if the response was served from Redis.
        row_limit: Row cap applied to all result sets.
        totals: Map of ``total_name`` → scalar value for tiles/gauges.
        aggregates: Map of ``aggregate_name`` → list of row dicts (≤ row_limit rows).
        raw: Raw rows of the filtered base dataset (≤ row_limit rows), keyed
            by the source table name.  Present only when widgets require it.
        widget_map: List of ``{widget_id, data_type, data_key}`` descriptors
            so the frontend knows which data key feeds each layout widget.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "dashboard_id": "sales_overview",
                "filter_hash": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
                "from_cache": False,
                "row_limit": 1000,
                "totals": {
                    "total_orders": 1482,
                    "revenue": 248300.50,
                },
                "aggregates": {
                    "by_region": [
                        {"region": "North", "total": 890},
                        {"region": "South", "total": 592},
                    ]
                },
                "raw": {},
                "widget_map": [
                    {"widget_id": "title_1", "data_type": "total", "data_key": "total_orders"},
                    {"widget_id": "my_bar_donations", "data_type": "aggregate", "data_key": "by_region"},
                ],
            }
        }
    )

    dashboard_id: str = Field(description="ID of the queried dashboard.")
    filter_hash: str = Field(
        description="MD5 hex digest of the resolved filter set (sorted and hashed)."
    )
    from_cache: bool = Field(
        default=False,
        description="True if the response was served from Redis cache.",
    )
    row_limit: int = Field(
        default=1000,
        description="Maximum rows returned per result set (configured in general.yaml).",
    )
    totals: Dict[str, Any] = Field(
        default={},
        description=(
            "Map of total name → scalar value for tiles and gauges. "
            "Keys match the `name` fields in the dashboard YAML `totals` section."
        ),
    )
    aggregates: Dict[str, List[Dict[str, Any]]] = Field(
        default={},
        description=(
            "Map of aggregate name → list of row dicts (≤ row_limit rows). "
            "Keys match the `name` fields in the dashboard YAML `aggregates` section."
        ),
    )
    raw: Dict[str, List[Dict[str, Any]]] = Field(
        default={},
        description=(
            "Raw rows from the filtered base dataset, keyed by source table name. "
            "Populated only when layout widgets reference the source table directly."
        ),
    )
    widget_map: List[Dict[str, str]] = Field(
        default=[],
        description=(
            "List of {widget_id, data_type, data_key} descriptors.  "
            "Tells the frontend which data key feeds each layout widget.  "
            "data_type is one of: total, aggregate, raw."
        ),
    )


class SyslogSettings(BaseModel):
    """Syslog export settings."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "enabled": True,
                "host": "syslog-server.local",
                "port": 514,
                "tls_enabled": True,
                "cert_path": "/app/cert/client.crt",
                "key_path": "/app/cert/client.key",
                "ca_cert_path": "/app/cert/ca.crt"
            }
        }
    )

    enabled: bool = Field(default=False, description="Enable or disable syslog export.")
    host: Optional[str] = Field(default=None, description="Syslog server hostname or IP.")
    port: Optional[int] = Field(default=None, description="Syslog server port.")
    tls_enabled: bool = Field(default=False, description="Enable TLS for syslog connection.")
    cert_path: Optional[str] = Field(default=None, description="Path to client certificate.")
    key_path: Optional[str] = Field(default=None, description="Path to client key.")
    ca_cert_path: Optional[str] = Field(default=None, description="Path to CA certificate.")


# ---------------------------------------------------------------------------
# System-manager schemas
# ---------------------------------------------------------------------------


class SystemSettings(BaseModel):
    """Mutable global system settings."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"auto_refresh": 60}}
    )

    auto_refresh: Optional[Union[int, str, List[str]]] = Field(
        default=None,
        description="Global auto-refresh setting: 'disabled', an interval in minutes, or a list of cron strings.",
    )

    @field_validator("auto_refresh", mode="before")
    @classmethod
    def validate_auto_refresh(cls, v):
        if v is None:
            return v
        
        # 1) String case
        if isinstance(v, str):
            if v.lower() == "disabled":
                return "disabled"
            if v.isdigit():
                return int(v)
            raise ValueError("auto_refresh string must be 'disabled' or a numeric interval.")
            
        # 2) Integer case
        if isinstance(v, int):
            if v < 0:
                raise ValueError("auto_refresh interval must be positive.")
            return v
            
        # 3) List of cron records case
        if isinstance(v, list):
            import croniter
            valid_list = []
            for item in v:
                if not isinstance(item, str):
                    raise ValueError("List items must be cron strings.")
                if not croniter.croniter.is_valid(item):
                    raise ValueError(f"Invalid cron expression: '{item}'")
                valid_list.append(item)
            return valid_list

        raise ValueError("Invalid format for auto_refresh.")
    redis_host: Optional[str] = Field(
        default=None,
        description="Redis server host (e.g., 'redis' or '127.0.0.1')."
    )
    redis_port: Optional[int] = Field(
        default=None,
        description="Redis server port (default: 6379 or 6380 for TLS)."
    )
    redis_password: Optional[str] = Field(
        default=None,
        description="Redis authentication token/password."
    )
    redis_user: Optional[str] = Field(
        default=None,
        description="Redis user for ACLs (Redis 6+)."
    )
    redis_tls_enabled: Optional[bool] = Field(
        default=None,
        description="Boolean to enforce encrypted connections."
    )
    duckdb_active_file: Optional[str] = Field(
        default=None,
        description="The currently active DuckDB file path."
    )
    syslog: Optional[SyslogSettings] = Field(
        default=None,
        description="Syslog configuration for audit logs."
    )
    sql_api_enabled: Optional[bool] = Field(
        default=None,
        description="Whether the /system/sql API is enabled."
    )
    row_limit: Optional[int] = Field(
        default=None,
        description="Maximum rows per DuckDB result set."
    )
    redis_ttl_seconds: Optional[int] = Field(
        default=None,
        description="Redis cache TTL in seconds."
    )


class SystemSettingsResponse(BaseModel):
    """Read-only view of :class:`SystemSettings` returned by the API.

    Mirrors ``SystemSettings`` except secrets are never echoed back —
    ``redis_password`` is replaced with ``redis_password_set``, a boolean
    indicating whether one is configured. Callers set/change the password
    by sending a new value to ``PUT /system/settings``; omitting it there
    leaves the existing password unchanged.
    """

    model_config = ConfigDict(
        json_schema_extra={"example": {"auto_refresh": 60, "redis_password_set": True}}
    )

    auto_refresh: Optional[Union[int, str, List[str]]] = Field(default=None)
    redis_host: Optional[str] = Field(default=None)
    redis_port: Optional[int] = Field(default=None)
    redis_password_set: bool = Field(
        default=False,
        description="Whether a Redis password is currently configured. The value itself is never returned.",
    )
    redis_user: Optional[str] = Field(default=None)
    redis_tls_enabled: Optional[bool] = Field(default=None)
    duckdb_active_file: Optional[str] = Field(default=None)
    syslog: Optional[SyslogSettings] = Field(default=None)
    sql_api_enabled: Optional[bool] = Field(default=None)
    row_limit: Optional[int] = Field(default=None)
    redis_ttl_seconds: Optional[int] = Field(default=None)


# ---------------------------------------------------------------------------
# Access control / SSO schemas
# ---------------------------------------------------------------------------

_VALID_ROLES = {"Administrator", "Data Admin", "Viewer", "Deny"}


class RoleMapping(BaseModel):
    """A single OIDC claim -> system role mapping.

    Evaluated in list order by ``app.core.roles.resolve_role_from_claims``;
    the first mapping whose claim value matches the caller's OIDC claims
    wins. ``claim`` may be a top-level claim name (e.g. ``groups``) or a
    dotted path into a nested claim (e.g. Keycloak's ``realm_access.roles``).
    """

    model_config = ConfigDict(
        json_schema_extra={"example": {"claim": "groups", "value": "admins", "role": "Administrator"}}
    )

    claim: str = Field(description="OIDC claim name, e.g. 'groups' or 'realm_access.roles'.")
    value: str = Field(description="Claim value (or list membership) that triggers this mapping.")
    role: str = Field(description="System role granted on a match.")

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        if v not in _VALID_ROLES:
            raise ValueError(f"role must be one of {sorted(_VALID_ROLES)}, got {v!r}")
        return v


class AccessSettings(BaseModel):
    """Request body for ``PUT /system/access``."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"anonymous_access": False, "role_mappings": []}}
    )

    anonymous_access: Optional[bool] = Field(
        default=None, description="True = no OIDC login required; every request is the anonymous Administrator."
    )
    role_mappings: Optional[List[RoleMapping]] = Field(
        default=None,
        description="OIDC claim -> role mappings, evaluated in order. Unmapped users are always denied.",
    )


class AccessSettingsResponse(BaseModel):
    """Response body for ``GET``/``PUT /system/access``."""

    anonymous_access: bool
    role_mappings: List[RoleMapping]


class SSOSettings(BaseModel):
    """Request body for ``PUT /system/sso``.

    ``client_secret``: omit or send an empty string to leave the currently
    stored secret unchanged — it is never echoed back by ``GET``.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "issuer_url": "https://keycloak.example.com/realms/buchimaker",
                "client_id": "buchimaker",
                "client_secret": "***",
                "scopes": "openid profile email",
                "redirect_uri": "http://localhost:3000/auth/callback",
            }
        }
    )

    issuer_url: Optional[str] = Field(default=None, description="OIDC issuer / discovery base URL.")
    client_id: Optional[str] = Field(default=None)
    client_secret: Optional[str] = Field(default=None, description="Omit or blank to leave unchanged.")
    scopes: Optional[str] = Field(default=None, description="Space-separated OIDC scopes.")
    redirect_uri: Optional[str] = Field(default=None, description="Must exactly match the redirect URI registered with the provider.")


class SSOSettingsResponse(BaseModel):
    """Response body for ``GET``/``PUT /system/sso``. The secret itself is never returned."""

    issuer_url: Optional[str] = None
    client_id: Optional[str] = None
    client_secret_set: bool = Field(default=False, description="Whether a client secret is currently configured.")
    scopes: Optional[str] = None
    redirect_uri: Optional[str] = None


class SSOTestRequest(BaseModel):
    """Request body for ``POST /system/sso/test`` — tests an issuer URL before saving it."""

    issuer_url: str = Field(description="OIDC issuer / discovery base URL to validate.")


class SSOTestResponse(BaseModel):
    """Response body for ``POST /system/sso/test``."""

    ok: bool
    message: str
    authorization_endpoint: Optional[str] = None
    token_endpoint: Optional[str] = None
    jwks_uri: Optional[str] = None
    userinfo_endpoint: Optional[str] = None
    end_session_endpoint: Optional[str] = None


class AuthMeResponse(BaseModel):
    """Response body for ``GET /auth/me`` — the current caller's resolved identity."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "is_anonymous": False,
                "name": "Jane Doe",
                "email": "jane@example.com",
                "role": "Data Admin",
                "sso_configured": True,
            }
        }
    )

    is_anonymous: bool
    name: str
    email: Optional[str] = None
    role: str
    sso_configured: bool = Field(description="Whether SSO connection settings are complete (issuer/client/secret/redirect all set).")


# ---------------------------------------------------------------------------
# Widget-set schemas
# ---------------------------------------------------------------------------


class WidgetSetLoadRequest(BaseModel):
    """Request body for ``POST /widgets/load``.

    The folder_path may be absolute or relative to the configured
    ``WIDGETS_DIR`` container path. Hot-reloading is supported — calling
    this endpoint again with the same folder re-validates it.

    Attributes:
        folder_path: Path to the widget package folder.
    """

    model_config = ConfigDict(
        json_schema_extra={"example": {"folder_path": "default"}}
    )

    folder_path: str = Field(
        description=(
            "Path to the widget package folder. Can be absolute "
            "(`/app/widgets/default`) or a folder name relative to "
            "`WIDGETS_DIR`. Supports hot-reload: calling again re-validates "
            "the same folder."
        )
    )


class WidgetSetLoadResponse(BaseModel):
    """Response body for ``POST /widgets/load``.

    Attributes:
        status: Always ``ok`` on success.
        widget_sets_loaded: Total number of widget sets currently registered.
    """

    model_config = ConfigDict(
        json_schema_extra={"example": {"status": "ok", "widget_sets_loaded": 2}}
    )

    status: str = Field(description="Always `ok` on success.")
    widget_sets_loaded: int = Field(
        description="Total number of widget sets registered after this load."
    )


class WidgetSetInfo(BaseModel):
    """Metadata for a registered widget set.

    Returned by ``GET /widgets`` (list endpoint) and the activate endpoint.

    Attributes:
        id: Widget set ID — the folder name under `WIDGETS_DIR`.
        title: Display name — the `packageName` declared in `index.js`, or the folder name.
        description: Always null today; no metadata source exists yet.
        folder_path: Resolved absolute folder path inside the backend container.
        active: True if the frontend currently renders dashboards with this set.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "default",
                "title": "default",
                "description": None,
                "folder_path": "/app/widgets/default",
                "active": True,
            }
        }
    )

    id: str = Field(description="Widget set ID — the folder name under `WIDGETS_DIR`.")
    title: str = Field(description="Display name shown in the UI.")
    description: Optional[str] = Field(
        default=None, description="Always null today; no metadata source exists yet."
    )
    folder_path: str = Field(description="Resolved absolute folder path inside the backend container.")
    active: bool = Field(description="True if this is the widget set the frontend currently renders with.")


# ---------------------------------------------------------------------------
# Table pagination schemas
# ---------------------------------------------------------------------------


class TablePageRequest(BaseModel):
    """Request body for ``POST /dashboards/{id}/widgets/{widget_id}/page``.

    Fetches a single page of rows from a table widget, applying the same
    filters used in the main ``POST /data`` call.  This endpoint bypasses
    Redis entirely — results are always queried live from DuckDB.

    Attributes:
        filters: Same widget-id-keyed filter dict as ``DashboardDataRequest``.
            Plain string = equality; ``{operator, value}`` = explicit operator.
        limit: Page size (rows to return). Default 100, max 5000.
        offset: Zero-based row offset (skip this many rows). Default 0.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "filters": {
                    "product_search": {"operator": "has", "value": "app"},
                    "region_select": "North",
                },
                "limit": 100,
                "offset": 1000,
            }
        }
    )

    filters: Dict[str, Any] = Field(
        default={},
        description=(
            "Widget-id-keyed filter dict, same format as DashboardDataRequest.filters. "
            "Plain string = eq; {operator, value} = explicit operator."
        ),
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=5000,
        description="Number of rows to return (1–5000). Default 100.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Zero-based row offset. Default 0.",
    )


class TablePageResponse(BaseModel):
    """Response body for the table pagination endpoint.

    Attributes:
        dashboard_id: Dashboard the widget belongs to.
        widget_id: Layout widget ID that was paginated.
        data_key: The aggregate name or raw datasource name backing this widget.
        limit: Limit applied to this page.
        offset: Offset applied to this page.
        rows: List of row dicts for this page (at most ``limit`` entries).
        total_count: Exact total row count for the filtered query (across all
            pages), from a ``COUNT(*)`` over the same query minus its
            ``LIMIT``/``OFFSET``.
        has_more: True if there are more rows beyond this page. Derived from
            ``total_count`` (``offset + len(rows) < total_count``).
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "dashboard_id": "sales_overview",
                "widget_id": "table_products",
                "data_key": "by_product",
                "limit": 100,
                "offset": 1000,
                "rows": [
                    {"product": "Widget A", "revenue": 15000},
                    {"product": "Widget B", "revenue": 12500},
                ],
                "total_count": 4382,
                "has_more": True,
            }
        }
    )

    dashboard_id: str = Field(description="Dashboard ID.")
    widget_id: str = Field(description="Widget ID from the dashboard layout.")
    data_key: str = Field(
        description="Aggregate name or raw datasource name backing this widget."
    )
    limit: int = Field(description="Page size applied.")
    offset: int = Field(description="Row offset applied.")
    rows: List[Dict[str, Any]] = Field(
        description="Rows for this page (at most `limit` entries)."
    )
    total_count: int = Field(
        description="Exact total row count for the filtered query, across all pages."
    )
    has_more: bool = Field(
        description=(
            "True if there are more rows past this page "
            "(offset + len(rows) < total_count)."
        )
    )


# ---------------------------------------------------------------------------
# Health schemas
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """Response body for ``GET /healthz``.

    Returns ``200 OK`` with ``status: ok`` when all subsystems are healthy.
    Returns ``503 Service Unavailable`` with ``status: degraded`` if DuckDB
    fails to respond.

    Attributes:
        status: ``ok`` or ``degraded``.
        version: Running application version string.
        duckdb_ok: Whether DuckDB responded successfully to ``SELECT 1``.
        dashboards_loaded: Number of successfully registered dashboards.
        data_sources_loaded: Number of registered data source connectors.
        details: Map of subsystem name → status string for diagnostics.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "ok",
                "version": "0.3.0",
                "duckdb_ok": True,
                "dashboards_loaded": 2,
                "data_sources_loaded": 1,
                "details": {"duckdb": "ok"},
            }
        }
    )

    status: str = Field(description="Overall status: `ok` or `degraded`.")
    version: str = Field(description="Running application version string.")
    duckdb_ok: bool = Field(
        description="True if DuckDB responded successfully to a `SELECT 1` probe."
    )
    dashboards_loaded: int = Field(
        description="Number of successfully registered dashboards."
    )
    data_sources_loaded: int = Field(
        description="Number of registered data source connectors."
    )
    details: Dict[str, str] = Field(
        default={},
        description=(
            "Per-subsystem diagnostic map. Example: `{duckdb: ok}`. "
            "Contains error details when `duckdb_ok` is false."
        ),
    )


# ---------------------------------------------------------------------------
# SQL schemas
# ---------------------------------------------------------------------------


class SQLQueryRequest(BaseModel):
    """Request body for ``POST /system/sql``."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "query": "SELECT * FROM orders;"
            }
        }
    )

    query: str = Field(description="SQL SELECT query to execute directly against DuckDB.")


# ---------------------------------------------------------------------------
# Audit log schemas
# ---------------------------------------------------------------------------


class AuditLogEntry(BaseModel):
    """A single audit-log row.

    Attributes:
        id: Sequence-generated row ID.
        ts: Local wall-clock timestamp of the request.
        client_ip: Client IP address, or null if unavailable.
        method: HTTP method.
        path: Request path.
        query: Raw query string (may be empty).
        status_code: HTTP response status code.
        duration_ms: Request duration in milliseconds.
        user_email: The authenticated caller's email (or display name, if no
            email claim was provided) — null for anonymous requests (see
            general_settings.anonymous_access) or requests to routes with no
            auth dependency (health, /auth/* itself).
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": 42,
                "ts": "2026-07-26T21:09:02.769403",
                "client_ip": "5.6.7.8",
                "method": "GET",
                "path": "/api/v1/dashboards",
                "query": "",
                "status_code": 200,
                "duration_ms": 12.5,
                "user_email": None,
            }
        }
    )

    id: int
    ts: datetime
    client_ip: Optional[str] = None
    method: str
    path: str
    query: Optional[str] = None
    status_code: int
    duration_ms: float
    user_email: Optional[str] = Field(
        default=None,
        description="Authenticated caller's email/name, or null for anonymous requests.",
    )


class AuditLogPageResponse(BaseModel):
    """Response body for ``GET /system/audit-logs``.

    Attributes:
        rows: Page of matching audit-log entries, newest first.
        total: Total number of rows matching the filters (powers pagination).
        limit: Page size used for this response.
        offset: Row offset used for this response.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "rows": [
                    {
                        "id": 42,
                        "ts": "2026-07-26T21:09:02.769403",
                        "client_ip": "5.6.7.8",
                        "method": "GET",
                        "path": "/api/v1/dashboards",
                        "query": "",
                        "status_code": 200,
                        "duration_ms": 12.5,
                        "user_email": None,
                    }
                ],
                "total": 45,
                "limit": 25,
                "offset": 0,
            }
        }
    )

    rows: List[AuditLogEntry]
    total: int = Field(description="Total rows matching the filters (not just this page).")
    limit: int
    offset: int
