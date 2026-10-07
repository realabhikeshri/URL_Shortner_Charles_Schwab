# Scenarios

Three scenarios, each runnable with one command, each with committed evidence under `runs/`. They chain:
brownfield starts from greenfield's workspace and ambiguous from brownfield's. `make demo` runs all three in one
workspace in about 45 s. All numbers below come from the committed runs' `state.db` / `report.md`.

| | Greenfield | Brownfield | Ambiguous |
|---|---|---|---|
| Run | [`greenfield-20261007T002309`](../runs/greenfield-20261007T002309/report.md) | [`brownfield-20261007T002321`](../runs/brownfield-20261007T002321/report.md) | [`ambiguous-20261007T002343`](../runs/ambiguous-20261007T002343/report.md) |
| Command | `make scenario-greenfield` | `make scenario-brownfield APPROVE=--auto-approve=low` | `make scenario-ambiguous` |
| Nodes | 14/14 | 14/14 | 17/17 (plan v2) |
| Attempts / retries / rollbacks | 14 / 0 / 0 | 15 / 1 / 1 (MTTR 0.94 s) | 18 / 0 / 0 (1 re-run after re-plan) |
| Gates evaluated (failed) | 44 (0) | 47 (1) | 55 (0) |
| Policy evaluations | 147, 0 violations | 158, 0 violations | 191, 0 violations |
| Approvals | 2 auto (medium) | 2 human (medium) | 3 auto (medium) + 3 human (high) |
| Wall time (excl. approval wait) | 11.7 s | 14.5 s | 17.5 s |

---

## 1. Greenfield: "Build a URL shortener…"

**Input:** [`requirements/greenfield.yaml`](../requirements/greenfield.yaml) (5 functional, 5 non-functional, 8
Given/When/Then criteria, each naming the test that verifies it).

**Normalised spec:** `REQ-GF-001`, capabilities `[core]`, ambiguity 0.0 (explicit criteria), risk medium.
**Impact analysis:** empty workspace → blast radius 0, "greenfield build".

**Decomposition** (topological generations, i.e. what can run at the same time):

```
intake → analysis.impact → plan → design.architecture → implement.foundation
  → { implement.codes ∥ implement.urls }
  → implement.links_api
  → { test.integration ∥ test.properties ∥ document.architecture }
  → test.coverage → review.code → release.readiness
```

**Gates:** foundation `compiles, lint_clean, migrations_apply, targeted_tests`; implement nodes `compiles,
lint_clean, targeted_tests`; `test.coverage` → `coverage:85` (97%); review `review_approved` (8/8 criteria
verified by running the tests named in the spec); release `typecheck` (mypy --strict), `changelog_entry`,
`release_ready`.
**Approvals:** `implement.foundation#1` (migration 0001 + new runtime dependencies → medium) and release sign-off
(medium); both auto-approved under `--auto-approve=low,medium`.
**Outputs:** the core service (settings, async DB layer, migration 0001, logging, error envelope, middleware,
health probes, base62 codes, URL validation, links API, redirect), 82 tests, design doc, README, API reference,
OpenAPI, CHANGELOG.
**Deliberately seeded defect:** aliases are canonicalised to lower case at creation but looked up exactly
(`implement.links_api`), so `/MyLink` 404s. It is documented in the opset and ADR-014.

## 2. Brownfield: analytics, expiry, alias case bug

**Input:** [`requirements/brownfield.yaml`](../requirements/brownfield.yaml).
**Spec:** `REQ-BF-002`, capabilities `[schema_v2, alias_case_fix, expiry, analytics]` (`schema_v2` is pulled in
by `requires`), risk medium.

**Impact analysis (`runs/brownfield-*/impact.json`):** AST import graph + live FastAPI route table + SQLAlchemy
metadata. Seeds from term overlap with the symbol/route index plus `service/models.py` (a migration is needed:
`expires_at` is not a column). Result: 9 of 23 modules impacted, all 7 routes, table `links`, 6 test modules to
update, blast radius ≈ 0.7, recommendations: reversible migration with approval, re-run impacted tests, keep
contracts backward compatible.

**Decomposition:**

```
design.analytics → { test.regression_alias → implement.alias_fix }
                 ∥ { implement.migration_0002 → implement.expiry → implement.analytics → test.analytics }
→ document.analytics (after both branches) → test.coverage → review.code → release.readiness
```

**Regression test first:** `test.regression_alias` has exit gate `tests_fail_first`: it passes only if the new
test *fails with an assertion* against current code ("2 failed").

**Retry with self-repair + rollback:** attempt 1 of `implement.alias_fix` lower-cases every lookup (a realistic
over-broad fix, ADR-012). The regression test now passes, but `targeted_tests` fails
`test_generated_codes_are_case_sensitive` (generated base62 codes are case-sensitive). The engine rolls back
`service/repository.py` to the checkpoint (`ROLLED_BACK`), waits with jittered backoff, and passes the pytest
failure summary as feedback. Attempt 2 recognises the failing test and produces the correct fix: exact match
first, then the canonical alias restricted to `is_custom`. Gates pass. Both diffs are in `diffs/`.

**Human approval of the migration:** run with `--auto-approve=low`, so `implement.migration_0002#1` (additive DDL →
medium; destructive DDL in `upgrade()` would be high) **blocked**. The diff was reviewed and approved:

```
orchestrator approve brownfield-20261007T002321 'implement.migration_0002#1' \
  --comment "Reviewed: additive only (nullable column, new table, indexes); downgrade reverses all; ..."
orchestrator resume brownfield-20261007T002321
```

Release sign-off was approved the same way after inspecting its checklist (6/6 criteria verified, coverage 97%,
migrations round-trip, changelog). Policies evaluated on every node included `CMP-003-analytics-retention`
(passes because `analytics_retention_days` exists) and `CMP-001-no-pii-in-logs`.

**Outputs:** migration 0002, `ClickRecorder` + stats endpoint, expiry, alias fix, 107 service tests.

## 3. Ambiguous: "Make the shortener enterprise-ready and faster."

**Input:** [`requirements/ambiguous.txt`](../requirements/ambiguous.txt) (one sentence) plus a scripted mid-run
event, [`ambiguous.events.yaml`](../requirements/ambiguous.events.yaml).

**Ambiguity detection:** "enterprise-ready" (high) and "faster" (medium) → score 0.65 > 0.35. Questions and
default assumptions are written to `clarification.md`:

- enterprise-ready → caching + per-client rate limiting + Prometheus metrics. **Authentication is not assumed**:
  it changes the public API contract, so it is raised as open question Q-1.
- faster → p95 redirect latency < 50 ms with 20 concurrent clients on a laptop, measured by
  `scripts/loadcheck.py` and enforced as a gate.

Non-interactive mode applies these assumptions and raises a **clarification approval** (`intake#1`, medium,
auto-approved under the demo policy). With `--interactive`, the human's answer is appended to the spec.

**Plan v1:** `design.enterprise → { cache_backend ∥ rate_limiter ∥ metrics ∥ loadcheck } → enterprise_wiring →
{ test.performance ∥ document } → test.coverage → review → release`. Risk medium.

**Mid-run requirement change → dynamic re-planning:** after `design.enterprise`, the scripted event appends the
security review's answer to Q-1 ("enterprise-ready requires API-key authentication on all write endpoints…") to
the run's requirement of record. The engine:

1. detects the requirement hash change and **holds scheduling** (no node of the stale plan starts);
2. re-analyses → spec **v2**: adds `api_key_auth` (+ hidden `auth_schema`), resolves Q-1, ambiguity 0.65 → 0.5,
   risk → **high**;
3. asks the Planner for a delta: **add** `implement.auth_migration`, `implement.api_key_auth`,
   `test.auth_security`; **modify** `test.performance`, `document.enterprise`, `test.coverage`,
   `release.readiness`; **re-run** `design.enterprise`;
4. validates plan v2 (`plan.v2.mmd`) and, because run risk rose medium → high, **blocks on `replan-1`**.

**High-risk gates blocked until approval** (each via `orchestrator approve` + `resume`, with comments recorded):
`replan-1` (high), `implement.api_key_auth#1` (high, `security_sensitive` trigger on `service/auth.py`), and
release sign-off (high, since run risk is high). Medium gates (migration 0003, new dependencies `redis` and
`prometheus-client` with allowlist + license + pip-audit checks) were auto-approved.

**Validation:** the `test.performance` node ran the load check against a real uvicorn process: p95 ≈ 6 ms vs
the 50 ms target (`p95_under_target` PASS). Coverage 97%; 8/8 criteria verified (`AC-P1` by the performance
gate); typecheck clean.
**Outputs:** cache (memory/Redis), token-bucket limiter (memory/Redis Lua), Prometheus metrics, API-key auth with
owner-only modification and a key CLI, migration 0003, load-check script, 155 service tests.

---

## Reproducing

```bash
make scenario-greenfield
make scenario-brownfield APPROVE=--auto-approve=low   # then approve + resume as printed
make scenario-ambiguous                               # approve replan-1, auth, release as printed
make demo                                             # all three, auto-approved incl. the explicit high-risk flag
make verify-lineage                                   # committed service/ == orchestrator output
```
