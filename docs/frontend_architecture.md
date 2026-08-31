# Frontend Architecture

## Overview
The BuchiMaker frontend is a lightweight, no-build SPA using Vue 3 (Composition API, CDN).
Layout is driven by Gridstack.js (12-column static grid). No Node.js, no npm.

## Technology Stack
| Layer | Technology |
|---|---|
| Framework | Vue 3 — CDN, ES Modules, Composition API |
| Grid | Gridstack.js 9.2 — read-only, 12-column dashboard layout |
| Charts | Apache ECharts 5.5 — all data visualisations |
| Styling | Vanilla CSS — design tokens in `styles.css` |
| Serving | Nginx (Alpine) — static files + secure HTTP headers |

## Key Architectural Decisions

1. **No Build Step.** Native ES modules (`<script type="module">`). No webpack, no Vite, no npm dependencies. Minimises attack surface.
2. **Component-per-Page.** Each view is a standalone `.js` Vue component imported into `app.js`. The root app only owns routing state.
3. **Dashboard lifecycle ownership.** `DashboardView.js` owns its own GridStack and ECharts instances. It initialises them in `onMounted` and disposes them in `onBeforeUnmount`, so navigating between pages never leaks grid state.
4. **`:key`-based remount.** Switching between dashboards uses `:key="activeDashboardId"` on `<dashboard-view>`, forcing Vue to fully tear down and rebuild the component — guaranteeing clean re-initialisation.
5. **Read-Only Grid.** Gridstack is in `staticGrid: true` mode. Widget `x/y/w/h` come from the backend; the user cannot drag or resize.
6. **API-first with fallback.** On startup, `app.js` fetches the dashboard list from `GET /api/v1/dashboards`. If unavailable, it falls back to a single default dashboard so the UI always renders.
7. **Dynamic Widget Component Registry.** `DashboardView.js` no longer hardcodes widget rendering. Widget UI lives in standalone, swappable "widget packages" under `widgets/`, loaded at runtime via dynamic `import()` and looked up by YAML widget-type key through `<component :is="...">`. See [widget_bundles.md](widget_bundles.md) for the full contract and how to author a custom bundle.

## Directory Structure
```text
frontend/
├── index.html            # Entry point — loads CDN libs, mounts Vue
├── app.js                # Root app: routing state, dashboard list, component registration
├── styles.css            # Global CSS, design tokens, layout
├── config.js.template    # Runtime env injection (API_BASE_URL)
├── entrypoint.sh         # Docker entrypoint — writes config.js from template
├── components/
│   ├── DashboardView.js  # Dashboard page: GridStack, data fetch, widget registry/loader
│   ├── Settings.js       # System Settings page
│   └── LogsView.js       # System Logs page (placeholder)
├── widgets/
│   └── default/          # Built-in widget package — see widget_bundles.md
│       ├── index.js          # Component registry + theme.css/echarts-theme injection
│       ├── theme.css          # [data-package="default"]-scoped CSS
│       ├── echarts-theme.js   # registerTheme('default-light'|'default-dark', ...)
│       └── *Widget.js         # One Vue component per widget type
├── Dockerfile            # Nginx Alpine container
└── nginx.conf            # Secure Nginx config (CSP, security headers)
```

## Page Routing

Routing is handled via a `currentPage` ref in the root app (`app.js`). There is no client-side router library.

| `currentPage` | Component | Trigger |
|---|---|---|
| `'dashboard'` | `<dashboard-view>` | Sidebar dashboard link |
| `'settings'` | `<settings-page>` | Gear icon (footer) |
| `'logs'` | `<logs-view>` | File-lines icon (footer) |

```
index.html
└── #app (Vue root)
    ├── <aside class="sidebar">          ← always visible
    ├── <dashboard-view v-if="...">      ← mounts/unmounts on navigation
    ├── <settings-page v-else-if="...">
    └── <logs-view v-else-if="...">
```

## Sidebar Navigation
- The sidebar's dashboard list is populated from `GET /api/v1/dashboards`.
- Each item calls `navigateToDashboard(id)` which sets `activeDashboardId` and `currentPage = 'dashboard'`.
- The active item is highlighted via `:class="{ active: currentPage === 'dashboard' && activeDashboardId === db.id }"`.

## DashboardView Component (`components/DashboardView.js`)

**Props:** `dashboardId` (String), `apiBaseUrl` (String)

**Lifecycle:**
- `onMounted`: `loadWidgetPackage('default')` (dynamic `import()` of the widget registry) → `fetchLayout()` (`GET /dashboards/{id}`) → `nextTick` → `GridStack.init()` → `fetchData()` (`POST /dashboards/{id}/data`, resolves `widget_map` into per-widget data)
- `onBeforeUnmount`: `grid.destroy(false)`; each mounted widget component disposes its own ECharts instance/`ResizeObserver` in its own `onUnmounted` (widgets are self-contained — DashboardView no longer tracks chart instances or resize listeners itself)

**Widget rendering** is fully delegated to the registered package: `<component :is="getComponent(widget.type)" :id :config :data :theme @row-select @update:filter>`. DashboardView only owns the grid, the loading/error wrapper, the row-detail right panel, and the `filters` state that feeds back into `fetchData()`. See [widget_bundles.md](widget_bundles.md) for the full props/events contract and the list of widget types the built-in `default` package implements.

**Dashboard-only UI:**
- **Right Panel** — slides in when a table row is clicked; shows field details + raw JSON with copy buttons.
- **AI Chat Panel** — collapsible overlay; triggered by the "AI Chat" button in the topbar. Not rendered on Settings or Logs pages.

## Settings Component (`components/Settings.js`)
Manages system configuration via API. Tabs: Data Sources (active), Dashboards, Widgets, General, Access, SSO, AI (future).

### Data Sources Tab

**Left column** contains three sections:

1. **Auto-Refresh Settings** — radio group (Disabled / Basic / Cron) with conditional inputs, wired to `GET/PUT /api/v1/system/settings`.
2. **Register a New Data Source** — foldable accordion panels, one per connector type returned by `GET /api/v1/system/connector-types`. Forms are driven by the static `CONNECTOR_FIELDS` map (keyed by connector type string) so adding new types requires no template changes. Each accordion includes:
   - Type-specific fields with `required` markers and inline placeholder hints
   - Per-field client-side validation on submit (empty required field → inline error message)
   - A **Submit** button that shows a spinner while the POST is in flight and is disabled to prevent double submission
   - On success: form resets, accordion closes, table refreshes, and the message panel shows a confirmation with the registered `name` and `last_updated`
3. **Registered Data Sources Table** — lists all sources from `GET /api/v1/system/data-sources` with:
   - **Name** column using a monospace `<code>` pill
   - **Type** column with coloured icon chip (fa-file-csv / fa-file-code / fa-database)
   - **Status** badge (green "Loaded" / red "Never loaded") derived from `last_updated`
   - **Last Updated** column with formatted `toLocaleString()` timestamp
   - **Actions**: reload (triggers `POST /api/v1/system/refresh`) and delete (`DELETE /api/v1/system/data-sources/{name}`) icon buttons
   - Empty-state row when no sources are registered
   - Loading overlay while the fetch is in progress
   - **Refresh All** button in the section header to trigger a global reload

**Right column** contains the message panel showing API feedback (success, error, warning) with:
- Icon + title + dismissable close button
- A secondary `detail` line for server error messages and context strings
- Auto-dismissal after 6 s for non-error messages

**API endpoints used:**
- `GET/PUT /api/v1/system/settings` — auto-refresh config
- `GET /api/v1/system/connector-types` — populate accordion list; also seeds `forms` and `formErrors` reactive maps
- `GET /api/v1/system/data-sources` — table data; refreshed on mount, after registration, and after deletion
- `POST /api/v1/system/data-sources/{csv|json|bigquery}` — register a new source
- `DELETE /api/v1/system/data-sources/{name}` — remove a source
- `POST /api/v1/system/refresh` — trigger global reload of all sources

### Dashboards Tab

Provides a Gridstack-based interface for managing the system's active dashboard definitions.

**Left column** contains a single Gridstack item wrapping a management table and controls:
1. **Reload All Button** — loops through all loaded dashboards and triggers a reload for each.
2. **Dashboard Table** — lists dashboards retrieved from `GET /api/v1/dashboards` with:
   - **Id** column using a monospace `<code>` pill
   - **Title** and **Description**
   - **Status** column showing "Loaded" or a parsed error message from the backend API if a reload fails
   - **Actions**: reload (`POST /api/v1/dashboards/load`) specific dashboard icon

**Right column** contains a unified Messages Panel for API feedback specific to these dashboard operations, mirroring the UI of other tabs.

**API endpoints used:**
- `GET /api/v1/dashboards` — retrieves the list of currently active dashboards.
- `POST /api/v1/dashboards/load` — reloads specific dashboards based on their filename (`{id}.yaml`).

### General Tab

Manages the global backend settings affecting connection configurations.

Directly under the UI Theme selector and above the Gridstack cards sits a **Redis Cache Reset** button. It calls `POST /api/v1/system/cache/reset` (after a confirm() prompt) to flush every key in the Redis cache — useful for troubleshooting stale or corrupted cached dashboard data without touching data sources or Redis connection settings.

**Left column** contains two Gridstack items:
1. **Redis Configuration** — sets the Redis connection parameters (Host, Port, User, Password, TTL seconds, Row Limit, TLS toggle). Submitting these parameters pushes an update to `/api/v1/system/settings`, which prompts the backend to attempt a dynamic reconnection to the new Redis instance.
2. **Syslog Export** — provides inputs for Syslog connection details (Host, Port, Certs) and an Export toggle.

**Right column** contains a unified Messages Panel for API feedback specific to these sections.

**API endpoints used:**
- `GET/PUT /api/v1/system/settings` — retrieves and updates Redis and Syslog parameters in `general.yaml`.
- `POST /api/v1/system/cache/reset` — flushes the entire Redis cache.

## Security Posture
- **CSP** in `nginx.conf` restricts scripts/styles to trusted CDNs and local origin.
- No inline scripts — all logic in `.js` files.
- Nginx serves with `X-Frame-Options`, `X-Content-Type-Options` headers.
- **Known gap:** `nginx.conf`'s CSP sets `connect-src 'self'`, which blocks `fetch()` to a backend on a different origin (e.g. the container's `:8080` frontend talking to `:8000` backend, as in the default `docker-compose.yml`/`API_BASE_URL` setup). This predates the widget-registry refactor — `app.js`'s dashboard-list fetch already silently failed under it — but now blocks `DashboardView.js`'s real data fetches too when deployed via the bundled nginx image. Fixing it needs `connect-src` to be templated from `API_BASE_URL` (same mechanism as `config.js.template`/`entrypoint.sh`) rather than a hardcoded value in `nginx.conf`; not yet implemented.

## Nginx → Backend Proxying (`nginx.conf`)

`/api/` and `/auth/` are proxied to the `backend` container's Docker-network service name — this is what makes the SSO session cookie work with zero per-request frontend code changes (see ADR-015 in `docs/backend_architecture.md`).

- **Dynamic DNS resolution.** `proxy_pass` targets a variable (`set $backend_upstream backend:8000; proxy_pass http://$backend_upstream;`) with `resolver 127.0.0.11 valid=10s ipv6=off;` (Docker's embedded DNS) declared at the `server` level. A **static** `proxy_pass http://backend:8000/...;` only resolves the hostname once, at nginx worker startup — if that one resolution is ever wrong (observed in practice: an unrelated public IP, likely a transient Docker Desktop/WSL2 networking hiccup) or the backend container gets recreated with a new IP, every request fails with a 502 `Connection refused` until nginx itself is restarted. The variable form makes nginx re-resolve on every request (capped by the `resolver`'s `valid=10s`), so a bad resolution self-heals within seconds instead of requiring manual intervention.
- **Gotcha — don't add a path suffix when using a variable.** Per nginx's documented behavior, `proxy_pass` only does its usual "replace the matched location prefix" URI rewriting when the target is a *static* string. Once a variable is involved, that rewriting doesn't happen — instead, whatever literal path follows the variable (e.g. the `/api/` in `proxy_pass http://$backend_upstream/api/;`) becomes the **entire** upstream path, discarding the rest of the original request URI. Concretely, a request for `/api/v1/dashboards` would be forwarded as just `/api/` (backend sees a 404). The fix is to omit any URI suffix — `proxy_pass http://$backend_upstream;` — which passes the original request URI through unchanged, exactly matching the old static-hostname behavior (which was an identity prefix substitution anyway: `/api/` → `/api/`).

## Running Locally
```bash
# 1. Start backend
docker compose up -d backend redis

# 2. Build frontend image
cd frontend && docker build -t buchi-frontend .

# 3. Run
docker run -d -p 8080:80 \
  -e API_BASE_URL="http://localhost:8000/api/v1" \
  --name buchi-frontend-app buchi-frontend

# 4. Open http://localhost:8080

# Cleanup
docker rm -f buchi-frontend-app && docker compose down
```
