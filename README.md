# BuchiMaker


BuchiMaker is an API-first web application for building visual data dashboards. It reads your data (CSV, JSON, or partitioned Parquet) into an embedded [DuckDB](https://duckdb.org/) engine, lets you define dashboards as YAML — one flattened "base view" per dashboard, with SQL-defined totals, aggregates, and filters on top — and serves them through a REST API to a small static-HTML/Vue frontend.
It is ideal for prototyping or hosting internal dashboards with big volume of data.

It is not a replacement for a full BI platform (Power BI, Looker, Tableau). The model is deliberately narrow: **one ready-to-query data source per dashboard**, transformed/joined once into a `base_view`, with every widget and filter operating on that one shape. See [`docs/dashboard_parser.md`](docs/dashboard_parser.md) for the full dashboard YAML spec.

## Architecture at a glance

- **Backend** — FastAPI + DuckDB (Python 3.11, `backend/`). One DuckDB connection per process, file-backed rather than `:memory:` so the OS can page under memory pressure. See [`docs/backend_architecture.md`](docs/backend_architecture.md) for the full ADR list; a few decisions worth knowing up front:
  - **Active/standby DB swap**: data reloads happen into a second "standby" DuckDB file, which is atomically promoted once loaded — so a refresh never serves a half-loaded table. The main data-engine DB is rebuilt from your data sources on *every container startup*; it's a cache, not a source of truth.
  - **Audit log is a separate DuckDB file**, isolated from that reload cycle, so audit history survives even though the main engine gets rebuilt from scratch on restart.
  - **Redis is a cache, not a dependency of correctness**: the first request for a given dashboard+filter combination hits DuckDB and caches the (gzip-compressed) result in Redis; the app degrades gracefully (always queries DuckDB) if Redis is unavailable.
  - **DuckDB is single-process, multi-read** — it does not support concurrent cross-process writers to the same file. This shapes how the app can and can't be scaled; see [Scaling](#scaling) below.
- **Frontend** — static HTML/Vue served by nginx (`frontend/`), no build step or Node toolchain required at runtime. It calls the backend over `API_BASE_URL`, injected at container start via `envsubst` (`frontend/entrypoint.sh`) — no rebuild needed to point it at a different backend.
- **Widgets** — pluggable per-type Vue components loaded from a "widget set" folder (`frontend/widgets/default/` ships as the bundled default), so custom widget themes/sets can be dropped in without touching core frontend code.
- **Dashboards as data, not code** — dashboards are YAML files loaded via API (`POST /api/v1/dashboards/load`), parsed and validated against the spec in `docs/dashboard_parser.md`, not hardcoded into the app. Dashboard YAML files are a mix of the markup (layout description) and SQLs for widgets.

## Onboarding: running it for the first time

### 1. Seed example dashboards, data, and settings

The repo ships a `templates/` folder with a minimal working example — a small sample dataset, a dashboard that runs against it, and default settings files. Nothing under `dashboards/`, `data/`, or `settings/` is committed to git (see `.gitignore`) or shipped pre-populated, so run the seed script once after cloning:

```bash
./scripts/init-onboarding.sh
```

This copies `templates/dashboards/*` → `dashboards/`, `templates/data/*` → `data/`, and `templates/settings/*` → `settings/`. It never overwrites a file that's already there, so it's safe to re-run at any point — including after you've started registering your own data sources and dashboards.

`dashboards/template.yaml` is a separate reference file also seeded by the same script — it demonstrates every supported widget type and validation rule, but it isn't runnable as-is (its `base_view` joins against data sources this project doesn't ship). Use it as a syntax reference, not a demo.

**Every CSV/JSON/Parquet data source must live under `./data`.** When you register one (`POST /api/v1/system/data-sources/{csv|json|parquet}`), its `filepath` is required to resolve inside the container's `DATA_DIR` (`/app/data`, i.e. whatever host folder you bind-mount to `./data` in `docker-compose.yml`) — the backend rejects any path outside it, including `../` traversal attempts, with a `422`. This isn't configurable per-request: put the file somewhere under `./data` (subfolders are fine) first, then reference it as `/app/data/<...>` in the request. See ADR-013 in [`docs/backend_architecture.md`](docs/backend_architecture.md) for why, and [Security](#security-recommendations-for-public-cloud-deployment) for what this does and doesn't protect against.

### 2. Configure environment (optional)

```bash
cp .env.example .env
```

Edit `.env` if you need to change CORS origins, the frontend's backend URL, or any of the host-side ports (see [Port overrides](#port-overrides) below). The defaults work out of the box for a single machine running everything locally.

### 3. Build and run everything

```bash
docker compose up --build
```

This builds the backend (`backend/Dockerfile`) and frontend (`frontend/Dockerfile`) images, starts Redis, and brings all three services up on the `buchi-net` bridge network. First boot with the seeded templates should give you:

- API: http://localhost:8000/api/v1
- Health check: http://localhost:35400/healthz *(served on its own dedicated port — see `HEALTH_HOST_PORT`)*
- Frontend: http://localhost:3000
- Demo dashboard: visible in the frontend once loaded, or directly at `GET /api/v1/dashboards/demo_dashboard`

Redis (port 6379) and the backend's API/health ports are exposed to the host for local development convenience only — see [Security](#security-recommendations-for-public-cloud-deployment) before deploying anywhere reachable from the internet.

### 4. Building and running backend/frontend separately

Useful for local development without the full compose stack (e.g. iterating on one service, or the pattern used in `docs/debugging.md`):

**Backend**, run directly with Python (fastest iteration loop — no image rebuild per change):
```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements.txt
DEBUG=true DASHBOARDS_DIR=../dashboards DATA_DIR=../data python main.py
```
Redis is optional here — the app logs a warning and falls back to querying DuckDB directly if it can't connect.

`requirements.txt` is a compiled, hash-pinned lockfile (`--require-hashes` fails the install if anything, including a transitive dependency, doesn't match a recorded hash) — see [Dependency lockfile](#dependency-lockfile) below if you need to add or bump a package.

**Frontend**, built and run as a standalone container (mirrors `frontend/rerun.sh`):
```bash
cd frontend
docker build -t buchi-frontend .
docker run -d -p 3000:80 \
  -e API_BASE_URL="http://localhost:8000/api/v1" \
  --name buchi-frontend-app buchi-frontend
```

Rebuilding after a change to frontend static files (`frontend/*.js`, `frontend/widgets/**`) requires re-running `docker build` — nginx serves baked-in files, there's no dev server/hot-reload. See `docs/adding-new-widget-type.md` for the full "what needs a rebuild vs. what's just a mount" table.

### Port overrides

`docker-compose.yml` reads host-side ports from environment variables (default shown):

| Variable | Default | Maps to |
|---|---|---|
| `API_HOST_PORT` | `8000` | Backend main API |
| `HEALTH_HOST_PORT` | `35400` | Backend `/healthz` (separate port so liveness probes stay immune to API-level overload — see ADR-004 in `docs/backend_architecture.md`) |
| `REDIS_HOST_PORT` | `6379` | Redis (only needs host exposure for local debugging with a Redis client — not required in production) |
| `FRONTEND_HOST_PORT` | `3000` | Frontend nginx (container listens on 80 internally) |

Set these in `.env` (copy `.env.example`) rather than editing `docker-compose.yml`. `API_BASE_URL`/`AUTH_BASE_URL` default to the relative paths `/api/v1`/`/auth` — nginx proxies these to the backend container internally (`frontend/nginx.conf`), so they don't need to track `API_HOST_PORT` at all; only override them to an absolute URL if you're bypassing that proxy and pointing the frontend at a backend on a different origin (see [Security](#security-recommendations-for-public-cloud-deployment) below — that also requires further cookie/CORS changes).

(`ALLOW_ORIGINS` defaults to `*` and is effectively unused in the default same-origin topology — see [Security](#security-recommendations-for-public-cloud-deployment) below if you go cross-origin.)

### Dependency lockfile

`backend/requirements.txt` and `backend/requirements-dev.txt` are **compiled, hash-pinned lockfiles**, not hand-edited — every package (including transitive dependencies) is pinned to an exact version with SHA-256 hashes for every distributable file, and `backend/Dockerfile` installs with `pip install --require-hashes`, which fails the build outright if anything resolves to an unlisted/unverified artifact. This closes the gap where an unpinned `fastapi>=0.111.0`-style requirement could silently pull in a newer (and potentially compromised, or just breaking) version on every rebuild.

The human-edited source files are `backend/requirements.in` (runtime deps, loosely version-bounded) and `backend/requirements-dev.in` (test-only deps). To add or bump a dependency:

1. Edit `requirements.in` (or `requirements-dev.in`).
2. Regenerate the lockfiles:
   ```bash
   cd backend
   ./scripts/update-lockfiles.sh
   ```
3. Review the diff — a lockfile diff for an unrelated dependency bump you didn't ask for is worth investigating before committing — then commit both the `.in` file and the regenerated `.txt` lockfile together.

Run `pip-audit -r requirements.txt` (or an image scanner like Trivy/Grype against the built image) periodically to catch known vulnerabilities in pinned versions — pinning stops *unexpected* changes, it doesn't stop a pinned version from having a known CVE.

## Security recommendations for public cloud deployment

The docker-compose file as shipped is a **local development topology**: every port is exposed to the host, there's no TLS, and Redis has no auth configured by default (`general_settings.redis_password`, or the `REDIS_PASSWORD` env var below, supports one). None of that is safe to expose directly to the internet. Minimum baseline for any public deployment:

- **OIDC login and role-based access control are implemented** (`backend/app/core/oidc.py`, `session_store.py`, `roles.py`, `app/api/auth.py`) — configure a provider (Keycloak, Entra ID, Okta, or any standards-compliant OIDC issuer) in Settings > SSO, define at least one OIDC claim → role mapping in Settings > Access, then turn off "Allow Anonymous Access" there. `docker-compose.yml` still seeds `ANONYMOUS_USER=ALLOW` so a fresh install is immediately usable — **do not expose this backend publicly with anonymous access still on**; it grants every request the Administrator role with no identity check at all. See [Authentication & Authorization](docs/backend_architecture.md#authentication--authorization) for the full flow and the `Administrator`/`Data Admin`/`Viewer`/`Deny` role matrix.
- **Restrict `ALLOW_ORIGINS` to your real frontend origin(s) if the frontend and backend are ever on different origins.** The default docker-compose topology never needs this: `frontend/nginx.conf` proxies `/api/`/`/auth/` straight to the backend container, so the browser only ever talks to one origin and the SSO session cookie (`SameSite=Lax`) just works. If you deploy the backend on a separate origin instead, `CORSMiddleware` runs with `allow_credentials=True` (`backend/app/app.py`) so the cookie can still be sent cross-origin — but you'll then also need `SameSite=None; Secure` on the cookie (`app/api/auth.py`'s `_cookie_kwargs`, currently same-origin-only) and a non-wildcard `ALLOW_ORIGINS`:
  ```
  ALLOW_ORIGINS=https://your-public-frontend-domain.com
  ```
  Comma-separate multiple origins if needed (e.g. a staging and a production frontend).
- **Only the frontend should have a public-facing address.** The backend API (8000), health port (35400), and Redis (6379) must be reachable only from inside your private network — not bound to a public load balancer or public IP at all.
- **Set `REDIS_PASSWORD` in `.env` and remove Redis's `ports:` mapping before any deployment beyond one local machine.** As shipped, `docker-compose.yml`'s `redis` service takes no password and publishes `6379` to the host — anyone who can reach that port can read or flush the entire dashboard cache, **and now also read/forge SSO session records** (`app/core/session_store.py` uses the same Redis instance). Setting `REDIS_PASSWORD` in `.env` is enough to turn auth on: the compose file wires the same value into both the `redis` service's `--requirepass` and the backend's `REDIS_PASSWORD` env var automatically. Deleting the `redis` service's `ports:` block (in `docker-compose.yml`, or an override file) is a separate, manual step — the backend always reaches Redis over the internal `buchi-net` network and never needs it published to the host, so there's no functional reason to keep that mapping outside local debugging.
- **Data sources are sandboxed to `DATA_DIR`, not the whole container filesystem — but `DATA_DIR` itself isn't per-file access-controlled.** Registering a CSV/JSON/Parquet source can only read files that resolve inside `DATA_DIR` (see [Onboarding](#onboarding-running-it-for-the-first-time) and ADR-013 in `docs/backend_architecture.md`) — but any caller with the `Data Admin` role or above can register a source pointing at *any* file already under that directory. Don't rely on this as per-file access control: keep `./data` scoped to files dashboards actually need.
- **Put a WAF in front of the public edge** (the frontend, and the backend if you ever need to expose an API endpoint directly to external clients) — not as a substitute for input validation, but to catch the class of attacks (bot traffic, credential stuffing, common exploit signatures) that shouldn't reach the app at all.
- **Terminate TLS in front of the app**, not inside it — neither nginx (`frontend/nginx.conf`) nor uvicorn is configured for TLS termination here; that's expected to be handled by the load balancer/ingress layer. Note the SSO session cookie is only marked `Secure` when the *backend* sees an `https` request scheme (`app/api/auth.py`), so make sure your TLS-terminating proxy forwards `X-Forwarded-Proto` and that uvicorn/Starlette is configured to trust it, or the cookie will be issued without `Secure` behind a TLS-terminating LB.
- **Secrets belong in your cloud's secret manager, not `.env` or `settings/general.yaml`** — Redis password, the OIDC client secret (`general_settings.sso.client_secret`), syslog TLS certs (`general_settings.syslog`), and anything else sensitive should be injected at deploy time, not committed or baked into an image. The `REDIS_PASSWORD`/`OIDC_*` env var wiring above is a plaintext-`.env` baseline suitable for a private-network deployment; for a real cloud deployment, source it from Key Vault/Secret Manager instead (see the Azure/GCP patterns below).

### Azure — zero-trust reference pattern

- Deploy the backend on **Azure Container Apps or AKS with no public ingress** — internal-only ingress (Container Apps: `ingress.external = false`) or a private AKS cluster with no public IP on the service.
- Put **Azure Front Door (or Application Gateway) with a WAF policy** in front of the frontend only. Front Door's WAF handles the public edge; the frontend then calls the backend over the VNet.
- **VNet-integrate** the Container Apps environment (or AKS cluster) and reach Redis via **Azure Cache for Redis with a Private Endpoint** — no public network access on the cache at all. Restrict access with NSGs scoped to the backend's subnet.
- Store the Redis password, syslog client cert/key, and any connector credentials in **Azure Key Vault**, referenced via managed identity — not as Container Apps secrets typed into the portal or left in `settings/general.yaml`.
- Use **Private DNS zones** so internal service-to-service calls (frontend → backend, backend → Redis) never traverse the public internet even by DNS resolution accident.
- If dashboards read data from Azure sources (Blob Storage, SQL, etc. via a custom connector — see `docs/backend_architecture.md`'s "Adding a new connector"), use **Private Endpoints** for those too, not service-endpoint/firewall-rule access.

### GCP — zero-trust reference pattern

- Deploy the backend as a **Cloud Run service with ingress set to "internal and Cloud Load Balancing only"** (or a **private GKE cluster** with no external node IPs) — never `ingress: all`.
- Put **Cloud Armor (WAF) + an external HTTPS Load Balancer** in front of the frontend only (Cloud Run frontend service, or a GKE Ingress). Cloud Armor policies handle rate limiting, geo-restriction, and OWASP rule sets before traffic reaches nginx.
- Run Redis as **Memorystore for Redis with a private IP**, reachable only via **Serverless VPC Access** (from Cloud Run) or in-VPC (from GKE) — no public IP, ever.
- Use **IAM-based service-to-service authentication** for the frontend→backend call if both run on Cloud Run: grant the frontend's service account the `roles/run.invoker` role on the backend service and require ID tokens, rather than relying on network reachability alone as the trust boundary.
- Store secrets (Redis auth string, connector credentials, syslog TLS material) in **Secret Manager**, mounted as environment variables or volumes at deploy time via each service's IAM-scoped access — not in `.env` or checked-in YAML.
- Consider **VPC Service Controls** around the project if dashboards connect to sensitive data sources (BigQuery, Cloud SQL, GCS) via custom connectors, to prevent data exfiltration even if a credential leaks.

## Scaling

**Redis scales horizontally without any extra design work** — it's already the shared, stateless caching layer (ADR-003 in `docs/backend_architecture.md`). Any number of backend replicas can point at the same Redis instance/cluster safely.

**The backend, as shipped, does not.** This is a real, documented architectural constraint (ADR-001), not an oversight: DuckDB is a single-process embedded engine — it does not support multiple processes writing to the same database file concurrently. The current `docker-compose.yml` bind-mounts one shared `./db/` directory into the backend container (`./db:/app/db:rw`), which holds both the active/standby query-engine files *and* the audit log. Naively running multiple backend replicas behind a load balancer with that same bind mount would have every replica opening and writing the same `duck.db`/`duck_standby.db`/`audit.db` files concurrently — unsupported by DuckDB and likely to corrupt state or crash.

If you need more backend throughput than one instance provides, in order of effort:

1. **Vertical scaling first** — give the backend container more CPU/memory (`DUCKDB_MEMORY_LIMIT` env var controls DuckDB's own cap) before reaching for replicas. Because the query engine rebuilds from your data sources on every startup and Redis absorbs repeat-query load, a single well-resourced instance goes a long way.
2. **Multiple replicas, each with its own private `db/` volume** (not the shared bind mount) — this works for the *query engine* specifically, because it's rebuilt from the same read-only data sources (`./dashboards`, `./data`) on every startup, so each replica is independently reconstructible rather than holding unique state. A load balancer (cloud LB, or nginx/Traefik) can then round-robin across replicas for read traffic. Two things this does **not** solve on its own:
   - **Audit log fragmentation** — each replica would maintain its own local `audit.db`, splitting the audit trail across instances instead of one canonical record. Route audit records to a centralized external target instead: the app already supports **syslog forwarding** for audit records (`general_settings.syslog` — TLS-capable), which is the intended path to a unified audit trail across replicas, rather than relying on the local per-replica DuckDB file.
   - **Config-mutation consistency** — registering a new data source or dashboard via the admin API (`POST /system/data-sources/*`, `POST /dashboards/load`) writes to that replica's local `settings/*.yaml` and in-memory DuckDB only; it does not propagate to other replicas. Treat `settings/*.yaml` as deploy-time configuration pushed identically to every replica (e.g., baked into the image or synced by your deploy pipeline) rather than something mutated live through the API in a multi-replica setup — or accept that admin changes need to be applied once and redeployed everywhere.
3. **A genuinely stateless, horizontally-scalable write path** is explicitly out of scope for the current architecture (ADR-001 calls this out directly as future work) — it would mean replacing the embedded-DuckDB-per-process model with a shared/networked query engine, which is a real redesign, not a configuration change.

In short: a load balancer in front of multiple *read-scaled* backend replicas is workable today with the private-volume-per-replica + syslog-audit adjustments above; a load balancer alone, pointed at the default `docker-compose.yml` topology, is not.
