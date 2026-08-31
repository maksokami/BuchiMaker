## Troubleshooting DuckDB corruption
- If you suspect that active, standby or both DBs are corrupted, you can delete the files `./db/duck_*` and restart the backend. The backend will re-create the files and re-load the data sources.

## Troubleshooting & monitoring Redis cache (RedisInsight)

A helper Docker Compose file is provided at [backend/tests/compose-redis-insight.yml](backend/tests/compose-redis-insight.yml) to run [RedisInsight](https://redis.io/insight/) (a web-based GUI for Redis cache inspection, query profiling, and memory analysis) connected directly to the `buchi-net` network.

**Start RedisInsight**:
```bash
# Start standalone against an already running BuchiMaker stack:
docker compose -f backend/tests/compose-redis-insight.yml up -d

# Or launch simultaneously with the main stack:
docker compose -f docker-compose.yml -f backend/tests/compose-redis-insight.yml up -d
```

**Access and connect**:
1. Open http://localhost:5540 in your browser.
2. Accept the terms and choose **Add Redis database** > **Add database manually**.
3. Fill in the connection details:
   - **Host**: `redis` (or `buchi-redis`)
   - **Port**: `6379`
   - **Database Alias**: `buchi-redis`
   - **Password**: Leave blank for local default, or enter `REDIS_PASSWORD` if configured in `.env`.
4. Click **Add Redis Database**.

**Troubleshooting workflows**:
- **Cache inspection**: Explore query cache entries and SSO session keys (`session:*`). Inspect TTLs and verify payload sizes or cache invalidations.
- **Memory Analyzer**: View memory distribution by key prefixes, identify memory-heavy dashboard queries, and evaluate memory fragmentation.
- **Profiler / Slowlog**: Monitor real-time commands and trace slow cache operations.

**Stop RedisInsight**:
```bash
docker compose -f backend/tests/compose-redis-insight.yml down
```
