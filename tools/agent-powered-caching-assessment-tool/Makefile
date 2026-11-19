# pg-cache-advisor
#
# Local development only. Deployment targets are intentionally absent —
# deployment is out of scope.

VENV := .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

.PHONY: help setup test lint typecheck security assess assess-negative api clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

setup: ## Create the venv and install dependencies
	python3 -m venv $(VENV)
	$(PIP) install -q -e ".[dev]"
	@echo "Ready. Try: make assess"

test: ## Run the test suite
	$(PY) -m pytest tests/ -q

lint: ## Lint with ruff
	$(PY) -m ruff check src/ tests/ scripts/

typecheck: ## Type-check with mypy
	$(PY) -m mypy src/

security: ## Static security scan
	$(PY) -m bandit -r src/ -q

assess: ## Run the positive fixture, deterministic bands only (no model)
	$(PY) scripts/run_assessment_v2.py --file tests/fixtures/strong_candidate_collection.json --no-llm --no-price

assess-negative: ## Run the negative fixture, deterministic bands only (no model)
	$(PY) scripts/run_assessment_v2.py --file tests/fixtures/no_candidate_collection.json --no-llm --no-price

api: ## Start the API on loopback only (no authentication)
	ARTIFACT_ROOT=./artifacts $(PY) -m src.api.main

clean: ## Remove caches and local artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
