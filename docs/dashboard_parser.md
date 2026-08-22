# Dashboard YAML Parser Documentation

## Overview
The dashboard YAML parser is responsible for loading dashboard definition files (YAML), parsing their properties, and creating `Dashboard` objects with validated layout widgets. The parser allows decoupling backend SQL execution from the frontend display by interpreting and validating configurations before making them available.

The parsing is mainly executed during the `/api/v1/dashboards/load` API endpoint which populates the internal registry, and the parsed structure is returned to the frontend via the `/api/v1/dashboards/{dashboard_id}` endpoint.

## Loaded Fields

The parser currently focuses on the following primary dashboard elements:

- **`title`**: (String) Human-readable dashboard title.
- **`id`**: (String) Unique identifier for the dashboard, used in API paths.
- **`description`**: (String) Optional description text.
- **`base_view`**: (String, **required**) A `CREATE [OR REPLACE] VIEW <name> AS SELECT ...` statement. See [Base View](#base-view-required) below.
- **`totals`**: (List of dicts) Queries that must return a single scalar value. Used in widgets like tiles and gauges. Must select from the `base_view`.
- **`aggregates`**: (List of dicts) Multi-row queries acting as virtual tables for charts and data tables. Must select from the `base_view`.
- **`mappings`**: (List of dicts) Dictionary mapping UI aliases to database column expressions (e.g., preventing sensitive column exposure).
- **`layout`**: (List of dicts) Defines the structure and order of widgets.

## Base View (required)

Every dashboard YAML must declare `base_view` — a single
`CREATE [OR REPLACE] VIEW <name> AS SELECT ...` statement — **before** the
`aggregates` and `totals` sections (the parser checks the raw key order of
the YAML file, not just presence). The parser:

1. Rejects the dashboard (empty-string-load error, surfaced as HTTP 422 by
   `POST /dashboards/load`) if `base_view` is missing, empty, appears after
   `aggregates`/`totals`, or isn't a valid `CREATE [OR REPLACE] VIEW <name>
   AS ...` statement.
2. Extracts `<name>` and executes the DDL against DuckDB (under the global
   write lock) — this is a real, once-per-load schema mutation, unlike the
   per-request CTE filtering described below.
3. Sets `Dashboard.source_table` to `<name>`.
4. Validates every `totals`/`aggregates` query text references `<name>` —
   any query that instead reads the original source table(s) directly is
   rejected with a validation error naming the offending total/aggregate.

**Why:** filters (`button_filter`, `input_filter`, `dropdown_single`,
`dropdown_multy`, `widget_start_end_time`, `button_datetime_filter`) are
applied by injecting one `WHERE`-clause CTE in front of each total/aggregate
query at request time (see "API Integration" below and
`docs/backend_architecture.md`'s Filter Pipeline / ADR-011). That injection
only works if every query shares the same column namespace and the same
already-resolved joins — otherwise each aggregate author would have to
independently keep their query's columns and aliases "filter-compatible."
This is the same reason most BI tools (Looker Explores, Power BI datasets,
Tableau data sources) put one flattening/join layer between raw sources and
every chart: the joins and enrichment are done once, and every widget — and
every filter — targets that one shape afterward.

The view is dropped when the dashboard is deleted, and when a hot-reload
changes the view's name (comparing old vs. new `source_table`); reloading
with the same view name is a no-op `CREATE OR REPLACE`.

Example:
```yaml
base_view: "CREATE OR REPLACE VIEW sales_overview_base AS
SELECT o.order_id, o.amount, o.status, c.region, c.segment
FROM orders AS o
LEFT JOIN customers AS c ON o.customer_id = c.customer_id;"

totals:
  - name: total_revenue
    query: "SELECT SUM(amount) FROM sales_overview_base"

aggregates:
  - name: revenue_by_region
    query: "SELECT region, SUM(amount) AS revenue FROM sales_overview_base GROUP BY 1"
```

## Layout & Widget Validation

Widgets defined under the `layout` section are parsed into a uniform representation, extracting `type`, `id`, `grid`, and the specific `config`. Strict validations are applied to certain widgets as per defined restrictions:

### `tile`
- **Validation:** A tile widget expects a scalar value. It **strictly accepts only Totals**. 
- **Rejection:** The parser will reject the dashboard and return an error if a tile attempts to use a named `aggregate`.

### `basic_table`
- **Validation:** A table widget expects multi-row data. It **strictly accepts only Aggregates** (or a data source table).
- **Rejection:** The parser will reject the dashboard and return an error if a `basic_table` attempts to use a named `total`.

### `input_filter`
- A free-text search/filter widget.
- Supports operators: `eq`, `has` (substring), `prefix`, `suffix` — all applied server-side.
- Requires a `mapping` field referencing a key in the dashboard `mappings` section.
- The `filter` list in the YAML acts as a **default hint** for the frontend (pre-selected operator); the actual operator is always specified by the frontend at query time.
- A warning is logged at load time if `mapping` is missing (no hard rejection, to allow partial YAML development).

Example:
```yaml
- input_filter:
    id: org_search
    label: "Search Organisation"
    mapping: "org_unit_col"       # must match a key in the mappings section
    filter:
      - operator: "has"
      - value: ""
    grid:
      x: 0
      y: 0
      w: 2
      h: 1
```

### `button_filter`
- A toggle-button filter widget, typically for boolean or enumerated values.
- Supports operators: `eq`, `neq`, `gt`, `lt`, `gte`, `lte` (numeric/string), `has`, `prefix`, `suffix`.
- Requires a `mapping` field referencing a key in the dashboard `mappings` section.
- The `filter` list declares the pre-selected operator and value (applied when the button is toggled on).

Example:
```yaml
- button_filter:
    id: active_toggle
    label: "Active Only"
    mapping: "active_col"
    filter:
      - operator: "eq"
      - value: "true"
    grid:
      x: 3
      y: 0
      w: 1
      h: 1
```

### `dropdown_single`
- A single-select dropdown filter.
- `mapping` is the field the selection filters on (a key in the `mappings` section or a column expression directly) — resolved server-side at query time, same as every other filter widget.
- `data` (optional) names an **aggregate** or **raw datasource** whose rows populate the dropdown's option list. It is independent of `mapping`: `data` decides what shows up in the list, `mapping` decides what column gets filtered when an option is picked. Typically points at a small `SELECT DISTINCT(col) ...` aggregate.
- **Validation:** `data` cannot reference a named `total` (same rule as the aggregate-backed widgets) — a total is a single value, not a list of options.

### `dropdown_multy`
- A multi-select dropdown filter widget.
- Same `mapping` / `data` split as `dropdown_single`: `mapping` is the filtered column, `data` (optional) is the aggregate or raw source populating the option list.
- **Validation:** `data` cannot reference a named `total`.

### `sankey`
- A Sankey diagram expecting to show relationships between multi-row data.
- **Validation:** Accepts only **Aggregates**. 
- **Rejection:** The parser will reject the dashboard and return an error if a `sankey` widget attempts to reference a named `total` in either its `data` field or within its `nodes` list.

### `widget_start_end_time`
- A calendar-based date range selector filter widget.
- Requires a `mapping` field referencing a key in the dashboard `mappings` section.
- Typically sends multiple filter conditions (e.g. `gte` and `lte`) for the mapped field.
- A warning is logged at load time if `mapping` is missing.

### `button_datetime_filter`
- A toggle-button filter specifically for relative time ranges (e.g., "Last 7 days").
- Supports the `last_minutes` operator (calculates `CURRENT_TIMESTAMP - INTERVAL '{value}' MINUTE`).
- Requires a `mapping` field referencing a key in the dashboard `mappings` section.
- Requires a `toggle_group` field; only one button within the same group can be active at a time in the UI.
- The `filter` field contains the number of minutes to look back (e.g., `172800` for 120 days).
- Warnings are logged at load time if `mapping` or `toggle_group` are missing.

### Placeholders for Future Widgets
Other widget types present in the layout (`radial_gauge_full`, `radial_gauge_semi`, `pie_chart`, `bar_chart`, `horizontal_bar_chart`, `stacked_bar_chart`, `line_chart`, `button_url`) are loaded and placed in the dashboard layout configuration as placeholders. Strict field validation for these widgets is deferred to a future phase, though their layout attributes (`id`, `grid`, etc.) are parsed and made available to the UI.

## API Integration

### `POST /api/v1/dashboards/load`
- Reads the YAML definition.
- Executes the required `base_view` DDL against DuckDB, then instantiates a `Dashboard` object.
- Runs validation checks on `base_view`, layouts, totals, and aggregates.
- Fails with `422 Unprocessable Content` if a validation rule is violated (e.g., a `tile` referencing an aggregate, a missing/misordered `base_view`, or a total/aggregate that doesn't query the `base_view`).

### `GET /api/v1/dashboards/{dashboard_id}`
- Returns the validated `DashboardDefinitionResponse` JSON to the frontend UI.
- Contains the parsed `mappings` and `layout` alongside totals and aggregates.
- The UI uses this schema to construct grid placement and dynamically build widget data-binding.
- Filter widget entries (`input_filter`, `button_filter`, `dropdown_single`, `dropdown_multy`) appear in `layout` with their `mapping` field so the frontend can populate dropdowns and resolve widget IDs for `POST /data` filter keys.

### `POST /api/v1/dashboards/{dashboard_id}/data`
- Accepts widget-ID-keyed filters in the request body.
- The `widget_id` keys must match the `id` of filter-type layout widgets (`input_filter`, `button_filter`, `dropdown_single`, `dropdown_multy`).
- The `mapping` field is resolved server-side to the actual DB column expression via the `mappings` section.
- Non-filter widgets (`tile`, `basic_table`, etc.) are **not** valid filter keys.
