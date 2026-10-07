# Testing

**285 tests, all passing** (`make test`, ~55 s). Coverage (branch): **service 97%** (gate ≥ 85%),
**orchestrator 91%** (gate ≥ 80%), combined 92%. Numbers are from the last full run; `make test` enforces both gates.

## Pyramid

| Layer | Count | Where | What it proves |
|---|---|---|---|
| Unit: pure logic | ~110 | `tests/service/test_{codes,urls,cache,ratelimit,metrics}.py`, `tests/orchestrator/test_{dag,audit,hashing,state,approvals}.py` | base62, alias and URL validation, LRU/TTL cache, token bucket (memory and Redis via fakeredis+Lua), DAG validation, audit chain, autonomy rules |
| Property-based (hypothesis) | 7 | `tests/service/test_properties.py` | base62 round-trip and ordering, URL normalisation idempotence, dangerous schemes never accepted, alias canonical form |
| Integration (real components) | ~170 | `tests/service/test_{api_links,integration,enterprise,auth,auth_security,expiry,stats_api,analytics_unit,health}.py`, `tests/orchestrator/test_{engine,controls_workspace,gates,policies,workspace,providers,analysis_agents,cli_report}.py` | HTTP API against a migrated SQLite DB (httpx ASGI), concurrency and idempotency races, collision retry, migrations up/down, engine with fake agents, real git rollback, real ruff/pytest/mypy gates, policy checks, static analysis on a blueprint-built service |
| Crash/resume | 1 | `tests/orchestrator/test_resume.py` | SIGKILLs a run mid-node in a subprocess, resumes: completed work is not redone, the attempt counter survives, chain intact |
| End-to-end (marked `slow`) | 1 | `tests/orchestrator/test_scenarios.py` | all three scenarios through the real engine, agents, gates and policies, with human approvals; asserts retry/rollback/re-plan/approval facts, chains, and that committed `service/` equals orchestrator output |

## What specific suites prove (orchestrator requirements)

| Requirement | Test |
|---|---|
| Parallelism + join sync, concurrency limit | `test_engine::test_parallel_branches_run_concurrently_and_join_waits`, `::test_concurrency_limit_is_respected` |
| Bounded retries with feedback, exhaustion → BLOCKED | `test_engine::test_retry_feeds_gate_failure_back_and_recovers`, `::test_exhausted_retries_fail_node_and_block_downstream`, `::test_retry_budget_caps_total_retries` |
| Rollback restores checkpoint, parallel-safe | `test_controls_workspace::test_failed_write_attempts_are_rolled_back_and_downstream_blocked`, `test_workspace::test_git_checkpoint_commit_and_path_scoped_rollback` |
| Safe-stop (kill switch, budgets) | `test_engine::test_kill_switch_safe_stops_and_resume_completes`, `::test_wall_time_budget_breach_safe_stops` |
| Approvals, high risk never auto | `test_engine::test_auto_approve_levels_and_high_risk_flag`, `test_approvals::test_auto_approve_policy` |
| Approval integrity (diff drift) | `test_controls_workspace::test_high_risk_change_pauses_and_tampering_after_request_is_detected` |
| Policy engine (all policies) | `test_policies.py` (secrets, bandit, dangerous calls, PII in logs, deps, license, retention, reversible migrations, diff size, write paths, changelog; fail-closed) |
| Audit tamper detection | `test_audit.py` (edit, delete, reorder, re-hashed forgery, garbage) and `test_cli_report::test_status_runs_metrics_and_audit` |
| Dynamic re-planning | `test_controls_workspace::test_human_edit_mid_run_invalidates_downstream`, `::test_requirement_change_replans_and_risk_increase_needs_approval`, `test_providers::test_deterministic_delta_adds_auth_and_reruns_design`, e2e scenario |
| Provider fallback, budget fallback | `test_providers::test_client_falls_back_*`, `::test_client_uses_fallback_when_token_budget_is_exhausted` |
| Reviewer cannot approve unverified criteria | `test_analysis_agents::test_reviewer_cannot_approve_unverified_criteria` |
| Sandbox: escapes, denylist, secrets not leaked to subprocess | `test_workspace.py` |

## Service-specific highlights

- Alias bug: `test_regression_alias_case.py` (written red-first by the brownfield run) and
  `test_integration::test_generated_codes_are_case_sensitive` (the guard the naive fix tripped).
- No PII stored: `test_analytics_unit::test_no_raw_pii_is_persisted` also scans the raw SQLite file bytes.
- Analytics never breaks redirects: `test_stats_api::test_analytics_failure_never_breaks_redirect` (enqueue failure and DB failure).
- Cache: served-from-cache is proven by making DB lookups raise; invalidation on update/delete; negative cache cleared on create; cache outage degrades to the DB.
- Auth: 401/403, malformed/non-ASCII keys, cross-owner idempotency, per-key rate limits, keys never in logs.

## Beyond the unit suite

- **Static gates on generated code** (run by the engine on every node): `compiles`, ruff lint+format, targeted tests,
  `coverage:85`, `mypy` on the service, `migrations_apply` (upgrade/downgrade/upgrade), `tests_fail_first`.
- **Load sanity**: `scripts/loadcheck.py` (docs/PERFORMANCE.md).
- **Compose smoke test**: `scripts/smoke.sh` against Postgres 16 + Redis 7 (output in BUILD_LOG.md).
- **CI** (`.github/workflows/ci.yml`): lint, mypy --strict, tests with both coverage gates, bandit + pip-audit,
  audit-chain verification of every committed run, `make demo` + `make verify-lineage`, compose smoke test.

## Not covered (see LIMITATIONS)

No browser/UI tests (no UI); no multi-replica or Postgres-specific latency tests in CI; the Anthropic provider is
tested against a fake SDK client (request shape, parsing, error mapping), not against the live API; mutation
testing was not run.

## Running

```bash
make test         # everything with coverage gates
make test-fast    # skips the slow end-to-end scenario test
uv run pytest tests/service -q
```
