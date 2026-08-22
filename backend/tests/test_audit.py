"""
Tests for the audit-log pipeline (app.core.audit_pipeline) and the dedicated
audit DuckDB store (app.core.audit_db).

Isolation: the `wipe_db_file` autouse fixture in conftest.py closes and
removes both the main and audit DuckDB files before/after every test, so
each test here starts from a fresh, empty audit_log table.
"""

import gzip
import queue
import time
from datetime import datetime, timedelta

import pytest

from app.core import audit_db, audit_pipeline


def _drain(q: queue.Queue) -> None:
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


@pytest.fixture(autouse=True)
def isolated_audit_queue():
    """Ensure the shared, process-wide audit queue starts (and ends) empty.

    Other test modules (e.g. test_api.py) issue real HTTP requests through
    AuditLogMiddleware, which unconditionally enqueues records onto this same
    module-level queue — without draining it here, leftover records from
    other test files would bleed into this file's exact-count assertions.
    """
    _drain(audit_pipeline._audit_queue)
    yield
    _drain(audit_pipeline._audit_queue)


# ---------------------------------------------------------------------------
# audit_db
# ---------------------------------------------------------------------------


class TestAuditDb:
    """Tests for the dedicated audit-log DuckDB store."""

    def test_schema_created_on_first_access(self):
        con = audit_db.get_audit_db()
        tables = con.execute("SHOW TABLES").fetchall()
        assert ("audit_log",) in tables

    def test_insert_batch_and_query_page(self):
        now = datetime.now()
        audit_db.insert_batch([
            (now, "1.1.1.1", "GET", "/api/v1/dashboards", "", 200, 10.0, None),
            (now, "2.2.2.2", "POST", "/api/v1/dashboards/x/data", "f=1", 500, 250.0, None),
        ])
        page = audit_db.query_page(limit=10, offset=0)
        assert page["total"] == 2
        assert len(page["rows"]) == 2
        # Newest-first ordering by ts; both rows share `now` here, so just
        # check both are present regardless of tie-break order.
        paths = {r["path"] for r in page["rows"]}
        assert paths == {"/api/v1/dashboards", "/api/v1/dashboards/x/data"}

    def test_insert_batch_empty_is_noop(self):
        audit_db.insert_batch([])
        assert audit_db.query_page()["total"] == 0

    def test_query_page_search_filter(self):
        now = datetime.now()
        audit_db.insert_batch([
            (now, "1.1.1.1", "GET", "/api/v1/dashboards", "", 200, 1.0, None),
            (now, "9.9.9.9", "GET", "/api/v1/system/settings", "", 200, 1.0, None),
        ])
        page = audit_db.query_page(search="dashboards")
        assert page["total"] == 1
        assert page["rows"][0]["path"] == "/api/v1/dashboards"

        page_ip = audit_db.query_page(search="9.9.9.9")
        assert page_ip["total"] == 1

    def test_query_page_method_and_status_filters(self):
        now = datetime.now()
        audit_db.insert_batch([
            (now, "1.1.1.1", "GET", "/a", "", 200, 1.0, None),
            (now, "1.1.1.1", "POST", "/b", "", 500, 1.0, None),
        ])
        assert audit_db.query_page(method="POST")["total"] == 1
        assert audit_db.query_page(status_code=500)["total"] == 1
        assert audit_db.query_page(method="GET", status_code=500)["total"] == 0

    def test_query_page_date_range_filter(self):
        old = datetime.now() - timedelta(days=10)
        recent = datetime.now()
        audit_db.insert_batch([
            (old, "1.1.1.1", "GET", "/old", "", 200, 1.0, None),
            (recent, "1.1.1.1", "GET", "/recent", "", 200, 1.0, None),
        ])
        page = audit_db.query_page(date_from=datetime.now() - timedelta(days=1))
        assert page["total"] == 1
        assert page["rows"][0]["path"] == "/recent"

    def test_query_page_pagination(self):
        now = datetime.now()
        rows = [
            (now + timedelta(seconds=i), "1.1.1.1", "GET", f"/p{i}", "", 200, 1.0, None)
            for i in range(5)
        ]
        audit_db.insert_batch(rows)
        page1 = audit_db.query_page(limit=2, offset=0)
        page2 = audit_db.query_page(limit=2, offset=2)
        assert page1["total"] == 5
        assert len(page1["rows"]) == 2
        assert len(page2["rows"]) == 2
        assert {r["path"] for r in page1["rows"]}.isdisjoint({r["path"] for r in page2["rows"]})

    def test_purge_old_records(self):
        old = datetime.now() - timedelta(days=100)
        recent = datetime.now()
        audit_db.insert_batch([
            (old, "1.1.1.1", "GET", "/old", "", 200, 1.0, None),
            (recent, "1.1.1.1", "GET", "/recent", "", 200, 1.0, None),
        ])
        deleted = audit_db.purge_old_records(retention_days=90)
        assert deleted == 1
        page = audit_db.query_page()
        assert page["total"] == 1
        assert page["rows"][0]["path"] == "/recent"

    def test_purge_old_records_nothing_to_delete(self):
        audit_db.insert_batch([
            (datetime.now(), "1.1.1.1", "GET", "/recent", "", 200, 1.0, None),
        ])
        assert audit_db.purge_old_records(retention_days=90) == 0

    def test_export_csv_is_valid_gzip_and_matches_rows(self):
        now = datetime.now()
        audit_db.insert_batch([
            (now, "1.1.1.1", "GET", "/api/v1/a", "", 200, 1.5, None),
            (now, "2.2.2.2", "POST", "/api/v1/b", "x=1", 500, 2.5, None),
        ])
        chunks = list(audit_db.export_csv())
        raw = b"".join(chunks)
        decompressed = gzip.decompress(raw).decode("utf-8")
        lines = [l for l in decompressed.splitlines() if l]
        assert lines[0].startswith("id,ts,client_ip,method,path")
        assert len(lines) == 3  # header + 2 rows

    def test_export_csv_respects_filters(self):
        now = datetime.now()
        audit_db.insert_batch([
            (now, "1.1.1.1", "GET", "/api/v1/a", "", 200, 1.0, None),
            (now, "2.2.2.2", "POST", "/api/v1/b", "", 500, 1.0, None),
        ])
        chunks = list(audit_db.export_csv(status_code=500))
        decompressed = gzip.decompress(b"".join(chunks)).decode("utf-8")
        lines = [l for l in decompressed.splitlines() if l]
        assert len(lines) == 2  # header + 1 matching row
        assert "/api/v1/b" in lines[1]


# ---------------------------------------------------------------------------
# audit_pipeline
# ---------------------------------------------------------------------------


class TestAuditPipeline:
    """Tests for the bounded queue + background writer thread."""

    def test_enqueue_never_blocks_when_queue_has_room(self):
        record = {
            "ts": datetime.now(), "client_ip": "1.1.1.1", "method": "GET",
            "path": "/x", "query": "", "status_code": 200, "duration_ms": 1.0,
            "user_email": None,
        }
        # Should return immediately without raising.
        audit_pipeline.enqueue_audit_record(record)
        assert audit_pipeline._audit_queue.qsize() >= 1
        # Drain it so it doesn't leak into other tests.
        audit_pipeline._audit_queue.get_nowait()

    def test_enqueue_drops_and_counts_when_queue_full(self, monkeypatch):
        # Swap in a tiny queue so we can force overflow without pushing
        # thousands of records.
        tiny_queue = queue.Queue(maxsize=1)
        monkeypatch.setattr(audit_pipeline, "_audit_queue", tiny_queue)
        monkeypatch.setattr(audit_pipeline, "_dropped_count", 0)

        record = {
            "ts": datetime.now(), "client_ip": "1.1.1.1", "method": "GET",
            "path": "/x", "query": "", "status_code": 200, "duration_ms": 1.0,
            "user_email": None,
        }
        audit_pipeline.enqueue_audit_record(record)  # fills the queue
        audit_pipeline.enqueue_audit_record(record)  # must be dropped, not raise

        assert audit_pipeline.dropped_count() == 1

    def test_writer_thread_flushes_to_db_on_batch_size(self):
        writer = audit_pipeline.AuditWriterThread(batch_size=3, flush_interval=10.0)
        writer.start()
        try:
            for i in range(3):
                audit_pipeline.enqueue_audit_record({
                    "ts": datetime.now(), "client_ip": "1.1.1.1", "method": "GET",
                    "path": f"/batch/{i}", "query": "", "status_code": 200,
                    "duration_ms": 1.0, "user_email": None,
                })
            # Give the thread a moment to drain and flush.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if audit_db.query_page(limit=10)["total"] >= 3:
                    break
                time.sleep(0.05)
            assert audit_db.query_page(limit=10)["total"] == 3
        finally:
            writer.stop(timeout=5)

    def test_writer_thread_stop_flushes_remaining_partial_batch(self):
        writer = audit_pipeline.AuditWriterThread(batch_size=100, flush_interval=10.0)
        writer.start()
        audit_pipeline.enqueue_audit_record({
            "ts": datetime.now(), "client_ip": "1.1.1.1", "method": "DELETE",
            "path": "/final", "query": "", "status_code": 204,
            "duration_ms": 0.5, "user_email": None,
        })
        # Never reaches batch_size or flush_interval before we stop —
        # stop() must still flush it.
        writer.stop(timeout=5)
        assert audit_db.query_page(search="/final")["total"] == 1

    def test_viewing_the_audit_log_is_not_itself_recorded(self, monkeypatch):
        """Browsing/paginating/exporting the Audit Logs page must not flood
        the very trail being viewed — see logging._AUDIT_COLLECTION_EXCLUDED_PATHS.
        A control request to an unrelated endpoint proves the middleware
        still records everything else.
        """
        from fastapi.testclient import TestClient
        from app.models.system_manager import system_manager

        monkeypatch.setattr(system_manager, "load_persisted_state", lambda: None)
        monkeypatch.setattr(system_manager, "start_refresh_thread", lambda: None)
        monkeypatch.setattr(system_manager, "stop_refresh_thread", lambda: None)
        monkeypatch.setattr(system_manager, "start_audit_writer", lambda: None)
        monkeypatch.setattr(system_manager, "stop_audit_writer", lambda: None)

        from app.app import create_app
        with TestClient(create_app()) as client:
            client.get("/api/v1/system/audit-logs?limit=5")
            client.get("/api/v1/system/audit-logs/export")
            client.get("/api/v1/system/settings")  # control: must still be recorded

        queued_paths = []
        while True:
            try:
                queued_paths.append(audit_pipeline._audit_queue.get_nowait()["path"])
            except queue.Empty:
                break

        assert "/api/v1/system/audit-logs" not in queued_paths
        assert "/api/v1/system/audit-logs/export" not in queued_paths
        assert "/api/v1/system/settings" in queued_paths

    def test_stop_is_responsive_even_with_long_flush_interval(self):
        """stop() must not block anywhere near a full flush_interval."""
        writer = audit_pipeline.AuditWriterThread(batch_size=1000, flush_interval=30.0)
        writer.start()
        time.sleep(0.05)  # let it enter its first queue.get() wait
        start = time.monotonic()
        writer.stop(timeout=5)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"stop() took {elapsed:.2f}s — writer thread isn't polling frequently enough"
