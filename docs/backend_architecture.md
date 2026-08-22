# BuchiMaker Backend – Architecture & Developer Guide

**Version**: 0.3.0 | **Constitution**: 1.0.0

---

## Table of Contents
1. [Project Layout](#project-layout)
2. [Class Structure](#class-structure)
3. [Filter Pipeline](#filter-pipeline)
4. [Caching Architecture](#caching-architecture)
5. [Middleware Stack](#middleware-stack)
6. [API Reference](#api-reference)
7. [Running Locally](#running-locally)
8. [Running with Docker](#running-with-docker)
9. [Running Tests](#running-tests)
10. [Configuration Reference](#configuration-reference)
11. [Adding a New Connector](#adding-a-new-connector)
12. [Architecture Decisions (ADRs)](#architecture-decisions)

---

## Project Layout

```
llm-databoom-v0.3/
├── docker-compose.yml          # Full stack orchestration
├── dashboards/                 # YAML dashboard definitions (mounted read-only)
│   └── template.yaml
├── data/                       # Local CSV / JSON files (mounted read-only)
├── spec/                       # Architecture specs and constitution
└── backend/
    ├── Dockerfile              # Multi-stage build (builder → runtime)
    ├── main.py                 # Entry point: starts 2 uvicorn servers
    ├── pyproject.toml          # pytest config
    ├── requirements.in         # Production deps, human-edited, loosely bounded
    ├── requirements.txt        # Production deps, compiled + hash-pinned (see ADR-014)
    ├── requirements-dev.in     # Test-only deps, human-edited
    ├── requirements-dev.txt    # Test-only deps, compiled + hash-pinned
    ├── scripts/
    │   └── update-lockfiles.sh # Regenerates the two .txt lockfiles from the .in files
    ├── settings/
    │   ├── general.yaml        # Runtime-editable settings (row_limit, redis_ttl)
    │   ├── data_sources.yaml   # Persisted data source configs (auto-written)
    │   └── dashboards.yaml     # Persisted dashboard paths (auto-written)
    └── app/
        ├── app.py              # FastAPI factory + lifespan
        ├── core/
        │   ├── config.py           # Pydantic-settings: all env-var config
        │   ├── db.py               # DuckDB singleton connection manager
        │   ├── general_settings.py # YAML-backed runtime settings singleton
        │   ├── logging.py          # structlog setup + AuditLogMiddleware
        │   ├── redis_client.py     # Redis cache wrapper + DS→key index
        │   └── validator.py        # Input sanitisation + InputValidationMiddleware
        ├── connectors/
        │   └── base.py         # BaseConnector, CSV/JSON/BigQuery + Registry
        ├── models/
        │   ├── dashboard.py    # Summary, Aggregate, Filter, Dashboard
        │   ├── schemas.py      # Pydantic request/response models
        │   └── system_manager.py # SystemManager singleton
        └── api/
            ├── health.py       # GET /healthz
            ├── system.py       # /api/v1/system/*
            └── dashboards.py   # /api/v1/dashboards/*
    └── tests/
        ├── test_validator.py   # Unit: input validation
        ├── test_connectors.py  # Unit: CSV/JSON/BigQuery connectors
        ├── test_dashboard.py   # Unit: Summary/Aggregate/Filter/Dashboard
        └── test_api.py         # Integration: all API endpoints
```

---

## Class Structure

```
SystemManager  (singleton in app/models/system_manager.py)
│  ├── dashboards: Dict[str, Dashboard]
│  ├── data_sources: Dict[str, BaseConnector]
│  ├── settings: Dict
│  ├── register_data_source(connector)   ← also calls redis_cache.invalidate_datasource()
│  ├── remove_data_source(name)
│  ├── list_data_sources() → List[Dict]
│  ├── load_dashboard(filepath) → str   (empty = ok)
│  ├── auto_load_dashboards() → List[str]  (errors)
│  ├── global_refresh(rate_limit=True) → None            ← NEW (background DuckDB swap)
│  ├── start/stop_refresh_thread()
│  └── health_summary() → Dict

Dashboard  (app/models/dashboard.py)
│  ├── id, name, description, prompt, source_table
│  │     └── source_table is NOT read from YAML directly — it's the view
│  │         name extracted from the required `base_view` DDL and set once
│  │         that DDL is executed against DuckDB in load_yaml(). See "Filter
│  │         Pipeline" and ADR-011.
│  ├── summaries: List[Summary]
│  ├── aggregates: List[Aggregate]
│  ├── filters: Dict[hash, Filter]        ← legacy view-based cache (background refresh)
│  ├── mappings: Dict[str, str]           ← widget_id alias → DB column expression
│  ├── layout: List[Dict]                 ← parsed widget definitions
│  ├── settings: {refresh_frequency_filter, unallocate_frequency_filter}
│  ├── load_yaml(filepath) → str
│  │     └── executes `base_view`, validates every total/aggregate query
│  │         references the resulting view name                 ← NEW
│  ├── drop_base_view() → None            ← NEW (called on delete/rename)
│  ├── parse_widget_filters(raw_filters) → (conditions, hash)   ← NEW
│  ├── get_data_for_filters(conditions, row_limit) → Dict       ← NEW
│  ├── _build_widget_map() → List[Dict]                         ← NEW
│  ├── _raw_sources_needed() → List[str]                        ← NEW
│  ├── _build_sql_where(conditions) → str                       ← NEW
│  ├── set_filters(conditions) → Filter    ← legacy (background thread)
│  ├── trim_filters() → int
│  ├── refresh_filters()
│  ├── get_dashboard_data(filter_hash?, truncate_mb) → Dict     ← legacy
│  ├── get_dashboard_definition() → Dict
│  ├── list_virtual_tables() → List[str]
│  └── widget_count() → int

Filter  (legacy, one per unique filter combination — used by background refresh thread)
│  ├── id, name, filter_hash, view_name (DuckDB view)
│  ├── filter_conditions, source_table, is_loaded
│  ├── last_refreshed, last_queried (epoch)
│  ├── is_fresh() → bool
│  ├── refresh()            ← (re)builds DuckDB view
│  ├── get_data(truncate_mb) → Dict
│  └── drop()

Summary   (single-value query → tiles/gauges)
│  ├── id, name, query
│  └── get_data(con) → Any

Aggregate  (multi-row query → charts/tables)
│  ├── id, name, query, column_aliases
│  └── get_data(con) → List[Dict]

GeneralSettings  (app/core/general_settings.py — singleton)
│  ├── row_limit: int          (default 1000, persisted in settings/general.yaml)
│  ├── redis_ttl_seconds: int  (default 1800, persisted in settings/general.yaml)
│  ├── auto_refresh: Union[int, List[str], None] (Global scheduled refresh)
│  ├── as_dict() → Dict
│  └── update(updates: Dict)

RedisCache  (app/core/redis_client.py — singleton)
│  ├── available: bool
│  ├── get(dashboard_id, filter_hash) → bytes | None
│  ├── set(dashboard_id, filter_hash, payload, datasource_names, ttl_seconds)
│  ├── invalidate_datasource(datasource_name) → int   ← bulk-delete by DS (reverse index)
│  ├── invalidate_dashboard(dashboard_id) → int        ← bulk-delete by dashboard (SCAN match)
│  ├── delete(dashboard_id, filter_hash)
│  └── flush_all() → bool                              ← FLUSHDB, wipes the whole cache

BaseConnector (ABC, app/connectors/base.py)
│   └── __init__ validates `name` via sanitize_name() — SQL identifier, not just
│       Pydantic-layer validation (see ADR-012)
├── CSVConnector      source_type="csv"
│   └── __init__ validates filepath is inside DATA_DIR (see ADR-013)
│   └── load(con?) → CREATE TABLE from read_csv_auto(?)      [filepath bound as param]
├── JSONConnector     source_type="json"
│   └── __init__ validates filepath is inside DATA_DIR (see ADR-013)
│   └── load(con?) → CREATE TABLE from read_json_auto(?)     [filepath bound as param]
├── ParquetConnector  source_type="parquet"
│   └── __init__ validates filepath is inside DATA_DIR (see ADR-013)
│   └── load(con?) → CREATE OR REPLACE VIEW from read_parquet('…')  [filepath escaped,
│       not bound — DuckDB rejects prepared params inside CREATE VIEW, see ADR-012]
└── BigQueryConnector source_type="bigquery"
    └── load(con?) → BQ client → pandas → DuckDB table   [no filepath — DATA_DIR
        sandboxing (ADR-013) doesn't apply; credentials_path is a container path
        to a service-account key, not sandboxed today]

ConnectorRegistry  (class-level factory)
├── register(class)
├── get(source_type) → class | None
└── list_types() → List[str]
```

---

## Filter Pipeline

The unified `POST /api/v1/dashboards/{dashboard_id}/data` endpoint handles **both** filter
resolution and data retrieval in a single round-trip. 

Filtering depends on every dashboard declaring a `base_view` (see ADR-011):
a `CREATE [OR REPLACE] VIEW <name> AS SELECT ...` statement, required in
every dashboard YAML, executed once at `load_yaml()` time. The resulting
view name becomes `Dashboard.source_table`, and every total/aggregate query
in the YAML is validated at load time to reference that view name — they
can no longer read the underlying source table(s) directly. This is what
lets step 5 below inject one WHERE clause that reliably applies to every
widget's query, regardless of what joins or column renames the dashboard
author's own queries perform internally.

### Input sources (merged, body wins on conflict)

```
1. URL query parameters   ?f[widget_id]=value    (equality-only, for deep-links)
2. JSON body              {"filters": {"widget_id": "value"}}
                          {"filters": {"widget_id": {"operator": "has", "value": "x"}}}
```

### Full pipeline

```
POST /api/v1/dashboards/{id}/data
  │
  ├─ InputValidationMiddleware  → reject body > 2 MB
  ├─ AuditLogMiddleware         → log client_ip, path, status, ms
  │
  └─ Route handler (dashboards.py)
       │
       ├─ 1. ID validation
       │    └─ raise_if_invalid_name(dashboard_id)
       │         └─ sanitize_name(): pattern [a-zA-Z0-9 _\-], reject empty
       │
       ├─ 2. Merge filter sources
       │    ├─ Query-params: ?f[widget_id]=value → all treated as "eq"
       │    └─ Body filters: override query-param entries with same key
       │
       ├─ 3. parse_widget_filters(merged_filters)  [Dashboard method]
       │    ├─ For each widget_id:
       │    │    ├─ sanitize_name(widget_id)          ← SECURITY
       │    │    ├─ lookup widget in layout (filter widgets only)
       │    │    ├─ resolve mapping alias → DB column expression
       │    │    ├─ validate operator against allowed set
       │    │    └─ build condition: {column, operator, value, widget_id}
       │    ├─ Sort conditions (deterministic)
       │    └─ hash = md5(json(sorted_conditions))
       │
       ├─ 4. Cache lookup: redis_cache.get(dashboard_id, hash)
       │    └─ HIT → return cached gzip bytes immediately (no DuckDB)
       │
       ├─ 5. DuckDB query (MISS path)
       │    ├─ _build_sql_where(conditions) → (sql, params)
       │    │    ├─ Numeric values: embedded as literals (float() probe already validated safe)
       │    │    ├─ "has"    → col LIKE ?         param: %val%
       │    │    ├─ "prefix" → col LIKE ?         param: val%
       │    │    ├─ "suffix" → col LIKE ?         param: %val
       │    │    └─ others   → col OP ?           param: val
       │    │    (string values are ALWAYS bound via `?`, never interpolated as
       │    │     literals — closes a SQL-injection gap where an unescaped
       │    │     quote in a filter value could alter the query)
       │    ├─ Totals:     inject CTE WHERE into each summary query (against base_view), execute with params
       │    ├─ Aggregates: inject CTE WHERE + LIMIT {row_limit} (against base_view), execute with params
       │    └─ Raw data:   SELECT * FROM {source} WHERE ... LIMIT {row_limit}, execute with params
       │
       ├─ 6. Build response payload
       │    ├─ totals:     {name → scalar}
       │    ├─ aggregates: {name → [rows]}
       │    ├─ raw:        {source_name → [rows]}  (only if widgets need it)
       │    └─ widget_map: [{widget_id, data_type, data_key}]
       │
       ├─ 7. GZIP compress → redis_cache.set(payload, datasource_names, ttl)
       │    └─ Also registers key in DS reverse-index set for future invalidation
       │
       └─ 8. Return Response(gzip_bytes, Content-Encoding: gzip)
```

### Filter validation & sanitization rules

| Check | Rule | Error |
|-------|------|-------|
| `dashboard_id` chars | `[a-zA-Z0-9 _\-]` only | HTTP 422 |
| `widget_id` chars | `[a-zA-Z0-9 _\-]` only | HTTP 422 |
| Widget exists in layout | Must be a filter-type widget | Silently skipped (external deep-links) |
| Operator value | Must be one of: `eq neq gt lt gte lte has prefix suffix` | HTTP 422 |
| Body `dashboard` assertion | Must match path `dashboard_id` if provided | HTTP 400 |
| Body size | Max 2 MB (InputValidationMiddleware) | HTTP 413 |

### Operator mapping

String-typed values are always bound as query parameters (`?` placeholder),
never interpolated into the SQL string — only numeric values (pre-validated
via `float()`) are embedded as literals, since a valid float can't carry SQL
syntax.

| Alias | SQL |
|-------|-----|
| `eq` | `=` |
| `neq` | `!=` |
| `gt` | `>` |
| `lt` | `<` |
| `gte` | `>=` |
| `lte` | `<=` |
| `has` | `LIKE ?` (param: `%val%`) |
| `prefix` | `LIKE ?` (param: `val%`) |
| `suffix` | `LIKE ?` (param: `%val`) |

### Filter widget types (recognised for filter application)

- `button_filter` — toggle button with a single operator+value
- `input_filter` — free-text input; operators `eq`, `has`, `prefix`, `suffix`
- `dropdown_multy` — multi-select dropdown
- `dropdown_single` — single-select dropdown

Each filter widget must declare a `mapping` field referencing a key in the dashboard
`mappings` section (or directly a DB column name).  The mapping is resolved at parse
time: `widget.mapping → mappings[key] → actual SQL column expression`.

---

## Caching Architecture

### Redis key structure

| Key pattern | Type | Content |
|-------------|------|---------|
| `db:<dashboard_id>:<filter_hash>` | String | GZIP-compressed JSON payload |
| `ds:<datasource_name>:keys` | Set | All cache keys that depend on this data source |

### Cache lifecycle

```
First request (MISS)
  └─ DuckDB queries → JSON (from_cache: false) → gzip → redis SET (with TTL)
  └─ Adds key to DS index set: redis SADD ds:<ds>:keys <key>

Subsequent requests (HIT)
  └─ redis GET → decompress → patch from_cache: false→true → re-compress → return
       (the stored payload always has from_cache:false baked in from write
        time; the hit path patches it so callers can trust the field, not
        just the X-Cache header)

Data source reload
  └─ SystemManager.register_data_source() / global_refresh()
       └─ redis_cache.invalidate_datasource(name)
            └─ redis SMEMBERS ds:<ds>:keys
            └─ redis DEL <all keys> + <index key>

Dashboard YAML reload (POST /dashboards/load)
  └─ SystemManager.load_dashboard()
       └─ redis_cache.invalidate_dashboard(dashboard_id)
            └─ redis SCAN MATCH db:<dashboard_id>:* (dashboard_id is already
               embedded in the data key, so no reverse index is needed here)
            └─ redis DEL <all matched keys>
       (without this, edits to a dashboard's queries/mappings/config are
        invisible to any previously-cached filter combination until the
        Redis TTL expires)
```

### Configuration

Settings live in `./settings/general.yaml` and are hot-reloadable via the API:

| Setting | Default | Description |
|---------|---------|-------------|
| `row_limit` | `1000` | Max rows per DuckDB result set |
| `redis_ttl_seconds` | `1800` | Redis key TTL in seconds (0 = no expiry) |

Redis connection is configured via environment variables:

| Env var | Default | Description |
|---------|---------|-------------|
| `REDIS_HOST` | `localhost` | Redis server hostname |
| `REDIS_PORT` | `6379` | Redis server port |
| `REDIS_DB` | `0` | Redis database index |
| `REDIS_PASSWORD` | *(none)* | Redis auth password |

> [!NOTE]
> Redis is **optional**.  If the `redis` package is not installed or the server is
> unreachable, all cache operations no-op silently and DuckDB is queried on every
> request.  The `X-Cache: MISS` response header confirms the cache bypass.

---

## Middleware Stack

Middleware is registered in **reverse** order (last added = runs first):

| Run order | Class | Responsibility |
|-----------|-------|----------------|
| 1st | `AuditLogMiddleware` | Structured JSON log of every request (Constitution §Security) |
| 2nd | `InputValidationMiddleware` | Reject payloads > 2 MB before body parsing |
| 3rd | `CORSMiddleware` | Allow frontend origin(s) from `ALLOW_ORIGINS` env var (default `*`); `allow_credentials=True` so the SSO session cookie is attached cross-origin — Starlette always echoes the specific request `Origin` (never a literal `*`) once `allow_credentials=True`. The default deployment never needs this at all: `frontend/nginx.conf` proxies `/api/`/`/auth/` to the backend, so frontend and backend are same-origin. |

All user-supplied name strings use `SafeName` / `SafeIdentifier` Pydantic
annotated types enforcing `[a-zA-Z0-9 _\-]` at parse time.

---

## Authentication & Authorization

Gated by `general_settings.anonymous_access` (Settings > Access tab's
"Allow Anonymous Access", persisted in `settings/general.yaml`), enforced
via the `require_authentication` FastAPI dependency (`app/core/auth.py`)
applied to the `system`, `dashboards`, and `widgets` routers — `/healthz`
and `/auth/*` stay unauthenticated (probes, and the login/logout/me
endpoints themselves can't require an existing session to reach them).

| Value | Behavior |
|-------|----------|
| `True` (default on a fresh install) | No authentication — every request is treated as the anonymous `Administrator` principal, unconditionally (any existing session cookie is ignored). |
| `False` | Requires a valid session cookie, created by completing OIDC login at `/auth/login`. |

The old `ANONYMOUS_USER` env var (`app/core/config.py`) only seeds
`anonymous_access` the very first time the app runs, before
`settings/general.yaml` exists — after that it's ignored and the UI toggle
is the sole runtime control. `docker-compose.yml` seeds it to `ALLOW` so a
fresh install is usable immediately.

**OIDC login flow** (the "BFF" pattern — see `app/core/oidc.py`,
`app/core/session_store.py`, `app/api/auth.py`):
1. `GET /auth/login` redirects the browser to the identity provider's
   authorization endpoint (resolved via OIDC discovery from
   `general_settings.sso.issuer_url`), with a PKCE challenge and a
   `state`/`nonce` pair stashed server-side in Redis (`oidc_flow:<state>`,
   5-minute TTL) — never in a cookie, so concurrent logins in different
   tabs don't collide.
2. `GET /auth/callback` exchanges the authorization code for tokens
   directly (client secret never leaves the server), verifies the ID
   token's signature/issuer/audience/nonce against the provider's JWKS
   (`PyJWKClient`), resolves the caller's role from
   `general_settings.role_mappings` (`app.core.roles.resolve_role_from_claims`
   — first matching `{claim, value}` wins; no match is always `Deny`, not
   a configurable default), and creates a session in Redis
   (`sso_session:<id>`, 8-hour TTL) referenced by an httpOnly,
   `SameSite=Lax` cookie.
3. `GET /auth/me` is polled by the frontend at startup to decide what to
   render; `GET /auth/logout` destroys the session and, if the provider
   supports it, redirects through RP-initiated logout.

**Roles** (`app/core/roles.py`): `Administrator` (unrestricted) →
`Data Admin` (dashboards, data sources, `/system/sql`; nothing else in
Settings) → `Viewer` (read-only dashboards) → `Deny` (no API access beyond
`/auth/*`/`/healthz`). Applied as a second, route-level
`Depends(require_role(...))` layered on top of the router-level
`require_authentication` — see the `dependencies=` on individual routes in
`app/api/system.py`/`dashboards.py`, and at the router level in
`app/api/widgets.py` (every route there is `Administrator`-only).

---

## API Reference

Swagger UI: **http://localhost:8000/docs** (when running)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/healthz` | Health probe (also on port 35400) |
| `GET` | `/auth/login` | Redirect to the OIDC provider's authorization endpoint |
| `GET` | `/auth/callback` | OIDC provider redirect target; completes login, sets the session cookie |
| `GET` | `/auth/logout` | Destroy the session; redirect through provider RP-logout if supported |
| `GET` | `/auth/me` | Current caller's resolved identity/role — polled by the frontend at startup |
| `GET` | `/api/v1/system/access` | **Administrator only.** Get anonymous-access toggle + role mappings |
| `PUT` | `/api/v1/system/access` | **Administrator only.** Update anonymous-access toggle + role mappings |
| `GET` | `/api/v1/system/sso` | **Administrator only.** Get OIDC connection settings (secret never returned) |
| `PUT` | `/api/v1/system/sso` | **Administrator only.** Update OIDC connection settings |
| `POST` | `/api/v1/system/sso/test` | **Administrator only.** Resolve an issuer's OIDC discovery document |
| `GET` | `/api/v1/system/settings` | Get global settings |
| `PUT` | `/api/v1/system/settings` | Update global settings |
| `POST` | `/api/v1/system/cache/reset` | **Flush the entire Redis cache (`FLUSHDB`)** |
| `GET` | `/api/v1/system/data-sources` | List registered data sources |
| `POST` | `/api/v1/system/data-sources/csv` | Register a CSV data source |
| `POST` | `/api/v1/system/data-sources/json` | Register a JSON data source |
| `POST` | `/api/v1/system/data-sources/bigquery` | Register a BigQuery source |
| `DELETE` | `/api/v1/system/data-sources/{name}` | Remove a data source |
| `GET` | `/api/v1/system/connector-types` | List connector type tags |
| `POST`| `/api/v1/system/refresh` | **Trigger a global background refresh and DuckDB swap** |
| `GET` | `/api/v1/dashboards` | List loaded dashboards |
| `POST` | `/api/v1/dashboards/load` | Load/reload a dashboard YAML |
| `GET` | `/api/v1/dashboards/{id}` | Get dashboard definition |
| `POST` | `/api/v1/dashboards/{id}/data` | **Apply filters + get all widget data** |
| `POST` | `/api/v1/dashboards/{id}/widgets/{widget_id}/page` | **Paginated table rows (no Redis)** |
| `POST` | `/api/v1/dashboards/{id}/widgets/{widget_id}/export` | **Streamed CSV export (up to 100,000 rows)** |
| `GET` | `/api/v1/dashboards/{id}/virtual-tables` | List DuckDB views |
| `DELETE` | `/api/v1/dashboards/{id}` | Unregister a dashboard |

### `POST /api/v1/dashboards/{id}/data` — request body

```json
{
  "dashboard": "sales-force_1",
  "filters": {
    "dpm_list": "ear-aa-1232",
    "input_filter_test": {"operator": "has", "value": "ear-aa-1"},
    "org_Unit": "Digital section (323)"
  }
}
```

`dashboard` — optional; if provided, must match the path `{id}`.

### `POST /api/v1/dashboards/{id}/widgets/{widget_id}/page` — table pagination

Fetches a single page of rows from a specific table widget.  **Redis is never
consulted** — DuckDB's native `LIMIT`/`OFFSET` is used directly.

**Request body:**
```json
{
  "filters": {
    "product_search": {"operator": "has", "value": "app"}
  },
  "limit": 100,
  "offset": 1000
}
```

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `filters` | dict | `{}` | — | Same widget-id-keyed format as `POST /data` |
| `limit` | int | `100` | 1–5000 | Page size (rows per response) |
| `offset` | int | `0` | ≥ 0 | Zero-based row offset |

**Response:**
```json
{
  "dashboard_id": "sales_overview",
  "widget_id": "table_products",
  "data_key": "by_product",
  "limit": 100,
  "offset": 1000,
  "rows": [{"product": "Widget A", "revenue": 15000}],
  "has_more": true
}
```

Response is GZIP-compressed (`Content-Encoding: gzip`).  
`X-Cache: BYPASS` header is always set to confirm Redis was skipped.

**Supported widgets:** any with a `data` config field (`basic_table`, `bar_chart`, etc.).  
**Rejected:** filter widgets (`input_filter`, `button_filter`, `dropdown_single`, `dropdown_multy`)
and total widgets (`tile`) — returns HTTP 400.

### `POST /api/v1/dashboards/{id}/widgets/{widget_id}/export` — CSV Export

Exports the underlying data of a widget as a streamed CSV file.

**Streaming:** Data is streamed directly from DuckDB to the client, allowing exports of up to 100,000 rows without high memory consumption. It fetches DuckDB results in pyarrow batches (`.fetch_record_batch()`) and streams them efficiently.

**Request body:**
```json
{
  "filters": {
    "product_search": {"operator": "has", "value": "app"}
  }
}
```

Response is a `text/csv` stream with a `Content-Disposition` attachment header.

**Supported widgets:** any with a `data` config field (same as pagination). Filter widgets are rejected.

### `POST /api/v1/dashboards/{id}/data` — response

```json
{
  "dashboard_id": "sales-force_1",
  "filter_hash": "a1b2c3d4...",
  "from_cache": false,
  "row_limit": 1000,
  "totals": {"total_orders": 1482, "revenue": 248300.50},
  "aggregates": {"by_region": [{"region": "North", "total": 890}]},
  "raw": {},
  "widget_map": [
    {"widget_id": "title_1", "data_type": "total", "data_key": "total_orders"},
    {"widget_id": "my_bar", "data_type": "aggregate", "data_key": "by_region"}
  ]
}
```

Response is **GZIP-compressed** (`Content-Encoding: gzip`).  Most HTTP clients
decompress automatically.

---

## Running Locally

```bash
cd backend

python -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements.txt -r requirements-dev.txt

# Run with dashboard auto-discovery
DEBUG=true DASHBOARDS_DIR=../dashboards DATA_DIR=../data python main.py
```

- Main API: http://localhost:8000
- Swagger: http://localhost:8000/docs
- Health: http://localhost:35400/healthz

> [!TIP]
> Redis is optional for local development. Without it, the app runs fine —
> every request simply queries DuckDB directly.

---

## Running with Docker

```bash
# From project root
docker compose up --build

curl http://localhost:8000/docs
curl http://localhost:35400/healthz
```

**Redis Cache Container:**
The `docker-compose.yml` includes an isolated `redis` service using the official `redis:7-alpine` image.
- **Why?**: Isolating Redis prevents massive DuckDB aggregations from triggering an Out-Of-Memory (OOM) kill that would wipe out user sessions and cached dashboard states.
- **Usage**: The backend service is automatically configured to connect to `redis` via the `REDIS_HOST=redis` environment variable in the compose file. You can also connect to it directly via `redis-cli -h localhost -p 6379`.

Create a `.env` file at project root for secrets:

```bash
ALLOW_ORIGINS=*
LOG_TIMEZONE=America/Chicago
REDIS_HOST=redis
REDIS_PORT=6379
```

> [!CAUTION]
> Never commit `.env` files with secrets to git. Use Docker secrets or a
> secrets manager (Vault, AWS SSM) in production environments.

---

## Running Tests

```bash
cd backend
source .venv/bin/activate

pytest                           # all tests
pytest tests/test_validator.py   # unit only
pytest --cov=app --cov-report=term-missing   # with coverage
```

| File | Scope | DuckDB |
|------|-------|--------|
| `test_validator.py` | Unit | No |
| `test_connectors.py` | Unit | Yes (in-memory) |
| `test_dashboard.py` | Unit | Yes (in-memory) |
| `test_api.py` | Integration | Yes (TestClient) |

Each module uses `autouse` fixtures to reset DuckDB and system_manager state.

---

## Configuration Reference

### Environment variables (`config.py`)

| Env var | Default | Description |
|---------|---------|-------------|
| `API_PORT` | `8000` | Main API server port |
| `HEALTH_PORT` | `35400` | Dedicated health server port |
| `DEBUG` | `false` | Debug mode / auto-reload |
| `DASHBOARDS_DIR` | `/app/dashboards` | Path to YAML dashboard files |
| `DATA_DIR` | `/app/data` | Path to local data files |
| `DUCKDB_DATABASE` | `./db/duck.db` | DuckDB database file path |
| `DUCKDB_MEMORY_LIMIT` | `4GB` | DuckDB memory cap |
| `LOG_TIMEZONE` | `local` | IANA TZ for log timestamps |
| `REFRESH_FREQUENCY_FILTER` | `10` | Filter refresh interval (minutes) |
| `UNALLOCATE_FREQUENCY_FILTER` | `30` | Evict idle filters after N minutes |
| `TABLE_TRUNCATE_MB` | `700` | Payload cap before truncation (legacy) |
| `ALLOW_ORIGINS` | `*` | CORS origins (comma-separated), or `*` for any origin — restrict this for public deployments, see README's Security section |
| `ANONYMOUS_USER` | `DENY` (`ALLOW` in `docker-compose.yml`) | Seeds `general_settings.anonymous_access` on first run only — see [Authentication & Authorization](#authentication--authorization). Ignored afterward; use the Access tab's toggle instead. |
| `OIDC_ISSUER_URL` | *(none)* | Optional fallback for `general_settings.sso.issuer_url` if not already saved via the SSO tab |
| `OIDC_CLIENT_ID` | *(none)* | Optional fallback for `general_settings.sso.client_id` |
| `OIDC_CLIENT_SECRET` | *(none)* | Optional fallback for `general_settings.sso.client_secret` |
| `OIDC_REDIRECT_URI` | *(none)* | Optional fallback for `general_settings.sso.redirect_uri` |
| `REDIS_HOST` | `localhost` | Redis server hostname |
| `REDIS_PORT` | `6379` | Redis server port |
| `REDIS_DB` | `0` | Redis database index |
| `REDIS_PASSWORD` | *(none)* | Redis auth password |

### Runtime settings (`settings/general.yaml`)

| Key | Default | Description |
|-----|---------|-------------|
| `row_limit` | `1000` | Max rows per DuckDB result set |
| `redis_ttl_seconds` | `1800` | Redis key TTL in seconds (0 = no expiry) |
| `syslog` | `{...}` | Remote Syslog configuration for API audit logs (`enabled`, `host`, `port`, `tls_enabled`, mTLS cert paths) |
| `anonymous_access` | `True` | Runtime auth gate — see [Authentication & Authorization](#authentication--authorization) |
| `sso` | `{...}` | OIDC connection settings (`issuer_url`, `client_id`, `client_secret`, `scopes`, `redirect_uri`) backing Settings > SSO |
| `role_mappings` | `[]` | OIDC claim → role list backing Settings > Access; evaluated in order, first match wins, no match = `Deny` |

---

## Adding a New Connector

1. Subclass `BaseConnector` in `app/connectors/base.py`:

```python
class S3Connector(BaseConnector):
    source_type = "s3"

    def __init__(self, name, title, bucket, key, **kwargs):
        super().__init__(name=name, title=title, **kwargs)
        self.bucket = bucket
        self.key = key

    def load(self) -> None:
        # download to /tmp, then register as DuckDB view
        ...
        self.last_updated = int(time.time())

ConnectorRegistry.register(S3Connector)
```

2. Add a Pydantic schema in `app/models/schemas.py`. Any free-text field that
   ends up inside a SQL string (paths, keys, credentials refs, etc.) needs
   either a `SafeName`/`SafeIdentifier` annotation (fixed allowlist values
   like `name`) or to be passed as a bound DuckDB parameter at the `load()`
   call site (see ADR-012) — never f-string-interpolated raw. If the new
   connector reads from the local filesystem, also validate that field with
   `validate_path_within_base(value, get_settings().data_dir)` (see ADR-013)
   — as both a `field_validator` on the schema *and* in the connector's
   `__init__`, matching the CSV/JSON/Parquet pattern — so it can't be
   pointed at files outside `DATA_DIR`.
3. Add a POST route in `app/api/system.py`.
4. Write tests in `tests/test_connectors.py` (TDD), including an
   injection-payload case for any new SQL-interpolated field (apostrophes,
   semicolons, comment markers — see the CSV/JSON/Parquet tests for the
   pattern).

---

## Architecture Decisions

### ADR-001: Single DuckDB connection
- **Decision**: One DuckDB connection (file-backed via `DUCKDB_DATABASE`) per process with a `threading.Lock`. Using local files instead of `:memory:` allows the OS to handle heavy memory paging dynamically, keeping CPU usage stable during background reloads without disrupting active queries.
- **Rationale**: DuckDB is optimised for single-process multi-read. Cross-process sharing is not supported.
- **Trade-off**: No horizontal scaling for write-heavy workloads without stateless redesign.

### ADR-002: Inline CTE filters (no per-request/per-filter DuckDB views)
- **Decision**: `POST /data` applies filters via `WITH __filtered AS (SELECT * FROM {table} WHERE {conditions})` CTEs, injected into each query at runtime.  No DuckDB `VIEW` objects are created per API request or per filter combination.
- **Rationale**: Creating a new view per filter combination would require schema mutations (write-lock, DuckDB DDL) and lifecycle management (drop-on-evict) on every request. CTE injection is purely read-side and avoids that shared mutable state.  Redis provides the caching layer instead.
- **Trade-off**: Longer query text per request (vs. a cached view name); acceptable because Redis short-circuits repeated identical queries.
- **Note**: This is distinct from `base_view` (ADR-011), which creates exactly **one** DuckDB view per dashboard at YAML load time (not per request/filter) — a one-time schema mutation, not a per-query one.

### ADR-003: Redis as cache, DuckDB as query engine
- **Decision**: First request for a filter combination hits DuckDB; result is GZIP-compressed and stored in Redis under `db:<dashboard_id>:<filter_hash>`. Subsequent identical requests are served from Redis bytes directly.  The table pagination endpoint (`POST /widgets/{widget_id}/page`) **always** bypasses Redis and queries DuckDB directly via `LIMIT`/`OFFSET`.
- **Rationale**: Separates the concerns of computation (DuckDB) and serving (Redis). Eliminates per-user DuckDB RAM overhead for popular filter combinations.  Pagination bypass avoids caching thousands of distinct offset-keyed pages, which would exhaust Redis memory.
- **Trade-off**: Requires Redis in production; gracefully degrades without it (always queries DuckDB).

### ADR-004: Dual-port health check
- **Decision**: Second uvicorn server on port 35400 serves only `/healthz`.
- **Rationale**: Matches spec (classes.md). Keeps liveness probes immune to main API overload.

### ADR-005: Pluggable connector registry
- **Decision**: `ConnectorRegistry` maps `source_type → class` at import time.
- **Rationale**: New connectors require zero changes to existing code (open/closed principle).

### ADR-006: Audit logging via middleware
- **Decision**: `AuditLogMiddleware` logs every request to `buchimaker.audit`.
- **Rationale**: Constitution mandates API-level audit trail. Middleware guarantees no endpoint accidentally bypasses logging.

### ADR-007: Widget-ID keyed filters
- **Decision**: Filters are identified by the YAML `id` of the filter widget, not by raw database column names.
- **Rationale**: The frontend should never know about underlying DB schema. Widget IDs are stable UI identifiers; DB column expressions (including JSON sub-field paths like `dpm->>'$.id'`) are resolved server-side via the `mappings` section.
- **Trade-off**: The dashboard YAML must declare `mappings` for any non-trivial column reference.

### ADR-008: Datasource → Redis key reverse index
- **Decision**: When a cache entry is written, its key is also added to a Redis Set `ds:<datasource_name>:keys`. On datasource reload, all member keys are bulk-deleted.
- **Rationale**: Without the index, stale cache entries would serve old data after a datasource reload. Bulk invalidation via Set membership is O(N) where N is the number of cached filter combinations for that datasource — typically small.

### ADR-009: Remote Redis caching instance
- **Decision**: Run Redis in a completely isolated container, while keeping DuckDB embedded in the API container. Connection details can be specified via environment variables or the API.
- **Rationale**: Keeping DuckDB embedded in the API container guarantees zero-network, zero-copy data retrieval for heavy analytical queries. Isolating Redis prevents massive DuckDB aggregations from triggering an Out-Of-Memory (OOM) kill that would wipe out user sessions and cached dashboard states.
- **Auth**: `docker-compose.yml`'s `redis` service and the backend's `REDIS_PASSWORD` env var both read the same `.env`-provided value, so setting `REDIS_PASSWORD` turns on Redis auth end-to-end with no other wiring required (empty/unset stays no-auth, matching Redis's own `requirepass ""` semantics — see the comments on the `redis` service in `docker-compose.yml`). This is unauthenticated by default because local development shouldn't require a secret to run `docker compose up` — see the README's Security section for why this must be set (and the service's host port removed) before any non-local deployment.

### ADR-010: Remote Syslog Export for Audit Logs
- **Decision**: Provide dynamic, API-managed Syslog settings (`host`, `port`, `tls_enabled`, `cert_path`, `key_path`, `ca_cert_path`) to export API audit logs to a remote Syslog server securely (TLS 1.2+ & mTLS).
- **Rationale**: Constitution mandates an API-level audit trail. Allowing remote syslog with mTLS ensures that audit logs can be shipped to a centralized security incident and event management (SIEM) system securely. Overriding `logging.handlers.SysLogHandler` to wrap the socket on connection avoids bringing in heavy dependencies.

### ADR-011: Required `base_view` — one flat dataset per dashboard, filters on top
- **Decision**: Every dashboard YAML must declare `base_view`, a `CREATE [OR REPLACE] VIEW <name> AS SELECT ...` statement, declared before `aggregates`/`totals`. `Dashboard.load_yaml()` executes it once against DuckDB and every total/aggregate query must select from the resulting view — the parser rejects (422) any query that doesn't reference it by name.
- **Rationale**: Before this, filtering only worked if a dashboard set `source_table` to a single already-flat table and every total/aggregate query happened to reference that exact table name by string match (the naive `query.replace(self.source_table, "__filtered")` in `get_data_for_filters`). Dashboards whose totals/aggregates spanned multiple joined sources with different aliases (e.g. `vuln_metrics.yaml`'s mix of a flat `vuln` table and a separately-joined fact/dim model) had **no reliable way to filter at all** — `source_table` could only ever point at one of them, so the CTE substitution silently no-opped for every query using the other alias. This mirrors why mainstream BI tools (Looker's "Explores", Power BI's dataset/semantic model, Tableau's data source layer) all converge on the same shape: one flattened, joined, filter-ready dataset per report, built once by whoever understands the joins, with every visualization and every filter control targeting that single shape afterward. The alternative — letting each chart/total author write its own ad-hoc joins — means every new query has to be independently kept "filter-compatible" (same column names, same grain, no missed join), which is exactly the class of bug this change closes. Centralizing the join logic once in `base_view` also means fixes/enrichments (e.g. adding a computed `risk_category` column) apply to every consumer instead of being copy-pasted into N queries.
- **Trade-off**: Dashboard authors can no longer write totals/aggregates against arbitrary independent sources within one dashboard — everything must flow through one view. This is a real constraint (see the `base_view` design note in `dashboards/template.yaml` and `dashboards/vuln_metrics.yaml` for a worked example of collapsing a multi-source dashboard into one view), but it's the same constraint every comparable BI tool imposes, and it's what makes ADR-002's CTE substitution actually correct instead of silently best-effort.
- **Lifecycle**: The view is dropped via `Dashboard.drop_base_view()` when a dashboard is deleted (`SystemManager.remove_dashboard()`) or when a hot-reload changes the view name (`SystemManager.load_dashboard()`), so renamed/removed dashboards don't leak DuckDB views. A same-name reload just re-executes `CREATE OR REPLACE VIEW`, which is idempotent.

### ADR-012: Parameterized connector filepaths (SQL injection fix)
- **Decision**: `CSVConnector.load()` and `JSONConnector.load()` bind `filepath` as a DuckDB prepared-statement parameter (`read_csv_auto(?, ...)` / `read_json_auto(?)`) instead of interpolating it into the SQL string. `ParquetConnector.load()` cannot use the same fix — DuckDB raises `Binder Error: Unexpected prepared parameter. This type of statement can't be prepared!` for any bound parameter inside a `CREATE VIEW` body, because a view's definition text must be static, re-evaluated on every read rather than bound once — so it instead escapes `filepath` as a SQL string literal (doubling embedded `'`, the standard SQL escape) before interpolating it. `BaseConnector.__init__` additionally validates `name` with the same `sanitize_name()` check used at the Pydantic/API layer, closing the gap where connectors are reconstructed straight from `settings/data_sources.yaml` on restart (`SystemManager.load_persisted_state()`), a path that never goes through request validation.
- **Rationale**: `filepath` was an unvalidated, unconstrained `str` field (unlike `name`/`title`, which already used `SafeName`/`SafeIdentifier`) f-string-interpolated directly into SQL. A path containing `'` could break out of the string literal and inject arbitrary SQL, executed with the backend's own DuckDB privileges (e.g. `DROP TABLE`, reading other tables). Parameter binding is the correct fix wherever DuckDB allows it; literal escaping is used only where the engine has no other option.
- **Trade-off**: Parquet connector paths get literal-escaping instead of true parameterization, so they're slightly more exposed to any future DuckDB string-parsing edge case than a bound parameter would be. Mitigated by only ever escaping `'` (the sole character with syntactic meaning inside a single-quoted SQL string) and by covered regression tests (`tests/test_connectors.py`) asserting apostrophe-containing paths load correctly and injection-payload paths (`'); DROP TABLE …; --`) neither error nor execute.
- **Verification**: `tests/test_connectors.py` — `test_filepath_with_apostrophe_loads_safely` / `test_filepath_injection_payload_does_not_execute_sql` for CSV, JSON, and Parquet; `TestConnectorNameValidation` for the identifier-validation defense-in-depth check.

### ADR-013: Sandbox file-connector `filepath` to `DATA_DIR`
- **Decision**: `CSVConnector`, `JSONConnector`, and `ParquetConnector` reject any `filepath` that doesn't resolve inside `settings.data_dir` (env var `DATA_DIR`, default `/app/data`). The check — `validate_path_within_base()` in `app/core/validator.py` — resolves both the candidate path and `data_dir` with `Path.resolve()` and requires the former to be `is_relative_to()` the latter. It's enforced twice: as a Pydantic `field_validator` on `DataSourceCSVCreate`/`DataSourceJSONCreate`/`DataSourceParquetCreate` (`app/models/schemas.py`) for a clean `422` on the API path, and again in each connector's `__init__` (`app/connectors/base.py`) so connectors rebuilt from `settings/data_sources.yaml` on restart — which bypasses Pydantic entirely, see `SystemManager.load_persisted_state()` — get the same guarantee. This mirrors the two-layer pattern ADR-012 already established for `name` validation.
- **Rationale**: Before this, `filepath` was a free-form string with no containment check — a caller (and under the default `ANONYMOUS_USER=ALLOW`, that's anyone who can reach the API) could register a data source pointing at any file the backend process can read: `/etc/passwd`, `/app/settings/data_sources.yaml` (which itself can contain other connectors' config, including a `BigQueryConnector.credentials_path`), or anything else mounted into the container. The fix turns "can register a data source" from "can read anything on this container's filesystem" into "can read anything already mounted under `DATA_DIR`" — a real reduction in blast radius, not a complete access-control story (see `docs/security.md`'s Data-source file paths section for what it doesn't cover).
- **Why `Path.resolve()` and not a string-prefix check**: A prefix check (`filepath.startswith(data_dir)`) is defeated trivially by `../` segments (`/app/data/../../etc/passwd` string-starts-with `/app/data` but escapes it) and doesn't follow symlinks (a symlink planted under `data_dir` pointing outside it would pass a prefix check but still read arbitrary files). `Path.resolve()` normalises `.`/`..` lexically *and* follows symlinks for path components that exist on disk, so both cases resolve to their true target before the containment check runs.
- **Glob patterns**: Parquet's `filepath` can be a glob (e.g. `agg_daily/**/*.parquet`). `Path.resolve()` doesn't error on nonexistent components (`**`, `*` never exist as literal directory entries), so glob patterns validate correctly as long as their literal, non-wildcard prefix stays inside `data_dir` — verified in `tests/test_validator.py::TestValidatePathWithinBase::test_accepts_glob_pattern_inside_base`.
- **Test infrastructure note**: Existing connector/API tests create fixture files under pytest's `tmp_path`, not the real `/app/data`. The autouse `sandbox_data_dir` fixture (`tests/conftest.py`) transparently points `settings.data_dir` at each test's own `tmp_path` (via `monkeypatch.setattr` on the `get_settings` name imported into `app.connectors.base` and `app.models.schemas`), so existing tests didn't need per-test changes — they already write inside `tmp_path`, which is now that test's sandboxed data root.
- **Verification**: `tests/test_validator.py::TestValidatePathWithinBase` (unit-level: nested paths, glob patterns, sibling paths, traversal, absolute paths elsewhere); `tests/test_connectors.py::TestConnectorFilepathSandboxing` (connector-level, all three file connectors plus a traversal case); `tests/test_api.py::TestDataSourcesAPI::test_register_{csv,json}_path_outside_data_dir_rejected` and `test_register_csv_path_traversal_rejected` (API-level, asserting `422`).

### ADR-014: Hash-pinned dependency lockfiles
- **Decision**: `backend/requirements.txt` and `backend/requirements-dev.txt` are no longer hand-edited — they're compiled by `pip-compile --generate-hashes` (via `backend/scripts/update-lockfiles.sh`) from `requirements.in`/`requirements-dev.in`, the actual human-edited, loosely-version-bounded source files. `backend/Dockerfile` installs with `pip install --require-hashes`, which refuses to proceed if any package — including a transitive dependency neither file names directly — resolves to a version or artifact without a matching recorded SHA-256 hash.
- **Rationale**: The previous `requirements.txt` pinned only lower bounds (`fastapi>=0.111.0`, etc.), so every image rebuild could silently resolve to a different (newer, and unreviewed) set of transitive dependencies — a supply-chain risk (a compromised or yanked-and-replaced PyPI upload could slip in unnoticed) as well as a reproducibility problem (two builds of "the same" `requirements.txt`, days apart, are not guaranteed to produce the same installed set). `--require-hashes` additionally blocks a whole class of dependency-confusion attacks: an attacker can't get a same-named-but-malicious package installed by manipulating index resolution, because the hash has to match a specific, already-known artifact.
- **Trade-off**: Every dependency change now requires a two-step edit (`.in` file, then regenerate the `.txt` lockfile) instead of one, and the lockfile diff is large and mostly-noise for a single direct-dependency bump (every transitive dependency's hash block is listed). This is the standard `pip-tools` cost of hash-pinning; it's accepted because the reproducibility/supply-chain guarantee is worth the extra step, and `update-lockfiles.sh` makes the regenerate step a single command.
- **What this doesn't cover**: Pinning stops *unexpected* version drift, not a *known* vulnerability in a version that was deliberately pinned — that still needs active scanning (`pip-audit`, Trivy/Grype), which isn't wired into CI yet. See `docs/security.md`'s "Dependency / supply-chain hygiene" section.

### ADR-015: OIDC login (BFF pattern) + role-based access control
- **Decision**: Replace the Entra-ID-specific, never-implemented auth stub with a generic OIDC Authorization Code + PKCE flow the backend performs itself (`app/core/oidc.py`), a Redis-backed server-side session (`app/core/session_store.py`) referenced by an httpOnly cookie (`app/api/auth.py`), and a four-role permission model (`Administrator`/`Data Admin`/`Viewer`/`Deny`, `app/core/roles.py`) resolved from OIDC claims via an admin-configured claim → role mapping list. The frontend never handles a token — only the opaque session cookie, sent automatically because `frontend/nginx.conf` proxies `/api/`/`/auth/` to the backend, keeping frontend and backend same-origin.
- **Rationale**: A pure frontend-driven (SPA + PKCE, Bearer-token) alternative was considered and rejected: it would have required touching every one of the ~50 existing `fetch()` call sites across `Settings.js`/`DashboardView.js`/`LogsView.js`/`app.js` to attach an `Authorization` header, and would expose access tokens to the page's JS context (XSS surface) for no compensating benefit in a server-rendered-shell app like this one. The BFF pattern keeps the client secret server-side, requires zero per-request frontend changes, and reuses Redis (already in the stack for dashboard-data caching) rather than introducing a new session backend.
- **Trade-off**: Session cookies assume same-origin frontend/backend (`SameSite=Lax`) — a deployment that puts them on separate origins needs `SameSite=None; Secure` and explicit `ALLOW_ORIGINS`, which the current `_cookie_kwargs()` in `app/api/auth.py` doesn't auto-detect. There's also no refresh-token rotation: a session simply expires after 8 hours (`session_store.SESSION_TTL_SECONDS`) and the user re-authenticates, which is simpler but means no long-lived background sessions.
- **Why no configurable default role for unmapped users**: The pre-existing frontend mockup (`Settings.js`'s original hardcoded `accessDefaultRole`) suggested one, but the actual requirement is fail-closed: any OIDC-authenticated caller matching none of the configured claim mappings is always `Deny`. This removes an entire class of misconfiguration (an admin leaving the default role more permissive than intended) at the cost of requiring at least one explicit mapping before any real user can do anything beyond viewing an empty app.
- **Verification**: `tests/test_auth.py` — claim/role resolution (`TestResolveRoleFromClaims`), the `anonymous_access` short-circuit and its unconditional override of any existing session (`TestAnonymousAccess`), the full role-gating matrix end-to-end via `TestClient` with synthetic Redis-backed sessions (`TestRoleGating`), and the Access/SSO settings persistence + secret-masking round-trip (`TestAccessAndSsoSettingsPersistence`).
- **Verification**: both lockfiles installed cleanly with `pip install --require-hashes` into a clean venv, the full test suite (238 tests) passed against that venv, and `docker compose build backend` + a live container boot (`/healthz`, dashboard/data-source loading) succeeded on the newly-pinned versions.
