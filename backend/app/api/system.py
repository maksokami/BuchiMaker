"""
API router: /system  –  global settings and data-source management.

Endpoints
---------
GET    /api/v1/system/settings              – retrieve current global settings
PUT    /api/v1/system/settings              – update global settings
POST   /api/v1/system/cache/reset           – flush the entire Redis cache
GET    /api/v1/system/data-sources          – list all registered data sources
POST   /api/v1/system/data-sources/csv     – register a CSV data source
POST   /api/v1/system/data-sources/json    – register a JSON data source
POST   /api/v1/system/data-sources/bigquery – register a BigQuery data source
DELETE /api/v1/system/data-sources/{name}  – remove a data source
GET    /api/v1/system/connector-types      – list registered connector type tags
GET    /api/v1/system/audit-logs           – list/filter/paginate audit log entries
GET    /api/v1/system/audit-logs/export    – gzip-streamed CSV export of audit log entries
"""

import gzip
import os
import re
from datetime import datetime
from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import Response, StreamingResponse

from app.connectors.base import (
    BigQueryConnector,
    CSVConnector,
    ConnectorRegistry,
    JSONConnector,
)
from app.core import audit_db, oidc
from app.core.logging import get_logger
from app.core.roles import ADMIN_ONLY, DATA_ADMIN_OR_ABOVE, require_role
from app.models.schemas import (
    AccessSettings,
    AccessSettingsResponse,
    AuditLogPageResponse,
    DataSourceBigQueryCreate,
    DataSourceCSVCreate,
    DataSourceInfo,
    DataSourceJSONCreate,
    DataSourceParquetCreate,
    RoleMapping,
    SSOSettings,
    SSOSettingsResponse,
    SSOTestRequest,
    SSOTestResponse,
    SyslogSettings,
    SystemSettings,
    SystemSettingsResponse,
    SQLQueryRequest,
)
from app.connectors.base import (
    BigQueryConnector,
    CSVConnector,
    ParquetConnector,
    ConnectorRegistry,
    JSONConnector,
)
from app.models.system_manager import system_manager

router = APIRouter(prefix="/system", tags=["system"])
_logger = get_logger("buchimaker.api.system")

# ---------------------------------------------------------------------------
# Common error response specs reused across endpoints
# ---------------------------------------------------------------------------
_400 = {
    "description": "Bad request — file not found or data load failed.",
    "content": {
        "application/json": {
            "example": {"detail": "CSV file not found: /app/data/missing.csv"}
        }
    },
}
_404_source = {
    "description": "Data source not found.",
    "content": {
        "application/json": {
            "example": {"detail": "Data source 'orders' not found."}
        }
    },
}
_422 = {
    "description": "Validation error — name contains invalid characters.",
    "content": {
        "application/json": {
            "example": {
                "detail": (
                    "name 'bad!name' contains invalid characters. "
                    "Only alphanumeric characters, spaces, dashes (-), "
                    "and underscores (_) are allowed."
                )
            }
        }
    },
}
_501 = {
    "description": "BigQuery optional dependencies not installed.",
    "content": {
        "application/json": {
            "example": {
                "detail": (
                    "BigQueryConnector requires 'google-cloud-bigquery' and 'pandas'. "
                    "Install them with: pip install google-cloud-bigquery pandas"
                )
            }
        }
    },
}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get(
    "/settings",
    response_model=SystemSettingsResponse,
    summary="Get global settings",
    description=(
        "Returns the current system-wide operational settings.\n\n"
        "The Redis password is never included in the response — "
        "`redis_password_set` indicates whether one is configured. To set "
        "or change it, send a new value to `PUT /system/settings`.\n\n"
        "| Setting | Default | Description |\n"
        "|---------|---------|-------------|\n"
    ),
    responses={
        200: {
            "description": "Current settings.",
            "content": {
                "application/json": {"example": {}}
            },
        }
    },
    dependencies=[Depends(require_role(*ADMIN_ONLY))],
)
async def get_settings_endpoint():
    """Return the current system-wide settings.

    Returns:
        SystemSettingsResponse. ``redis_password`` is never included —
        see ``redis_password_set``.
    """
    from app.core.general_settings import general_settings
    from app.core.config import get_settings
    return SystemSettingsResponse(
        auto_refresh=general_settings.auto_refresh,
        redis_host=general_settings.redis_host,
        redis_port=general_settings.redis_port,
        redis_password_set=bool(general_settings.redis_password),
        redis_user=general_settings.redis_user,
        redis_tls_enabled=general_settings.redis_tls_enabled,
        duckdb_active_file=get_settings().duckdb_database,
        syslog=SyslogSettings(**general_settings.syslog),
        row_limit=general_settings.row_limit,
        redis_ttl_seconds=general_settings.redis_ttl_seconds,
        sql_api_enabled=general_settings.sql_api_enabled,
    )


@router.put(
    "/settings",
    response_model=SystemSettingsResponse,
    summary="Update global settings",
    description=(
        "Update system-wide operational settings.\n\n"
        "To set or change the Redis password, include `redis_password` in "
        "the body. Omit it (or send `null`) to leave the existing password "
        "unchanged — it is never echoed back in the response."
    ),
    responses={
        200: {
            "description": "Settings updated successfully.",
            "content": {
                "application/json": {"example": {}}
            },
        },
        422: _422,
    },
    dependencies=[Depends(require_role(*ADMIN_ONLY))],
)
async def update_settings(body: SystemSettings):
    """Update system-wide settings.

    Args:
        body: New settings values.

    Returns:
        Updated SystemSettings.

    Raises:
        HTTPException: 422 if a provided syslog TLS cert/key/CA path doesn't
            exist on disk — fails fast rather than only discovering it later
            when the background syslog handler tries to connect.
    """
    from app.core.general_settings import general_settings
    from app.core.config import get_settings
    from app.core.redis_client import redis_cache

    if body.syslog is not None and body.syslog.tls_enabled:
        for field_name in ("cert_path", "key_path", "ca_cert_path"):
            candidate = getattr(body.syslog, field_name)
            if candidate and not os.path.isfile(candidate):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Syslog {field_name} not found: {candidate}",
                )

    redis_changed = False
    
    if body.auto_refresh is not None:
        general_settings.auto_refresh = body.auto_refresh
    if body.redis_host is not None:
        general_settings.redis_host = body.redis_host
        redis_changed = True
    if body.redis_port is not None:
        general_settings.redis_port = body.redis_port
        redis_changed = True
    if body.redis_password is not None:
        general_settings.redis_password = body.redis_password
        redis_changed = True
    if body.redis_user is not None:
        general_settings.redis_user = body.redis_user
        redis_changed = True
    if body.redis_tls_enabled is not None:
        general_settings.redis_tls_enabled = body.redis_tls_enabled
        redis_changed = True
        
    if body.row_limit is not None:
        general_settings.row_limit = body.row_limit
        
    if body.redis_ttl_seconds is not None:
        general_settings.redis_ttl_seconds = body.redis_ttl_seconds

    if body.sql_api_enabled is not None:
        general_settings.sql_api_enabled = body.sql_api_enabled

    if body.syslog is not None:
        general_settings.syslog = body.syslog.model_dump()

    if redis_changed:
        redis_cache.reconnect()

    return SystemSettingsResponse(
        auto_refresh=general_settings.auto_refresh,
        redis_host=general_settings.redis_host,
        redis_port=general_settings.redis_port,
        redis_password_set=bool(general_settings.redis_password),
        redis_user=general_settings.redis_user,
        redis_tls_enabled=general_settings.redis_tls_enabled,
        duckdb_active_file=get_settings().duckdb_database,
        syslog=SyslogSettings(**general_settings.syslog),
        row_limit=general_settings.row_limit,
        redis_ttl_seconds=general_settings.redis_ttl_seconds,
        sql_api_enabled=general_settings.sql_api_enabled,
    )

@router.post(
    "/cache/reset",
    summary="Reset the Redis cache",
    description=(
        "Flushes every key in the configured Redis database (`FLUSHDB`), evicting "
        "all cached dashboard data. Use this to troubleshoot stale or corrupted "
        "cache entries. Data sources and dashboards are unaffected — the next "
        "read simply repopulates the cache from DuckDB."
    ),
    responses={
        200: {"description": "Cache flushed successfully."},
        503: {"description": "Redis is unavailable."},
    },
    dependencies=[Depends(require_role(*DATA_ADMIN_OR_ABOVE))],
)
async def reset_cache():
    """Flush the entire Redis cache.

    Returns:
        Confirmation message.

    Raises:
        HTTPException: 503 if Redis is unavailable or the flush failed.
    """
    from app.core.redis_client import redis_cache

    if not redis_cache.flush_all():
        raise HTTPException(status_code=503, detail="Redis cache is unavailable.")
    return {"status": "success", "message": "Redis cache cleared."}


# ---------------------------------------------------------------------------
# Data-source listing
# ---------------------------------------------------------------------------


@router.post(
    "/refresh",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger a global refresh of all data sources",
    description=(
        "Reloads all registered data sources into a new background DuckDB instance "
        "and swaps it with the active one. Enforces a rate limit of max 2 refreshes "
        "per minute."
    ),
    responses={
        202: {"description": "Refresh triggered successfully."},
        429: {"description": "Too many requests. Max 2 per minute."},
        500: {"description": "Failed to load data sources."},
    },
    dependencies=[Depends(require_role(*DATA_ADMIN_OR_ABOVE))],
)
async def trigger_global_refresh():
    try:
        system_manager.global_refresh()
        return {"status": "success", "message": "Global refresh completed."}
    except RuntimeError as e:
        err_msg = str(e)
        if "Rate limit exceeded" in err_msg:
            raise HTTPException(status_code=429, detail=err_msg)
        raise HTTPException(status_code=500, detail=err_msg)


@router.get(
    "/data-sources",
    response_model=list[DataSourceInfo],
    summary="List registered data sources",
    description=(
        "Returns metadata for all currently registered data-source connectors.\n\n"
        "Each entry shows the connector's UUID, SQL name, display title, type, "
        "and the Unix epoch timestamp of the last successful data load.\n\n"
        "Returns an empty list `[]` if no data sources have been registered yet."
    ),
    responses={
        200: {
            "description": "List of registered data sources (may be empty).",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                            "name": "orders",
                            "title": "Sales Orders",
                            "description": "Monthly ERP export",
                            "source_type": "csv",
                            "last_updated": 1720483200,
                        }
                    ]
                }
            },
        }
    },
    dependencies=[Depends(require_role(*DATA_ADMIN_OR_ABOVE))],
)
async def list_data_sources():
    """Return metadata for all registered data sources.

    Returns:
        List of DataSourceInfo objects, or an empty list.
    """
    return system_manager.list_data_sources()


# ---------------------------------------------------------------------------
# CSV connector
# ---------------------------------------------------------------------------


@router.post(
    "/data-sources/csv",
    response_model=DataSourceInfo,
    status_code=status.HTTP_201_CREATED,
    summary="Register a CSV data source",
    description=(
        "Loads a CSV file from the container filesystem into a DuckDB in-memory view "
        "using `read_csv_auto()` with automatic schema inference.\n\n"
        "**The `name` field becomes the SQL table name** you reference in dashboard "
        "YAML queries — e.g. `SELECT COUNT(*) FROM orders`.\n\n"
        "- File must exist at the given `filepath` inside the container or on a mounted volume.\n"
        "- Calling this endpoint again with the same `name` overwrites the existing view.\n"
        "- Supports standard CSV (comma-separated) and TSV (tab-separated) formats."
    ),
    responses={
        201: {
            "description": "CSV data source registered and loaded successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                        "name": "orders",
                        "title": "Sales Orders",
                        "description": "Monthly ERP export",
                        "source_type": "csv",
                        "last_updated": 1720483200,
                    }
                }
            },
        },
        400: _400,
        422: _422,
    },
    dependencies=[Depends(require_role(*DATA_ADMIN_OR_ABOVE))],
)
async def register_csv(body: DataSourceCSVCreate):
    """Load a CSV file into DuckDB and register it as a data source.

    Args:
        body: CSV connector configuration including name, title, and filepath.

    Returns:
        DataSourceInfo for the newly registered connector.

    Raises:
        HTTPException: 400 if the file is not found or the DuckDB load fails.
        HTTPException: 422 if the name contains invalid characters.
    """
    connector = CSVConnector(
        name=body.name,
        title=body.title,
        filepath=body.filepath,
        description=body.description,
    )
    try:
        system_manager.register_data_source(connector)
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return connector.to_info()


# ---------------------------------------------------------------------------
# JSON connector
# ---------------------------------------------------------------------------


@router.post(
    "/data-sources/json",
    response_model=DataSourceInfo,
    status_code=status.HTTP_201_CREATED,
    summary="Register a JSON data source",
    description=(
        "Loads a JSON file from the container filesystem into a DuckDB in-memory view "
        "using `read_json_auto()` with automatic schema inference.\n\n"
        "**Supported formats:**\n"
        "- JSON array: `[{...}, {...}]`\n"
        "- JSON-Lines (NDJSON): one JSON object per line\n\n"
        "**The `name` field becomes the SQL table name** referenced in dashboard YAML queries.\n\n"
        "- File must exist at the given `filepath` inside the container or on a mounted volume."
    ),
    responses={
        201: {
            "description": "JSON data source registered and loaded successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
                        "name": "inventory",
                        "title": "Warehouse Inventory",
                        "description": "Daily snapshot",
                        "source_type": "json",
                        "last_updated": 1720483200,
                    }
                }
            },
        },
        400: _400,
        422: _422,
    },
    dependencies=[Depends(require_role(*DATA_ADMIN_OR_ABOVE))],
)
async def register_json(body: DataSourceJSONCreate):
    """Load a JSON file into DuckDB and register it as a data source.

    Args:
        body: JSON connector configuration including name, title, and filepath.

    Returns:
        DataSourceInfo for the newly registered connector.

    Raises:
        HTTPException: 400 if the file is not found or the DuckDB load fails.
        HTTPException: 422 if the name contains invalid characters.
    """
    connector = JSONConnector(
        name=body.name,
        title=body.title,
        filepath=body.filepath,
        description=body.description,
    )
    try:
        system_manager.register_data_source(connector)
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return connector.to_info()


# ---------------------------------------------------------------------------
# Parquet connector
# ---------------------------------------------------------------------------


@router.post(
    "/data-sources/parquet",
    response_model=DataSourceInfo,
    status_code=status.HTTP_201_CREATED,
    summary="Register a Parquet data source",
    description=(
        "Loads a Parquet file (or glob pattern) into a DuckDB in-memory view "
        "using `read_parquet()`.\n\n"
        "**The `name` field becomes the SQL table name** referenced in dashboard YAML queries.\n\n"
        "- File must exist at the given `filepath` inside the container or on a mounted volume."
    ),
    responses={
        201: {
            "description": "Parquet data source registered and loaded successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
                        "name": "sales_data",
                        "title": "Daily Sales Data",
                        "description": "Daily aggregated sales",
                        "source_type": "parquet",
                        "last_updated": 1720483200,
                    }
                }
            },
        },
        400: _400,
        422: _422,
    },
    dependencies=[Depends(require_role(*DATA_ADMIN_OR_ABOVE))],
)
async def register_parquet(body: DataSourceParquetCreate):
    """Load a Parquet file into DuckDB and register it as a data source.

    Args:
        body: Parquet connector configuration including name, title, filepath, and hive_partitioning.

    Returns:
        DataSourceInfo for the newly registered connector.

    Raises:
        HTTPException: 400 if the DuckDB load fails.
        HTTPException: 422 if the name contains invalid characters.
    """
    connector = ParquetConnector(
        name=body.name,
        title=body.title,
        filepath=body.filepath,
        description=body.description,
        hive_partitioning=body.hive_partitioning,
    )
    try:
        system_manager.register_data_source(connector)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return connector.to_info()


# ---------------------------------------------------------------------------
# BigQuery connector
# ---------------------------------------------------------------------------


@router.post(
    "/data-sources/bigquery",
    response_model=DataSourceInfo,
    status_code=status.HTTP_201_CREATED,
    summary="Register a BigQuery data source",
    description=(
        "Fetches data from Google BigQuery using the `google-cloud-bigquery` client "
        "and loads it into a DuckDB in-memory table.\n\n"
        "**Prerequisites:**\n"
        "- Install optional dependencies: `pip install google-cloud-bigquery pandas`\n"
        "- Provide a service-account JSON key file path in `credentials_path`. "
        "Never hardcode credentials — mount the file as a Docker secret.\n\n"
        "**`query` field:**\n"
        "Omit to use the default `SELECT * FROM <project.dataset.table>`. "
        "Provide a custom query to filter or transform data before ingestion.\n\n"
        "> ⚠️ This endpoint is long-running — it transfers the query result set into memory. "
        "Use a selective query for large tables."
    ),
    responses={
        201: {
            "description": "BigQuery data source loaded and registered successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
                        "name": "bq_events",
                        "title": "Analytics Events",
                        "description": "GA4 events",
                        "source_type": "bigquery",
                        "last_updated": 1720483200,
                    }
                }
            },
        },
        400: _400,
        422: _422,
        501: _501,
    },
    dependencies=[Depends(require_role(*DATA_ADMIN_OR_ABOVE))],
)
async def register_bigquery(body: DataSourceBigQueryCreate):
    """Pull data from BigQuery into DuckDB and register it as a data source.

    Args:
        body: BigQuery connector configuration.

    Returns:
        DataSourceInfo for the newly registered connector.

    Raises:
        HTTPException: 400 if credentials are missing or the query fails.
        HTTPException: 422 if the name contains invalid characters.
        HTTPException: 501 if google-cloud-bigquery or pandas is not installed.
    """
    connector = BigQueryConnector(
        name=body.name,
        title=body.title,
        project_id=body.project_id,
        dataset_id=body.dataset_id,
        table_id=body.table_id,
        credentials_path=body.credentials_path,
        query=body.query,
        description=body.description,
    )
    try:
        system_manager.register_data_source(connector)
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)
        )
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return connector.to_info()


# ---------------------------------------------------------------------------
# Remove data source
# ---------------------------------------------------------------------------


@router.delete(
    "/data-sources/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a data source",
    description=(
        "Drops the DuckDB table/view for the named connector and removes it from the registry.\n\n"
        "> ⚠️ If a loaded dashboard references this data source, its queries will fail "
        "until a replacement source with the same `name` is registered.\n\n"
        "Returns **204 No Content** on success (no response body)."
    ),
    responses={
        204: {"description": "Data source removed successfully. No response body."},
        404: _404_source,
    },
    dependencies=[Depends(require_role(*DATA_ADMIN_OR_ABOVE))],
)
async def remove_data_source(
    name: str = Path(
        description=(
            "The `name` field of the data source to remove. "
            "This is the SQL table name used in dashboard queries. "
            "Example: `orders`"
        )
    ),
):
    """Drop a data source connector and its DuckDB view.

    Args:
        name: Data source connector name (the SQL table/view name).

    Raises:
        HTTPException: 404 if no connector with that name is registered.
    """
    if name not in system_manager.data_sources:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Data source '{name}' not found.",
        )
    system_manager.remove_data_source(name)


# ---------------------------------------------------------------------------
# Connector types
# ---------------------------------------------------------------------------


@router.get(
    "/connector-types",
    response_model=list[str],
    summary="List supported connector types",
    description=(
        "Returns all registered connector type tags. "
        "Built-in types: `csv`, `json`, `bigquery`.\n\n"
        "Additional connectors (e.g. S3, Snowflake) can be registered at startup "
        "by calling `ConnectorRegistry.register()` — they will appear here automatically."
    ),
    responses={
        200: {
            "description": "List of registered connector type tags.",
            "content": {
                "application/json": {
                    "example": ["csv", "json", "bigquery"]
                }
            },
        }
    },
    dependencies=[Depends(require_role(*DATA_ADMIN_OR_ABOVE))],
)
async def list_connector_types():
    """Return all registered connector type tags.

    Returns:
        List of source_type strings (e.g. ``["csv", "json", "bigquery"]``).
    """
    return ConnectorRegistry.list_types()


# ---------------------------------------------------------------------------
# SQL Execution
# ---------------------------------------------------------------------------

# DuckDB table/scalar functions that read from the filesystem (or, via the
# httpfs extension, a URL). A leading-`SELECT` prefix check does not stop
# these — they're valid inside an ordinary SELECT's FROM/expression list —
# so they're blocked by name here. This endpoint's only legitimate purpose
# is querying tables already loaded into DuckDB; it has no need to open
# files, so the block is unconditional rather than scoped to specific
# directories (a directory allowlist checked against a string literal can
# be defeated by string concatenation, relative paths, or symlinks).
_SQL_FILE_READ_FUNCTIONS = re.compile(
    r"\b(read_csv(_auto)?|read_json(_auto|_objects(_auto)?)?|read_parquet|"
    r"read_ndjson(_auto|_objects)?|read_text|read_blob|read_xlsx|"
    r"glob|sniff_csv|st_read)\s*\(",
    re.IGNORECASE,
)


@router.post(
    "/sql",
    summary="Execute SQL directly against DuckDB",
    description=(
        "Run a SELECT query directly against the DB avoiding Redis. Results "
        "are gzip compressed.\n\n"
        "Exactly one statement is allowed and it must be a SELECT — this is "
        "enforced via DuckDB's own parser (`extract_statements`), not a "
        "text prefix check, so it cannot be bypassed by stacking a second "
        "statement after a semicolon. File-reading functions "
        "(`read_csv`, `read_parquet`, `glob`, etc.) are also blocked, since "
        "they'd otherwise let a SELECT read arbitrary files on the "
        "container filesystem."
    ),
    dependencies=[Depends(require_role(*DATA_ADMIN_OR_ABOVE))],
)
async def execute_sql(body: SQLQueryRequest):
    from app.core.general_settings import general_settings
    if not general_settings.sql_api_enabled:
        raise HTTPException(status_code=403, detail="SQL API is disabled.")

    query = body.query.strip()

    from app.core.db import get_db
    conn = get_db()

    try:
        statements = conn.extract_statements(query)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid SQL: {e}")

    if len(statements) != 1:
        raise HTTPException(
            status_code=400,
            detail="Exactly one SQL statement is allowed (no stacked/chained statements).",
        )

    statement = statements[0]
    if statement.type != duckdb.StatementType.SELECT:
        raise HTTPException(status_code=400, detail="Only SELECT queries are allowed.")

    if _SQL_FILE_READ_FUNCTIONS.search(statement.query):
        raise HTTPException(
            status_code=400,
            detail="Query references a disallowed file-reading function.",
        )

    try:
        cursor = conn.execute(statement.query)
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        
        lines = ["\t".join(columns)]
        for row in rows:
            lines.append("\t".join(str(x) if x is not None else "" for x in row))
        
        tsv_data = "\n".join(lines) + "\n"
        compressed_data = gzip.compress(tsv_data.encode("utf-8"))
        
        return Response(
            content=compressed_data,
            media_type="text/plain",
            headers={
                "Content-Encoding": "gzip",
            }
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Audit logs — Administrator only (see app.core.roles); anyone who can
# reach these reads every recorded request, including query strings.
# ---------------------------------------------------------------------------


@router.get(
    "/audit-logs",
    response_model=AuditLogPageResponse,
    summary="List audit log entries",
    description=(
        "Returns a filtered, paginated page of API-call audit records from the "
        "dedicated audit-log store — completely independent of the dashboard "
        "data engine, so it is never wiped by a data-source refresh.\n\n"
        "**Filters:** `search` matches path OR client IP (substring). "
        "`method` and `status_code` are exact matches. `date_from`/`date_to` "
        "bound the timestamp range (inclusive).\n\n"
        "**Pagination:** `total` reflects the full filtered count (not just "
        "this page), driving page-number UI."
    ),
    dependencies=[Depends(require_role(*ADMIN_ONLY))],
)
async def list_audit_logs(
    search: Optional[str] = Query(default=None, description="Free-text filter over path or client IP."),
    method: Optional[str] = Query(default=None, description="Exact HTTP method match, e.g. `GET`."),
    status_code: Optional[int] = Query(default=None, description="Exact HTTP status code match."),
    date_from: Optional[datetime] = Query(default=None, description="Lower bound (inclusive) on timestamp."),
    date_to: Optional[datetime] = Query(default=None, description="Upper bound (inclusive) on timestamp."),
    limit: int = Query(default=25, ge=1, le=500, description="Max rows to return."),
    offset: int = Query(default=0, ge=0, description="Zero-based row offset."),
):
    """Return a filtered, paginated page of audit-log rows.

    Returns:
        AuditLogPageResponse with the page of rows and the total filtered count.
    """
    page = audit_db.query_page(
        search=search,
        method=method,
        status_code=status_code,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return AuditLogPageResponse(
        rows=page["rows"], total=page["total"], limit=limit, offset=offset
    )


@router.get(
    "/audit-logs/export",
    summary="Export audit log entries as gzip-compressed CSV",
    description=(
        "Streams matching audit-log rows as CSV, gzip-compressed incrementally "
        "so memory stays flat regardless of export size — the response is "
        "never buffered whole in memory at any point. Same filters as the "
        "list endpoint; capped at 100,000 rows.\n\n"
        "Browsers decode `Content-Encoding: gzip` transparently, so this "
        "behaves like any other CSV download."
    ),
    responses={200: {"description": "Gzip-compressed CSV stream.", "content": {"text/csv": {}}}},
    dependencies=[Depends(require_role(*ADMIN_ONLY))],
)
async def export_audit_logs(
    search: Optional[str] = Query(default=None),
    method: Optional[str] = Query(default=None),
    status_code: Optional[int] = Query(default=None),
    date_from: Optional[datetime] = Query(default=None),
    date_to: Optional[datetime] = Query(default=None),
):
    """Stream a gzip-compressed CSV export of matching audit-log rows.

    Returns:
        StreamingResponse with `Content-Type: text/csv` and
        `Content-Encoding: gzip`.
    """
    generator = audit_db.export_csv(
        search=search,
        method=method,
        status_code=status_code,
        date_from=date_from,
        date_to=date_to,
    )
    return StreamingResponse(
        generator,
        media_type="text/csv",
        headers={
            "Content-Encoding": "gzip",
            "Content-Disposition": 'attachment; filename="audit_logs_export.csv"',
        },
    )


# ---------------------------------------------------------------------------
# Access control (Settings > Access tab) — Administrator only
# ---------------------------------------------------------------------------


@router.get(
    "/access",
    response_model=AccessSettingsResponse,
    summary="Get access-control settings",
    description="Returns the anonymous-access toggle and the OIDC claim -> role mapping list.",
    dependencies=[Depends(require_role(*ADMIN_ONLY))],
)
async def get_access_settings():
    from app.core.general_settings import general_settings
    return AccessSettingsResponse(
        anonymous_access=general_settings.anonymous_access,
        role_mappings=[RoleMapping(**m) for m in general_settings.role_mappings],
    )


@router.put(
    "/access",
    response_model=AccessSettingsResponse,
    summary="Update access-control settings",
    description=(
        "Update the anonymous-access toggle and/or replace the OIDC claim -> role "
        "mapping list (the mapping list is replaced wholesale, not merged — send "
        "the full desired list). There is no configurable default role for "
        "unmapped users: any OIDC caller matching none of these mappings is "
        "always denied."
    ),
    dependencies=[Depends(require_role(*ADMIN_ONLY))],
)
async def update_access_settings(body: AccessSettings):
    from app.core.general_settings import general_settings
    if body.anonymous_access is not None:
        general_settings.anonymous_access = body.anonymous_access
    if body.role_mappings is not None:
        general_settings.role_mappings = [m.model_dump() for m in body.role_mappings]
    return AccessSettingsResponse(
        anonymous_access=general_settings.anonymous_access,
        role_mappings=[RoleMapping(**m) for m in general_settings.role_mappings],
    )


# ---------------------------------------------------------------------------
# SSO connection settings (Settings > SSO tab) — Administrator only
# ---------------------------------------------------------------------------


@router.get(
    "/sso",
    response_model=SSOSettingsResponse,
    summary="Get SSO (OIDC) connection settings",
    description="The client secret is never returned — see `client_secret_set`.",
    dependencies=[Depends(require_role(*ADMIN_ONLY))],
)
async def get_sso_settings():
    from app.core.general_settings import general_settings
    sso = general_settings.sso
    return SSOSettingsResponse(
        issuer_url=sso.get("issuer_url"),
        client_id=sso.get("client_id"),
        client_secret_set=bool(sso.get("client_secret")),
        scopes=sso.get("scopes"),
        redirect_uri=sso.get("redirect_uri"),
    )


@router.put(
    "/sso",
    response_model=SSOSettingsResponse,
    summary="Update SSO (OIDC) connection settings",
    description=(
        "Updates the OIDC provider connection used by `/auth/login`. Omit or "
        "send an empty `client_secret` to leave the currently stored secret "
        "unchanged — it is never echoed back by `GET`."
    ),
    dependencies=[Depends(require_role(*ADMIN_ONLY))],
)
async def update_sso_settings(body: SSOSettings):
    from app.core.general_settings import general_settings
    update = {}
    if body.issuer_url is not None:
        update["issuer_url"] = body.issuer_url
    if body.client_id is not None:
        update["client_id"] = body.client_id
    if body.client_secret:  # blank/omitted = keep the existing secret
        update["client_secret"] = body.client_secret
    if body.scopes is not None:
        update["scopes"] = body.scopes
    if body.redirect_uri is not None:
        update["redirect_uri"] = body.redirect_uri
    if update:
        general_settings.sso = update

    sso = general_settings.sso
    return SSOSettingsResponse(
        issuer_url=sso.get("issuer_url"),
        client_id=sso.get("client_id"),
        client_secret_set=bool(sso.get("client_secret")),
        scopes=sso.get("scopes"),
        redirect_uri=sso.get("redirect_uri"),
    )


@router.post(
    "/sso/test",
    response_model=SSOTestResponse,
    summary="Test an OIDC issuer URL",
    description=(
        "Resolves the OpenID Connect discovery document for the given issuer "
        "URL — which may not be saved yet — and reports back the endpoints it "
        "found. This validates reachability and provider configuration; it "
        "does not perform a real login (use `/auth/login` for that, which "
        "works regardless of the current anonymous-access setting)."
    ),
    dependencies=[Depends(require_role(*ADMIN_ONLY))],
)
async def test_sso_connection(body: SSOTestRequest):
    try:
        doc = await oidc.discover(body.issuer_url)
    except oidc.OIDCError as exc:
        return SSOTestResponse(ok=False, message=str(exc))
    return SSOTestResponse(
        ok=True,
        message="Discovery document resolved successfully.",
        authorization_endpoint=doc.get("authorization_endpoint"),
        token_endpoint=doc.get("token_endpoint"),
        jwks_uri=doc.get("jwks_uri"),
        userinfo_endpoint=doc.get("userinfo_endpoint"),
        end_session_endpoint=doc.get("end_session_endpoint"),
    )
