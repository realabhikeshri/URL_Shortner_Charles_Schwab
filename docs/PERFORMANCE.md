# Performance

## Target

The ambiguous requirement "make it faster" was turned into a measurable assumption (see
`runs/ambiguous-*/clarification.md`): **p95 redirect latency < 50 ms with 20 concurrent clients on a developer
laptop (SQLite)**. The `test.performance` node enforces it as the `p95_under_target` gate in every ambiguous run.

## Method (`scripts/loadcheck.py`)

1. Migrate a fresh temporary SQLite database and seed 50 links through the service layer.
2. Start a real `uvicorn` process (single worker, uvloop/httptools via `uvicorn[standard]`) on a free port.
   Rate limiting is disabled for the run (all load comes from one client address; the limiter has its own tests).
   Cache, metrics and analytics stay **enabled**, so the hot path is the production one.
3. Warm up (200 requests), then fire `--requests` redirects from `--concurrency` workers, each with its own
   keep-alive HTTP connection, choosing codes at random (seeded RNG). Latency = client-observed time per request.
4. Report p50/p95/p99/mean, throughput and errors; non-302 responses count as errors.

**Measurement pitfall found during the build:** with a single shared `httpx` connection pool, 20 workers measured
p95 ~80 ms and throughput *fell* as concurrency rose (2.7k rps at 5 workers, 0.75k at 20). Giving each worker its
own client changed p95 to ~6 ms at 3.2k rps: the bottleneck was the load generator's pool lock, not the service.
The tool was fixed; the target was not relaxed.

## Results

Latest local measurement (`make loadcheck`, 2,000 requests):

<!-- loadcheck:start -->
Measured 2026-10-07T00:26:25+00:00 on Darwin arm64, Python 3.12.15, sqlite (aiosqlite), single uvicorn worker.

| Requests | Concurrency | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) | req/s | errors | target p95 |
|---|---|---|---|---|---|---|---|---|
| 2000 | 20 | 5.0 | 6.08 | 23.35 | 5.44 | 3338.5 | 0 | 50.0 ms ✅ |
<!-- loadcheck:end -->

The gate runs at 600 requests per ambiguous run; see `runs/ambiguous-*/report.md` (`p95_under_target`).

## Where the time goes on the hot path

`GET /{code}`: request-id + security-header + metrics middleware -> rate-limit token bucket (in-memory or one Redis
Lua call) -> read-through cache lookup (in-memory LRU or one Redis GET; DB only on a miss) -> `put_nowait` of the
click on a bounded queue -> 302. No database write, no awaited I/O for analytics.

## Scaling notes

- Run N uvicorn workers/replicas behind a load balancer; set `SHORTENER_REDIS_URL` so cache and rate limits are
  shared (the in-memory fallbacks are per process).
- Postgres via `asyncpg` with a pooled engine (`SHORTENER_DB_POOL_SIZE`); the clicks table is append-only and
  indexed by `(link_id, day)`; partition by month and/or move analytics to a column store when it grows.
- Not measured here: multi-replica throughput, Postgres latency, Redis network RTT, cold-cache behaviour under
  enumeration attacks (negative cache + per-IP limits are the mitigations). See `docs/LIMITATIONS.md`.
