.PHONY: help install check lint format type test run clean

UV ?= uv

help:
	@echo "agent-service make targets:"
	@echo "  install   sync dependencies (incl. dev) into .venv"
	@echo "  check     run lint + type-check + tests (the PR 1 quality gate)"
	@echo "  lint      ruff check"
	@echo "  format    ruff format + ruff check --fix"
	@echo "  type      mypy on src + tests"
	@echo "  test      pytest"
	@echo "  run       uvicorn dev server with --reload on :8000"

install:
	$(UV) sync --all-extras --dev

check: lint type test

lint:
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

format:
	$(UV) run ruff format src tests
	$(UV) run ruff check --fix src tests

type:
	$(UV) run mypy src tests

test:
	$(UV) run pytest

run:
	$(UV) run uvicorn clinical_copilot.main:app --reload --host 0.0.0.0 --port 8000

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
