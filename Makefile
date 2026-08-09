# Maine Property Video Factory — task runner (§22)
PY ?= .venv/bin/python
UV ?= uv

.PHONY: help setup install test lint fmt typecheck doctor init run preview serve clean check

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "  %-12s %s\n",$$1,$$2}'

setup: ## create the venv and install dev dependencies
	$(UV) venv
	$(UV) pip install --python $(PY) -e ".[dev]"

install: ## install every optional extra (acquisition, local-ai, speech, publish)
	$(UV) pip install --python $(PY) -e ".[dev,acquisition,local-ai,speech,publish]"
	$(PY) -m playwright install chromium

test: ## run the offline test suite
	$(PY) -m pytest -q

lint: ## ruff check
	$(PY) -m ruff check src tests

fmt: ## ruff format + fix
	$(PY) -m ruff format src tests
	$(PY) -m ruff check --fix src tests

typecheck: ## mypy
	$(PY) -m mypy

check: lint typecheck test ## everything CI runs

doctor: ## verify runtime dependencies
	$(PY) -m mpvf.cli.main doctor

init: ## scaffold data dirs, database and starter config
	$(PY) -m mpvf.cli.main init

run: ## TEMPLATE=slug make run
	$(PY) -m mpvf.cli.main run $(TEMPLATE)

preview: ## TEMPLATE=slug make preview — plan the render without encoding
	$(PY) -m mpvf.cli.main run $(TEMPLATE) --dry-run

serve: ## start the local dashboard
	$(PY) -m mpvf.cli.main serve

clean: ## remove caches and build artifacts
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache
