# Adding a new widget type — checklist for AI agents

Written after adding `horizontal_bar_chart`, where the code changes were
correct on the first pass but the widget still showed "no data" for several
turns. The wasted time was 100% deployment/caching issues, not logic bugs.
Read the "Deploy the change" section before declaring the task done — code
edits alone are not enough in this repo.

Stack reminder: Vue 3 + Gridstack.js + eCharts, no build step, no npm
packages, no React, no Vue2. Widgets are plain `.js` modules loaded via
dynamic `import()`, not SFCs.

## 1. Frontend — one new file + one registry line

- **New file** `frontend/widgets/default/<Type>Widget.js` — copy the closest
  existing widget of the same shape (e.g. `BarChartWidget.js` for another
  chart type) rather than writing from scratch. Preserve the boilerplate
  exactly: `ResizeObserver` lifecycle, `watch(..., { flush: 'post' })` on
  both `data` and `theme` (Vue's default `'pre'` flush fires before the
  `v-if` swaps in the chart `<div>`, so `chartRef.value` would still be
  `null`), and the `No data` / chart-container `v-if` in the template.
- **Data Contract Validation**: Widgets must proactively validate the shape of `props.data` before passing it to ECharts. If the data is missing required fields (e.g. `value`), compute an `isDataInvalid` property and render a `<div v-else-if="isDataInvalid" class="widget-unknown">Data format invalid</div>` placeholder to prevent ECharts from crashing.
- **Chart Rendering Try/Catch**: Wrap `chart.setOption(...)` in a `try/catch` block. If ECharts throws a synchronous layout calculation error, catch it internally, set a `hasError` ref to true, dispose the chart instance, and render a `<div v-else-if="hasError" class="widget-unknown">Chart rendering failed</div>` placeholder so the error does not escape into Vue's reactivity cycle.
- **`frontend/widgets/default/index.js`** — add the import and one line to
  the `components: { ... }` map: `<yaml_type_key>: <Type>Widget`. This map
  is the *only* place a YAML `type:` string resolves to a Vue component.
  `DashboardView.js` dispatches generically (`getComponent(widget.type)`)
  and needs no changes.
- No CSS changes needed unless the widget needs type-specific styling
  (`frontend/widgets/default/theme.css` keys rules on
  `[data-widget-type="..."]`).

## 2. Backend — classify the type in `dashboard.py`

Almost everything server-side is driven by three module-level sets near the
top of `backend/app/models/dashboard.py` (~line 52-65):

```python
_AGGREGATE_WIDGETS = {"bar_chart", "area_chart", ...}   # consumes aggregates or a raw table
_TOTAL_WIDGETS     = {"tile", "radial_gauge_full", ...}  # consumes a single total value
_FILTER_WIDGETS    = {"button_filter", "input_filter", ...}  # excluded from widget_map entirely
```

Add the new type string to whichever set matches its data shape. That one
line is enough to make `load_yaml()`'s validation (rejects total/aggregate
mismatches), `_build_widget_map()`, and `_raw_sources_needed()` all treat it
correctly — there is no separate per-type schema to update. Grep first to
make sure you're not missing a second copy of a similar list:

```
grep -rn "_AGGREGATE_WIDGETS\|_TOTAL_WIDGETS\|_FILTER_WIDGETS" backend/app/
```

If the new type needs *stricter* field validation than "reject the wrong
data-source kind" (most existing chart types don't — see the
`elif widget_type in _AGGREGATE_WIDGETS:` branch around line 489, it's a
generic check), add a new `elif widget_type == "your_type":` branch in
`load_yaml()` following the pattern used for filter widgets just below it.

## 3. Tests — `backend/tests/test_dashboard.py`

Two places, both keyed off the `TestBuildWidgetMap` class's shared YAML
fixture (`_make_dash`, ~line 761):

1. Add a `<your_type>:` block to the fixture's `layout:` referencing an
   existing aggregate (e.g. `by_cat`), then a
   `test_<your_type>_mapped_to_aggregate` asserting
   `wm["<id>"]["data_type"] == "aggregate"`.
2. Add a `test_load_layout_<your_type>_validation` test (see
   `test_load_layout_sankey_validation_data` for the pattern) asserting a
   dashboard is rejected with `"cannot use total"` when the widget's `data`
   field points at a `totals:` entry instead of an `aggregates:` entry.

Run `python -m pytest tests/test_dashboard.py -q` from `backend/` — full
suite is fast (~1s, no docker needed, it's plain pytest against an in-memory
DuckDB).

## 4. Docs to update

Grep for the sibling type you copied from — these all keep a widget-type
enumeration that goes stale otherwise:

- `docs/widget_bundles.md` — the data-shape table, and the "known gaps /
  legacy types not ported" list near the bottom.
- `docs/dashboard_parser.md` — "Placeholders for Future Widgets" list.
- `spec/dashboard_validation.md` — the `_AGGREGATE_WIDGETS`/`_TOTAL_WIDGETS`
  "Includes:" bullet.
- `backend/app/api/dashboards.py` — one or two endpoint docstrings list
  example widget types for Swagger (search `bar_chart` in that file).
- `dashboards/template.yaml` — if the type already had a placeholder entry
  here (pre-dating implementation), fix any stale comments describing its
  behavior now that it's real (this bit us: the horizontal_bar_chart
  placeholder comment described the *vertical* bar_chart's axis convention,
  copy-pasted and never updated).

## 5. Deploy the change — the part that actually caused the delay

This app runs in Docker and **nothing here has a code volume mount**. Editing
files on the host does not affect a running container until it's rebuilt.
Know which of these three you need — they are not interchangeable:

| You changed | Command | Why |
|---|---|---|
| `backend/**/*.py` | `docker compose up -d --build backend` | Backend image bakes in `app/` at build time (`COPY app/ ./app/` in `backend/Dockerfile`). No mount, no hot reload. Old container = old `_AGGREGATE_WIDGETS`, silently wrong `widget_map`, empty data — no error anywhere. |
| `frontend/widgets/**/*.js` or other frontend static files | `cd frontend && ./rerun.sh` (rebuilds `buchi-frontend` image and recreates `buchi-frontend-app`; not part of `docker-compose.yml`, which has the frontend service commented out) | `frontend/Dockerfile` does `COPY widgets /usr/share/nginx/html/widgets/` — also baked in, served by nginx with no mount. |
| `dashboards/*.yaml` only | `curl -X POST http://localhost:8000/api/v1/dashboards/load -H "Content-Type: application/json" -d '{"filepath": "your_dashboard.yaml"}'` | This directory **is** bind-mounted read-only (`./dashboards:/app/dashboards:ro` in `docker-compose.yml`), so the new file content is already visible inside the container — but `SystemManager` caches a parsed `Dashboard` object per id, built once at startup. `/dashboards/load` re-parses and hot-swaps it in place; no restart needed for YAML-only edits. |

`settings/*.yaml`, `./data`, and `./db` are also bind-mounted — same
"already visible, but check if something caches a parsed copy" caution
applies if a change there doesn't seem to take effect.

### Redis: reload after *every* YAML edit, not just the first one

On top of the parser-object cache above, `/data` responses are cached
independently in Redis:

- Key: `db:<dashboard_id>:<filter_hash>` (`backend/app/core/redis_client.py`),
  a GZIP-compressed JSON blob. `filter_hash` is an MD5 of the applied filter
  conditions, so "no filters" is one specific key, each distinct filter
  combination gets its own.
- TTL: 30 minutes by default (`redis_ttl_seconds` in `settings/general.yaml`,
  default `1800`). A broken/empty result can keep being served for up to 30
  minutes on its own even if you touch nothing else.
- `POST /dashboards/load` calls `redis_cache.invalidate_dashboard(dash.id)`
  (`system_manager.py:289`) which deletes **every** `db:<dashboard_id>:*`
  key for that dashboard — this is the only thing that busts the cache early.

The trap: you fix aggregate A's SQL, reload, confirm A works. Then you fix
aggregate B's SQL in the same file and check the UI directly *without*
reloading again — B still shows the old (error-caused-empty) cached result,
because the `/data` response is cached as one blob for the whole dashboard,
keyed by filter hash, not per-aggregate. **Every** YAML save needs its own
`/dashboards/load` call before you check results again, no exceptions, even
if you already reloaded once this session.

How to tell if you're looking at a stale cache: the `/data` response has a
top-level `"from_cache": true/false` field. If a widget looks empty and
`from_cache` is `true`, reload first — don't start debugging the SQL again
until you've confirmed the same emptiness on a `from_cache: false` response.

```bash
# reload, then immediately re-fetch — from_cache should read false right after
curl -s -X POST http://localhost:8000/api/v1/dashboards/load -H "Content-Type: application/json" -d '{"filepath": "your_dashboard.yaml"}'
curl -s -X POST http://localhost:8000/api/v1/dashboards/<dashboard_id>/data -H "Content-Type: application/json" --compressed -d '{}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('from_cache:', d['from_cache']); print(d['aggregates'])"
```

If a query is genuinely still broken (not just stale-cached), it never
raises an HTTP error — `get_data_for_filters()` wraps every aggregate/raw/
total query in its own try/except and logs
`{"event": "aggregate_query_failed", "name": ..., "error": ...}` instead.
Check `docker logs buchi-backend --since 10m | grep aggregate_query_failed`
for the real DuckDB error message before assuming the YAML fix didn't take.

**Sanity check before telling the user "it should work now"**: hit the data
endpoint directly (using the reload-then-fetch pattern above, so you know
it's not stale) and confirm the new widget id shows up in `widget_map` with
the right `data_type`/`data_key`, and that the corresponding
`aggregates`/`raw`/`totals` entry has actual rows — don't infer this from
reading the code, verify it from the live response:

```bash
curl -s -X POST http://localhost:8000/api/v1/dashboards/<dashboard_id>/data \
  -H "Content-Type: application/json" --compressed -d '{}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['widget_map']); print(list(d['aggregates'].keys()))"
```

(Note `--compressed` — the response is gzip-encoded; `curl` without it will
hand you binary garbage that looks like a decode error, not an API error.)

If `widget_map` is missing your widget id → backend wasn't rebuilt, or the
type isn't in the right `_*_WIDGETS` set. If the aggregate key has `[]` for
rows → either stale Redis cache (see above) or a genuinely failing query.

## Quick end-to-end checklist

- [ ] `frontend/widgets/default/<Type>Widget.js` created
- [ ] `frontend/widgets/default/index.js` imports + registers it
- [ ] `backend/app/models/dashboard.py` — type added to the right `_*_WIDGETS` set
- [ ] `backend/tests/test_dashboard.py` — widget-map test + validation-rejection test added, suite passes
- [ ] Docs updated (widget_bundles.md, dashboard_parser.md, dashboard_validation.md, template.yaml, any API docstrings)
- [ ] **Backend rebuilt** (`docker compose up -d --build backend`) if `dashboard.py` changed
- [ ] **Frontend rebuilt** (`frontend/rerun.sh`) if any `frontend/widgets/**` file changed
- [ ] Dashboard YAML hot-reloaded (`POST /dashboards/load`) after **every** save that touches it, not just the first — this also busts the Redis `/data` cache for that dashboard
- [ ] Verified via live `curl` to `/data` that `widget_map` and `aggregates`/`raw` actually contain the new widget's data, and `from_cache: false` on that check — not just "tests pass"
