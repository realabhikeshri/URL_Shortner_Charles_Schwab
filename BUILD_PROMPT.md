# Master Build Prompt: Agentic SDLC Orchestrator + URL Shortener

> Paste everything below the line into your coding agent (Claude Code recommended), started in an EMPTY folder. Or save this file as `BUILD_PROMPT.md` in the folder and say: "Read BUILD_PROMPT.md and execute it fully."

---

## ROLE

You are a principal software engineer delivering a take-home assignment for a senior engineering role at a large financial services firm. Reviewers will score: effectiveness of agentic orchestration, architecture quality, depth of decomposition, realism of outputs, validation/risk rigor, clarity and defensibility of decisions, core engineering principles, and judgment. Treat this as production-grade work. Every claim in the docs must be true of the code.

## OPERATING RULES (read first, follow throughout)

1. **Work autonomously.** Do not stop to ask me questions. When something is ambiguous, choose the most defensible option, record it in `docs/DECISIONS.md` (ADR format) or `docs/ASSUMPTIONS.md`, and continue.
2. **Install everything yourself.** Create the virtualenv, install dependencies, run tests, fix failures. Never leave a step for me unless it truly needs a secret I hold (only `ANTHROPIC_API_KEY`, which must be OPTIONAL — see §4.9).
3. **Work in phases (§7).** At the end of each phase run `make check` (lint + types + tests + security). Do not start the next phase until it is green. Commit after each green phase with a conventional-commit message (`git init` first).
4. **No placeholders.** No `TODO`, `pass`, `...`, `NotImplementedError`, fake tests, or tests that assert `True`. If something is out of scope, say so in `docs/LIMITATIONS.md` instead of stubbing it.
5. **Keep a running log** in `docs/BUILD_LOG.md`: phase, what was done, what broke, how it was fixed.
6. **Verify, don't assume.** After writing any CLI command or README instruction, actually run it and confirm the output.
7. If a tool or network call fails, retry once with a different approach, then document and move on — do not loop.

## 1. WHAT WE ARE BUILDING (two layers)

**Layer A — `orchestrator/`**: an agentic SDLC orchestration engine. This is the primary thing being evaluated. Given a requirement (text/YAML), it plans and executes the lifecycle — requirements → design → implementation → testing → documentation → release readiness — as a stateful, governed, non-linear dependency graph of agent tasks, with human approval gates.

**Layer B — `service/`**: a URL shortener built (and later enhanced) *through* the orchestrator. It must be a genuinely good, production-quality service on its own.

The repo must make it obvious that Layer B's code, tests and docs were produced and validated via Layer A's runs, with full traceability (run IDs, decision lineage, audit logs committed under `runs/`).

## 2. TECH STACK (fixed — do not deviate without an ADR)

- Python 3.11+, `uv` (fall back to `pip` + `venv` if `uv` is unavailable)
- Service: FastAPI, Pydantic v2, SQLAlchemy 2.0 (async) + Alembic, SQLite by default, Postgres via Docker Compose; Redis optional (cache + rate limit) with in-memory fallback
- Orchestrator: pure Python + `asyncio`, `networkx` for the DAG, SQLite for state, Typer + Rich for the CLI, `anthropic` SDK (optional)
- Quality: pytest, pytest-asyncio, pytest-cov, hypothesis, httpx; ruff (lint+format), mypy `--strict` on `orchestrator/` and `service/`, bandit, pip-audit
- Packaging: `pyproject.toml`, `Makefile`, `Dockerfile` (multi-stage, non-root), `docker-compose.yml`, GitHub Actions CI

## 3. LAYER B — URL SHORTENER REQUIREMENTS

### API (versioned `/api/v1`, OpenAPI exported to `docs/openapi.json`)
- `POST /api/v1/links` — create short link. Body: `url`, optional `custom_alias`, `expires_at`, `tags`. Supports `Idempotency-Key` header. Returns 201 with `code`, `short_url`, `created_at`.
- `GET /{code}` — 302 redirect (307 configurable); 404 unknown; 410 expired/disabled.
- `GET /api/v1/links/{code}` — metadata.
- `PATCH /api/v1/links/{code}` — update expiry/disable.
- `DELETE /api/v1/links/{code}` — soft delete.
- `GET /api/v1/links/{code}/stats` — total clicks, unique visitors (hashed IP+UA, daily-salted, no raw PII stored), clicks by day, top referrers, top user-agent families.
- `GET /healthz` (liveness), `GET /readyz` (DB + cache check), `GET /metrics` (Prometheus).

### Behaviour and non-functionals
- Code generation: base62 from a collision-checked random 7-char space (document the math: collision probability, capacity), with retry on collision; custom aliases validated (regex, length, reserved words like `api`, `healthz`, `metrics`).
- URL validation: http/https only, max length 2048, reject `javascript:`/`data:`/`file:`, reject private/loopback hosts by default (configurable), normalize URL.
- Auth: API key via `X-API-Key` for write endpoints (keys hashed at rest); redirect is public.
- Rate limiting: token bucket per API key / per IP (Redis or in-memory).
- Caching: read-through cache on redirect path with TTL and invalidation on update/delete.
- Analytics recorded off the hot path (background task/queue) so redirect latency is unaffected; failures in analytics never fail a redirect.
- Structured JSON logging with request IDs; consistent error envelope (`{"error": {"code", "message", "request_id"}}`).
- Config via environment (pydantic-settings), `.env.example` provided, no secrets in code.
- Security headers, CORS config, input size limits.
- Alembic migrations, including one added in the brownfield scenario.

### Tests (for Layer B)
- Unit tests for code generation, validation, cache, rate limiter.
- Property-based tests (hypothesis) for base62 encode/decode and URL validation.
- Integration tests via httpx `AsyncClient` against the app with a temporary SQLite DB.
- A small load-sanity script (`scripts/loadcheck.py`) that measures p50/p95 redirect latency locally and writes results to `docs/PERFORMANCE.md`.
- Coverage ≥ 85% for `service/`, enforced in CI.

## 4. LAYER A — ORCHESTRATOR REQUIREMENTS (the critical differentiator)

Implement each of these as real, tested code — not prose.

### 4.1 Requirement intake and normalization
- Input: free text or YAML (`requirements/*.yaml`). A `RequirementAnalyst` agent produces a normalized `RequirementSpec`: goal, functional reqs, non-functional reqs, acceptance criteria (testable, Given/When/Then), constraints, **identified ambiguities**, assumptions, risk level (low/med/high), and open questions.
- If ambiguity score exceeds a threshold, the run enters a `CLARIFICATION` gate: questions are presented to the human; in non-interactive mode, documented default assumptions are applied and flagged for approval.

### 4.2 Task decomposition and planning
- A `Planner` agent converts the spec into a **plan DAG**: nodes are typed tasks (`design`, `implement`, `test`, `document`, `review`, `release_check`), each with inputs, outputs (artifact paths), owner agent, entry criteria, exit criteria, risk class, and estimated effort.
- Validate the DAG: acyclic, all inputs produced upstream, no orphan nodes. Render it to Mermaid in the run report.

### 4.3 Codebase reasoning (brownfield)
- A `CodebaseAnalyst` agent builds an impact analysis using real static analysis (Python `ast` import graph, symbol index, route table extracted from FastAPI, DB models from SQLAlchemy metadata, test-to-module mapping). Output: impacted modules, APIs, data flows, migrations needed, tests to update, blast-radius score. This feeds risk classification.

### 4.4 Execution engine
- States per node: `PENDING → READY → RUNNING → (AWAITING_APPROVAL) → SUCCEEDED | FAILED | RETRYING | ROLLED_BACK | SKIPPED | BLOCKED`. Run-level states including `PAUSED` and `SAFE_STOPPED`.
- **Entry/exit gates** per node (e.g. implement's exit gate = code compiles + lint clean + targeted tests pass; test's exit gate = coverage threshold met; release gate = all policies pass + human approval).
- **Parallelism with synchronization**: independent nodes run concurrently via `asyncio` with a configurable concurrency limit; join nodes wait for all predecessors (e.g. `docs` and `tests` in parallel after `implement`, joined at `release_check`).
- **Stateful and resumable**: all state persisted in SQLite (`runs/<run_id>/state.db`); `orchestrator resume <run_id>` continues after a crash or pause. Prove this with a test that kills a run mid-way and resumes it.
- **Cross-stage context and decision lineage**: a `ContextStore` passes typed artifacts between stages; every decision is a record `{decision_id, node_id, agent, inputs_hash, rationale, alternatives_considered, timestamp, parent_decision_ids}` so you can trace any line of output back to the requirement.

### 4.5 Controls: retries, fallback, rollback, safe-stop
- **Bounded retries**: per-node max attempts with exponential backoff + jitter; on a retry the agent receives the failure output (test failures, lint errors) as feedback (a self-repair loop).
- **Fallback**: if the LLM provider fails or exceeds budget, fall back to the deterministic provider (§4.9) or a simpler strategy; record the fallback.
- **Rollback**: every write-node runs against a git checkpoint (commit or `git stash`/worktree). On gate failure after retries are exhausted, the workspace is restored to the checkpoint and downstream nodes are marked `BLOCKED`. Test this.
- **Safe-stop**: a kill-switch (`orchestrator stop <run_id>` or a `STOP` file), plus hard budgets (max wall time, max LLM tokens/cost, max files changed, max retries per run). Breach → graceful stop, state persisted, report generated.

### 4.6 Human approval checkpoints (controlled autonomy)
- Define an autonomy policy in `policies/autonomy.yaml`: which actions agents may take alone (read code, write in `service/` and `tests/`, run tests) vs. which need approval (DB migrations, dependency additions, auth/security-related changes, deleting files, changes above N lines, release sign-off).
- Approvals via CLI (`orchestrator approve <run_id> <gate_id> --comment ...` / `reject`), interactive prompt, or `--auto-approve=low,medium` for demos. **High-risk gates can never be auto-approved** unless an explicit `--i-understand-high-risk` flag is passed, and that is logged.
- Every approval/rejection is recorded with approver identity (from env/git config), timestamp, comment, and diff hash.

### 4.7 Policy guardrails (policy-as-code)
Implement a pluggable `PolicyEngine` with policies in `policies/*.yaml`, evaluated at gates:
- **Security**: secret scanning on diffs (regex + entropy), bandit findings at or above medium block, no `eval`/`exec`/`shell=True`, dependency allowlist + pip-audit for known CVEs.
- **Compliance**: no PII in logs (lint rule checking log calls), license allowlist for new deps, data-retention setting present for analytics.
- **Change control**: write-path allowlist (agents may not touch `policies/`, `.github/`, or `orchestrator/` itself), max diff size per node, migrations must be reversible (has a `downgrade`), CHANGELOG entry required for release.
- Policy results are part of the audit trail; violations either block, require approval, or warn, per policy severity.

### 4.8 Observability, audit and reliability metrics
- **Audit log**: append-only JSONL per run, **hash-chained** (each event includes the hash of the previous one) so tampering is detectable; `orchestrator audit verify <run_id>` checks the chain.
- **Tracing**: OpenTelemetry-style spans per node (implement a lightweight span model if you avoid the full OTel dependency; document the choice).
- **Metrics** computed per run and across runs (`orchestrator metrics`): success rate, retry rate, rollback frequency, fallback frequency, MTTR (time from node failure to recovery), per-stage and end-to-end latency, approval wait time, LLM tokens/cost. Exportable as Prometheus text and JSON.
- **Run report**: `runs/<run_id>/report.md` and a self-contained `report.html` with the DAG (Mermaid), timeline, gates, decisions, policy results, approvals, metrics, and artifacts produced.

### 4.9 Agent/LLM layer (must run with NO API key)
- `LLMProvider` interface with:
  - `AnthropicProvider` (used when `ANTHROPIC_API_KEY` is set; model configurable; structured outputs validated with Pydantic; token/cost accounting).
  - `DeterministicProvider` — offline, no network: a rule/template-based implementation for each agent role that produces real, correct artifacts for the three scenarios (it may use curated templates and code-generation logic for the URL shortener domain). This guarantees the demo, CI and tests are reproducible.
  - `RecordReplayProvider` — records real LLM responses to `fixtures/` and replays them in tests.
- Agents: `RequirementAnalyst`, `Architect`, `Planner`, `CodebaseAnalyst`, `Implementer`, `TestEngineer`, `Reviewer` (diff review against acceptance criteria + policies), `DocWriter`, `ReleaseManager`. Each has a narrow role, explicit tool permissions (read/write/run), and a typed input/output contract.
- Agents act on the repo only through a sandboxed `Workspace` tool layer that enforces the path allowlist, runs subprocesses with timeouts, and never uses `shell=True`.

### 4.10 Dynamic re-planning
- Every artifact has a content hash. If an upstream output changes (requirement edited, design revised after review rejection, a human edits a file mid-run), the orchestrator detects the hash change, invalidates affected downstream nodes, asks the `Planner` for a delta plan (add/remove/modify nodes), re-validates the DAG, and requires approval if the re-plan raises the risk class. Show this in the ambiguous scenario and in a test.

## 5. THE THREE SCENARIOS (each must be runnable with one command)

Provide `requirements/` inputs and `make scenario-greenfield`, `make scenario-brownfield`, `make scenario-ambiguous`. Each produces a committed run folder `runs/<scenario>-<timestamp>/` with report, audit log, decisions, and metrics.

1. **Greenfield** — "Build a URL shortener with create, redirect, metadata and health endpoints, persistence and tests." Shows full decomposition, parallel test+docs branches, gates, and release readiness. Result: the working core service.
2. **Brownfield** — "Add click analytics (stats endpoint with daily breakdown, referrers, unique visitors) and link expiry, and fix the bug where custom aliases are case-sensitive in lookups but not in creation." (Seed this bug deliberately in the greenfield output and document that you did.) Shows `CodebaseAnalyst` impact analysis, a migration requiring human approval, a regression test written first, at least one retry with self-repair (inject a realistic failure, e.g. the first implementation fails a test), and a demonstrated rollback path.
3. **Ambiguous** — "Make the shortener enterprise-ready and faster." Shows ambiguity detection, clarification questions, documented assumptions (e.g. interprets as: caching + rate limiting + API-key auth + p95 target), risk classification, a mid-run requirement change that triggers **dynamic re-planning**, and a high-risk gate (auth change) blocked until approval.

Provide `make demo` that runs all three in sequence with `--auto-approve=low,medium` and auto-approves the high-risk gates via the explicit flag, printing a summary table at the end. Total demo time should be under ~3 minutes with the deterministic provider.

## 6. REPOSITORY LAYOUT (target)

```
.
├── orchestrator/          # engine, agents, policies, workspace, observability, cli
├── service/               # URL shortener (app, api, domain, infra, migrations)
├── tests/
│   ├── orchestrator/      # unit + integration (resume, rollback, retries, re-plan, policy, audit chain)
│   └── service/
├── policies/              # autonomy.yaml, security.yaml, compliance.yaml, change_control.yaml
├── requirements/          # greenfield.yaml, brownfield.yaml, ambiguous.txt
├── runs/                  # committed run outputs for the 3 scenarios
├── fixtures/              # record/replay LLM fixtures
├── scripts/               # loadcheck.py, seed data, helpers
├── docs/
├── .github/workflows/ci.yml
├── Dockerfile  docker-compose.yml  Makefile  pyproject.toml  .env.example
└── README.md  CHANGELOG.md
```

## 7. EXECUTION PHASES (do them in order; `make check` green at the end of each)

1. **Scaffold**: git init, pyproject, Makefile targets (`setup`, `lint`, `format`, `typecheck`, `test`, `security`, `check`, `run`, `demo`, `scenario-*`, `docker-up`), CI workflow, pre-commit config, `.env.example`.
2. **Orchestrator core**: models, state store, DAG engine, gates, parallel execution, persistence/resume, audit hash-chain, metrics. Fully unit tested with fake agents.
3. **Controls**: retries/backoff, fallback, rollback via git checkpoints, safe-stop/budgets, approval system, policy engine with all policies. Tests for each.
4. **Agents + providers**: all agent roles, Deterministic/Anthropic/RecordReplay providers, sandboxed Workspace, CodebaseAnalyst static analysis, re-planning.
5. **Greenfield run**: execute scenario 1 through the orchestrator to produce the service core. Then review the generated service yourself and harden it to the standard in §3 (record any manual hardening as a decision so lineage stays honest).
6. **Brownfield run**: scenario 2.
7. **Ambiguous run**: scenario 3.
8. **Reports and observability**: HTML/MD run reports, `orchestrator metrics`, Prometheus export.
9. **Docs** (§8), Docker, `make demo` verified from a clean clone (`git clone . /tmp/verify && cd /tmp/verify && make setup && make demo && make check`).
10. **Self-review** (§9) and final fixes.

## 8. DOCUMENTATION (all required, all accurate)

- **README.md**: one-paragraph pitch; architecture diagram (Mermaid); quickstart in ≤ 5 commands (no API key needed); running with Claude; running each scenario; approving gates; viewing reports; Docker; project layout; links to all docs.
- **docs/ARCHITECTURE.md**: components, orchestration model, state machine diagram, control flow, data/context flow, sequence diagram of one run, how parallelism/sync works, how governance is enforced, scaling path (e.g. swapping SQLite state for Postgres, asyncio for a queue/workers like Temporal, single process to distributed).
- **docs/DECISIONS.md**: ADRs (at least 8): why a custom engine vs LangGraph/Temporal, SQLite state, deterministic provider, hash-chained audit, git-based rollback, redirect status code, code-length math, analytics off hot path, etc. Each with context, decision, alternatives, consequences.
- **docs/SCENARIOS.md**: for each scenario — input, normalized spec, decomposition (DAG), execution timeline, gates/approvals hit, retries/rollbacks/re-plans, validation results, outputs, link to run folder.
- **docs/TESTING.md**: test pyramid, what each suite proves, how to run, coverage numbers (actual, pasted from the run).
- **docs/RISKS.md**: risk register (risk, likelihood, impact, mitigation, residual) covering both the service (abuse/phishing links, enumeration, hot keys, DB growth) and the orchestrator (LLM hallucinated code, prompt injection via repo content, runaway cost, approval fatigue, partial writes).
- **docs/SECURITY.md**: threat model (STRIDE-lite), controls in place.
- **docs/ASSUMPTIONS.md**, **docs/LIMITATIONS.md** (honest — what is simplified, what is not production-ready and what it would take), **docs/PERFORMANCE.md**, **docs/OPERATIONS.md** (config, runbook, metrics/alerts to watch).
- **docs/ENGINEERING_SUMMARY.md**: the final summary required by the assignment — plan and rationale, artifacts produced (with paths), risks/trade-offs/validation, assumptions, limitations, and a mapping table from **every assignment requirement** (Core Requirements 1–8 and each clause of requirement 4: dependency graph, entry/exit gates, sequential + parallel paths with sync, cross-stage context, decision lineage, human approvals, bounded retries, fallback, rollback, safe-stop, security/compliance/change-control policies, audit observability, success rate, retry/rollback frequency, MTTR, end-to-end latency, dynamic re-planning, controlled autonomy) → the file(s), test(s), and run evidence that satisfy it.
- **CHANGELOG.md** (Keep a Changelog format).

## 9. FINAL SELF-REVIEW (do not skip)

Before declaring done:
1. From a clean clone in a temp dir: `make setup && make check && make demo` all succeed with no API key set. Paste the actual summary output into `docs/BUILD_LOG.md`.
2. `docker compose up` starts the service against Postgres + Redis; hit create → redirect → stats with curl and record the results.
3. Grep the repo for `TODO|FIXME|NotImplementedError|pass  # ` and remove or justify each.
4. Walk the requirement mapping table in `ENGINEERING_SUMMARY.md` and confirm every row points to code that exists and a test that passes.
5. Score yourself against the 8 evaluation criteria (1–10 each) in `docs/BUILD_LOG.md`, list the weakest areas, fix what can be fixed within scope, and document the rest in `LIMITATIONS.md`.
6. Final commit. Then print for me: how to run it, where the reports are, the test/coverage numbers, and the top 5 talking points I should be ready to defend in the interview.
