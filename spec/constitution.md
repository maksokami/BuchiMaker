<!--
## Sync Impact Report
- Version: 1.0.0 (Initial)
- Added sections: Core Principles, Security Requirements, Development Workflow
- Templates updated: Pending validation of dependencies
-->
# BuchiMaker Constitution

## Core Principles

### I. API-First Architecture
All frontend interactions and data operations must be supported by a robust backend API, enabling clear separation of concerns, containerization, and easy PaaS deployment.

### II. Test Driven development
Write the test first. Watch it fail. Write minimal code to pass. Core principle: If you didn't watch the test fail, you don't know if it tests the right thing.

### III. In-Memory Data Processing
Utilize in-RAM database (DuckDB) to efficiently load, normalize, and query structured data formats (CSV, JSON), ensuring fast performance for filtering and visualizations up to 4GB of data.

### IV. Modular Widget System
Dashboard widgets must be self-contained modules defined with observablehq (https://d3js.org/what-is-d3, https://observablehq.com/plot/) or , supporting dynamic layout adjustments, filtering, and easy addition of new widget types without application restarts.

### V. Declarative Configuration
Dashboards are defined using human-friendly YAML, incorporating simple SQL for calculated fields. The configuration system must enforce strict linting and backend validation for security and consistency.

### VI. Extensible Data Sources
Implement data source connectors as pluggable modules that support local and cloud storage (S3, GCP, Azure), allowing hot-reloading and modular normalization.

## Security Requirements

- All front-end inputs must be back-end validated. Sanitize all applications-level objects that users can define like widgets, dashboard names, calculated field names, etc. Only alphanumeric characters, spaces, dashes, and underscores.
- No secrets in code allowed.
- Prefer docker hardened images, where possible: https://www.docker.com/products/hardened-images/
- Application must log all API usage (who did what action  on what targets and when). Default log timezone is local.

## Development Workflow
- Follow Test Driven development
- All functions and methods must have docstrings in Google format. Ensure to develop proper markdown documentation  for each new feature developed.
API swagger must be available and up-to-date.

## Governance

All architectural decisions must be documented in an ADR. The application must remain containerized and compatible with PaaS environments.

**Version**: 1.0.0 | **Ratified**: 2026-07-01| **Last Amended**: 2026-07-01
