"""
General settings backed by ./settings/general.yaml.

These settings complement the env-var-based Settings in config.py.
They are stored in a YAML file so they can be updated via the API
and survive application restarts without requiring env var changes.

Usage:
    from app.core.general_settings import general_settings
    limit = general_settings.row_limit
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from app.core.logging import get_logger

_logger = get_logger("buchimaker.general_settings")

# Default values used if the YAML file is missing or a key is absent
_DEFAULTS: Dict[str, Any] = {
    "row_limit": 1000,
    "redis_ttl_seconds": 1800,
    "auto_refresh": None,
    "active_widget_set": "default",
    "redis_host": None,
    "redis_port": None,
    "redis_password": None,
    "redis_user": None,
    "redis_tls_enabled": None,
    "syslog": {
        "enabled": False,
        "host": None,
        "port": None,
        "tls_enabled": False,
        "cert_path": None,
        "key_path": None,
        "ca_cert_path": None,
    },
    # Off by default: an admin must opt in via PUT /system/settings. This
    # endpoint executes arbitrary SELECT queries directly against DuckDB.
    "sql_api_enabled": False,
    # Runtime-owned replacement for the ANONYMOUS_USER env var — see
    # _load()'s first-run seeding below. True = every request is treated
    # as the anonymous Administrator, no OIDC login required.
    "anonymous_access": True,
    # OIDC connection settings backing the Settings > SSO tab. Populated
    # either through the UI or, for GitOps-style deployments, seeded from
    # OIDC_ISSUER_URL/OIDC_CLIENT_ID/OIDC_CLIENT_SECRET/OIDC_REDIRECT_URI
    # env vars the same way redis_host/redis_port fall back to env vars
    # below — see the `sso` property.
    "sso": {
        "issuer_url": None,
        "client_id": None,
        "client_secret": None,
        "scopes": "openid profile email",
        "redirect_uri": None,
    },
    # OIDC claim -> system role mapping backing the Settings > Access tab.
    # Each entry: {"claim": "groups", "value": "admins", "role": "Administrator"}.
    # Evaluated in order; the first match wins. No match = Deny.
    "role_mappings": [],
}

_SETTINGS_PATH = Path("./settings/general.yaml")


class GeneralSettings:
    """Mutable general settings persisted in ``./settings/general.yaml``.

    Thread-safe via an internal lock.  Reads the YAML file on construction.

    Attributes:
        row_limit: Maximum rows per DuckDB result set (default 1000).
        redis_ttl_seconds: Redis key expiration in seconds (default 1800 = 30 min).
        active_widget_set: ID of the widget set the frontend currently uses (default "default").
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = dict(_DEFAULTS)
        self._load()

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def row_limit(self) -> int:
        """Maximum rows returned per query result."""
        with self._lock:
            return int(self._data.get("row_limit", _DEFAULTS["row_limit"]))

    @row_limit.setter
    def row_limit(self, value: int) -> None:
        if value < 1:
            raise ValueError("row_limit must be >= 1.")
        with self._lock:
            self._data["row_limit"] = value
        self._save()

    @property
    def redis_ttl_seconds(self) -> int:
        """Redis cache TTL in seconds (0 = no expiry)."""
        with self._lock:
            return int(self._data.get("redis_ttl_seconds", _DEFAULTS["redis_ttl_seconds"]))

    @redis_ttl_seconds.setter
    def redis_ttl_seconds(self, value: int) -> None:
        if value < 0:
            raise ValueError("redis_ttl_seconds must be >= 0.")
        with self._lock:
            self._data["redis_ttl_seconds"] = value
        self._save()

    @property
    def auto_refresh(self) -> Any:
        """Global auto-refresh setting: None, int, or list of cron strings."""
        with self._lock:
            return self._data.get("auto_refresh", _DEFAULTS.get("auto_refresh"))

    @auto_refresh.setter
    def auto_refresh(self, value: Any) -> None:
        with self._lock:
            self._data["auto_refresh"] = value
        self._save()

    @property
    def active_widget_set(self) -> str:
        """ID of the widget set the frontend currently renders dashboards with."""
        with self._lock:
            return str(self._data.get("active_widget_set", _DEFAULTS["active_widget_set"]))

    @active_widget_set.setter
    def active_widget_set(self, value: str) -> None:
        if not value or not value.strip():
            raise ValueError("active_widget_set must not be empty.")
        with self._lock:
            self._data["active_widget_set"] = value
        self._save()

    @property
    def redis_host(self) -> Optional[str]:
        with self._lock:
            return self._data.get("redis_host", _DEFAULTS.get("redis_host"))

    @redis_host.setter
    def redis_host(self, value: Optional[str]) -> None:
        with self._lock:
            self._data["redis_host"] = value
        self._save()

    @property
    def redis_port(self) -> Optional[int]:
        with self._lock:
            port = self._data.get("redis_port", _DEFAULTS.get("redis_port"))
            return int(port) if port is not None else None

    @redis_port.setter
    def redis_port(self, value: Optional[int]) -> None:
        with self._lock:
            self._data["redis_port"] = value
        self._save()

    @property
    def redis_password(self) -> Optional[str]:
        with self._lock:
            return self._data.get("redis_password", _DEFAULTS.get("redis_password"))

    @redis_password.setter
    def redis_password(self, value: Optional[str]) -> None:
        with self._lock:
            self._data["redis_password"] = value
        self._save()

    @property
    def redis_user(self) -> Optional[str]:
        with self._lock:
            return self._data.get("redis_user", _DEFAULTS.get("redis_user"))

    @redis_user.setter
    def redis_user(self, value: Optional[str]) -> None:
        with self._lock:
            self._data["redis_user"] = value
        self._save()

    @property
    def redis_tls_enabled(self) -> Optional[bool]:
        with self._lock:
            return self._data.get("redis_tls_enabled", _DEFAULTS.get("redis_tls_enabled"))

    @redis_tls_enabled.setter
    def redis_tls_enabled(self, value: Optional[bool]) -> None:
        with self._lock:
            self._data["redis_tls_enabled"] = value
        self._save()

    @property
    def syslog(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data.get("syslog", _DEFAULTS["syslog"]))

    @syslog.setter
    def syslog(self, value: Dict[str, Any]) -> None:
        with self._lock:
            current = self._data.get("syslog", _DEFAULTS["syslog"]).copy()
            current.update(value)
            self._data["syslog"] = current
        self._save()
        self._apply_syslog()

    def _apply_syslog(self):
        """Reconfigure the syslog handler in a background thread.

        ``configure_syslog()`` constructs a ``TLSSysLogHandler``, whose
        constructor connects eagerly — for a TCP+TLS target that's
        unreachable, that connect can block for the OS's full TCP timeout
        (over two minutes on Linux by default). Calling it synchronously
        here would hang whatever called this: the `PUT /system/settings`
        request when an admin saves a bad host, or — far worse — the entire
        application's startup, since this same setter runs from `_load()`
        when a previously-saved config is read back on boot. Running it on
        a one-off daemon thread keeps both paths instant regardless of
        whether the configured syslog target is reachable.
        """
        import threading
        from app.core.logging import configure_syslog
        syslog_data = self.syslog
        threading.Thread(
            target=configure_syslog,
            kwargs=dict(
                enabled=syslog_data.get("enabled", False),
                host=syslog_data.get("host"),
                port=syslog_data.get("port"),
                tls_enabled=syslog_data.get("tls_enabled", False),
                cert_path=syslog_data.get("cert_path"),
                key_path=syslog_data.get("key_path"),
                ca_cert_path=syslog_data.get("ca_cert_path"),
            ),
            daemon=True,
            name="buchimaker-syslog-configure",
        ).start()

    @property
    def sql_api_enabled(self) -> bool:
        with self._lock:
            return bool(self._data.get("sql_api_enabled", _DEFAULTS["sql_api_enabled"]))

    @sql_api_enabled.setter
    def sql_api_enabled(self, value: bool) -> None:
        with self._lock:
            self._data["sql_api_enabled"] = value
        self._save()

    @property
    def anonymous_access(self) -> bool:
        """Runtime toggle: True = no OIDC login required, everyone is Administrator."""
        with self._lock:
            return bool(self._data.get("anonymous_access", _DEFAULTS["anonymous_access"]))

    @anonymous_access.setter
    def anonymous_access(self, value: bool) -> None:
        with self._lock:
            self._data["anonymous_access"] = bool(value)
        self._save()

    @property
    def sso(self) -> Dict[str, Any]:
        """OIDC connection settings, falling back to env vars for unset fields.

        Mirrors the redis_host/redis_port env-fallback pattern above, so a
        deployment can preconfigure SSO via OIDC_ISSUER_URL/OIDC_CLIENT_ID/
        OIDC_CLIENT_SECRET/OIDC_REDIRECT_URI without touching the UI.
        """
        with self._lock:
            data = dict(self._data.get("sso", _DEFAULTS["sso"]))
        data["issuer_url"] = data.get("issuer_url") or os.getenv("OIDC_ISSUER_URL") or None
        data["client_id"] = data.get("client_id") or os.getenv("OIDC_CLIENT_ID") or None
        data["client_secret"] = data.get("client_secret") or os.getenv("OIDC_CLIENT_SECRET") or None
        data["redirect_uri"] = data.get("redirect_uri") or os.getenv("OIDC_REDIRECT_URI") or None
        data.setdefault("scopes", _DEFAULTS["sso"]["scopes"])
        return data

    @sso.setter
    def sso(self, value: Dict[str, Any]) -> None:
        with self._lock:
            current = dict(self._data.get("sso", _DEFAULTS["sso"]))
            current.update(value)
            self._data["sso"] = current
        self._save()

    @property
    def role_mappings(self) -> List[Dict[str, Any]]:
        """OIDC claim -> role mapping list, evaluated in order (first match wins)."""
        with self._lock:
            return list(self._data.get("role_mappings", _DEFAULTS["role_mappings"]))

    @role_mappings.setter
    def role_mappings(self, value: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._data["role_mappings"] = list(value)
        self._save()

    def as_dict(self) -> Dict[str, Any]:
        """Return a copy of all settings as a plain dict."""
        with self._lock:
            return dict(self._data)

    def update(self, updates: Dict[str, Any]) -> None:
        """Apply a batch of setting updates and persist.

        Args:
            updates: Dict of setting_key → new_value.

        Raises:
            ValueError: If any key is unknown or value is invalid.
        """
        unknown = set(updates) - set(_DEFAULTS)
        if unknown:
            raise ValueError(f"Unknown setting keys: {unknown}")
        with self._lock:
            self._data.update(updates)
        self._save()

    # ------------------------------------------------------------------
    # Internal I/O
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Read settings from the YAML file; fall back to defaults on error."""
        if not _SETTINGS_PATH.exists():
            _logger.info("general_settings_not_found", path=str(_SETTINGS_PATH))
            # First-ever run (no persisted general.yaml yet): seed the new
            # runtime-owned anonymous_access toggle from the legacy
            # ANONYMOUS_USER env var so upgrading an existing deployment
            # doesn't silently change its auth posture. Every run after
            # this one is controlled entirely via the Access tab / this
            # YAML file — the env var is never consulted again.
            try:
                from app.core.config import get_settings
                with self._lock:
                    self._data["anonymous_access"] = get_settings().anonymous_user == "ALLOW"
            except Exception as exc:
                _logger.warning("anonymous_access_seed_error", error=str(exc))
            return
        try:
            with _SETTINGS_PATH.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            with self._lock:
                for key, default in _DEFAULTS.items():
                    self._data[key] = data.get(key, default)
            _logger.info("general_settings_loaded", path=str(_SETTINGS_PATH))
            self._apply_syslog()
        except Exception as exc:
            _logger.error("general_settings_load_error", error=str(exc))

    def _save(self) -> None:
        """Persist current settings to the YAML file."""
        try:
            _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = dict(self._data)
            with _SETTINGS_PATH.open("w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False)
            _logger.info("general_settings_saved", path=str(_SETTINGS_PATH))
        except Exception as exc:
            _logger.error("general_settings_save_error", error=str(exc))


# Module-level singleton
general_settings = GeneralSettings()
