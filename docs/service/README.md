# URL Shortener

A production-minded URL shortener (FastAPI, SQLAlchemy 2.0 async, Alembic). Built and evolved by the agentic SDLC orchestrator; every change traces to a run in `runs/`.

## Run

```bash
uvicorn service.app:create_app --factory --port 8000
```

## Endpoints

| Method | Path | Summary |
|---|---|---|
| GET | `/healthz` | Liveness probe |
| GET | `/readyz` | Readiness probe (database and cache reachable) |
| GET | `/metrics` | metrics endpoint |
| POST | `/api/v1/links` | create link |
| GET | `/api/v1/links/{code}` | get link |
| PATCH | `/api/v1/links/{code}` | update link |
| DELETE | `/api/v1/links/{code}` | delete link |
| GET | `/api/v1/links/{code}/stats` | Click statistics |
| GET | `/{code}` | follow |

Full reference: `docs/service/API.md`; OpenAPI: `docs/openapi.json`.

## Configuration

All settings are environment variables with the `SHORTENER_` prefix:

- `SHORTENER_ALLOW_PRIVATE_HOSTS`
- `SHORTENER_ANALYTICS_BATCH_SIZE`
- `SHORTENER_ANALYTICS_ENABLED`
- `SHORTENER_ANALYTICS_PURGE_INTERVAL_S`
- `SHORTENER_ANALYTICS_QUEUE_SIZE`
- `SHORTENER_ANALYTICS_RETENTION_DAYS`
- `SHORTENER_ANALYTICS_SALT_SECRET`
- `SHORTENER_APP_NAME`
- `SHORTENER_AUTH_ENABLED`
- `SHORTENER_BASE_URL`
- `SHORTENER_CACHE_ENABLED`
- `SHORTENER_CACHE_MAX_ENTRIES`
- `SHORTENER_CACHE_NEGATIVE_TTL_S`
- `SHORTENER_CACHE_TTL_S`
- `SHORTENER_CODE_LENGTH`
- `SHORTENER_CODE_MAX_ATTEMPTS`
- `SHORTENER_CORS_ORIGINS`
- `SHORTENER_DATABASE_URL`
- `SHORTENER_DB_AUTO_MIGRATE`
- `SHORTENER_DB_POOL_SIZE`
- `SHORTENER_ENVIRONMENT`
- `SHORTENER_HSTS_ENABLED`
- `SHORTENER_LOG_JSON`
- `SHORTENER_LOG_LEVEL`
- `SHORTENER_MAX_REQUEST_BODY_BYTES`
- `SHORTENER_MAX_URL_LENGTH`
- `SHORTENER_METRICS_ENABLED`
- `SHORTENER_RATE_LIMIT_ENABLED`
- `SHORTENER_RATE_LIMIT_REDIRECT_CAPACITY`
- `SHORTENER_RATE_LIMIT_REDIRECT_REFILL_PER_S`
- `SHORTENER_RATE_LIMIT_WRITE_CAPACITY`
- `SHORTENER_RATE_LIMIT_WRITE_REFILL_PER_S`
- `SHORTENER_REDIRECT_STATUS_CODE`
- `SHORTENER_REDIS_URL`

## Requirements implemented

Latest change: **REQ-AMBIGUOUS - Make the shortener enterprise-ready and faster. Update from security review: ent**

- Read-through redirect cache
- Token-bucket rate limiting
- Prometheus metrics
- Redirect latency target
- API-key authentication on write endpoints
