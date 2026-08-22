"""
SystemManager – top-level application singleton.

Owns the registries for dashboards and data-source connectors.
Runs background refresh threads for filter staleness management.
Surfaces system-wide settings that map to the API.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml

from app.connectors.base import BaseConnector
from app.core import audit_db
from app.core.audit_pipeline import AuditWriterThread
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.redis_client import redis_cache
from app.models.dashboard import Dashboard
from app.models.widget_set import WidgetSet

_logger = get_logger("buchimaker.system")


class SystemManager:
    """Top-level singleton managing dashboards, data sources, and settings.

    Attributes:
        dashboards: Dict of dashboard_id → Dashboard.
        data_sources: Dict of connector name → BaseConnector.
        settings: Mutable key-value settings store.
        _refresh_thread: Background thread for periodic filter refresh/trim.
        _stop_event: Signals the background thread to stop.
    """

    def __init__(self):
        """Initialise the SystemManager with default settings."""
        cfg = get_settings()
        self.dashboards: Dict[str, Dashboard] = {}
        self.data_sources: Dict[str, BaseConnector] = {}
        self.widget_sets: Dict[str, WidgetSet] = {}
        self.settings: Dict[str, Any] = {
            "refresh_frequency_filter": cfg.refresh_frequency_filter,
            "unallocate_frequency_filter": cfg.unallocate_frequency_filter,
        }
        self._refresh_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._refresh_history: List[float] = []
        self._refresh_lock = threading.Lock()
        self._audit_writer: Optional[AuditWriterThread] = None
        self._audit_scheduler: Optional[Any] = None  # BackgroundScheduler, created lazily
        
        # We use a ThreadPoolExecutor as our task queue for background DB loads
        from concurrent.futures import ThreadPoolExecutor
        self._task_queue = ThreadPoolExecutor(max_workers=1)
        
        from apscheduler.schedulers.background import BackgroundScheduler
        self._scheduler = BackgroundScheduler()
        self._last_auto_refresh: Any = None

    def _save_data_sources(self) -> None:
        path = Path("./settings/data_sources.yaml")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [c.to_config() for c in self.data_sources.values()]
        with path.open("w") as f:
            yaml.dump(data, f)

    def _save_dashboards(self) -> None:
        path = Path("./settings/dashboards.yaml")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [{"filepath": d.yaml_path} for d in self.dashboards.values() if d.yaml_path]
        with path.open("w") as f:
            yaml.dump(data, f)

    def _save_widget_sets(self) -> None:
        path = Path("./settings/widget_sets.yaml")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [{"folder_path": w.folder_path} for w in self.widget_sets.values() if w.folder_path]
        with path.open("w") as f:
            yaml.dump(data, f)

    def load_persisted_state(self) -> None:
        from app.connectors.base import ConnectorRegistry
        
        # Load data sources
        ds_path = Path("./settings/data_sources.yaml")
        if ds_path.exists():
            try:
                with ds_path.open("r") as f:
                    ds_data = yaml.safe_load(f) or []
                for cfg in ds_data:
                    source_type = cfg.pop("source_type")
                    connector_cls = ConnectorRegistry.get(source_type)
                    if connector_cls:
                        connector = connector_cls(**cfg)
                        self.data_sources[connector.name] = connector
                
                if self.data_sources:
                    self.global_refresh(rate_limit=False)
            except Exception as e:
                _logger.error("error_loading_data_sources_settings", error=str(e))
                
        # Load dashboards
        dash_path = Path("./settings/dashboards.yaml")
        if dash_path.exists():
            try:
                with dash_path.open("r") as f:
                    dash_data = yaml.safe_load(f) or []
                for cfg in dash_data:
                    filepath = cfg.get("filepath")
                    if filepath:
                        self.load_dashboard(filepath, save=False)
            except Exception as e:
                _logger.error("error_loading_dashboards_settings", error=str(e))

        # Load widget sets
        widgets_path = Path("./settings/widget_sets.yaml")
        if widgets_path.exists():
            try:
                with widgets_path.open("r") as f:
                    widgets_data = yaml.safe_load(f) or []
                for cfg in widgets_data:
                    folder_path = cfg.get("folder_path")
                    if folder_path:
                        self.load_widget_set(folder_path, save=False)
            except Exception as e:
                _logger.error("error_loading_widget_sets_settings", error=str(e))

        # Seed the bundled "default" widget set if nothing was registered yet
        # (fresh install / first run before any manual registration).
        if not self.widget_sets:
            error = self.load_widget_set("default", save=True)
            if error:
                _logger.warning("default_widget_set_seed_failed", error=error)

    # -- Data-source management ---------------------------------------------

    def register_data_source(self, connector: BaseConnector, save: bool = True) -> None:
        """Register and load a data source via global refresh.

        Args:
            connector: Any BaseConnector subclass instance.
            save: Whether to save to YAML persistence.
        """
        is_update = connector.name in self.data_sources
        old_connector = self.data_sources.get(connector.name)
        
        self.data_sources[connector.name] = connector
        try:
            self.global_refresh(rate_limit=False)
        except Exception as e:
            if is_update and old_connector:
                self.data_sources[connector.name] = old_connector
            else:
                del self.data_sources[connector.name]
            raise RuntimeError(f"Failed to register data source: {e}")
            
        if save:
            self._save_data_sources()
            
        _logger.info(
            "data_source_registered",
            name=connector.name,
            type=connector.source_type,
            is_update=is_update
        )

    def remove_data_source(self, name: str) -> None:
        """Remove a connector and trigger a global refresh.

        Args:
            name: Connector name to remove.

        Raises:
            KeyError: If no connector with that name exists.
        """
        connector = self.data_sources.pop(name)
        connector.drop()
        self._save_data_sources()
        _logger.info("data_source_removed", name=name)

    def global_refresh(self, rate_limit: bool = True) -> None:
        """Trigger a global refresh of all data sources in a background DB and swap.
        
        Enforces a rate limit of max 2 refreshes per minute if rate_limit=True.
        Uses a task queue. Raises RuntimeError if rate limit exceeded or if load fails.
        """
        now = time.time()
        with self._refresh_lock:
            # Clean up history older than 60s
            self._refresh_history = [t for t in self._refresh_history if now - t < 60]
            if rate_limit and len(self._refresh_history) >= 2:
                raise RuntimeError("Rate limit exceeded: maximum 2 refreshes per minute allowed.")
            if rate_limit:
                self._refresh_history.append(now)
            
        def _do_refresh():
            from app.core.db import get_standby_db, promote_standby
            try:
                con = get_standby_db()
            except Exception as e:
                raise RuntimeError(f"Failed to open standby DB: {e}")

            try:
                for ds in self.data_sources.values():
                    ds.load(con=con)
            except Exception as e:
                # The standby keeps whatever partial state was loaded — it's
                # a persistent file, not a throwaway temp one — but since we
                # never promote it here, nothing becomes visible. The next
                # refresh attempt just overwrites it via each connector's
                # own DROP/CREATE.
                raise RuntimeError(f"Data source load failed: {e}")

            promote_standby()

            # Clean Redis cache for all sources
            for ds_name in self.data_sources:
                redis_cache.invalidate_datasource(ds_name)
                
        # Submit to task queue and wait for it
        future = self._task_queue.submit(_do_refresh)
        try:
            future.result()
        except Exception as e:
            raise RuntimeError(str(e))

    def list_data_sources(self) -> List[Dict[str, Any]]:
        """Return metadata for all registered data sources.

        Returns:
            List of DataSourceInfo-compatible dicts with id, name, description,
            type, last_updated.
        """
        return [c.to_info() for c in self.data_sources.values()]

    # -- Dashboard management -----------------------------------------------

    def load_dashboard(self, filepath: str, save: bool = True) -> str:
        """Load or reload a dashboard from a YAML file.

        Preserves last_queried times on existing filters (hot-reload support).

        Args:
            filepath: Path to the YAML file (absolute or relative to dashboards_dir).
            save: Whether to save to YAML persistence.

        Returns:
            Empty string on success, or a user-friendly error message.
        """
        path = Path(filepath)
        if not path.is_absolute():
            path = Path(get_settings().dashboards_dir) / filepath

        # Reuse existing Dashboard object for hot-reload (preserves filter cache)
        dash = Dashboard()
        error = dash.load_yaml(str(path))
        if error:
            return error

        # Ensure we are not loading a file that is already registered under a different ID
        for existing_id, existing_dash in self.dashboards.items():
            if existing_dash.yaml_path and Path(existing_dash.yaml_path).resolve() == path.resolve():
                if existing_id != dash.id:
                    return f"Validation error: File '{path}' is already registered under Dashboard ID '{existing_id}'. Unregister it first."

        if dash.id in self.dashboards:
            old = self.dashboards[dash.id]
            # Ensure we are hot-reloading the same file, not overwriting with a different file
            if old.yaml_path and Path(old.yaml_path).resolve() != path.resolve():
                return f"Validation error: Dashboard ID '{dash.id}' is already registered by another file: {old.yaml_path}"

            # Preserve last_queried on existing filters
            for h, old_f in old.filters.items():
                if h in dash.filters:
                    dash.filters[h].last_queried = old_f.last_queried
            old._drop_all_filters()
            # If the reloaded YAML renamed its base_view, drop the orphaned
            # old one so it doesn't linger in DuckDB.
            if old.source_table and old.source_table != dash.source_table:
                old.drop_base_view()

        self.dashboards[dash.id] = dash
        if save:
            self._save_dashboards()
        # Bust any cached /data responses computed from the previous
        # definition — queries, mappings, or widget config may have changed.
        redis_cache.invalidate_dashboard(dash.id)
        _logger.info("dashboard_registered", id=dash.id, name=dash.name)
        return ""

    def remove_dashboard(self, dashboard_id: str) -> bool:
        """Remove a dashboard and its DuckDB views.

        Args:
            dashboard_id: The ID of the dashboard to remove.
            
        Returns:
            True if dashboard was removed, False if not found.
        """
        dash = self.dashboards.pop(dashboard_id, None)
        if dash:
            dash._drop_all_filters()
            dash.drop_base_view()
            self._save_dashboards()
            _logger.info("dashboard_deleted", id=dashboard_id)
            return True
        return False


    def auto_load_dashboards(self) -> List[str]:
        """Scan dashboards_dir and load all YAML files found.

        Returns:
            List of error strings for any files that failed to load
            (empty list on full success).
        """
        dashboards_dir = Path(get_settings().dashboards_dir)
        errors: List[str] = []
        if not dashboards_dir.exists():
            _logger.warning("dashboards_dir_missing", path=str(dashboards_dir))
            return [f"Dashboards directory not found: {dashboards_dir}"]

        yaml_files = list(dashboards_dir.glob("*.yaml")) + list(
            dashboards_dir.glob("*.yml")
        )
        for yaml_file in yaml_files:
            if yaml_file.name.startswith("template"):
                continue  # skip template files
            err = self.load_dashboard(str(yaml_file), save=False)
            if err:
                errors.append(f"{yaml_file.name}: {err}")
        _logger.info(
            "auto_load_complete",
            loaded=len(self.dashboards),
            errors=len(errors),
        )
        return errors

    # -- Widget-set management -----------------------------------------------

    def load_widget_set(self, folder_path: str, save: bool = True) -> str:
        """Register or reload a widget package folder.

        Args:
            folder_path: Path to the widget folder (absolute, or relative to
                `WIDGETS_DIR`).
            save: Whether to persist to YAML.

        Returns:
            Empty string on success, or a user-friendly error message.
        """
        path = Path(folder_path)
        if not path.is_absolute():
            path = Path(get_settings().widgets_dir) / folder_path

        ws = WidgetSet()
        error = ws.load(str(path))
        if error:
            return error

        self.widget_sets[ws.id] = ws
        if save:
            self._save_widget_sets()

        # First widget set ever registered (or the active one having been
        # removed) automatically becomes active so the frontend always has
        # something valid to render with.
        from app.core.general_settings import general_settings
        if general_settings.active_widget_set not in self.widget_sets:
            general_settings.active_widget_set = ws.id

        _logger.info("widget_set_registered", id=ws.id, title=ws.title)
        return ""

    def remove_widget_set(self, widget_set_id: str) -> bool:
        """Remove a registered widget set.

        Args:
            widget_set_id: The widget set ID to remove.

        Returns:
            True if removed, False if not found.

        Raises:
            ValueError: If attempting to remove the currently active set.
        """
        if widget_set_id not in self.widget_sets:
            return False

        from app.core.general_settings import general_settings
        if general_settings.active_widget_set == widget_set_id:
            raise ValueError(
                f"Cannot delete widget set '{widget_set_id}' while it is active. "
                "Activate a different widget set first."
            )

        del self.widget_sets[widget_set_id]
        self._save_widget_sets()
        _logger.info("widget_set_deleted", id=widget_set_id)
        return True

    def activate_widget_set(self, widget_set_id: str) -> bool:
        """Mark a registered widget set as the one the frontend should use.

        Only one widget set is active at a time — this simply overwrites the
        single ``active_widget_set`` setting.

        Args:
            widget_set_id: The widget set ID to activate.

        Returns:
            True if activated, False if the ID is not a registered widget set.
        """
        if widget_set_id not in self.widget_sets:
            return False
        from app.core.general_settings import general_settings
        general_settings.active_widget_set = widget_set_id
        _logger.info("widget_set_activated", id=widget_set_id)
        return True

    def list_widget_sets(self) -> List[Dict[str, Any]]:
        """Return metadata for all registered widget sets.

        Returns:
            List of WidgetSetInfo-compatible dicts, each flagged with
            whether it is the currently active set.
        """
        from app.core.general_settings import general_settings
        active_id = general_settings.active_widget_set
        return [w.to_info(active=(w.id == active_id)) for w in self.widget_sets.values()]

    # -- Background refresh thread ------------------------------------------

    def start_refresh_thread(self) -> None:
        """Start the background thread that refreshes and trims filter caches."""
        if self._refresh_thread and self._refresh_thread.is_alive():
            return
        self._stop_event.clear()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop,
            name="buchimaker-refresh",
            daemon=True,
        )
        self._refresh_thread.start()
        self._scheduler.start()
        _logger.info("refresh_thread_started")

    def stop_refresh_thread(self) -> None:
        """Signal the background refresh thread to stop and join it."""
        self._stop_event.set()
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        if self._refresh_thread:
            self._refresh_thread.join(timeout=5)
        _logger.info("refresh_thread_stopped")

    # -- Audit log writer -----------------------------------------------------

    def start_audit_writer(self) -> None:
        """Start the background audit-log writer thread and its retention job.

        The retention job runs on a dedicated ``BackgroundScheduler`` — not
        ``self._scheduler`` — because ``_sync_scheduler()`` unconditionally
        clears *every* job on that scheduler whenever the ``auto_refresh``
        setting changes (see below). Sharing it would silently delete the
        retention job the next time someone edits the auto-refresh interval.
        """
        if self._audit_writer and self._audit_writer.is_alive():
            return
        self._audit_writer = AuditWriterThread()
        self._audit_writer.start()

        if self._audit_scheduler is None:
            from apscheduler.schedulers.background import BackgroundScheduler
            self._audit_scheduler = BackgroundScheduler()
            self._audit_scheduler.add_job(
                audit_db.purge_old_records,
                "interval",
                hours=24,
                id="audit_log_retention",
                next_run_time=datetime.now(),
            )
            self._audit_scheduler.start()
        _logger.info("audit_writer_started")

    def stop_audit_writer(self) -> None:
        """Stop the background audit-log writer thread and its retention job."""
        if self._audit_scheduler is not None:
            self._audit_scheduler.shutdown(wait=False)
            self._audit_scheduler = None
        if self._audit_writer:
            self._audit_writer.stop(timeout=5)
            self._audit_writer = None
        _logger.info("audit_writer_stopped")

    def _sync_scheduler(self):
        from app.core.general_settings import general_settings
        auto_refresh = general_settings.auto_refresh
        if self._last_auto_refresh == auto_refresh:
            return
            
        self._last_auto_refresh = auto_refresh
        
        # Remove all existing jobs
        for job in self._scheduler.get_jobs():
            job.remove()
            
        if not auto_refresh or auto_refresh == "disabled":
            return
            
        try:
            if isinstance(auto_refresh, int):
                self._scheduler.add_job(
                    self.global_refresh,
                    "interval",
                    minutes=auto_refresh,
                    kwargs={"rate_limit": False},  # background jobs don't rate limit
                    id="auto_refresh"
                )
            elif isinstance(auto_refresh, list):
                from apscheduler.triggers.cron import CronTrigger
                for i, cron_str in enumerate(auto_refresh):
                    self._scheduler.add_job(
                        self.global_refresh,
                        CronTrigger.from_crontab(cron_str),
                        kwargs={"rate_limit": False},
                        id=f"auto_refresh_{i}"
                    )
        except Exception as e:
            _logger.error("error_syncing_scheduler", error=str(e))

    def _refresh_loop(self) -> None:
        """Background loop: trim stale filters every minute.

        The actual refresh frequency is governed per-dashboard by
        refresh_frequency_filter; this loop wakes up every 60 seconds and
        lets each Dashboard decide what needs refreshing.
        """
        while not self._stop_event.is_set():
            try:
                self._sync_scheduler()
                for dash in list(self.dashboards.values()):
                    dash.trim_filters()
                    dash.refresh_filters()
            except Exception as exc:
                _logger.error("refresh_loop_error", error=str(exc))
            self._stop_event.wait(timeout=60)

    # -- Health summary -------------------------------------------------------

    def health_summary(self) -> Dict[str, Any]:
        """Return a snapshot of system health for the /healthz endpoint.

        Returns:
            Dict with: dashboards_loaded, data_sources_loaded, refresh_running.
        """
        return {
            "dashboards_loaded": len(self.dashboards),
            "data_sources_loaded": len(self.data_sources),
            "refresh_running": (
                self._refresh_thread is not None
                and self._refresh_thread.is_alive()
            ),
        }


# Module-level singleton – imported by routers and startup hooks
system_manager = SystemManager()
