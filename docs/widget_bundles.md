# Widget Bundles

How dashboard widgets are rendered, and how to build a custom widget package
("bring your own widgets") without touching `DashboardView.js`.

## Overview

`frontend/components/DashboardView.js` does not hardcode widget rendering.
It fetches a dashboard's `layout` (from `GET /dashboards/{id}`) and its data
(from `POST /dashboards/{id}/data`), then for each layout row looks up a Vue
component by the row's `type` key in a **widget registry**, and renders it
with `<component :is="...">`. The registry comes from a **widget package** —
a self-contained folder under `frontend/widgets/` — loaded at runtime via a
dynamic `import()`.

The only widget package that ships today is `frontend/widgets/default/`. A
custom/branded package is just another folder in the same shape.

## Package structure

```
frontend/widgets/<package-name>/
├── index.js            # required: exports { packageName, components }, injects theme.css
├── theme.css            # CSS scoped under [data-package="<package-name>"]
├── echarts-theme.js      # optional: echarts.registerTheme(...) calls
└── <Type>Widget.js       # one Vue component per widget type this package implements
```

`index.js` is the only file DashboardView imports directly:

```js
// frontend/widgets/default/index.js
import './echarts-theme.js';               // side-effect: registers eCharts themes
import TileWidget from './TileWidget.js';
// ...

const link = document.createElement('link');
link.rel = 'stylesheet';
link.href = new URL('./theme.css', import.meta.url).href;
document.head.appendChild(link);            // side-effect: injects package CSS once

export default {
    packageName: 'default',
    components: {
        tile: TileWidget,
        bar_chart: BarChartWidget,
        stacked_bar_chart: StackedBarChartWidget,
        // ... one entry per YAML widget-type key this package supports
    }
};
```

Because ES modules are cached by URL, `index.js`'s side effects (CSS
injection, `echarts.registerTheme`) only ever run once, even if
`loadWidgetPackage()` is called again later (e.g. switching dashboards).

**Component keys must exactly match the backend's widget `type` string**
(the YAML layout's top-level key per widget, e.g. `basic_table`, not
`table`). Backend-validated types live in `_AGGREGATE_WIDGETS` /
`_TOTAL_WIDGETS` / `_FILTER_WIDGETS` in
`backend/app/models/dashboard.py:52-65`.

## Switching packages

`DashboardView.js`'s `loadWidgetPackage(pkgName)` currently always loads
`'default'` — there is no UI or per-dashboard config to pick a different
package yet. To point a dashboard at a custom bundle, change the call in
`onMounted` (`frontend/components/DashboardView.js`) to pass a different
`pkgName`, or wire it to a per-dashboard/org setting. `loadWidgetPackage`
sets `data-package="<pkgName>"` on `.dashboard-area`, which is what scopes
the package's `theme.css`.

## The Standard Props Contract

Every widget component receives exactly these four props — nothing else.
It never talks to the backend, never knows about filters state, never knows
about other widgets:

```js
props: {
    id:     { type: String, required: true },  // this widget's layout id
    config: { type: Object, required: true },   // the YAML layout row's `config` node
    data:   { default: null },                  // resolved value: scalar | array-of-row-dicts | null
    theme:  { type: String, default: 'light' }   // 'light' | 'dark'
}
```

`data` is `null`/empty until the bulk data fetch resolves, and stays
`null`/`[]` if the widget has no matching entry in the backend's
`widget_map` or the underlying query returned nothing. **There is no
`.error` field on `data`** — DashboardView's loading skeleton only gates on
whether the *first* fetch has completed at all; after that, every widget
component is responsible for rendering its own graceful "no data" state
(see any `*Widget.js` file for the pattern — a `v-if="!data || ..."`
`.widget-unknown` block).

**Data Contract Validation**: Additionally, widget components are responsible
for proactively validating the shape of their `props.data` before passing it to
complex rendering libraries like ECharts. If the data is present but fails validation
(e.g., missing required fields), widgets should compute an `isDataInvalid` property
and display a `<div v-else-if="isDataInvalid" class="widget-unknown">Data format invalid</div>`
placeholder rather than rendering a broken or empty chart.

**Chart Rendering Try/Catch**: When using third-party charting libraries like ECharts,
wrap `chart.setOption(...)` (or equivalent layout/rendering calls) in a `try/catch` block.
If the library throws a synchronous error due to unexpected data shapes that passed the
basic contract validation, catch the error, dispose of the chart instance, set a `hasError` state,
and render a `<div v-else-if="hasError" class="widget-unknown">Chart rendering failed</div>`
placeholder. This prevents layout calculation errors from escaping into Vue's reactivity cycle.

### Events

A widget can emit two events; DashboardView is the only listener:

- `emit('row-select', row)` — `BasicTableWidget` only. `row` is the raw
  row-dict object (not the widget's own props). DashboardView uses this to
  populate the right-hand detail panel via `widget.config['on-row-click-panel']`.
- `emit('update:filter', { widgetId, value, operator })` — any filter
  widget (`input_filter`, `button_filter`, `dropdown_multy`,
  `dropdown_single`). `value: null` clears that widget's filter.
  DashboardView debounces (~300ms) and re-POSTs `/data` with the merged
  `filters` object on every emit. `button_url` never emits — it's a plain
  link and contributes no filter (matches the backend, which excludes it
  from filter-condition building).

## Data shape per widget type

The backend's `config` field names differ per widget type — see
`dashboards/template.yaml` for the authoritative schema-by-example.
Summary, as implemented by the `default` package:

| Type | `config` fields read | `data` shape |
|---|---|---|
| `tile`, `radial_gauge_full`, `radial_gauge_semi` | `label`, `bg` (tile only) | scalar (number/string) or `null` |
| `bar_chart`, `area_chart` | `title` | array of row-dicts; first key = category/x-axis label, every other *numeric* key becomes its own series (multi-series for free) |
| `horizontal_bar_chart` | `title` | same row shape as `bar_chart`, but the first key becomes the y-axis (category) label and value keys are plotted along the x-axis (value axis) |
| `stacked_bar_chart` | `title` | same row shape as `bar_chart`, but each value-key series is stacked (`stack: 'total'`) into a single bar per category instead of rendered side-by-side |
| `pie_chart` | `title` | array of row-dicts; first key = slice name, first *numeric* key = value |
| `basic_table` | `title`, `columns` (link-cell overrides), `on-row-click-panel` | array of row-dicts; column order follows the row-dict's own key order |
| `sankey` | `title`, `data` (an aggregate whose query returns `{source, target, value}` rows) | array of `{source, target, value}` row-dicts |
| `input_filter`, `button_filter` | `label`, `filter` (list of single-key dicts, e.g. `[{operator:"ge"},{value:"10"}]` — merge into one object) | not used |
| `button_url` | `label`, `url` | not used |
| `dropdown_multy`, `dropdown_single` | `label`, `data` (optional aggregate/raw source for the option list) | array of row-dicts (same shape as every other aggregate-backed widget); the widget takes the first key's value from each row as the option string. `null`/`[]` if `data` is omitted. |

## Theming

Two independent mechanisms, matched by the `theme` prop (`'light'`/`'dark'`):

1. **CSS custom properties** — package `theme.css` is scoped under
   `[data-package="<name>"]` (set on `.dashboard-area`) and should read the
   app-shell's existing tokens (`--bg-surface`, `--text-primary`,
   `--border-color`, etc. — see `frontend/styles.css:1-51`) rather than
   hardcoding colors, so a custom package stays consistent with the user's
   light/dark toggle without needing its own dark-mode CSS.
2. **eCharts palettes** — register named themes in `echarts-theme.js`
   (e.g. `echarts.registerTheme('default-light', {...})`) and select one in
   each chart widget's `onMounted`:
   ```js
   chart = echarts.init(chartRef.value, props.theme === 'dark' ? 'default-dark' : 'default-light');
   ```

`currentTheme` is read once by DashboardView from
`document.documentElement.getAttribute('data-theme')` — it is **not**
reactive. This is safe because `dashboard-view` is `v-if`'d (not
`keep-alive`'d) in `index.html` and always remounts fresh after the
Settings-page theme toggle. If that ever changes to `v-show`/`keep-alive`,
this assumption breaks and `currentTheme` would need to become a `ref` with
a `MutationObserver`.

## Pitfalls for widget authors

1. **Never use a hardcoded DOM `id`.** Two instances of the same widget
   type on one dashboard will collide (`document.getElementById` returns
   the first match, the other silently breaks). Use a template `ref`
   instead — every `*Widget.js` chart component follows this pattern:
   ```js
   template: `<div ref="chartRef" style="width:100%;height:100%;"></div>`,
   setup() {
     const chartRef = ref(null);
     onMounted(() => { chart = echarts.init(chartRef.value, themeName); });
     return { chartRef };
   }
   ```
2. **Clean up your own resize handling.** Don't add a `window.addEventListener('resize', ...)`
   — N widgets would mean N global listeners, and forgetting to remove one
   leaks memory when a dashboard unmounts. Use a `ResizeObserver` bound to
   your own container ref instead, disconnected in `onUnmounted`:
   ```js
   const resizeObserver = new ResizeObserver(() => chart?.resize());
   // after echarts.init(): resizeObserver.observe(chartRef.value);
   onUnmounted(() => { resizeObserver.disconnect(); chart?.dispose(); });
   ```
3. **Own your interactive state locally.** Dropdown open/close, button
   toggle state, search-box text — none of it is a prop. Each component
   instance keeps its own `ref`s (Vue isolates them per-instance
   automatically) and, for click-outside-to-close, adds its own scoped
   `document` click listener in `onMounted`/removes it in `onUnmounted`
   (see `DropdownSingleWidget.js`/`DropdownMultyWidget.js`).
4. **Watch `data` and `theme` with `flush: 'post'`** if your render
   function touches a template ref that's behind a `v-if` (e.g. a
   "no data" placeholder swaps with the chart container). Vue's default
   watch flush (`'pre'`) fires *before* that DOM patch, so the ref can
   still be `null` — see any chart widget's `watch(() => props.data, render, { flush: 'post' })`
   for the pattern.
5. **Set `tooltip.appendToBody: true` on every eCharts `tooltip` option.**
   `.grid-stack-item-content.widget-frame` has `overflow: hidden`
   (`styles.css`), and eCharts renders its tooltip DOM node inside the
   chart's own container by default — so a tooltip near a card edge gets
   clipped instead of showing its full content. `appendToBody: true` makes
   eCharts render the tooltip under `<body>` as `position: fixed` instead,
   which escapes the clipping and always renders above sibling widgets.
   Every tooltip-emitting widget in this package sets it —
   `tooltip: { trigger: 'axis', appendToBody: true }` (or `trigger: 'item'`
   for `pie_chart`/`sankey`) — see any `*Widget.js`'s `setOption()` call.

## Known gaps / follow-ups

These are pre-existing backend/infra gaps surfaced while building the
`default` package — not bugs in the widget-registry architecture itself.
None block the registry from working; they limit what real data currently
reaches a few widget types.

- ~~`sankey` never receives data~~ — **fixed.** Sankey's YAML schema used
  to declare `nodes: [aggregate1, aggregate2]`, which
  `Dashboard._build_widget_map()` never read (it only read `config.data`),
  so sankey widgets never got a `widget_map` entry. The schema now uses the
  same `data:` field as every other aggregate-backed widget — the
  referenced aggregate's query returns `{source, target, value}` rows
  directly, matching `SankeyWidget.js`'s contract exactly. See
  `spec/dashboard_validation.md`'s Sankey note and
  `dashboards/vuln_metrics.yaml`'s `sankey_*` aggregates for the pattern.
- ~~`dropdown_multy`/`dropdown_single` never receive options~~ — **fixed.**
  `mapping` and `data` are now a deliberate split: `mapping` is the DB column
  the widget's selection filters on (unchanged), `data` is an optional
  aggregate or raw-source name whose rows populate the option list.
  `_build_widget_map()`/`_raw_sources_needed()` special-case both dropdown
  types to still resolve `data` even though they remain excluded from the
  generic aggregate/total widget_map logic (they're still `_FILTER_WIDGETS`
  for filter-condition purposes). See `dashboards/vuln_metrics.yaml`'s
  `filter_data_env`/`filter_data_risk` aggregates for the pattern (a plain
  `SELECT DISTINCT(col) ...` query).
- **`dropdown_multy`'s multi-value filter semantics are unverified.** It
  currently emits the selected array as `value` with `operator: 'eq'`;
  whether the backend's condition builder (`dashboard.py`) treats a list
  value as OR-of-equals for a single widget id is untested. Verify against
  a real multi-select dropdown before relying on it in production.
- **Deferred widget types with no implementation**: `line_chart`,
  `widget_type`, `widget_start_end_time`, `button_datetime_filter` are
  backend-validated but have no widget component in `default` — no
  reference implementation existed to port. Registering a dashboard that
  uses one renders a `.widget-unknown` fallback instead of crashing.
- **2 legacy chart types still dropped**: `stacked_horizontal_bar_chart`,
  `treemap_chart` existed in the old mock-data UI but the backend doesn't
  validate or supply real data for them (`dashboard.py`'s
  `_AGGREGATE_WIDGETS` doesn't include them). Not ported. (`area_chart` was
  ported — see `AreaChartWidget.js`; it reuses `bar_chart`'s row→series
  convention with `type: 'line'` + `areaStyle` for overlapping, translucent
  areas rather than stacking. `horizontal_bar_chart` was also ported — see
  `HorizontalBarChartWidget.js`; it reuses `bar_chart`'s row→series
  convention with `xAxis`/`yAxis` swapped, and is now in
  `_AGGREGATE_WIDGETS`. `stacked_bar_chart` was also ported — see
  `StackedBarChartWidget.js`; it reuses `bar_chart`'s row→series convention
  with `stack: 'total'` added to each series, and is now in
  `_AGGREGATE_WIDGETS`.)
- **`basic_table` has no pagination wiring.** `POST /dashboards/{id}/widgets/{widget_id}/page`
  exists server-side but `BasicTableWidget.js` only renders what the bulk
  `/data` response returns (capped at the backend's row limit).
- **nginx CSP blocks the deployed frontend's own API calls.**
  `frontend/nginx.conf`'s `connect-src 'self'` rejects `fetch()` to a
  backend on a different origin (the default `docker-compose.yml` setup:
  frontend on `:8080`, backend on `:8000`). Predates this refactor — see
  `docs/frontend_architecture.md`'s Security Posture section.
