"""
Dedicated DuckDB connection for the audit log.

This is a deliberately separate module from ``app.core.db`` — a different
file, a different singleton connection, a different lock. The main data
engine's file (`duckdb_database`) is deleted and recreated on every
``system_manager.global_refresh()`` (data-source reload); putting audit
history in that same file would silently wipe it on every refresh. A
second, independent DuckDB connection to a different file is unaffected by
that swap — DuckDB's file locking is per-file, not process-wide.

Only the background ``AuditWriterThread`` (see ``app.core.audit_pipeline``)
writes here. Read endpoints (``app.api.system``) query it directly, the same
way every other read endpoint in this codebase queries the main connection
without an explicit lock — only DDL and batch writes are guarded.
"""

from __future__ import annotations

import csv
import io
import os
import threading
import warnings
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import structlog

from app.core.config import get_settings

# structlog is imported directly (not via app.core.logging.get_logger) to
# avoid a circular import: app.core.logging.AuditLogMiddleware imports
# app.core.audit_pipeline, which imports this module — so this module must
# not import anything back from app.core.logging.
_logger = structlog.get_logger("buchimaker.audit_db")
_lock = threading.Lock()
_connection: Optional[duckdb.DuckDBPyConnection] = None

# Fixed default retention window. Not user-configurable in v1 — see plan.
AUDIT_RETENTION_DAYS = 90

_SCHEMA_SQL = """
CREATE SEQUENCE IF NOT EXISTS audit_log_id_seq START 1;
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGINT PRIMARY KEY DEFAULT nextval('audit_log_id_seq'),
    ts TIMESTAMP NOT NULL,
    client_ip VARCHAR,
    method VARCHAR NOT NULL,
    path VARCHAR NOT NULL,
    query VARCHAR,
    status_code INTEGER NOT NULL,
    duration_ms DOUBLE NOT NULL,
    user_email VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts);
"""

_INSERT_SQL = (
    "INSERT INTO audit_log "
    "(ts, client_ip, method, path, query, status_code, duration_ms, user_email) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


def get_audit_db() -> duckdb.DuckDBPyConnection:
    """Return the singleton audit-log DuckDB connection, creating it on first call.

    Returns:
        An open ``duckdb.DuckDBPyConnection`` instance for the dedicated
        audit database file.
    """
    global _connection
    if _connection is None:
        with _lock:
            if _connection is None:  # double-checked locking
                settings = get_settings()
                db_path = settings.audit_duckdb_database
                db_dir = os.path.dirname(db_path)
                if db_dir:
                    os.makedirs(db_dir, exist_ok=True)
                _connection = duckdb.connect(database=db_path, read_only=False)
                _connection.execute(_SCHEMA_SQL)
                _logger.info("audit_db_initialised", database=db_path)
    return _connection


def close_audit_db() -> None:
    """Close and dispose of the singleton audit-log DuckDB connection."""
    global _connection
    with _lock:
        if _connection is not None:
            _connection.close()
            _connection = None
            _logger.info("audit_db_closed")


def get_audit_write_lock() -> threading.Lock:
    """Return the module-level lock guarding audit-log writes (batch INSERT, DELETE).

    Returns:
        The shared ``threading.Lock`` instance.
    """
    return _lock


def insert_batch(rows: List[Tuple]) -> None:
    """Batch-insert audit records.

    Args:
        rows: List of tuples matching ``_INSERT_SQL``'s column order
            (ts, client_ip, method, path, query, status_code, duration_ms,
            user_email).
    """
    if not rows:
        return
    con = get_audit_db()
    with _lock:
        con.executemany(_INSERT_SQL, rows)


def purge_old_records(retention_days: int = AUDIT_RETENTION_DAYS) -> int:
    """Delete audit records older than the retention window.

    Args:
        retention_days: Rows with ``ts`` older than this many days are removed.

    Returns:
        Number of rows deleted.
    """
    con = get_audit_db()
    cutoff = datetime.now() - timedelta(days=retention_days)
    with _lock:
        result = con.execute(
            "DELETE FROM audit_log WHERE ts < ? RETURNING id", [cutoff]
        ).fetchall()
    deleted = len(result)
    if deleted:
        _logger.info("audit_log_purged", deleted=deleted, retention_days=retention_days)
    return deleted


# ---------------------------------------------------------------------------
# Query helpers shared by the list and CSV-export endpoints
# ---------------------------------------------------------------------------


def _build_where(
    search: Optional[str],
    method: Optional[str],
    status_code: Optional[int],
    date_from: Optional[datetime],
    date_to: Optional[datetime],
) -> Tuple[str, List[Any]]:
    """Build a parameterized WHERE clause from optional audit-log filters.

    Args:
        search: Free-text filter, matched against path OR client_ip (ILIKE).
        method: Exact HTTP method match.
        status_code: Exact status code match.
        date_from: Lower bound (inclusive) on ``ts``.
        date_to: Upper bound (inclusive) on ``ts``.

    Returns:
        Tuple of (WHERE clause without the WHERE keyword, ordered params).
        Clause is ``1=1`` with no params when no filters are set.
    """
    parts: List[str] = []
    params: List[Any] = []

    if search:
        parts.append("(path ILIKE ? OR client_ip ILIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])
    if method:
        parts.append("method = ?")
        params.append(method)
    if status_code is not None:
        parts.append("status_code = ?")
        params.append(status_code)
    if date_from is not None:
        parts.append("ts >= ?")
        params.append(date_from)
    if date_to is not None:
        parts.append("ts <= ?")
        params.append(date_to)

    if not parts:
        return "1=1", []
    return " AND ".join(parts), params


def query_page(
    search: Optional[str] = None,
    method: Optional[str] = None,
    status_code: Optional[int] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = 25,
    offset: int = 0,
) -> Dict[str, Any]:
    """Return a filtered, paginated page of audit-log rows plus a total count.

    Args:
        search: Free-text filter over path/client_ip.
        method: Exact HTTP method filter.
        status_code: Exact status code filter.
        date_from: Lower bound (inclusive) on timestamp.
        date_to: Upper bound (inclusive) on timestamp.
        limit: Max rows to return.
        offset: Zero-based row offset.

    Returns:
        Dict with keys: ``rows`` (list of dicts) and ``total`` (int).
    """
    con = get_audit_db()
    where, params = _build_where(search, method, status_code, date_from, date_to)

    total = con.execute(
        f"SELECT COUNT(*) FROM audit_log WHERE {where}", params
    ).fetchone()[0]

    cursor = con.execute(
        f"SELECT id, ts, client_ip, method, path, query, status_code, "
        f"duration_ms, user_email FROM audit_log WHERE {where} "
        f"ORDER BY ts DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    )
    cols = [d[0] for d in cursor.description]
    rows = [dict(zip(cols, r)) for r in cursor.fetchall()]

    return {"rows": rows, "total": total}


def export_csv(
    search: Optional[str] = None,
    method: Optional[str] = None,
    status_code: Optional[int] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = 100_000,
):
    """Yield gzip-compressed CSV chunks of filtered audit-log rows.

    Streams from DuckDB in batches so memory stays flat regardless of export
    size, and compresses incrementally so the response can be gzip-encoded
    without ever buffering the whole CSV (or the whole compressed output) in
    memory at once.

    Args:
        search: Free-text filter over path/client_ip.
        method: Exact HTTP method filter.
        status_code: Exact status code filter.
        date_from: Lower bound (inclusive) on timestamp.
        date_to: Upper bound (inclusive) on timestamp.
        limit: Maximum rows to export (safety cap).

    Yields:
        Gzip-compressed byte chunks.
    """
    import zlib

    con = get_audit_db()
    where, params = _build_where(search, method, status_code, date_from, date_to)
    query = (
        f"SELECT id, ts, client_ip, method, path, query, status_code, "
        f"duration_ms, user_email FROM audit_log WHERE {where} "
        f"ORDER BY ts DESC LIMIT ?"
    )
    cursor = con.execute(query, params + [limit])
    col_names = [d[0] for d in cursor.description]

    compressor = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)

    def _compress(text: str) -> bytes:
        return compressor.compress(text.encode("utf-8"))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(col_names)
    chunk = _compress(output.getvalue())
    if chunk:
        yield chunk

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=DeprecationWarning)
            reader = cursor.fetch_record_batch(5000)
        for batch in reader:
            output = io.StringIO()
            writer = csv.writer(output)
            for row_dict in batch.to_pylist():
                writer.writerow([row_dict[c] for c in col_names])
            chunk = _compress(output.getvalue())
            if chunk:
                yield chunk
    except (AttributeError, Exception):
        while True:
            batch = cursor.fetchmany(5000)
            if not batch:
                break
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerows(batch)
            chunk = _compress(output.getvalue())
            if chunk:
                yield chunk

    yield compressor.flush()
