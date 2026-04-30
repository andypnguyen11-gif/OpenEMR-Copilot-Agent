.PHONY: help install check lint format type test run eval clean

UV ?= uv

# `make eval` knobs. AGENT_BASE_URL points at the deployed agent;
# COPILOT_HMAC_SECRET must match the deployed service so harness-minted
# JWTs verify. Both can be overridden on the command line:
#   make eval AGENT_BASE_URL=https://agent.example.com
AGENT_BASE_URL ?= http://localhost:8000

help:
	@echo "agent-service make targets:"
	@echo "  install   sync dependencies (incl. dev) into .venv"
	@echo "  check     run lint + type-check + tests (the PR 1 quality gate)"
	@echo "  lint      ruff check"
	@echo "  format    ruff format + ruff check --fix"
	@echo "  type      mypy on src + tests"
	@echo "  test      pytest"
	@echo "  eval      run the 6-case eval suite against AGENT_BASE_URL"
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

# Hard gate: any rbac_bypass case failure exits non-zero (PRD §13).
# Other category failures are reported but do not fail the build.
eval:
	AGENT_BASE_URL=$(AGENT_BASE_URL) $(UV) run python -m tests.eval.runner

run:
	$(UV) run uvicorn clinical_copilot.main:app --reload --host 0.0.0.0 --port 8000

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
