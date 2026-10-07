# Build Log

Chronological record of each build phase: what was done, what broke, how it was fixed.

## Phase 1 — Scaffold

**Done:** `git init`; `pyproject.toml` (hatchling, uv dependency groups, ruff/mypy/pytest/coverage/bandit config);
`Makefile` with `setup lint format typecheck test security check run demo scenario-* docker-up` and helpers;
GitHub Actions CI (`check`, `demo`, `docker` jobs); pre-commit config; `.gitignore`; `.python-version`.
First real module: `orchestrator/hashing.py` (canonical JSON + SHA-256) with tests.

**Broke / fixed:**
- Host had only Python 3.9 and no `uv`. Installed `uv` with `pip install --user uv` and let uv fetch a managed
  CPython 3.12.15. The Makefile locates `uv` outside `PATH` and falls back to `pip`+`venv` when it is absent.
- Host has no global git identity; set a repo-local `user.name`/`user.email`.
- `mypy scripts` failed on an empty directory → Makefile uses `$(wildcard scripts/*.py)`.
- `--cov-report=json:.coverage.json` collided with coverage's `.coverage.*` data-file glob → dropped the JSON report.
- Docker CLI is installed but the daemon is not running on this host (recorded; revisited in Phase 9).

## Phases 2 + 3 — Orchestrator core and controls

**Done:** `models` (typed state machine with an explicit legal-transition table), `state` (SQLite store,
one DB per run), `dag` (validation: acyclic, inputs produced by ancestors, connectivity, no dangling work,
disjoint write sets for parallel nodes; delta application; Mermaid), `audit` (hash-chained JSONL +
verifier), `tracing` (OTel-shaped spans), `context` (versioned, content-hashed artifacts + decision lineage),
`metrics` (per-run and cross-run, JSON + Prometheus), `engine` (asyncio scheduler with concurrency limit,
gates, retries with backoff+jitter and feedback, path-scoped git rollback, approvals, safe-stop, budgets,
plan expansion, dynamic re-planning, resume). Controls: `controls`, `approvals` (autonomy policy),
`workspace` (sandbox + git checkpoints), `gates`, `policy/` (engine + 12 checks) and `policies/*.yaml`.

Phases 2 and 3 were built in one pass because the engine integrates retries/rollback/approvals directly;
`make check` was run green at the combined boundary and they are committed together.

**Broke / fixed:**
- Optional-node failures still blocked descendants → `_fail_node` skips blocking for optional nodes.
- Safe-stop test expected the next node to be `READY`; correct behaviour is that it is never promoted
  (`PENDING`) once a stop is requested. Test fixed, engine unchanged.
- `RUNNING -> RETRYING` and `AWAITING_APPROVAL -> RETRYING` were missing from the transition table
  (non-write retries and post-rejection retries); added.
- LLM usage attribution used a shared "current node" field, which is wrong under parallelism →
  the engine now builds a per-node `LLMClient` whose usage sink is bound to the node id.
- bandit B608 on the allowlisted dynamic `UPDATE` statements → annotated with `nosec B608` + reason.

**Tests:** 97 passing; orchestrator coverage 91%. Includes a real SIGKILL-and-resume test.

## Phase 4 — Agents, providers, blueprints (with the service they generate)

**Done:** nine agents (`RequirementAnalyst`, `CodebaseAnalyst`, `Planner`, `Architect`, `Implementer`,
`TestEngineer`, `DocWriter`, `Reviewer`, `ReleaseManager`) with declared permissions and typed contracts;
`LLMClient` (structured output + fallback + usage accounting); `DeterministicProvider` (rule/catalog/template
based), `AnthropicProvider` (streaming, adaptive thinking, effort, server-side refusal fallback, cost), and
`RecordReplayProvider` (13 fixtures recorded from a full greenfield run; a replay run makes 13 calls with 0
fallbacks). Static analysis (`analysis/codebase.py`: AST import graph, symbol index, FastAPI route table and
SQLAlchemy metadata via sandboxed runtime introspection, test map, impact + blast radius). Run reports, CLI.

The URL-shortener blueprints were authored in a scratch tree (`.work/dev`, one commit per plan node) and turned
into opsets by a generator (new files -> templates, modified files -> minimal unique `replace` ops verified by
re-application). Because the blueprints are only meaningful when the scenarios execute them, phases 4-7 were
iterated together; `service/` in the repo is **synced from orchestrator output** (`make sync-service`) and
`make verify-lineage` / `test_scenarios.py` prove it is byte-identical.

**Broke / fixed (selection, all found by running the system):**
- Foundation template had a trailing blank line -> the `lint_clean` gate failed it 3x -> template fixed.
- `drop_table` in a migration's `downgrade()` escalated the approval to HIGH -> destructive-DDL detection now
  only inspects `upgrade()`.
- The base62 alphabet literal tripped the entropy secret scanner (5.95 bits/char) -> generated code builds it
  from `string` constants instead of suppressing the policy.
- FastAPI 0.142 wraps included routers lazily (`_IncludedRouter`) -> route introspection walks nested routers.
- Checkpoints committed *other* parallel nodes' in-flight files, breaking per-node attribution -> checkpoints
  now only record HEAD; each node commits only its own files; human edits get an explicit commit.
- Retry feedback led with captured log noise -> gates run pytest with `--tb=short -rfE --show-capture=no`.
- **Engine bug:** a requirement change detected while other nodes were running deferred the re-plan but kept
  scheduling nodes from the stale plan -> scheduling is now held as soon as a change is detected; in-flight
  nodes drain; then the re-plan runs. Replace ops whose edit is already present are idempotent no-ops.
- Planner delta re-rooted plan roots (disconnecting the DAG) -> roots normalised to hang off `plan`; a failed
  re-plan now safe-stops instead of crashing.
- Retry metric counted re-plan re-executions as retries -> retries = RETRYING transitions; `reruns_after_replan`
  reported separately.
- **Flaky test caught by the orchestrator's own retry:** the analytics purger's first sweep raced the test's
  inserts -> startup purge now runs synchronously inside `ClickRecorder.start()`.
- Load-check reported p95 = 80 ms at 20 clients; an experiment (one HTTP client per worker) showed the shared
  httpx pool in the *load generator* was the bottleneck (p95 5.9 ms, 3.2k rps) -> tool fixed, target unchanged.
- Reviewer: one missing test id made pytest abort collection for every criterion -> existence is checked first;
  JUnit XML parsing (bandit B314) replaced by parsing pytest's `-rA` summary.
- Workspace lint config was looser than the repo's (`PL` rules) -> aligned; generated `db.py` moved Alembic
  imports to module level.

**Tests:** 284 passing (incl. a slow end-to-end test of all three scenarios with human approvals);
service coverage 97%, orchestrator 92%.

## Phase 5 — Greenfield run (committed evidence)

`make scenario-greenfield` from an empty scaffold: 14/14 nodes, 0 retries, 2 auto-approved medium gates
(migration 0001 + new dependencies; release sign-off). Evidence: `runs/greenfield-*/`.

Manual review of the generated service against §3 found issues that were fixed **in the blueprints** (the
deterministic provider's "prompt"), so lineage stays honest — the committed service is still byte-identical to
orchestrator output. They are listed in ADR-014: entropy false positive on the base62 alphabet, Alembic imports,
the streaming body-limit path (raise inside the app so the handler renders 413), and the seeded alias-case bug
(kept deliberately for the brownfield scenario).

## Phase 6 — Brownfield run (committed evidence)

`make scenario-brownfield APPROVE=--auto-approve=low` so the medium-risk gates require a human:
the migration gate blocked; its diff was reviewed (additive DDL only, reversible) and approved with
`orchestrator approve brownfield-20261007T000441 implement.migration_0002#1`, then `resume`; release sign-off likewise.
The alias branch ran in parallel: the regression test failed first (red), the naive fix failed the targeted
suite (`test_generated_codes_are_case_sensitive`), was rolled back, and the retry self-repaired from that
feedback. Evidence: `runs/brownfield-20261007T000441/` (impact.json, diffs/, report).

## Phase 7 — Ambiguous run (committed evidence)

`make scenario-ambiguous` ("Make the shortener enterprise-ready and faster."): ambiguity score 0.65 > 0.35 ->
clarification gate (documented default assumptions, auto-approved at medium) -> plan v1 (cache, rate limit,
metrics, p95 target; auth deliberately *not* assumed, raised as Q-1). After `design.enterprise`, the scripted
security-review answer amended the requirement; the engine detected the hash change, held scheduling, re-analysed
(spec v2), asked the Planner for a delta (+3 nodes, 4 re-wired, design re-run) and, because risk rose
medium -> high, blocked on `replan-1` until approved. The auth node (high) and release sign-off (high) were each
reviewed and approved via the CLI. p95 6 ms vs 50 ms target. Evidence: `runs/ambiguous-*/`.

**Broke / fixed:** the spec id came out as `REQ-REQUIREMENT` (derived from the run's copy of the requirement
file) -> the requirement artifact now carries the source filename and re-analysis keeps the previous id. The
paused run with the wrong id was discarded and the scenario re-run; replay fixtures were re-recorded because
the analyst prompt changed.

## Phase 8 — Reports and observability

Every run writes `report.md` + self-contained `report.html` (inline SVG DAG and timeline, light/dark theme, no
external scripts), `metrics.json`, `decisions.json`, `traces.jsonl`, `plan.mmd`, `changes.patch`. Cross-run
metrics exported to `runs/metrics.json` and `runs/metrics.prom` via `orchestrator metrics [--format prometheus]`.

**Broke / fixed:** reviewing the ambiguous report showed spec v2 quality problems: the amendment's phrase
"security review" matched the vague term *secure* (ambiguity 1.0), the title was rebuilt from amended text, and an
answered open question did not reduce ambiguity. Analyst rules fixed (narrower pattern, keep the previous title,
credit per resolved question); the ambiguous run was re-executed with the same human approvals and replaces the
earlier evidence. `make demo` reproduces the committed service byte-for-byte.

## Phase 9 — Docs, Docker, clean-clone verification

**Docker (real run, Docker Desktop 29.6.2).** `docker compose up --build -d` built the image (352 MB, multi-stage,
non-root) and started Postgres 16 + Redis 7 + service, all healthy. `bash scripts/smoke.sh` output:

```text
readyz:   {"status":"ready","checks":{"database":"ok","cache:redis":"ok"}}
create:   {"code":"smoke-test","short_url":"http://localhost:8000/smoke-test","target_url":"https://example.com/smoke","is_custom":true,"tags":[],"disabled":false,"created_at":"2026-10-07T07:13:04.565568Z","updated_at":"2026-10-07T07:13:04.565571Z","expires_at":null,"is_expired":false}
redirect: 302 -> https://example.com/smoke
redirect: 302 -> https://example.com/smoke
redirect: 302 -> https://example.com/smoke
stats:    {"code":"smoke-test","window_days":30,"total_clicks":3,"unique_visitors":3,"clicks_by_day":[{"day":"2026-10-07","clicks":3,"unique_visitors":3}],"top_referrers":[{"host":"ref.example","clicks":3}],"top_user_agents":[{"family":"other","clicks":3}]}
metrics:  redirects_total{outcome="ok"} 3.0
```

Inside Postgres afterwards: `alembic_version = 0003`, `count(clicks) = 3`. Note `/Smoke-Test` (mixed case) resolved
the lower-case alias: the brownfield fix working on Postgres. Stack torn down with `docker compose down -v`.
(First `compose up` attempt appeared hung while the base images were pulling; it completed once re-run in the
background. No code change was needed.)

**Docs.** Added TESTING, RISKS, SECURITY, ASSUMPTIONS, LIMITATIONS, OPERATIONS, ENGINEERING_SUMMARY, CHANGELOG,
PERFORMANCE, README, ARCHITECTURE, DECISIONS (17 ADRs), SCENARIOS. Numbers come from `pytest --cov` (285 tests;
service 97%, orchestrator 91%), the committed runs' `state.db`, and `make loadcheck` (p95 6.08 ms, 3,338 req/s).
`scripts/verify_mapping.py` (`make verify-mapping`, also in CI) checks that every file, test and symbol named in
ENGINEERING_SUMMARY exists: 161 references; it caught a wrong reference on its first run (fixed).

**Clean clone** (`git clone . /tmp/verify`, `ANTHROPIC_API_KEY` unset): `make setup` OK; `make check` ALL GREEN
(285 passed in 63 s, both coverage gates, bandit, pip-audit); `make demo` exit 0; `make verify-lineage` and
`make verify-mapping` OK. The demo summary table printed truncated cells when stdout was not a TTY (80 columns),
so the CLI now renders at 120 columns when piped and with compact headers. Final table:

```text
                                      Demo summary (38s)                                       
┏━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━┳━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━┳━━━━━━━┓
┃ scenario   ┃ state     ┃ nodes ┃ retry ┃ rollbk ┃ appr(auto) ┃ replan ┃ blocks ┃ s  ┃ audit ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━╇━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━╇━━━━━━━┩
│ greenfield │ SUCCEEDED │ 14/14 │ 0     │ 0      │ 2(2)       │ 0      │ 0      │ 12 │ ok    │
│ brownfield │ SUCCEEDED │ 14/14 │ 1     │ 1      │ 2(2)       │ 0      │ 0      │ 12 │ ok    │
│ ambiguous  │ SUCCEEDED │ 17/17 │ 0     │ 0      │ 6(6)       │ 1      │ 0      │ 15 │ ok    │
└────────────┴───────────┴───────┴───────┴────────┴────────────┴────────┴────────┴────┴───────┘
committed service/ matches orchestrator output: yes
```

## Phase 10 — Self-review

1. **Clean clone**: done above (setup, check, demo with no key).
2. **docker compose**: done above; create -> redirect -> stats recorded.
3. **Markers** (`grep -rnE 'TODO|FIXME|NotImplementedError|pass  # '`): only hit is `orchestrator/agents/governance.py`,
   where `TODO|FIXME|XXX|HACK` is the *pattern the Reviewer searches for* in generated code: legitimate.
4. **Requirement mapping**: `make verify-mapping` -> 161 references, 0 problems.
5. **Self-score** (1-10, honest):

| Criterion | Score | Why / what is weak |
|---|---|---|
| Effectiveness of agentic orchestration | 8 | Full governed loop proven end-to-end with real gates; **but** the "agents" default to a deterministic provider, so the intelligence is curated, and the Anthropic path was never run live |
| Architecture quality | 8 | Clean separation, persisted state machine, explicit transition table, typed contracts; engine.py is large (1.1k lines) and would benefit from splitting (scheduler / approvals / re-plan) |
| Depth of decomposition | 8 | Real DAGs of 14-17 nodes with disjoint write sets, parallel branches, joins, re-plan deltas; node granularity is catalog-driven rather than learned |
| Realism of outputs | 8 | Service is genuinely good (97% cov, mypy strict, Postgres+Redis verified, p95 measured); some outputs (docs text, design rationale) are templated |
| Validation / risk rigor | 9 | Independent gates, evidence-based reviewer, tamper-evident audit, risk register, fault injection, kill-and-resume test; CI workflow never executed |
| Clarity / defensibility of decisions | 9 | 17 ADRs with alternatives, honest LIMITATIONS, BUILD_LOG records what broke |
| Core engineering principles | 8 | Fail-closed policies, least privilege, idempotency, bounded resources, graceful degradation; shared-workspace isolation is the main compromise (ADR-015) |
| Judgment | 8 | Fixed generators not outputs (ADR-014), refused to relax the p95 target when the tool was at fault, separated scripted from organic behaviour in the docs |

**Weakest areas and what was done:** (a) unrun CI and live-Claude path: cannot be fixed without a remote/API key, documented in
LIMITATIONS 2 and 11; (b) scripted self-repair/mid-run change: documented in ADR-012 and ASSUMPTIONS 15-16;
(c) unauthenticated approver identity: documented (LIMITATIONS 4); (d) fallback/safe-stop paths are not exercised
in the committed runs (covered by tests, listed in the mapping table).
