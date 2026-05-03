.PHONY: help install check lint format type test run eval deploy discrepancy-cache-clear clean

UV ?= uv

# `make eval` knobs. AGENT_BASE_URL points at the deployed agent;
# COPILOT_HMAC_SECRET must match the deployed service so harness-minted
# JWTs verify. Both can be overridden on the command line:
#   make eval AGENT_BASE_URL=https://agent.example.com
AGENT_BASE_URL ?= http://localhost:8000

# `make deploy` invokes `railway up --service $(RAILWAY_SERVICE)`. Override
# when deploying a non-default Railway service (e.g. a staging instance):
#   make deploy RAILWAY_SERVICE=agent-service-staging
RAILWAY_SERVICE ?= agent-service

help:
	@echo "agent-service make targets:"
	@echo "  install   sync dependencies (incl. dev) into .venv"
	@echo "  check     run lint + type-check + tests (the PR 1 quality gate)"
	@echo "  lint      ruff check"
	@echo "  format    ruff format + ruff check --fix"
	@echo "  type      mypy on src + tests"
	@echo "  test      pytest"
	@echo "  eval      run check + eval suite against AGENT_BASE_URL (PR 24 gate)"
	@echo "  deploy    require eval to pass, then railway up --service \$$(RAILWAY_SERVICE)"
	@echo "  run       uvicorn dev server with --reload on :8000"
	@echo "  discrepancy-cache-clear   wipe durable discrepancy_cache rows (restart agent or call invalidate after — in-memory tier is separate)"

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

# Pre-deploy gate (PR 24). Order is intentional:
#   1. `check` — lint + type + unit/integration tests run first so a syntax
#      regression or broken unit test fails fast without spending tokens
#      driving the LLM through the eval suite.
#   2. eval runner — drives every case in tests/eval/cases/ against the
#      deployed agent at AGENT_BASE_URL. Two gates: RBAC suite must be 100%
#      and overall pass rate must be ≥90% (configurable via --min-pass-rate).
# Either gate failing exits non-zero, which Make then propagates to block
# the `deploy` target below.
eval: check
	AGENT_BASE_URL=$(AGENT_BASE_URL) $(UV) run python -m tests.eval.runner

# Manual deploy. Make's prerequisite mechanism enforces the gate: if `eval`
# exits non-zero, `railway up` is never invoked. Production env vars (the
# real COPILOT_HMAC_SECRET, OAUTH_*, ANTHROPIC_API_KEY, etc.) live in the
# Railway dashboard, not in repo config. Deploy is intentionally manual —
# there is no CI/CD that calls railway up on its own.
deploy: eval
	@echo "==> eval gate green; deploying $(RAILWAY_SERVICE) to Railway"
	railway up --service $(RAILWAY_SERVICE)

run:
	$(UV) run uvicorn clinical_copilot.main:app --reload --host 0.0.0.0 --port 8000

# Wipes the durable tier of DiscrepancyCache (the discrepancy_cache table).
# The in-memory tier survives in any running agent process for up to 30
# minutes after this runs — restart the service or call
# POST /api/agent/internal/invalidate/{patient_id} to drop both tiers.
discrepancy-cache-clear:
	$(UV) run python -m clinical_copilot.scripts.clear_discrepancy_cache

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
