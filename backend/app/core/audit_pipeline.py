"""
In-process, non-blocking pipeline feeding audit records to DuckDB and syslog.

``AuditLogMiddleware.dispatch()`` calls :func:`enqueue_audit_record` with a
plain dict. That call is a single ``put_nowait()`` on a bounded
``queue.Queue`` — O(1), touches neither disk nor network, and never blocks
the request path even under sustained overload (excess records are dropped
and counted, never queued indefinitely).

A single background daemon thread (:class:`AuditWriterThread`) drains the
queue, batches records by size/time, and on each flush:

1. Batch-inserts into the dedicated audit DuckDB file (``app.core.audit_db``).
2. If syslog export is enabled, forwards each record to the
   ``"buchimaker.audit.syslog_sink"`` logger (see
   ``app.core.logging.configure_syslog``) — independently of step 1, so a
   syslog outage never blocks or discards the DuckDB write.

Both sinks are best-effort: a batch that fails to write or forward is
logged and dropped rather than retried, so a stalled sink can never grow
unbounded backlog. The existing stdout JSON audit log (``AuditLogMiddleware``'s
existing ``self._audit.info(...)`` call) is untouched and remains the
guaranteed record.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any, Dict, List

import structlog

from app.core import audit_db

# structlog is imported directly (not via app.core.logging.get_logger) to
# avoid a circular import: app.core.logging.AuditLogMiddleware needs to call
# into this module to enqueue records, so this module must not import
# anything back from app.core.logging.
_logger = structlog.get_logger("buchimaker.audit_pipeline")
_syslog_sink = logging.getLogger("buchimaker.audit.syslog_sink")

# Bounded so a stalled DB/syslog sink can never grow memory unbounded or
# apply backpressure to the request path. Starting values, not measured
# against real traffic — easy to retune without touching call sites.
_QUEUE_MAXSIZE = 10_000
_BATCH_SIZE = 500
_FLUSH_INTERVAL_SECONDS = 2.0

_audit_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=_QUEUE_MAXSIZE)

_dropped_lock = threading.Lock()
_dropped_count = 0
_last_drop_log = 0.0


def enqueue_audit_record(record: Dict[str, Any]) -> None:
    """Enqueue an audit record for background persistence/forwarding.

    Never blocks. If the queue is full, the record is dropped and counted
    (a warning is logged at most once per second while drops are occurring).

    Args:
        record: Dict with keys: ts, client_ip, method, path, query,
            status_code, duration_ms, user_email (may be None).
    """
    try:
        _audit_queue.put_nowait(record)
    except queue.Full:
        _record_drop()


def _record_drop() -> None:
    global _dropped_count, _last_drop_log
    with _dropped_lock:
        _dropped_count += 1
        now = time.monotonic()
        if now - _last_drop_log > 1.0:
            _logger.warning("audit_queue_full_dropping", dropped_total=_dropped_count)
            _last_drop_log = now


def dropped_count() -> int:
    """Return the total number of audit records dropped due to a full queue."""
    with _dropped_lock:
        return _dropped_count


class AuditWriterThread(threading.Thread):
    """Background daemon thread that batches queued audit records to DuckDB
    and (optionally) forwards them to the configured syslog sink.

    Mirrors ``system_manager._refresh_thread``'s loop style: a plain daemon
    thread with a ``threading.Event``-gated ``while`` loop, started/stopped
    via idempotent ``start()``/``stop()`` methods.
    """

    def __init__(
        self,
        batch_size: int = _BATCH_SIZE,
        flush_interval: float = _FLUSH_INTERVAL_SECONDS,
    ):
        """Initialise the writer thread.

        Args:
            batch_size: Max records per flush before forcing a write.
            flush_interval: Max seconds between flushes when the queue is
                below ``batch_size``.
        """
        super().__init__(name="buchimaker-audit-writer", daemon=True)
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._stop_event = threading.Event()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the thread to stop and join it, flushing any remaining records.

        Args:
            timeout: Max seconds to wait for the thread to finish.
        """
        self._stop_event.set()
        self.join(timeout=timeout)

    # Upper bound on how long a single queue.get() call may block, regardless
    # of flush_interval — keeps stop() responsive (it must re-check
    # _stop_event at least this often) instead of potentially waiting a full
    # flush_interval before noticing the stop signal.
    _MAX_POLL_SECONDS = 0.2

    def run(self) -> None:
        """Drain the queue, batching by size/time, until stopped."""
        batch: List[Dict[str, Any]] = []
        last_flush = time.monotonic()
        while not self._stop_event.is_set():
            remaining = self.flush_interval - (time.monotonic() - last_flush)
            wait = min(remaining, self._MAX_POLL_SECONDS) if remaining > 0 else 0.1
            try:
                item = _audit_queue.get(timeout=wait)
                batch.append(item)
            except queue.Empty:
                pass

            should_flush = len(batch) >= self.batch_size or (
                batch and (time.monotonic() - last_flush) >= self.flush_interval
            )
            if should_flush:
                self._flush(batch)
                batch = []
                last_flush = time.monotonic()

        # Best-effort drain on shutdown.
        while True:
            try:
                batch.append(_audit_queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            self._flush(batch)

    def _flush(self, batch: List[Dict[str, Any]]) -> None:
        """Write a batch to DuckDB and forward it to syslog, independently.

        Args:
            batch: List of audit record dicts.
        """
        if not batch:
            return
        self._write_to_db(batch)
        self._forward_to_syslog(batch)

    def _write_to_db(self, batch: List[Dict[str, Any]]) -> None:
        """Persist a batch to the audit DuckDB table.

        Never raises — a DB failure is logged and the batch is dropped so
        the writer thread can never die or stall on a bad write.
        """
        rows = [
            (
                r["ts"], r.get("client_ip"), r["method"], r["path"],
                r.get("query"), r["status_code"], r["duration_ms"], r.get("user_email"),
            )
            for r in batch
        ]
        try:
            audit_db.insert_batch(rows)
        except Exception as exc:
            _logger.error("audit_db_write_failed", error=str(exc), batch_size=len(rows))

    def _forward_to_syslog(self, batch: List[Dict[str, Any]]) -> None:
        """Forward a batch to the syslog sink logger, one record at a time.

        No-ops entirely (skipping JSON serialization) when no syslog handler
        is currently attached. Never raises — a forwarding failure (e.g. the
        remote syslog server being down) is logged and that record is
        skipped, independent of the DuckDB write already completed above.
        """
        if not _syslog_sink.handlers:
            return
        for record in batch:
            try:
                _syslog_sink.info(json.dumps(record, default=str))
            except Exception as exc:
                _logger.warning("audit_syslog_forward_failed", error=str(exc))
