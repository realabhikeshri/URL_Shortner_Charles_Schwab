# Changelog

All notable changes to this repository. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [SemVer](https://semver.org/). The generated service keeps its own changelog in its workspace
(`orchestrator/blueprints/url_shortener/scaffold/CHANGELOG.md`, filled by the DocWriter agent per run).

## [Unreleased]

## [0.1.0] - 2026-10-07

### Added
- Orchestrator: validated plan DAG with parallel execution and join synchronisation; per-node state machine persisted
  in SQLite; crash-safe `resume`; hash-chained audit log; OTel-shaped spans; reliability metrics (JSON + Prometheus).
- Controls: bounded retries with exponential backoff + jitter and failure feedback; deterministic fallback provider;
  path-scoped git rollback; kill switch and hard budgets; risk-tiered human approvals (high risk never auto-approved
  without an audited flag); approval-integrity check on diff drift.
- Policy-as-code: 12 checks across security, compliance and change control (`policies/*.yaml`).
- Agents: RequirementAnalyst, CodebaseAnalyst (AST import graph, FastAPI routes, SQLAlchemy metadata, test map,
  blast radius), Planner, Architect, Implementer, TestEngineer, DocWriter, Reviewer, ReleaseManager.
- Providers: deterministic (offline default), Anthropic (streaming, adaptive thinking, server-side refusal
  fallback, cost accounting), record/replay.
- Dynamic re-planning on requirement or file hash drift, with approval when risk rises.
- Three scenarios with committed evidence in `runs/`; `make demo`; HTML/Markdown run reports.
- URL shortener service built by those runs: links API (idempotent create, metadata, patch, soft delete), redirect
  (302/307), expiry, click analytics (off the hot path, PII-free), read-through cache, token-bucket rate limiting,
  API-key auth, Prometheus metrics, health/readiness, Alembic migrations 0001-0003.
- Dockerfile (multi-stage, non-root), Compose with Postgres 16 + Redis 7, GitHub Actions CI, load-check script.

### Fixed
- Alias lookups were case-sensitive while alias creation was case-insensitive (seeded in greenfield, fixed by the
  brownfield run with a red-first regression test).
- `SHORTENER_REDIRECT_STATUS_CODE` was rejected when supplied via the environment (string vs `Literal[302, 307]`).
