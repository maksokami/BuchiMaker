import os
import pytest
from app.core.db import close_db
from app.core.audit_db import close_audit_db
from app.core.config import get_settings
from app.core.general_settings import general_settings, _DEFAULTS
import app.connectors.base as connectors_base
import app.models.schemas as schemas

def _remove_if_exists(path):
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass

def _reset_general_settings():
    # general_settings is a process-wide singleton loaded once from disk, so
    # deleting the YAML file alone doesn't reset it within a running test
    # session — the in-memory copy has to be reset too, or state set by one
    # test (e.g. sql_api_enabled) silently leaks into unrelated later tests.
    general_settings._data = dict(_DEFAULTS)
    _remove_if_exists("./settings/general.yaml")

@pytest.fixture(autouse=True)
def sandbox_data_dir(tmp_path, monkeypatch):
    """Point ``settings.data_dir`` at this test's own tmp_path.

    File connectors (CSV/JSON/Parquet) now reject any filepath that
    resolves outside the configured data directory (path-traversal /
    arbitrary-file-read guard — see ADR-013 in docs/backend_architecture.md).
    Tests create their fixture files under pytest's per-test ``tmp_path``,
    which isn't the real ``/app/data`` default, so both call sites that
    consult ``get_settings().data_dir`` (``app.connectors.base`` and
    ``app.models.schemas``) are patched here to treat ``tmp_path`` as the
    sandboxed data root for the duration of each test.
    """
    sandboxed = get_settings().model_copy(update={"data_dir": str(tmp_path)})
    monkeypatch.setattr(connectors_base, "get_settings", lambda: sandboxed)
    monkeypatch.setattr(schemas, "get_settings", lambda: sandboxed)

@pytest.fixture(autouse=True, scope="function")
def wipe_db_file():
    """Ensure the DuckDB files (main + standby + audit) are removed before and after each test."""
    close_db()
    close_audit_db()
    db_path = get_settings().duckdb_database
    standby_db_path = get_settings().duckdb_database_standby
    audit_db_path = get_settings().audit_duckdb_database
    marker_path = f"{db_path}.active_slot"
    _remove_if_exists(db_path)
    _remove_if_exists(standby_db_path)
    _remove_if_exists(audit_db_path)
    _remove_if_exists(marker_path)
    _reset_general_settings()
    yield
    close_db()
    close_audit_db()
    _remove_if_exists(db_path)
    _remove_if_exists(standby_db_path)
    _remove_if_exists(audit_db_path)
    _remove_if_exists(marker_path)
    _reset_general_settings()
