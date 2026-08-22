"""
Entry point for the BuchiMaker backend.

Starts two uvicorn servers in separate threads:
  1. Main API server  – all routers, default port 8000
  2. Health server    – /healthz only,  port 35400 (per spec: classes.md)

The dual-server approach ensures the health endpoint is always reachable
even when the main API is under load, and maps cleanly to Docker / k8s
liveness/readiness probe configurations.
"""

import threading

import uvicorn

from app.core.config import get_settings


def run_main() -> None:
    """Launch the primary FastAPI application on the main API port."""
    settings = get_settings()
    uvicorn.run(
        "app.app:app",
        host="0.0.0.0",
        port=settings.api_port,
        reload=settings.debug,
        log_level="debug" if settings.debug else "info",
    )


def run_health() -> None:
    """Launch a lightweight health-only FastAPI application on port 35400.

    A minimal FastAPI app that exposes only /healthz, so the health probe
    does not share a process with the main API load.
    """
    from fastapi import FastAPI
    from app.api.health import router as health_router

    health_app = FastAPI(title="BuchiMaker Health", docs_url=None, redoc_url=None)
    health_app.include_router(health_router)

    settings = get_settings()
    uvicorn.run(
        health_app,
        host="0.0.0.0",
        port=settings.health_port,
        log_level="warning",
    )


if __name__ == "__main__":
    settings = get_settings()

    # Start health server in a daemon thread so it dies with the main process
    health_thread = threading.Thread(
        target=run_health, name="health-server", daemon=True
    )
    health_thread.start()

    # Main API runs in the primary thread (blocking)
    run_main()
