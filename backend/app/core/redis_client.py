"""
Redis client wrapper for BuchiMaker dashboard data caching.

Design decisions
----------------
- Stores GZIP-compressed JSON payloads keyed by ``db:<dashboard_id>:<filter_hash>``.
- Maintains a secondary index ``ds:<datasource_name>:keys`` (a Redis Set) that
  maps each data source to the cache keys it contributed to.  This allows
  targeted invalidation when a data source is reloaded.
- Gracefully degrades: if Redis is unavailable, all cache operations return
  ``None`` / no-op so the application continues without caching.

Configuration
-------------
Redis connection parameters are read from the environment:
    REDIS_HOST  (default: "localhost")
    REDIS_PORT  (default: 6379)
    REDIS_DB    (default: 0)
    REDIS_PASSWORD (default: None)

TTL is pulled from ``general_settings.redis_ttl_seconds`` at call time.
"""

from __future__ import annotations

import os
from typing import List, Optional

from app.core.logging import get_logger

_logger = get_logger("buchimaker.redis")

# Lazy-import redis so the app starts without it installed (CSV-only mode)
try:
    import redis as _redis_module  # type: ignore

    _REDIS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _REDIS_AVAILABLE = False


def create_redis_connection(decode_responses: bool = False):
    """Create a Redis client from the shared connection settings, or None.

    Shared by :class:`RedisCache` (dashboard-data cache, raw gzipped bytes)
    and ``app.core.session_store`` (SSO session store, JSON text) so both
    resolve host/port/password/user/TLS the same way instead of duplicating
    the settings/env fallback logic.

    Args:
        decode_responses: True to get back ``str`` values instead of
            ``bytes`` — the session store wants JSON text, the dashboard
            cache wants raw gzipped bytes.

    Returns:
        A connected ``redis.Redis`` client, or None if the ``redis``
        package isn't installed or the server is unreachable.
    """
    if not _REDIS_AVAILABLE:
        _logger.warning("redis_unavailable", reason="redis package not installed")
        return None
    try:
        from app.core.general_settings import general_settings
        host = general_settings.redis_host or os.getenv("REDIS_HOST", "localhost")
        port = general_settings.redis_port or int(os.getenv("REDIS_PORT", "6379"))
        password = general_settings.redis_password or os.getenv("REDIS_PASSWORD") or None
        user = general_settings.redis_user or os.getenv("REDIS_USER") or None
        tls_str = str(os.getenv("REDIS_TLS_ENABLED", "false")).lower()
        tls_env = tls_str in ("true", "1", "yes")
        tls_enabled = general_settings.redis_tls_enabled if general_settings.redis_tls_enabled is not None else tls_env

        kwargs = {
            "host": host,
            "port": port,
            "db": int(os.getenv("REDIS_DB", "0")),
            "password": password,
            "decode_responses": decode_responses,
            "socket_connect_timeout": 2,
        }
        if user:
            kwargs["username"] = user
        if tls_enabled:
            kwargs["ssl"] = True

        client = _redis_module.Redis(**kwargs)
        client.ping()
        _logger.info(
            "redis_connected",
            host=host,
            port=port,
        )
        return client
    except Exception as exc:
        _logger.warning("redis_connect_failed", error=str(exc))
        return None


def _make_client():
    """Create and return a Redis client for raw-bytes cache use, or None."""
    return create_redis_connection(decode_responses=False)


class RedisCache:
    """Thin wrapper around Redis for dashboard data caching.

    Keys
    ----
    ``db:<dashboard_id>:<filter_hash>``
        Stores the GZIP-compressed dashboard data payload (bytes).

    ``ds:<datasource_name>:keys``
        Redis Set of cache keys that reference this data source.
        Used to bulk-invalidate when the data source is reloaded.

    All methods are no-ops when Redis is unavailable.
    """

    # Key prefix templates
    _DATA_KEY = "db:{dashboard_id}:{filter_hash}"
    _DS_INDEX_KEY = "ds:{datasource_name}:keys"

    def __init__(self) -> None:
        self._client = _make_client()

    @property
    def available(self) -> bool:
        """True if the Redis client is connected and responsive."""
        return self._client is not None

    def reconnect(self) -> None:
        """Force a reconnection using current settings."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = _make_client()

    # ------------------------------------------------------------------
    # Core cache operations
    # ------------------------------------------------------------------

    def get(self, dashboard_id: str, filter_hash: str) -> Optional[bytes]:
        """Retrieve a cached gzipped payload.

        Args:
            dashboard_id: Dashboard ID string.
            filter_hash: MD5 hex digest of the filter set.

        Returns:
            Raw gzipped bytes, or None if not cached / Redis unavailable.
        """
        if not self._client:
            return None
        key = self._DATA_KEY.format(dashboard_id=dashboard_id, filter_hash=filter_hash)
        try:
            return self._client.get(key)
        except Exception as exc:
            _logger.warning("redis_get_error", key=key, error=str(exc))
            return None

    def set(
        self,
        dashboard_id: str,
        filter_hash: str,
        payload: bytes,
        datasource_names: List[str],
        ttl_seconds: int = 1800,
    ) -> None:
        """Store a gzipped payload and register it in the datasource index.

        Args:
            dashboard_id: Dashboard ID string.
            filter_hash: MD5 hex digest of the filter set.
            payload: GZIP-compressed JSON bytes.
            datasource_names: Data source names this payload depends on.
            ttl_seconds: Key expiry (0 = no expiry).
        """
        if not self._client:
            return
        key = self._DATA_KEY.format(dashboard_id=dashboard_id, filter_hash=filter_hash)
        try:
            if ttl_seconds > 0:
                self._client.setex(key, ttl_seconds, payload)
            else:
                self._client.set(key, payload)

            # Register in datasource reverse-index sets
            for ds_name in datasource_names:
                index_key = self._DS_INDEX_KEY.format(datasource_name=ds_name)
                self._client.sadd(index_key, key)
                # Give the index set the same TTL (plus a small buffer)
                if ttl_seconds > 0:
                    self._client.expire(index_key, ttl_seconds + 60)

            _logger.debug("redis_set", key=key, bytes=len(payload))
        except Exception as exc:
            _logger.warning("redis_set_error", key=key, error=str(exc))

    def invalidate_datasource(self, datasource_name: str) -> int:
        """Delete all cache keys associated with a data source.

        Called when a data source is reloaded so stale payloads are evicted.

        Args:
            datasource_name: Name of the data source being reloaded.

        Returns:
            Number of cache keys deleted.
        """
        if not self._client:
            return 0
        index_key = self._DS_INDEX_KEY.format(datasource_name=datasource_name)
        try:
            keys = self._client.smembers(index_key)
            if not keys:
                return 0
            deleted = self._client.delete(*keys, index_key)
            _logger.info(
                "redis_invalidated_datasource",
                datasource=datasource_name,
                keys_deleted=deleted,
            )
            return deleted
        except Exception as exc:
            _logger.warning("redis_invalidate_error", datasource=datasource_name, error=str(exc))
            return 0

    def invalidate_dashboard(self, dashboard_id: str) -> int:
        """Delete all cached data payloads for a dashboard.

        Called when a dashboard's YAML is (hot-)reloaded, so cached
        results computed from the previous definition (queries, mappings,
        widget layout) aren't served after the definition changes. The
        dashboard ID is embedded directly in every data key
        (``db:<dashboard_id>:<filter_hash>``), so no reverse index is
        needed here — unlike :meth:`invalidate_datasource`.

        Args:
            dashboard_id: Dashboard ID whose cache entries should be evicted.

        Returns:
            Number of cache keys deleted.
        """
        if not self._client:
            return 0
        pattern = self._DATA_KEY.format(dashboard_id=dashboard_id, filter_hash="*")
        try:
            keys = list(self._client.scan_iter(match=pattern))
            if not keys:
                return 0
            deleted = self._client.delete(*keys)
            _logger.info(
                "redis_invalidated_dashboard",
                dashboard_id=dashboard_id,
                keys_deleted=deleted,
            )
            return deleted
        except Exception as exc:
            _logger.warning("redis_invalidate_error", dashboard_id=dashboard_id, error=str(exc))
            return 0

    def delete(self, dashboard_id: str, filter_hash: str) -> None:
        """Delete a single cache entry.

        Args:
            dashboard_id: Dashboard ID string.
            filter_hash: Filter hash string.
        """
        if not self._client:
            return
        key = self._DATA_KEY.format(dashboard_id=dashboard_id, filter_hash=filter_hash)
        try:
            self._client.delete(key)
        except Exception as exc:
            _logger.warning("redis_delete_error", key=key, error=str(exc))

    def flush_all(self) -> bool:
        """Wipe every key in the configured Redis DB (``FLUSHDB``).

        Uses ``flushdb`` rather than ``flushall`` so only the DB selected via
        ``REDIS_DB`` is cleared, leaving other logical databases on the same
        Redis server untouched.

        Returns:
            True if the flush succeeded, False if Redis is unavailable or
            the flush failed.
        """
        if not self._client:
            return False
        try:
            self._client.flushdb()
            _logger.info("redis_flushed")
            return True
        except Exception as exc:
            _logger.warning("redis_flush_error", error=str(exc))
            return False


# Module-level singleton
redis_cache = RedisCache()
