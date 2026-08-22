"""
Base storage connector and concrete implementations.

Architecture (Constitution §VI – Extensible Data Sources):
All data sources inherit from BaseConnector. Each connector loads raw data
into a DuckDB virtual table whose name equals the connector's ``name``
attribute, making it directly referenceable in dashboard SQL queries.

Supported: CSVConnector, JSONConnector, ParquetConnector, BigQueryConnector

Security: ``name`` is validated as a SQL identifier at construction time
(``BaseConnector.__init__``) since it is interpolated directly into DDL.
User-controlled file paths are never interpolated raw — they are bound as
DuckDB query parameters where the engine allows it (CSV/JSON), or escaped
as SQL string literals where it doesn't (Parquet's ``CREATE VIEW``, which
DuckDB rejects prepared parameters in). See ADR-012 in
``docs/backend_architecture.md``.

File-connector ``filepath`` values are also constrained to resolve inside
``settings.data_dir`` (``CSVConnector``/``JSONConnector``/``ParquetConnector``
``__init__``), so a caller can't read arbitrary files elsewhere on the
container filesystem. This is enforced twice: once at the Pydantic/API
layer (``app/models/schemas.py``) for a clean 422, and again here so
connectors reconstructed from ``settings/data_sources.yaml`` on restart
(which bypasses Pydantic — see ``SystemManager.load_persisted_state``)
can't smuggle in an out-of-bounds path. See ADR-013 in
``docs/backend_architecture.md``.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Type, Union

import duckdb

from app.core.config import get_settings
from app.core.db import get_db, get_write_lock
from app.core.logging import get_logger
from app.core.validator import sanitize_name, validate_path_within_base

_logger = get_logger("buchimaker.connectors")


class BaseConnector(ABC):
    """Abstract base for all data-source connectors.

    Subclasses must implement :meth:`load` to ingest data into a DuckDB
    virtual table named ``self.name``.

    Attributes:
        id: Unique UUID for this connector instance.
        name: SQL-safe short name used as the DuckDB table name.
        title: Human-readable display name.
        description: Optional free-text description.
        source_type: Class-level type tag (e.g. "csv", "json").
        last_updated: Unix epoch of last successful load.
    """

    source_type: ClassVar[str] = "base"

    def __init__(
        self,
        name: str,
        title: str,
        description: Optional[str] = None
    ):
        """Initialise a connector.

        Args:
            name: SQL-safe short name (becomes the DuckDB table name).
            title: Human-readable display name.
            description: Optional description shown in the UI.

        Raises:
            ValueError: If ``name`` is not SQL-safe. This is enforced here
                (not just at the API/Pydantic layer) because ``name`` is
                interpolated directly into SQL as a table/view identifier,
                and connectors can also be constructed from saved config
                files that bypass request validation.
        """
        sanitize_name(name, "name")
        self.id: str = str(uuid.uuid4())
        self.name: str = name
        self.title: str = title
        self.description: Optional[str] = description
        self.last_updated: int = 0

    @abstractmethod
    def load(self, con: Optional[duckdb.DuckDBPyConnection] = None) -> None:
        """Ingest source data into a DuckDB table named ``self.name``.

        Must acquire get_write_lock(), drop the previous table, recreate it,
        and set self.last_updated = int(time.time()) on success.
        """

    def get_schema(self) -> List[Dict[str, str]]:
        """Return column names and types for the DuckDB table.

        Returns:
            List of {"column": str, "type": str} dicts.

        Raises:
            RuntimeError: If the table has not been loaded yet.
        """
        con = get_db()
        try:
            result = con.execute(
                f"DESCRIBE SELECT * FROM {self.name} LIMIT 0"
            ).fetchall()
            return [{"column": row[0], "type": row[1]} for row in result]
        except Exception as exc:
            raise RuntimeError(
                f"Could not describe table '{self.name}'. Has it been loaded? {exc}"
            ) from exc

    def drop(self) -> None:
        """Remove the DuckDB table or view. Silently succeeds if absent."""
        con = get_db()
        with get_write_lock():
            try:
                con.execute(f"DROP VIEW IF EXISTS {self.name}")
            except Exception:
                pass
            try:
                con.execute(f"DROP TABLE IF EXISTS {self.name}")
            except Exception:
                pass
        _logger.info("connector_dropped", name=self.name)

    def to_info(self) -> Dict[str, Any]:
        """Serialise connector metadata for API responses.

        Returns:
            Dict compatible with DataSourceInfo schema.
        """
        return {
            "id": self.id,
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "source_type": self.source_type,
            "filepath": getattr(self, "filepath", None),
            "last_updated": self.last_updated,
        }

    @abstractmethod
    def to_config(self) -> Dict[str, Any]:
        """Serialise connector configuration for saving across restarts.

        Returns:
            Dict containing the arguments required to recreate this connector.
        """



class CSVConnector(BaseConnector):
    """Load a local CSV file into a DuckDB view via read_csv_auto.

    Args:
        name: SQL-safe table name.
        title: Display title.
        filepath: Absolute path to the CSV file.
        description: Optional description.
    """

    source_type: ClassVar[str] = "csv"

    def __init__(self, name: str, title: str, filepath: str,
                 description: Optional[str] = None):
        super().__init__(name=name, title=title, description=description)
        validate_path_within_base(filepath, get_settings().data_dir)
        self.filepath: str = filepath

    def load(self, con: Optional[duckdb.DuckDBPyConnection] = None) -> None:
        """Register the CSV file as a DuckDB view.

        Raises:
            FileNotFoundError: If filepath does not exist.
        """
        path = Path(self.filepath)
        if not path.exists():
            raise FileNotFoundError(f"CSV file not found: {self.filepath}")
        _con = con if con is not None else get_db()
        with get_write_lock():
            _con.execute(f"DROP TABLE IF EXISTS {self.name}")
            _con.execute(
                f"CREATE TABLE {self.name} AS "
                f"SELECT * FROM read_csv_auto(?, header=true)",
                [path.as_posix()],
            )
        self.last_updated = int(time.time())
        _logger.info("csv_loaded", name=self.name, filepath=self.filepath)

    def to_config(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "filepath": self.filepath,
            "description": self.description,
            "source_type": self.source_type,
        }


class JSONConnector(BaseConnector):
    """Load a local JSON/JSON-Lines file into a DuckDB view.

    Args:
        name: SQL-safe table name.
        title: Display title.
        filepath: Absolute path to the JSON file.
        description: Optional description.
    """

    source_type: ClassVar[str] = "json"

    def __init__(self, name: str, title: str, filepath: str,
                 description: Optional[str] = None):
        super().__init__(name=name, title=title, description=description)
        validate_path_within_base(filepath, get_settings().data_dir)
        self.filepath: str = filepath

    def load(self, con: Optional[duckdb.DuckDBPyConnection] = None) -> None:
        """Register the JSON file as a DuckDB view.

        Raises:
            FileNotFoundError: If filepath does not exist.
        """
        path = Path(self.filepath)
        if not path.exists():
            raise FileNotFoundError(f"JSON file not found: {self.filepath}")
        _con = con if con is not None else get_db()
        with get_write_lock():
            _con.execute(f"DROP TABLE IF EXISTS {self.name}")
            _con.execute(
                f"CREATE TABLE {self.name} AS "
                f"SELECT * FROM read_json_auto(?)",
                [path.as_posix()],
            )
        self.last_updated = int(time.time())
        _logger.info("json_loaded", name=self.name, filepath=self.filepath)

    def to_config(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "filepath": self.filepath,
            "description": self.description,
            "source_type": self.source_type,
        }


class ParquetConnector(BaseConnector):
    """Load a local Parquet file (or glob pattern) into a DuckDB view.

    Args:
        name: SQL-safe table name.
        title: Display title.
        filepath: Absolute path or glob pattern to the Parquet file(s).
        description: Optional description.
        hive_partitioning: Boolean to enable Hive partitioning.
    """

    source_type: ClassVar[str] = "parquet"

    def __init__(self, name: str, title: str, filepath: str,
                 description: Optional[str] = None, hive_partitioning: bool = False):
        super().__init__(name=name, title=title, description=description)
        validate_path_within_base(filepath, get_settings().data_dir)
        self.filepath: str = filepath
        self.hive_partitioning: bool = hive_partitioning

    def load(self, con: Optional[duckdb.DuckDBPyConnection] = None) -> None:
        """Register the Parquet file(s) as a DuckDB view.

        Note: filepath might be a glob, so we don't do strict .exists() check here,
        letting DuckDB handle missing paths or globs.
        """
        _con = con if con is not None else get_db()
        hive_part_str = "true" if self.hive_partitioning else "false"
        with get_write_lock():
            try:
                _con.execute(f"DROP VIEW IF EXISTS {self.name}")
            except Exception:
                pass
            try:
                _con.execute(f"DROP TABLE IF EXISTS {self.name}")
            except Exception:
                pass
            # DuckDB's read_parquet supports globs natively. CREATE VIEW
            # bodies can't use bound parameters (DuckDB rejects prepared
            # parameters in CREATE VIEW statements), so the path is escaped
            # as a SQL string literal instead: doubling embedded single
            # quotes is the standard SQL escape and prevents breaking out
            # of the literal.
            escaped_filepath = self.filepath.replace("'", "''")
            _con.execute(
                f"CREATE OR REPLACE VIEW {self.name} AS "
                f"SELECT * FROM read_parquet('{escaped_filepath}', hive_partitioning={hive_part_str})"
            )
        self.last_updated = int(time.time())
        _logger.info("parquet_loaded", name=self.name, filepath=self.filepath)

    def to_config(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "filepath": self.filepath,
            "description": self.description,
            "source_type": self.source_type,
            "hive_partitioning": self.hive_partitioning,
        }



class BigQueryConnector(BaseConnector):
    """Load data from Google BigQuery into a DuckDB in-memory table.

    Requires google-cloud-bigquery and pandas (optional dependency).
    Credentials are supplied via a service-account JSON file path –
    never hardcoded in source (Constitution §Security).

    Args:
        name: SQL-safe table name.
        title: Display title.
        project_id: GCP project ID.
        dataset_id: BigQuery dataset ID.
        table_id: BigQuery table ID.
        credentials_path: Path to service-account JSON key.
        query: Optional override SQL (defaults to SELECT * FROM table).
        description: Optional description.
    """

    source_type: ClassVar[str] = "bigquery"

    def __init__(self, name: str, title: str, project_id: str,
                 dataset_id: str, table_id: str, credentials_path: str,
                 query: Optional[str] = None,
                 description: Optional[str] = None):
        super().__init__(name=name, title=title, description=description)
        self.project_id = project_id
        self.dataset_id = dataset_id
        self.table_id = table_id
        self.credentials_path = credentials_path
        self.query = query or f"SELECT * FROM `{project_id}.{dataset_id}.{table_id}`"

    def load(self, con: Optional[duckdb.DuckDBPyConnection] = None) -> None:
        """Fetch from BigQuery and materialise into DuckDB.

        Queries BigQuery into a Pandas DataFrame, then registers it in
        DuckDB.

        Raises:
            ImportError: If google-cloud-bigquery or pandas is missing.
        """
        try:
            from google.cloud import bigquery
            from google.oauth2 import service_account
        except ImportError as exc:
            raise ImportError(
                "Missing bigquery dependency. Install with: pip install google-cloud-bigquery pandas db-dtypes"
            ) from exc

        _logger.info("bigquery_fetching", project=self.project_id, table=self.table_id)
        
        credentials = service_account.Credentials.from_service_account_file(self.credentials_path)
        client = bigquery.Client(credentials=credentials, project=self.project_id)
        
        # Execute query and convert to pandas DataFrame
        df = client.query(self.query).to_dataframe()
        
        _con = con if con is not None else get_db()
        with get_write_lock():
            _con.execute(f"DROP VIEW IF EXISTS {self.name}")
            _con.execute(f"DROP TABLE IF EXISTS {self.name}")
            
            # Using _con.register which creates a view pointing to the df
            _con.register(f"__{self.name}_df", df)
            _con.execute(
                f"CREATE TABLE {self.name} AS SELECT * FROM __{self.name}_df"
            )
            _con.execute(f"DROP VIEW IF EXISTS __{self.name}_df")
            
        self.last_updated = int(time.time())
        _logger.info("bigquery_loaded", name=self.name, rows=len(df))

    def to_config(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "project_id": self.project_id,
            "dataset_id": self.dataset_id,
            "table_id": self.table_id,
            "credentials_path": self.credentials_path,
            "query": self.query,
            "description": self.description,
            "source_type": self.source_type,
            "auto_refresh": self.auto_refresh,
        }


class ConnectorRegistry:
    """Factory and catalogue of known connector types.

    Register new connectors at import time with :meth:`register`.

    Attributes:
        _registry: Mapping of source_type tag → connector class.
    """

    _registry: ClassVar[Dict[str, Type[BaseConnector]]] = {}

    @classmethod
    def register(cls, connector_class: Type[BaseConnector]) -> None:
        """Register a connector class under its source_type tag.

        Args:
            connector_class: Subclass of BaseConnector.
        """
        cls._registry[connector_class.source_type] = connector_class

    @classmethod
    def get(cls, source_type: str) -> Optional[Type[BaseConnector]]:
        """Retrieve a connector class by type tag.

        Args:
            source_type: Type tag string (e.g. "csv").

        Returns:
            Connector class or None.
        """
        return cls._registry.get(source_type)

    @classmethod
    def list_types(cls) -> List[str]:
        """Return all registered source_type tags.

        Returns:
            List of registered type strings.
        """
        return list(cls._registry.keys())


# Register built-in connectors on module import
ConnectorRegistry.register(CSVConnector)
ConnectorRegistry.register(JSONConnector)
ConnectorRegistry.register(ParquetConnector)
ConnectorRegistry.register(BigQueryConnector)
