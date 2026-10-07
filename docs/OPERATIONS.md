# Operations

## Running the service

| Mode | Command |
|---|---|
| Local (SQLite) | `make run` -> http://localhost:8000 (`/docs`) |
| Compose (Postgres 16 + Redis 7) | `make docker-up`; smoke test: `bash scripts/smoke.sh http://localhost:8000`; `make docker-down` |
| Container | `docker build -t shortener . && docker run -p 8000:8000 -e SHORTENER_DATABASE_URL=... shortener` |

The container runs `python -m service.cli migrate` and then `uvicorn ... --proxy-headers --no-access-log`.
Run one migration step per deploy, not per replica, if replicas start concurrently.

## Configuration

Environment variables, prefix `SHORTENER_`; the full annotated list is in [`.env.example`](../.env.example).
Must-set in production: `DATABASE_URL` (Postgres, `postgresql+asyncpg://`), `REDIS_URL` (shared cache and limits),
`BASE_URL` (public origin used in `short_url`), `ANALYTICS_SALT_SECRET` (long random; without it unique-visitor
hashes reset on restart), `ENVIRONMENT=prod`, `HSTS_ENABLED=true` behind TLS, `CORS_ORIGINS` if browsers call the API.
`FORWARDED_ALLOW_IPS` should list the load balancer so client IPs (rate limits, visitor hashes) are right.

### API keys

```bash
python -m service.cli create-key --name ci-pipeline   # prints the key once
python -m service.cli list-keys                       # prefix, name, state (never the key)
python -m service.cli revoke-key <prefix>
```

## Probes and endpoints

| Endpoint | Use |
|---|---|
| `GET /healthz` | Liveness: process is up |
| `GET /readyz` | Readiness: database and cache reachable (503 otherwise) |
| `GET /metrics` | Prometheus; restrict to the monitoring network |
| `GET /api/v1/openapi.json`, `/docs` | API schema and UI; consider disabling or restricting in prod |

## Metrics and suggested alerts

| Metric | Alert (starting point) |
|---|---|
| `http_requests_total{status=~"5.."}` / total | > 1% for 5 min |
| `http_request_duration_seconds` p95, route `/{code}` | > 50 ms for 10 min (the build's target) |
| `/readyz` failing | any, 2 min |
| `rate_limited_total{scope="redirect"}` | sudden spike: scraping/enumeration or a misconfigured proxy (all clients share one IP) |
| `rate_limited_total{scope="write"}` | sustained: a client needs a higher limit or is abusive |
| `link_cache_requests_total{result="miss"}` ratio | > 50% sustained: cache down, TTL too low, or enumeration |
| `analytics_clicks_dropped` | > 0 and rising: writer cannot keep up, raise `ANALYTICS_QUEUE_SIZE`/batch or scale the DB |
| `analytics_queue_depth` | near `ANALYTICS_QUEUE_SIZE` |
| `redirects_total{outcome}` | traffic baseline |

## Runbook

- **Redirects 5xx / readyz failing:** check `readyz` body (`database` vs `cache:*`). A cache outage degrades to the
  DB (slower, not down); a DB outage fails redirects for uncached links only.
- **High latency:** cache hit ratio, DB connections (`DB_POOL_SIZE`), `analytics_queue_depth`. Redis RTT.
- **Bad link reported:** `PATCH /api/v1/links/{code}` `{"disabled": true}` with the owner's key (or revoke the key
  with `revoke-key`; its links stay but it can no longer modify them). Cache is invalidated immediately.
- **Key compromised:** `revoke-key <prefix>`; takes effect on the next request (keys are not cached).
- **Analytics storage growing:** lower `ANALYTICS_RETENTION_DAYS`; purge runs at start-up and hourly.
- **Rollback a release:** migrations 0001-0003 are additive and have `downgrade()`; `alembic -c service/alembic.ini downgrade 0002`.

## Operating the orchestrator

```bash
orchestrator status <run>       orchestrator runs
orchestrator approve|reject <run> '<gate>' --comment "..."     orchestrator resume <run>
orchestrator stop <run>         # kill switch; run safe-stops, state persists
orchestrator audit verify <run> # run in CI/scheduled; non-zero exit on tamper
orchestrator metrics [--format prometheus]
```

Orchestrator reliability metrics (per run and aggregated, `runs/metrics.json`): node/run success rate, retry
rate (RETRYING transitions / attempts), rollback frequency, fallback frequency, MTTR (failure → recovery of the
same node), per-stage and end-to-end latency, approval wait, tokens and cost, re-plans. Watch: rising retry or
fallback rates (provider quality), approval wait (human bottleneck), `unrecovered_failures`, and any audit
verification failure.

A paused run waits indefinitely and holds no resources; a crashed run is resumed with `orchestrator resume`
(in-flight nodes are rolled back and re-run, completed work is kept).
