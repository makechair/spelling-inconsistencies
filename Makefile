# Convenience targets. `make dev` runs the whole stack locally with fake data.
PY := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: help venv install migrate test lint dev-collector dev-api dev seed clean

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

venv: ## Create the virtualenv
	python3 -m venv .venv

install: venv ## Install the package with dev extras
	$(PIP) install -q -e ".[dev]"

migrate: ## Apply database migrations
	$(PY) -m usstocks.db.migrate

test: ## Run the test suite
	$(PY) -m pytest -q

lint: ## Lint and format-check
	.venv/bin/ruff check src tests
	.venv/bin/ruff format --check src tests

seed: ## Add a few symbols to the watchlist
	$(PY) scripts/seed.py AAPL MSFT NVDA

dev-collector: ## Run the collector against the mock feed
	USSTOCKS_PRIMARY_SOURCE=mock USSTOCKS_AUTH_MODE=disabled $(PY) -m usstocks.collector

dev-api: ## Run the API with auth disabled (loopback only)
	USSTOCKS_PRIMARY_SOURCE=mock USSTOCKS_AUTH_MODE=disabled $(PY) -m usstocks.api

dev: ## Run collector + API together with fake data
	./scripts/dev.sh

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__ data/*.db*
