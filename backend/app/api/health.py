"""
API router: /healthz

Serves the liveness/readiness probe on the main port (8000).
A second copy of this router runs on the dedicated health port (35400)
via a lightweight uvicorn server started in main.py — ensuring health
probes remain responsive under heavy API load.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.db import get_db
from app.core.logging import get_logger
from app.models.schemas import HealthResponse
from app.models.system_manager import system_manager

router = APIRouter(tags=["health"])
_logger = get_logger("buchimaker.health")


@router.get(
    "/healthz",
    response_model=HealthResponse,
    summary="Liveness / readiness probe",
    description=(
        "Checks that all critical subsystems are operational:\n\n"
        "1. **DuckDB** — executes `SELECT 1` and verifies the result.\n"
        "2. **Dashboards** — reports the count of successfully loaded dashboards.\n"
        "3. **Data sources** — reports the count of registered connectors.\n\n"
        "Returns **200 OK** with `status: ok` when healthy.\n"
        "Returns **503 Service Unavailable** with `status: degraded` when DuckDB is unavailable.\n\n"
        "> This endpoint is also exposed on port **35400** for Docker/k8s health probes."
    ),
    responses={
        200: {
            "description": "All subsystems healthy.",
            "content": {
                "application/json": {
                    "example": {
                        "status": "ok",
                        "version": "0.3.0",
                        "duckdb_ok": True,
                        "dashboards_loaded": 2,
                        "data_sources_loaded": 1,
                        "details": {"duckdb": "ok"},
                    }
                }
            },
        },
        503: {
            "description": "DuckDB is unavailable — system is degraded.",
            "content": {
                "application/json": {
                    "example": {
                        "status": "degraded",
                        "version": "0.3.0",
                        "duckdb_ok": False,
                        "dashboards_loaded": 0,
                        "data_sources_loaded": 0,
                        "details": {"duckdb": "error: connection refused"},
                    }
                }
            },
        },
    },
)
async def healthcheck() -> JSONResponse:
    """Execute a lightweight health probe against all critical subsystems.

    Args: None

    Returns:
        200 HealthResponse with ``status: ok`` when all checks pass.
        503 HealthResponse with ``status: degraded`` on DuckDB failure.
    """
    settings = get_settings()
    details: dict = {}
    duckdb_ok = False

    try:
        con = get_db()
        result = con.execute("SELECT 1").fetchone()
        duckdb_ok = result is not None and result[0] == 1
        details["duckdb"] = "ok"
    except Exception as exc:
        details["duckdb"] = f"error: {exc}"
        _logger.error("health_duckdb_failed", error=str(exc))

    summary = system_manager.health_summary()

    status = "ok" if duckdb_ok else "degraded"
    http_code = 200 if duckdb_ok else 503

    body = HealthResponse(
        status=status,
        version=settings.app_version,
        duckdb_ok=duckdb_ok,
        dashboards_loaded=summary["dashboards_loaded"],
        data_sources_loaded=summary["data_sources_loaded"],
        details=details,
    )

    _logger.info(
        "healthcheck",
        status=status,
        duckdb_ok=duckdb_ok,
        dashboards=summary["dashboards_loaded"],
        sources=summary["data_sources_loaded"],
    )
    return JSONResponse(content=body.model_dump(), status_code=http_code)
