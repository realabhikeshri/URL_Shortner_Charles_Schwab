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
`workspace` (sandbox + git checkpoints), `gates`, `policy/` (engine + 13 checks) and `policies/*.yaml`.

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
