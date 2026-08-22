"""
Application configuration loaded from environment variables.

All sensitive values (secrets, credentials) must be provided via env vars.
No secrets are allowed in source code (Constitution §Security).
"""

from functools import lru_cache
from pydantic_settings import BaseSettings
from pydantic import ConfigDict, field_validator


class Settings(BaseSettings):
    """Application-wide settings backed by environment variables.

    Attributes:
        app_name: Human-readable application name.
        app_version: Semver string for the running release.
        debug: Enable verbose logging and auto-reload.
        api_port: Port for the FastAPI/uvicorn server.
        health_port: Dedicated port for the /healthz endpoint (per spec: 35400).
        dashboards_dir: Host path (or container mount) for YAML dashboard files.
        data_dir: Host path (or container mount) for local data files (CSV/JSON).
        widgets_dir: Host path (or container mount) for frontend widget-package folders.
        duckdb_database: DuckDB database file path or ":memory:" — slot "a"
            of the active/standby pair used by `global_refresh()`.
        duckdb_database_standby: DuckDB database file path for slot "b" —
            the other half of the active/standby pair.
        duckdb_memory_limit: DuckDB in-process memory cap (e.g. "4GB").
        audit_duckdb_database: Dedicated DuckDB file path for the audit log,
            isolated from `duckdb_database`'s reload lifecycle.
        log_timezone: Timezone name used when formatting log timestamps.
        table_truncate_mb: Row-data payload cap in MB before truncation is applied.
        allow_origins: Comma-separated CORS origins for the frontend container,
            or "*" to allow any origin (the default — see README's Security
            section for why this is safe only because `allow_credentials` is
            hardcoded to `False`, and how to restrict it for production).
        anonymous_user: "ALLOW" to skip authentication entirely, or "DENY" to
            require an OIDC session (see `app/core/auth.py`). Only consulted
            once, to seed `general_settings.anonymous_access` the very first
            time the app runs (no `settings/general.yaml` yet) — after that,
            the Settings > Access tab's "Allow Anonymous Access" toggle is
            the sole runtime source of truth, and this env var is ignored.
    """

    app_name: str = "BuchiMaker"
    app_version: str = "0.3.0"
    debug: bool = False

    api_port: int = 8000
    health_port: int = 35400

    dashboards_dir: str = "/app/dashboards"
    data_dir: str = "/app/data"
    widgets_dir: str = "/app/widgets"

    duckdb_database: str = "./db/duck.db"
    duckdb_database_standby: str = "./db/duck_standby.db"
    duckdb_memory_limit: str = "4GB"

    # Dedicated audit-log store — a separate DuckDB file/connection from
    # `duckdb_database`/`duckdb_database_standby` so audit history survives
    # `global_refresh()`'s active/standby promotion cycle.
    audit_duckdb_database: str = "./db/audit.db"
    log_timezone: str = "local"

    # Operational defaults (can be overridden per-dashboard or per-system)
    refresh_frequency_filter: int = 10   # minutes
    unallocate_frequency_filter: int = 30  # minutes
    table_truncate_mb: int = 700

    allow_origins: str = "*"

    # First-run seed only for general_settings.anonymous_access — see the
    # docstring above and app/core/general_settings.py's _load(). Fails
    # closed: "DENY" is the default so a fresh deployment must opt in to
    # anonymous access rather than silently getting neither auth nor a
    # working OIDC setup.
    anonymous_user: str = "DENY"

    model_config = ConfigDict(env_file=".env", env_file_encoding="utf-8")

    @field_validator("anonymous_user")
    @classmethod
    def _validate_anonymous_user(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"ALLOW", "DENY"}:
            raise ValueError(
                f"ANONYMOUS_USER must be 'ALLOW' or 'DENY', got {value!r}"
            )
        return normalized


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached singleton Settings instance.

    Returns:
        Fully-populated Settings object.
    """
    return Settings()
