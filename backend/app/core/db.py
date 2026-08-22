"""
DuckDB connection manager (singleton per process).

The backend serves reads from a single "active" DuckDB connection while
`global_refresh()` loads fresh data into a separate "standby" connection on
a fixed second file. Once the standby load finishes it is promoted to
active via :func:`promote_standby`, and the connection that used to be
active is demoted to become the new standby — ready to receive the next
load. Both slots are persistent, fixed-path files (not throwaway temp
files), so a refresh never has to create-then-delete a whole database.

The connection is wrapped in a threading.RLock so that writes (table
registration, DROP, CREATE) are serialised while reads can proceed via
DuckDB's built-in MVCC. The lock is reentrant because promotion needs to
call back into `get_db()`/`get_standby_db()` while already holding it.

Usage
-----
    from app.core.db import get_db

    con = get_db()
    rows = con.execute("SELECT 1").fetchall()
"""

import os
import time
import threading
from pathlib import Path
from typing import Optional

import duckdb

from app.core.config import get_settings
from app.core.logging import get_logger

_logger = get_logger("buchimaker.db")
_lock = threading.RLock()

_active_con: Optional[duckdb.DuckDBPyConnection] = None
_standby_con: Optional[duckdb.DuckDBPyConnection] = None
_active_slot: Optional[str] = None  # "a" or "b"

# How long to keep the demoted connection open after a promotion before
# closing it. Any caller that already grabbed a reference to it (e.g.
# `con = get_db(); ...; con.execute(...)` as two separate lines) gets to
# finish its in-flight query instead of hitting a closed connection.
_STANDBY_GRACE_SECONDS = 2.0


def _slot_paths() -> dict[str, str]:
    settings = get_settings()
    return {"a": settings.duckdb_database, "b": settings.duckdb_database_standby}


def _marker_path() -> Path:
    return Path(f"{get_settings().duckdb_database}.active_slot")


def _read_active_slot() -> str:
    marker = _marker_path()
    if marker.exists():
        value = marker.read_text().strip()
        if value in ("a", "b"):
            return value
    return "a"


def _write_active_slot(slot: str) -> None:
    marker = _marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    tmp = marker.with_suffix(marker.suffix + ".tmp")
    tmp.write_text(slot)
    os.replace(tmp, marker)


def _open(path: str, read_only: bool) -> duckdb.DuckDBPyConnection:
    db_dir = os.path.dirname(path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    con = duckdb.connect(database=path, read_only=read_only)
    if not read_only:
        settings = get_settings()
        con.execute(f"SET memory_limit='{settings.duckdb_memory_limit}';")
    return con


def get_db() -> duckdb.DuckDBPyConnection:
    """Return the singleton active DuckDB connection, creating it on first call.

    Returns:
        An open ``duckdb.DuckDBPyConnection`` instance, read-write.
    """
    global _active_con, _active_slot
    if _active_con is None:
        with _lock:
            if _active_con is None:  # double-checked locking
                _active_slot = _read_active_slot()
                path = _slot_paths()[_active_slot]
                _active_con = _open(path, read_only=False)
                _logger.info("duckdb_initialised", slot=_active_slot, database=path)
    return _active_con


def get_standby_db() -> duckdb.DuckDBPyConnection:
    """Return the singleton standby DuckDB connection, creating it on first call.

    The standby is the other fixed-path slot from the active one, opened
    read-write so connectors can load fresh data into it ahead of promotion.

    Returns:
        An open ``duckdb.DuckDBPyConnection`` instance, read-write.
    """
    global _standby_con
    get_db()  # ensure _active_slot is known before picking the other slot
    if _standby_con is None:
        with _lock:
            if _standby_con is None:
                standby_slot = "b" if _active_slot == "a" else "a"
                path = _slot_paths()[standby_slot]
                _standby_con = _open(path, read_only=False)
                _logger.info("duckdb_standby_initialised", slot=standby_slot, database=path)
    return _standby_con


def close_db() -> None:
    """Close and dispose of both the active and standby DuckDB connections.

    Intended for use in application shutdown hooks only.
    """
    global _active_con, _standby_con
    with _lock:
        if _active_con is not None:
            _active_con.close()
            _active_con = None
        if _standby_con is not None:
            _standby_con.close()
            _standby_con = None
        _logger.info("duckdb_closed")


def get_write_lock() -> threading.RLock:
    """Return the module-level write lock for serialised DDL operations.

    Returns:
        The shared ``threading.RLock`` instance.
    """
    return _lock


def promote_standby(grace_seconds: float = _STANDBY_GRACE_SECONDS) -> None:
    """Promote the fully-loaded standby connection to active.

    Sequence: close the standby's RW handle, reopen it read-only as a
    validation gate (an unreadable/corrupt file raises here and the old
    active is left untouched), then reopen it read-write and make it the
    new active connection. After a short grace period — during which any
    caller still holding a reference to the old active connection can
    finish an in-flight query — the old active connection is closed and
    reopened read-write as the new standby, ready for the next refresh.

    Raises:
        RuntimeError: If there is no standby connection to promote, or if
            the standby file fails its read-only validation reopen.
    """
    global _active_con, _standby_con, _active_slot

    with _lock:
        if _standby_con is None:
            raise RuntimeError("No standby connection to promote.")

        standby_slot = "b" if _active_slot == "a" else "a"
        path = _slot_paths()[standby_slot]
        old_active_con = _active_con
        old_active_slot = _active_slot

        _standby_con.close()
        _standby_con = None
        try:
            validation_con = duckdb.connect(database=path, read_only=True)
            validation_con.close()
        except Exception as exc:
            # Leave the standby usable again for the next attempt; the old
            # active connection was never touched, so it keeps serving.
            _standby_con = _open(path, read_only=False)
            raise RuntimeError(
                f"Standby DB failed validation, promotion aborted: {exc}"
            ) from exc

        new_active_con = _open(path, read_only=False)
        _active_con = new_active_con
        _active_slot = standby_slot
        _write_active_slot(standby_slot)
        _logger.info("duckdb_promoted", slot=standby_slot, database=path)

    # Outside the lock: drain in-flight readers of the old active connection.
    time.sleep(grace_seconds)

    with _lock:
        old_active_con.close()
        old_path = _slot_paths()[old_active_slot]
        _standby_con = _open(old_path, read_only=False)
        _logger.info("duckdb_demoted", slot=old_active_slot, database=old_path)
