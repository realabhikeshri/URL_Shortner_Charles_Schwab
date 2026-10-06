# Agentic SDLC orchestrator + URL shortener.
# Every target works without an ANTHROPIC_API_KEY (deterministic provider is the default).

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Locate uv: PATH first, then the usual install locations. Fall back to pip+venv.
UV := $(shell command -v uv 2>/dev/null || ls $(HOME)/.local/bin/uv $(HOME)/.cargo/bin/uv $(HOME)/Library/Python/*/bin/uv 2>/dev/null | head -1)
ifeq ($(UV),)
  RUN := .venv/bin/
  SETUP_CMD := python3 -m venv .venv && .venv/bin/pip install -q -e . && .venv/bin/pip install -q $$(python3 -c "import tomllib;print(' '.join(tomllib.load(open('pyproject.toml','rb'))['dependency-groups']['dev']))")
else
  RUN := $(UV) run --frozen --
  SETUP_CMD := $(UV) sync --frozen
endif

PY := $(RUN) python
SRC := orchestrator service tests scripts
SERVICE_COV_MIN := 85
ORCH_COV_MIN := 80
APPROVE := --auto-approve=low,medium

.PHONY: help setup lint format typecheck test test-fast security check run demo \
        scenario-greenfield scenario-brownfield scenario-ambiguous sync-service verify-lineage \
        loadcheck openapi docker-up docker-down clean

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n",$$1,$$2}'

setup: ## Create the virtualenv and install all dependencies
	$(SETUP_CMD)
	@echo "setup complete"

lint: ## Ruff lint + format check
	$(RUN) ruff check $(SRC)
	$(RUN) ruff format --check $(SRC)

format: ## Auto-format and fix lint where safe
	$(RUN) ruff format $(SRC)
	$(RUN) ruff check --fix $(SRC)

typecheck: ## mypy --strict on orchestrator/ and service/
	$(RUN) mypy orchestrator service $(wildcard scripts/*.py)

test: ## Full test suite with coverage gates (service >= 85%, orchestrator >= 80%)
	$(RUN) pytest --cov --cov-report=term -q
	$(RUN) coverage report --include='service/*' --fail-under=$(SERVICE_COV_MIN) > /dev/null && echo "service coverage >= $(SERVICE_COV_MIN)%: OK"
	$(RUN) coverage report --include='orchestrator/*' --fail-under=$(ORCH_COV_MIN) > /dev/null && echo "orchestrator coverage >= $(ORCH_COV_MIN)%: OK"

test-fast: ## Tests without the slow end-to-end scenario runs
	$(RUN) pytest -q -m "not slow"

security: ## bandit (medium+ blocks) and pip-audit
	$(RUN) bandit -q -r orchestrator service $(wildcard scripts/*.py) -ll -c pyproject.toml
	$(RUN) pip-audit --skip-editable --progress-spinner off

check: lint typecheck test security ## Everything CI runs
	@echo "make check: ALL GREEN"

run: ## Run the URL shortener locally on :8000 (SQLite)
	$(RUN) uvicorn service.app:create_app --factory --reload --port 8000

scenario-greenfield: ## Scenario 1: build the core service through the orchestrator
	$(RUN) orchestrator scenario greenfield $(APPROVE) $(EXTRA)

scenario-brownfield: ## Scenario 2: analytics + expiry + alias bug fix (pauses for migration approval unless approved)
	$(RUN) orchestrator scenario brownfield $(APPROVE) $(EXTRA)

scenario-ambiguous: ## Scenario 3: "enterprise-ready and faster" (blocks on the high-risk auth gate)
	$(RUN) orchestrator scenario ambiguous $(APPROVE) $(EXTRA)

demo: ## All three scenarios end-to-end with auto-approval (incl. explicit high-risk flag)
	$(RUN) orchestrator demo

sync-service: ## Copy the orchestrator-produced service from the last demo workspace into the repo
	$(RUN) orchestrator sync --from .work/demo

verify-lineage: ## Prove committed service/ is byte-identical to what the orchestrator produces
	$(RUN) orchestrator verify-lineage --workspace .work/demo

loadcheck: ## Measure p50/p95 redirect latency and update docs/PERFORMANCE.md
	$(PY) scripts/loadcheck.py --requests 2000 --concurrency 20 --write docs/PERFORMANCE.md

openapi: ## Export docs/openapi.json
	$(PY) -c "import json; from service.app import create_app; print(json.dumps(create_app().openapi(), indent=2, sort_keys=True))" > docs/openapi.json

docker-up: ## Service + Postgres + Redis via Docker Compose
	docker compose up --build -d
	@echo "service on http://localhost:8000"

docker-down: ## Stop compose stack
	docker compose down -v

clean: ## Remove caches and scratch workspaces
	rm -rf .work .pytest_cache .mypy_cache .ruff_cache .coverage .coverage.json htmlcov
