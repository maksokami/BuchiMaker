# Security Recommendations for production deployment

## Using dependency lockfile

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

## Container Image
- Rebuild with a vulnerability-free container image like Chainguard (`backend/Dockerfile` already notes this).
- The backend container already runs as a non-root user (`databoom`, see `backend/Dockerfile`) — keep this. 
- Run the frontend `nginx:alpine` container as non-root too (`user nginx;` is nginx's default worker behaviour, but the master process and file ownership should be checked/hardened for your target image).
- Prefer `read_only: true` root filesystems in `docker-compose.yml`/orchestrator manifests where possible, with explicit `tmpfs`/volume mounts only for paths that need to be writable (`./db`, `./settings`).
- Drop Linux capabilities you don't need (`cap_drop: [ALL]`, add back only what's required) and set `no-new-privileges:true`.


##  Network
- Backend must never be exposed to the internet directly.
- Neither frontend nor backend has TLS enabled by design — they must sit behind a reverse proxy that terminates TLS.
- Recommended architecture: `Internet → WAF (OWASP rule set, bot protection) + reverse proxy (TLS) → [isolated network] Frontend → [NSG/ACL] → Backend`.
- **Redis**: `docker-compose.yml` ships with the host port published (`${REDIS_HOST_PORT:-6379}:6379`) and no password — safe for a single local machine, not for anything else. Auth is wired end-to-end: set `REDIS_PASSWORD` in `.env` and both the `redis` service's `--requirepass` and the backend's `REDIS_PASSWORD` env var (consumed by `app/core/redis_client.py`) pick it up automatically — see the comments on the `redis` service in `docker-compose.yml`. **You still have to do two things yourself for production**: (1) actually set `REDIS_PASSWORD` in `.env` (it's empty by default, so auth is off unless you opt in), and (2) remove the `redis` service's `ports:` block from `docker-compose.yml` — the backend reaches Redis over the internal `buchi-net` network and never needs it published to the host; an unauthenticated *or* host-exposed Redis instance lets anyone who can reach that port read/flush the entire dashboard cache.

**Authentication & Authorization**
- Fixed: OIDC login (Authorization Code + PKCE) is implemented end-to-end (`app/core/oidc.py`, `app/core/session_store.py`, `app/api/auth.py`). The backend performs the exchange itself and issues an httpOnly, `SameSite=Lax` session cookie (`SESSION_COOKIE_NAME` in `app/core/auth.py`) — the browser never handles a token. Sessions are stored server-side in Redis with an 8-hour TTL (`session_store.SESSION_TTL_SECONDS`).
- Controlled at runtime via Settings > Access ("Allow Anonymous Access", persisted as `general_settings.anonymous_access`) rather than the old env-var-only `ANONYMOUS_USER` gate — that var now only seeds the very first run, before `settings/general.yaml` exists. Default is `True` (anonymous Administrator) so a fresh install stays usable out of the box; disable it once SSO is configured and tested via Settings > SSO.
- A full role/permission model now exists (`app/core/roles.py`): `Administrator` / `Data Admin` / `Viewer` / `Deny`, resolved from OIDC claims via the Settings > Access claim → role mapping list (`general_settings.role_mappings`). There is **no configurable default role for unmapped users** — any authenticated caller matching none of the mappings is always `Deny`. Every mutating/admin-surface endpoint (data-source and dashboard CRUD, widget-set management, raw SQL, audit logs, and the Access/SSO settings endpoints themselves) is gated with `app.core.roles.require_role(...)` on top of the base `require_authentication` check — see the router-level `dependencies=` on `app/api/system.py`, `dashboards.py`, and `widgets.py` for the exact matrix.
- The default docker-compose deployment routes all frontend → backend traffic through nginx's `/api/` and `/auth/` reverse-proxy locations (`frontend/nginx.conf`), keeping frontend and backend same-origin so the session cookie works without any cross-origin CORS/cookie configuration. If you deploy the backend on a separate origin instead, you'll need `SameSite=None; Secure` cookies and an explicit `ALLOW_ORIGINS` — the current cookie flags (`app/api/auth.py`'s `_cookie_kwargs`) assume same-origin.

**Raw SQL execution endpoint (`POST /api/v1/system/sql`)**
- Defaults to disabled (`sql_api_enabled: False` in `app/core/general_settings.py`) and, when enabled, is reasonably hardened already: single-statement `SELECT`-only (parsed via DuckDB's own `extract_statements`, not a text prefix check) and file-reading table functions (`read_csv`, `read_parquet`, `glob`, etc.) are blocklisted.
- It is still effectively "read access to every table currently loaded into DuckDB" with no per-table/per-row scoping — gated to `Data Admin`/`Administrator` roles (`app/core/roles.py`), but any caller with either role can read any table regardless of which data source they manage. Leave `sql_api_enabled` off in production unless you specifically need it, and only toggle it on for deployments where that's an acceptable trust boundary.

**Data-source file paths (CSV/JSON/Parquet connectors)**
- Fixed: `filepath` on `DataSourceCSVCreate`/`DataSourceJSONCreate`/`DataSourceParquetCreate` (`app/models/schemas.py`) is now validated to resolve inside `settings.data_dir` (default `/app/data`) via `validate_path_within_base()` (`app/core/validator.py`) — both at the Pydantic/API layer (clean `422`) and again inside `CSVConnector`/`JSONConnector`/`ParquetConnector.__init__` (`app/connectors/base.py`), so connectors reconstructed from `settings/data_sources.yaml` on restart are covered too, not just requests through the API. See ADR-013 in `docs/backend_architecture.md`. Combined with the ADR-012 SQL-injection fix, a registered file path can now neither break out of the SQL it's interpolated/bound into nor point outside the mounted data directory (`.`/`..` traversal and symlink escapes are both rejected — the check resolves the real path, not just string-prefix-matches it).
- This still means the entire mounted `DATA_DIR` is readable by anyone who can register a data source (see the Authentication gap above) — the fix narrows "any file the container can read" down to "any file under the data directory you chose to mount," it doesn't add per-file/per-user access control within that directory. Keep `DATA_DIR`/`./data` scoped to files dashboards actually need, and don't mount anything sensitive under it.

**Secrets & credentials**
- `BigQueryConnector.credentials_path` (`app/connectors/base.py`) points at a service-account JSON key file on disk. Mount it read-only via your orchestrator's secret mechanism (Docker secrets, Kubernetes `Secret` volume) rather than baking it into an image layer or a broadly-mounted directory, and rotate it periodically. Where the deployment target is GCP, prefer Workload Identity Federation over a long-lived key file entirely.
- Confirm no `.env` file (with real secrets) is ever committed — `.env.example` is safe as a template, but double-check `.gitignore` covers `.env` before every release.

**Dependency / supply-chain hygiene**
- Fixed: `backend/requirements.txt` and `backend/requirements-dev.txt` are now compiled, hash-pinned lockfiles (`pip-compile --generate-hashes`, via `backend/scripts/update-lockfiles.sh`), generated from the loosely-bounded `requirements.in`/`requirements-dev.in`. `backend/Dockerfile` installs with `pip install --require-hashes`, which fails the build if any package — including a transitive dependency — doesn't match a recorded hash, so a rebuild can no longer silently pull in a newer (and potentially compromised or breaking) version. See the README's "Dependency lockfile" section for the update workflow.
- Still worth doing: run `pip-audit -r backend/requirements.txt` (or equivalent SCA) and an image scanner (Trivy/Grype) in CI against the built image before every release. Pinning stops *unexpected* version drift, it doesn't stop a pinned version from having a known CVE — that still needs active scanning.

**Audit logging**
- Remote Syslog export with mTLS is already supported (ADR-010, `app/core/general_settings.py` syslog config) but is opt-in. Enable it in production so audit history survives a compromised/wiped host, and ship logs to a SIEM independent of the application container.
- Fixed: `GET /api/v1/system/audit-logs` and `.../export` are now `Administrator`-only (`app/core/roles.py`'s `ADMIN_ONLY`), not just anonymous-vs-authenticated. Each record's `user_email` is also now populated from the resolved session principal (`app/core/logging.py`'s `AuditLogMiddleware`), where previously it was always null.

**Frontend CSP**
- `frontend/nginx.conf`'s CSP includes `script-src 'unsafe-eval'` and loads Vue/Gridstack/Chart.js/FontAwesome/fonts from `unpkg.com`, `cdnjs.cloudflare.com`, and `cdn.jsdelivr.net`. This trusts three third-party CDNs to not serve malicious JS/CSS, and `unsafe-eval` reduces the CSP's XSS mitigation value. For a production posture, vendor those libraries into the served bundle (drop the CDN origins from the CSP) and remove `unsafe-eval` once nothing in the widget stack requires it.
- `connect-src 'self'` means the frontend can only call an API on the same origin — this is fine (and intentionally restrictive) as long as your reverse proxy puts the API under the same origin as the frontend; if `API_BASE_URL` ever points cross-origin in production, this CSP will silently block it, so keep that consistency in mind when configuring the reverse proxy.


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
