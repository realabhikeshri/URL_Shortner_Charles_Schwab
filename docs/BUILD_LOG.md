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
