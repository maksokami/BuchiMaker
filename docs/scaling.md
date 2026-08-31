
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

**Notes on Windows performance**
If you use Docker on Windows + WSL2, you bind mounts will likely use 9p file system instead of native FS. Docker Desktop has to proxy every file op from the container through drvfs over the 9p protocol.
```
docker exec buchi-backend mount | grep /app/data
C:\ on /app/data type 9p (ro,...,aname=drvfs,...)
C:\ on /app/db   type 9p (rw,...,aname=drvfs,...)
```
You will see significant (x800+) performance impact for direct DuckDB queries. Example:
- 9p: 50 small open()+read() calls on data (9p): 0.67s (~13.5ms/op)
- Native FS:  50 calls on the container's native overlay fs (tmp): 0.0008s (~0.017ms/op)