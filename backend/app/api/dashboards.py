"""
API router: /dashboards  –  dashboard lifecycle and widget data retrieval.

Endpoints
---------
GET    /api/v1/dashboards                                    – list all loaded dashboards
POST   /api/v1/dashboards/load                               – load a dashboard from YAML
GET    /api/v1/dashboards/{dashboard_id}                     – get dashboard definition
POST   /api/v1/dashboards/{dashboard_id}/data                – apply filters AND get all widget data
POST   /api/v1/dashboards/{dashboard_id}/widgets/{widget_id}/page  – paginated table rows (no Redis)
GET    /api/v1/dashboards/{dashboard_id}/virtual-tables      – list DuckDB views
DELETE /api/v1/dashboards/{dashboard_id}                     – unregister a dashboard

Data Endpoint Design
--------------------
POST /{dashboard_id}/data combines filter resolution and data retrieval in a
single round-trip.  Filters are passed as a widget-id-keyed dict (body) and/or
as URL query parameters (simple equality filters, useful for deep-links).
Query-param filters are pinned/locked: they always override a body filter with
the same key, so a shareable link's fixed filters can't be silently dropped by
later widget interaction on the same page.

Pagination Endpoint Design
--------------------------
POST /{dashboard_id}/widgets/{widget_id}/page fetches a single page of rows
from a specific table widget.  Redis is **never** consulted — DuckDB's native
LIMIT/OFFSET is used directly.  The same widget-id-keyed filter format applies.

Caching
-------
On the first request for a filter combination, DuckDB is queried and the result
is GZIP-compressed, stored in Redis, and returned.
Subsequent requests with the same filter hash pull directly from Redis.
"""

from __future__ import annotations

import gzip
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import Response, StreamingResponse

from app.core.general_settings import general_settings
from app.core.logging import get_logger
from app.core.redis_client import redis_cache
from app.core.roles import ANY_AUTHENTICATED, DATA_ADMIN_OR_ABOVE, require_role
from app.core.validator import raise_if_invalid_name
from app.models.schemas import (
    DashboardDataRequest,
    DashboardDataResponse,
    DashboardDefinitionResponse,
    DashboardInfo,
    DashboardLoadRequest,
    DashboardLoadResponse,
    TablePageRequest,
    TablePageResponse,
)
from app.models.system_manager import system_manager

router = APIRouter(prefix="/dashboards", tags=["dashboards"])
_logger = get_logger("buchimaker.api.dashboards")

# ---------------------------------------------------------------------------
# Reusable error response specs
# ---------------------------------------------------------------------------
_404_dash = {
    "description": "Dashboard not found — not loaded or wrong ID.",
    "content": {
        "application/json": {
            "example": {"detail": "Dashboard 'sales_overview' not found."}
        }
    },
}
_422_name = {
    "description": "Dashboard ID or filter field contains invalid characters.",
    "content": {
        "application/json": {
            "example": {
                "detail": (
                    "dashboard_id 'bad!id' contains invalid characters. "
                    "Only alphanumeric characters, spaces, dashes (-), "
                    "and underscores (_) are allowed."
                )
            }
        }
    },
}
_422_yaml = {
    "description": "YAML file not found or contains a syntax error.",
    "content": {
        "application/json": {
            "example": {"detail": "YAML syntax error (line 14): mapping values are not allowed here"}
        }
    },
}


# ---------------------------------------------------------------------------
# List dashboards
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=List[DashboardInfo],
    summary="List all loaded dashboards",
    description=(
        "Returns a lightweight summary of every dashboard currently registered "
        "in the system.\n\n"
        "Use this endpoint to populate the dashboard selector in the UI or to "
        "check which dashboards were auto-loaded at startup.\n\n"
        "Returns an empty list `[]` if no dashboards have been loaded yet."
    ),
    responses={
        200: {
            "description": "List of loaded dashboards (may be empty).",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "id": "sales_overview",
                            "name": "Sales Overview",
                            "description": "Monthly KPI dashboard",
                            "widget_count": 5,
                            "filter_count": 2,
                            "filepath": "/app/dashboards/sales_overview.yaml"
                        }
                    ]
                }
            },
        }
    },
    dependencies=[Depends(require_role(*ANY_AUTHENTICATED))],
)
async def list_dashboards():
    """Return a summary of every currently loaded dashboard.

    Returns:
        List of DashboardInfo objects, or an empty list.
    """
    result = []
    for dash in system_manager.dashboards.values():
        result.append(
            DashboardInfo(
                id=dash.id,
                name=dash.name,
                description=dash.description,
                widget_count=dash.widget_count(),
                filter_count=len(dash.filters),
                filepath=dash.yaml_path,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Load dashboard
# ---------------------------------------------------------------------------


@router.post(
    "/load",
    response_model=DashboardLoadResponse,
    status_code=status.HTTP_200_OK,
    summary="Load or reload a dashboard from YAML",
    description=(
        "Loads a YAML dashboard definition file and registers it with the system.\n\n"
        "**Hot-reload supported:** calling this endpoint again with the same file "
        "reloads the YAML definition while preserving all active filter caches. "
        "The `last_queried` timestamps on existing filters are preserved so that "
        "the eviction logic does not flush them immediately.\n\n"
        "**File path resolution:**\n"
        "- Absolute path: used as-is (`/app/dashboards/sales.yaml`).\n"
        "- Relative path: resolved against the `DASHBOARDS_DIR` env variable.\n\n"
        "**YAML format:** see `dashboards/template.yaml` for a full example with "
        "all supported widget types, totals, aggregates, and filter configurations."
    ),
    responses={
        200: {
            "description": "Dashboard loaded successfully.",
            "content": {
                "application/json": {
                    "example": {"status": "ok", "dashboards_loaded": 3}
                }
            },
        },
        422: _422_yaml,
    },
    dependencies=[Depends(require_role(*DATA_ADMIN_OR_ABOVE))],
)
async def load_dashboard(body: DashboardLoadRequest):
    """Load a dashboard YAML file and register it with the system.

    Args:
        body: Request containing the filepath to the YAML file.

    Returns:
        DashboardLoadResponse with status and total dashboards loaded count.

    Raises:
        HTTPException: 422 on YAML parse error or file-not-found.
    """
    error = system_manager.load_dashboard(body.filepath)
    if error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=error,
        )
    loaded_ids = list(system_manager.dashboards.keys())
    return DashboardLoadResponse(status="ok", dashboards_loaded=len(loaded_ids))


# ---------------------------------------------------------------------------
# Get dashboard definition
# ---------------------------------------------------------------------------


@router.get(
    "/{dashboard_id}",
    response_model=DashboardDefinitionResponse,
    summary="Get dashboard definition",
    description=(
        "Returns the full dashboard definition the frontend uses to construct the "
        "grid layout and bind widget data sources.\n\n"
        "**Response includes:**\n"
        "- `summaries` — single-value widgets (tiles, gauges); results appear in "
        "`POST /data` → `totals` dict.\n"
        "- `aggregates` — multi-row widgets (charts, tables); results appear in "
        "`POST /data` → `aggregates` dict.\n"
        "- `source_table` — the DuckDB table/view name this dashboard queries.\n"
        "- `settings` — per-dashboard operational settings.\n\n"
        "> Dashboard IDs are defined in the YAML `id` field. "
        "Example: `id: sales_overview` in YAML → `dashboard_id=sales_overview` here."
    ),
    responses={
        200: {
            "description": "Dashboard definition.",
            "content": {
                "application/json": {
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
                        ],
                        "settings": {
                            "refresh_frequency_filter": 10,
                            "unallocate_frequency_filter": 30,
                        },
                    }
                }
            },
        },
        404: _404_dash,
        422: _422_name,
    },
    dependencies=[Depends(require_role(*ANY_AUTHENTICATED))],
)
async def get_dashboard_definition(
    dashboard_id: str = Path(
        description=(
            "Dashboard ID as defined in the YAML `id` field. "
            "Example: `sales_overview`"
        )
    ),
):
    """Return the full dashboard definition for frontend UI construction.

    Args:
        dashboard_id: Dashboard ID from the YAML ``id`` field.

    Returns:
        DashboardDefinitionResponse with summaries, aggregates, and settings.

    Raises:
        HTTPException: 404 if dashboard is not loaded.
        HTTPException: 422 if dashboard_id contains invalid characters.
    """
    raise_if_invalid_name(dashboard_id, "dashboard_id")
    dash = system_manager.dashboards.get(dashboard_id)
    if not dash:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dashboard '{dashboard_id}' not found.",
        )
    raw = dash.get_dashboard_definition()
    return DashboardDefinitionResponse(**raw)


# ---------------------------------------------------------------------------
# POST /data  —  unified filter + data endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/{dashboard_id}/data",
    summary="Get all widget data (with optional filters)",
    response_model=DashboardDataResponse,
    response_class=Response,
    description=(
        "Resolves filter conditions and returns all widget data for the dashboard "
        "in a single GZIP-compressed JSON response.\n\n"
        "**Filter format:**\n"
        "Filters are a dict of `widget_id → value`.  The `widget_id` must match "
        "a filter-type widget declared in the dashboard YAML layout "
        "(`button_filter`, `input_filter`, `dropdown_multy`, `dropdown_single`).\n\n"
        "- **Simple value** (string): implied `eq` operator.\n"
        "- **Operator object** `{\"operator\": \"has\", \"value\": \"x\"}`: explicit operator.\n\n"
        "**Supported operators:** `eq`, `neq`, `gt`, `lt`, `gte`, `lte`, "
        "`has` (substring), `prefix`, `suffix`.\n\n"
        "**Query-parameter filters:** simple (equality) filters can also be passed "
        "as URL query parameters using the `f[widget_id]=value` syntax.  "
        "Query-param filters are pinned/locked — they always win over a body filter "
        "with the same key, even across repeated requests (e.g. from later widget "
        "interaction). Use this for shareable/embedded links that must not be "
        "overridable from the dashboard UI.\n\n"
        "**Caching:**\n"
        "- First request for a filter combination queries DuckDB, GZIP-compresses "
        "the result, caches it in Redis, and returns it.\n"
        "- Subsequent requests with the same filter hash are served directly from "
        "Redis without touching DuckDB.\n"
        "- When a data source is reloaded, all related cache entries are invalidated.\n\n"
        "**Row limit:** configurable in `./settings/general.yaml` "
        f"(current default: 1000 rows per result set).\n\n"
        "**Response:** the JSON body is GZIP-compressed. "
        "Set `Accept-Encoding: gzip` on your client (most HTTP clients do this automatically)."
    ),
    responses={
        200: {
            "description": "All widget data, GZIP-compressed.",
            "content": {
                "application/json": {
                    "example": {
                        "dashboard_id": "sales_overview",
                        "filter_hash": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
                        "from_cache": False,
                        "row_limit": 1000,
                        "totals": {"total_orders": 1482, "revenue": 248300.50},
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
            },
        },
        404: _404_dash,
        422: _422_name,
    },
    dependencies=[Depends(require_role(*ANY_AUTHENTICATED))],
)
async def post_dashboard_data(
    request: Request,
    dashboard_id: str = Path(
        description="Dashboard ID as defined in the YAML `id` field. Example: `sales_overview`"
    ),
    body: Optional[DashboardDataRequest] = None,
):
    """Apply filters and return all widget data, with Redis caching.

    Filter sources (merged with query params pinned/locked, i.e. always winning):
    1. JSON body ``filters`` dict (supports operators)
    2. URL query parameters:  ``?f[widget_id]=value`` (equality only) — overrides
       a body filter with the same key

    Args:
        request: Raw Starlette request (for reading query params).
        dashboard_id: Target dashboard ID.
        body: Optional request body with ``dashboard`` and ``filters`` fields.

    Returns:
        GZIP-compressed JSON (Content-Encoding: gzip) matching DashboardDataResponse.

    Raises:
        HTTPException: 404 if dashboard is not loaded.
        HTTPException: 422 if dashboard_id or filter keys contain invalid characters.
        HTTPException: 400 if ``body.dashboard`` does not match the path ``dashboard_id``.
        HTTPException: 400 if an unknown operator is provided.
    """
    raise_if_invalid_name(dashboard_id, "dashboard_id")
    dash = system_manager.dashboards.get(dashboard_id)
    if not dash:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dashboard '{dashboard_id}' not found.",
        )

    # -- Validate body.dashboard assertion ------------------------------------
    if body and body.dashboard and body.dashboard != dashboard_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Body 'dashboard' field '{body.dashboard}' does not match "
                f"path 'dashboard_id' '{dashboard_id}'."
            ),
        )

    # -- Collect query-param filters  (f[widget_id]=value) --------------------
    qp_filters: Dict[str, Any] = {}
    for key, value in request.query_params.items():
        if key.startswith("f[") and key.endswith("]"):
            widget_id = key[2:-1]
            qp_filters[widget_id] = value  # always equality from query params

    # -- Merge: query-param filters are pinned/locked and always win ----------
    # Query params represent a shareable/embedded link's fixed filters — they
    # must override whatever the widget UI (body) currently holds, not just
    # seed it, so re-fetches triggered by later widget interaction can't
    # silently drop the pin.
    merged_filters: Dict[str, Any] = dict(body.filters) if body and body.filters else {}
    merged_filters.update(qp_filters)  # query params win on conflict

    # -- Parse and validate filters -------------------------------------------
    try:
        conditions, filter_hash = dash.parse_widget_filters(merged_filters)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    row_limit = general_settings.row_limit

    # -- Cache lookup ---------------------------------------------------------
    cached = redis_cache.get(dashboard_id, filter_hash)
    if cached is not None:
        _logger.info(
            "dashboard_data_cache_hit",
            dashboard_id=dashboard_id,
            filter_hash=filter_hash,
        )
        # Decompress to inject from_cache=True then re-compress. A raw byte
        # replace on the known `"from_cache": false` substring (rather than a
        # full json.loads/dumps round-trip) keeps this cheap — the payload is
        # always serialized with json.dumps's default separators, so the
        # substring is stable.
        patched = gzip.compress(
            gzip.decompress(cached).replace(b'"from_cache": false', b'"from_cache": true', 1)
        )
        return Response(
            content=patched,
            media_type="application/json",
            headers={"Content-Encoding": "gzip", "X-Cache": "HIT"},
        )

    # -- DuckDB query ---------------------------------------------------------
    _logger.info(
        "dashboard_data_duckdb_query",
        dashboard_id=dashboard_id,
        filter_hash=filter_hash,
        conditions=len(conditions),
    )
    try:
        data = dash.get_data_for_filters(conditions=conditions, row_limit=row_limit)
    except Exception as exc:
        _logger.error("dashboard_data_query_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Data query failed: {exc}",
        ) from exc

    # -- Build response payload -----------------------------------------------
    payload = {
        "dashboard_id": dashboard_id,
        "filter_hash": filter_hash,
        "from_cache": False,
        "row_limit": row_limit,
        "totals": data["totals"],
        "aggregates": data["aggregates"],
        "raw": data["raw"],
        "widget_map": data["widget_map"],
    }

    # -- GZIP compress --------------------------------------------------------
    compressed = gzip.compress(json.dumps(payload, default=str).encode("utf-8"))

    # -- Store in Redis -------------------------------------------------------
    redis_cache.set(
        dashboard_id=dashboard_id,
        filter_hash=filter_hash,
        payload=compressed,
        datasource_names=data.get("source_tables", []),
        ttl_seconds=general_settings.redis_ttl_seconds,
    )

    return Response(
        content=compressed,
        media_type="application/json",
        headers={"Content-Encoding": "gzip", "X-Cache": "MISS"},
    )


# ---------------------------------------------------------------------------
# Virtual tables
# ---------------------------------------------------------------------------


@router.get(
    "/{dashboard_id}/virtual-tables",
    response_model=List[str],
    summary="List active DuckDB virtual tables",
    description=(
        "Returns the names of all DuckDB views currently created by this "
        "dashboard's filter cache.\n\n"
        "**Use this for:**\n"
        "- Debugging: verify that filter views exist after calling POST /filters.\n"
        "- Monitoring: track how many filter views are consuming RAM.\n\n"
        "Returns an empty list if no filters have been applied yet.\n\n"
        "View names follow the pattern `__filter_<hash>` where `<hash>` is the "
        "first 32 characters of the filter MD5 hex digest."
    ),
    responses={
        200: {
            "description": "List of active DuckDB view names for this dashboard.",
            "content": {
                "application/json": {
                    "example": [
                        "__filter_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
                        "__filter_deadbeef0123456789abcdef01234567",
                    ]
                }
            },
        },
        404: _404_dash,
        422: _422_name,
    },
    dependencies=[Depends(require_role(*DATA_ADMIN_OR_ABOVE))],
)
async def list_virtual_tables(
    dashboard_id: str = Path(
        description="Dashboard ID. Example: `sales_overview`"
    ),
):
    """Return all active DuckDB views created by this dashboard's filters.

    Args:
        dashboard_id: Dashboard ID.

    Returns:
        List of DuckDB view name strings.

    Raises:
        HTTPException: 404 if dashboard is not loaded.
        HTTPException: 422 if dashboard_id contains invalid characters.
    """
    raise_if_invalid_name(dashboard_id, "dashboard_id")
    dash = system_manager.dashboards.get(dashboard_id)
    if not dash:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dashboard '{dashboard_id}' not found.",
        )
    return dash.list_virtual_tables()


# ---------------------------------------------------------------------------
# Delete dashboard
# ---------------------------------------------------------------------------


@router.delete(
    "/{dashboard_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unregister a dashboard",
    description=(
        "Removes the dashboard from the registry and drops all its DuckDB filter views, "
        "freeing the associated RAM.\n\n"
        "The underlying data source (CSV/JSON/BigQuery table) is **not** removed. "
        "Only the dashboard definition and its cached filter views are cleaned up.\n\n"
        "Returns **204 No Content** on success (no response body)."
    ),
    responses={
        204: {"description": "Dashboard unregistered. No response body."},
        404: _404_dash,
        422: _422_name,
    },
    dependencies=[Depends(require_role(*DATA_ADMIN_OR_ABOVE))],
)
async def delete_dashboard(
    dashboard_id: str = Path(
        description="Dashboard ID to remove. Example: `sales_overview`"
    ),
):
    """Remove a dashboard and free all its DuckDB views.

    Args:
        dashboard_id: Dashboard ID to remove.

    Raises:
        HTTPException: 404 if dashboard is not loaded.
        HTTPException: 422 if dashboard_id contains invalid characters.
    """
    raise_if_invalid_name(dashboard_id, "dashboard_id")
    if not system_manager.remove_dashboard(dashboard_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dashboard '{dashboard_id}' not found.",
        )


# ---------------------------------------------------------------------------
# Table pagination (no Redis)
# ---------------------------------------------------------------------------


@router.post(
    "/{dashboard_id}/widgets/{widget_id}/page",
    summary="Paginated table widget rows",
    response_model=TablePageResponse,
    response_class=Response,
    description=(
        "Fetches a single page of rows from a specific table-type widget on the dashboard.\n\n"
        "**Use case:** the user scrolls past the initial row limit in a `basic_table` widget "
        "and the frontend needs the next batch of rows (e.g. rows 1001–1100).\n\n"
        "**Redis is bypassed entirely.** DuckDB's native ``LIMIT``/``OFFSET`` is used "
        "directly — this is one of DuckDB's most optimised query patterns and is typically "
        "faster than a Redis lookup for large offsets.\n\n"
        "**Filter format:** same widget-id-keyed dict as `POST /data`. Filters are resolved "
        "and applied inline as a CTE ``WHERE`` clause, then the full aggregate or raw "
        "datasource query is wrapped with ``LIMIT`` and ``OFFSET``.\n\n"
        "**Supported widgets:** any layout widget with a `data` config field "
        "(``basic_table``, ``bar_chart``, ``horizontal_bar_chart``, ``stacked_bar_chart``, ``area_chart``, ``line_chart``, ``pie_chart``, ``sankey``). "
        "Filter widgets (``input_filter``, ``button_filter``, etc.) are rejected.\n\n"
        "**Response:** GZIP-compressed JSON (same format as `/data` rows), plus an exact "
        "`total_count` (`COUNT(*)` over the same filtered query) and `has_more` derived "
        "from it (`offset + len(rows) < total_count`).\n\n"
        "**Limits:** `limit` 1–5000 rows; `offset` must be ≥ 0."
    ),
    responses={
        200: {
            "description": "Paginated rows for the widget, GZIP-compressed.",
            "content": {
                "application/json": {
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
            },
        },
        400: {
            "description": "Widget is not a data widget or has no 'data' config field.",
            "content": {
                "application/json": {
                    "example": {"detail": "Widget 'search_box' is a filter widget. Pagination is only supported for data widgets."}
                }
            },
        },
        404: _404_dash,
        422: _422_name,
    },
    dependencies=[Depends(require_role(*ANY_AUTHENTICATED))],
)
async def get_widget_page(
    dashboard_id: str = Path(
        description="Dashboard ID. Example: `sales_overview`"
    ),
    widget_id: str = Path(
        description=(
            "Layout widget ID to paginate. Must be a data widget with a `data` config field "
            "(e.g. a `basic_table` widget). Example: `table_products`"
        )
    ),
    body: TablePageRequest = TablePageRequest(),
):
    """Fetch a paginated page of rows from a table-type widget.

    This endpoint always queries DuckDB directly.  Redis is never consulted.

    Args:
        dashboard_id: Target dashboard ID.
        widget_id: Layout widget ID (must have a ``data`` config field).
        body: Filters, limit, and offset for the page.

    Returns:
        GZIP-compressed JSON with ``dashboard_id``, ``widget_id``, ``data_key``,
        ``limit``, ``offset``, ``rows``, ``total_count``, and ``has_more``.

    Raises:
        HTTPException: 404 if dashboard is not loaded.
        HTTPException: 422 if dashboard_id or widget_id contain invalid characters.
        HTTPException: 400 if widget is a filter widget or has no data key.
    """
    raise_if_invalid_name(dashboard_id, "dashboard_id")
    raise_if_invalid_name(widget_id, "widget_id")

    dash = system_manager.dashboards.get(dashboard_id)
    if not dash:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dashboard '{dashboard_id}' not found.",
        )

    # Parse and validate filters (same logic as POST /data)
    try:
        conditions, _ = dash.parse_widget_filters(body.filters)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    # Query DuckDB directly — no Redis
    try:
        result = dash.get_widget_page(
            widget_id=widget_id,
            conditions=conditions,
            limit=body.limit,
            offset=body.offset,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        _logger.error("widget_page_failed", widget_id=widget_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Query failed: {exc}",
        ) from exc

    payload = {
        "dashboard_id": dashboard_id,
        "widget_id": widget_id,
        "data_key": result["data_key"],
        "limit": body.limit,
        "offset": body.offset,
        "rows": result["rows"],
        "total_count": result["total_count"],
        "has_more": result["has_more"],
    }
    compressed = gzip.compress(json.dumps(payload, default=str).encode("utf-8"))
    return Response(
        content=compressed,
        media_type="application/json",
        headers={"Content-Encoding": "gzip", "X-Cache": "BYPASS"},
    )


# ---------------------------------------------------------------------------
# CSV Export
# ---------------------------------------------------------------------------


@router.post(
    "/{dashboard_id}/widgets/{widget_id}/export",
    summary="Export table widget data to CSV",
    response_class=Response,
    description=(
        "Exports the underlying data of a widget as a CSV file.\n\n"
        "**Streaming:** Data is streamed directly from DuckDB to the client, "
        "allowing exports of up to 100,000 rows without high memory consumption.\n\n"
        "**Filter format:** Same widget-id-keyed dict as `POST /data`. "
        "Filters are resolved and applied inline as a CTE ``WHERE`` clause.\n\n"
        "**Supported widgets:** any layout widget with a `data` config field "
        "(``basic_table``, ``bar_chart``, etc.). "
        "Filter widgets (``input_filter``, ``button_filter``, etc.) are rejected.\n\n"
        "**Response:** A chunked text/csv stream."
    ),
    responses={
        200: {
            "description": "CSV data stream.",
            "content": {"text/csv": {}},
        },
        400: {
            "description": "Widget is not a data widget or has no 'data' config field.",
            "content": {
                "application/json": {
                    "example": {"detail": "Widget is a filter widget. Export is only supported for data widgets."}
                }
            },
        },
        404: _404_dash,
        422: _422_name,
    },
    dependencies=[Depends(require_role(*ANY_AUTHENTICATED))],
)
async def export_widget_csv(
    dashboard_id: str = Path(description="Dashboard ID. Example: `sales_overview`"),
    widget_id: str = Path(description="Layout widget ID to export. Example: `table_products`"),
    body: DashboardDataRequest = DashboardDataRequest(),
):
    """Export the underlying widget data as a streaming CSV response.

    Args:
        dashboard_id: Target dashboard ID.
        widget_id: Layout widget ID (must have a ``data`` config field).
        body: Filters to apply.

    Returns:
        StreamingResponse with content-type ``text/csv``.
    """
    raise_if_invalid_name(dashboard_id, "dashboard_id")
    raise_if_invalid_name(widget_id, "widget_id")

    dash = system_manager.dashboards.get(dashboard_id)
    if not dash:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dashboard '{dashboard_id}' not found.",
        )

    # Validate dashboard assertion if provided
    if body.dashboard and body.dashboard != dashboard_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Body 'dashboard' field '{body.dashboard}' does not match "
                f"path 'dashboard_id' '{dashboard_id}'."
            ),
        )

    # Parse filters
    try:
        conditions, _ = dash.parse_widget_filters(body.filters)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    # Attempt to create the generator
    try:
        # Pass limit=100000
        generator = dash.export_widget_csv(
            widget_id=widget_id,
            conditions=conditions,
            limit=100000,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        _logger.error("widget_export_failed", widget_id=widget_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Query failed: {exc}",
        ) from exc

    return StreamingResponse(
        generator,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="export_{dashboard_id}_{widget_id}.csv"'
        },
    )

