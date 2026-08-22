"""
FastAPI application factory and startup/shutdown lifecycle.

Architecture notes
------------------
- A single FastAPI instance is created by ``create_app()``.
- Two uvicorn servers are started externally:
    * Main API on ``settings.api_port``   (default 8000)
    * Health-only on ``settings.health_port`` (default 35400)
  See ``main.py`` for the dual-server bootstrap.

Middleware stack (inner → outer):
  1. CORSMiddleware           – allows frontend origin(s)
  2. InputValidationMiddleware – rejects oversized payloads
  3. AuditLogMiddleware        – logs every request (Constitution §Security)

Authentication (app/core/auth.py), gated by
``general_settings.anonymous_access`` (Settings > Access tab):
  - True (default on a fresh install) – every request is treated as the
    anonymous Administrator, no checks.
  - False – requires a session cookie created by completing OIDC login at
    /auth/login (app/api/auth.py, app/core/oidc.py). Role-gated per-route
    on top via app/core/roles.require_role().
  Applied per-router (not global middleware) so /healthz and /auth/* stay
  reachable without an existing session.

On startup:
  - structlog is configured.
  - DuckDB connection is initialised.
  - All YAML dashboards in dashboards_dir are auto-loaded.
  - Background refresh thread is started.

On shutdown:
  - Refresh thread is stopped.
  - DuckDB connection is closed.
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from app.api import auth as auth_api
from app.api import dashboards, health, system, widgets
from app.core import audit_db
from app.core.auth import require_authentication
from app.core.config import get_settings
from app.core.db import close_db, get_db
from app.core.logging import AuditLogMiddleware, configure_logging, get_logger
from app.core.validator import InputValidationMiddleware
from app.models.system_manager import system_manager

# ---------------------------------------------------------------------------
# OpenAPI tag metadata — drives the collapsible sections in Swagger UI
# ---------------------------------------------------------------------------

_TAGS_METADATA = [
    {
        "name": "auth",
        "description": (
            "**OIDC login/logout and the current caller's identity.** "
            "`/auth/login` and `/auth/callback` perform the Authorization Code + "
            "PKCE exchange server-side and issue an httpOnly session cookie — "
            "the browser never handles a token. `/auth/me` is polled by the "
            "frontend to decide what to render."
        ),
    },
    {
        "name": "health",
        "description": (
            "**Liveness and readiness probes.** "
            "The `/healthz` endpoint is also served on a dedicated port (35400) "
            "for use by Docker/Kubernetes health checks independently of the main API load. "
            "Returns `200 OK` when all subsystems are healthy, `503` when DuckDB is unavailable."
        ),
    },
    {
        "name": "system",
        "description": (
            "**Global system settings and data-source management.** "
            "Register, inspect, and remove pluggable data-source connectors (CSV, JSON, BigQuery). "
            "Configure system-wide settings such as the data refresh frequency. "
            "\n\n"
            "**Workflow:** "
            "Register at least one data source before loading dashboards. "
            "The connector `name` field becomes the SQL table name referenced in dashboard YAML queries."
        ),
    },
    {
        "name": "dashboards",
        "description": (
            "**Dashboard lifecycle and widget data retrieval.** "
            "Load YAML dashboard definitions and retrieve all widget data in a single call. "
            "\n\n"
            "**Typical frontend workflow:**\n"
            "1. `GET /api/v1/dashboards/{id}` — fetch the dashboard definition to build the grid layout.\n"
            "2. `POST /api/v1/dashboards/{id}/data` — send filter selections and receive all widget "
            "data (totals, aggregates, raw rows) GZIP-compressed in a single response.\n"
            "\n"
            "**Filter format:** widget-id-keyed dict — `{widget_id: value}` or "
            "`{widget_id: {operator, value}}`.\n"
            "**Caching:** Redis is used for repeated filter combinations (same hash = cache hit).\n"
            "**Row limit:** configurable in `./settings/general.yaml` (default 1000 rows)."
        ),
    },
    {
        "name": "widgets",
        "description": (
            "**Widget-set (frontend rendering package) lifecycle.** "
            "A widget set is a folder under `WIDGETS_DIR` containing an `index.js` "
            "that exports a Vue component registry for rendering dashboard widgets. "
            "\n\n"
            "Only one widget set is active at a time — activating one immediately "
            "changes which widget styles the frontend renders dashboards with."
        ),
    },
]

_DESCRIPTION = """
## BuchiMaker Backend API

**API-first dashboard backend** powered by DuckDB in-memory processing and Redis caching.

### Architecture
- **DuckDB** holds all datasets as in-memory virtual tables — no separate database container needed.
- **Data sources** (CSV, JSON, BigQuery) are registered via the `/system` API and become SQL-queryable tables.
- **Dashboards** are defined in YAML files and loaded via the `/dashboards/load` endpoint.
- **Filters** are applied inline in DuckDB queries (no views) and results are cached in Redis by filter hash.

### Security
All user-supplied name fields (`name`, `title`, widget IDs, filter field names) are validated
server-side against the pattern `[a-zA-Z0-9 _\\-]+`. Every API call is logged with IP address,
method, path, status code, and duration.

### Quick-start (try it here)
1. Register a data source: `POST /api/v1/system/data-sources/csv`
2. Load a dashboard: `POST /api/v1/dashboards/load`
3. List dashboards: `GET /api/v1/dashboards`
4. Get all widget data with optional filters: `POST /api/v1/dashboards/{id}/data`

### General Settings
Row limit and Redis TTL are configurable at runtime in `./settings/general.yaml`.
Default: 1000 rows per result set, 30-minute Redis cache TTL.

### Constitution
This API is governed by the [BuchiMaker Constitution v1.0.0](constitution.md) which mandates
API-first architecture, TDD, in-memory DuckDB processing, and full audit logging.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown lifecycle.

    Args:
        app: The FastAPI application instance.

    Yields:
        Control to the running application.
    """
    settings = get_settings()
    configure_logging(debug=settings.debug, log_timezone=settings.log_timezone)
    logger = get_logger("buchimaker.startup")

    # Initialise DuckDB
    get_db()
    # Dedicated audit-log DuckDB file — separate connection/lock, isolated
    # from the data engine's global_refresh() swap lifecycle.
    audit_db.get_audit_db()
    logger.info("buchimaker_starting", version=settings.app_version)

    # Load persisted data sources and dashboards
    system_manager.load_persisted_state()

    # Start background refresh thread and the audit-log writer
    system_manager.start_refresh_thread()
    system_manager.start_audit_writer()
    logger.info("buchimaker_ready", api_port=settings.api_port)

    yield  # Application is running

    # Graceful shutdown
    system_manager.stop_audit_writer()
    system_manager.stop_refresh_thread()
    audit_db.close_audit_db()
    close_db()
    logger.info("buchimaker_shutdown")


def create_app() -> FastAPI:
    """Construct and configure the FastAPI application.

    Returns:
        A fully-wired FastAPI instance ready for uvicorn.
    """
    settings = get_settings()

    app = FastAPI(
        title="BuchiMaker",
        version=settings.app_version,
        description=_DESCRIPTION,
        openapi_tags=_TAGS_METADATA,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        contact={
            "name": "BuchiMaker Team",
            "url": "https://github.com/your-org/buchimaker",
        },
        license_info={
            "name": "MIT",
            "url": "https://opensource.org/licenses/MIT",
        },
        lifespan=lifespan,
    )

    # --- Middleware (order matters: added last = runs first) ----------------
    origins = [o.strip() for o in settings.allow_origins.split(",")]

    # allow_credentials=True is required so the browser attaches the SSO
    # session cookie (app/core/auth.py) on cross-origin requests. Starlette
    # still echoes back the specific request Origin (never a literal "*")
    # whenever allow_credentials=True, even with the default allow_origins
    # of "*" — that satisfies the browser's credentialed-CORS rule without
    # requiring every deployment to list its exact origin. The default
    # docker-compose setup avoids this entirely: nginx proxies /api/ and
    # /auth/ to the backend, so frontend and backend are same-origin and
    # the browser never treats these as cross-origin requests at all.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(InputValidationMiddleware)
    app.add_middleware(AuditLogMiddleware)

    # --- Routers ------------------------------------------------------------
    # health and /auth/* stay unauthenticated (liveness probes, and the
    # login/logout/me endpoints themselves can't require a session to
    # reach them); everything else is gated by require_authentication,
    # with individual routes layering app.core.roles.require_role() on top
    # (see system.py/dashboards.py/widgets.py) for anything beyond Viewer.
    auth_dependency = [Depends(require_authentication)]
    app.include_router(health.router)
    app.include_router(auth_api.router)
    app.include_router(system.router, prefix="/api/v1", dependencies=auth_dependency)
    app.include_router(dashboards.router, prefix="/api/v1", dependencies=auth_dependency)
    app.include_router(widgets.router, prefix="/api/v1", dependencies=auth_dependency)

    return app


# Module-level app instance used by uvicorn in ``main.py``
app = create_app()
