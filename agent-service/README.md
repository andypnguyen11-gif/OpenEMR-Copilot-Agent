# Clinical Co-Pilot — Agent Service

Python/FastAPI sidecar for the Clinical Co-Pilot AI agent. Runs alongside the
OpenEMR PHP app on Railway. This package is the runtime that owns the
orchestrator, verification middleware, discrepancy engine, tools, and audit
log; the OpenEMR PHP gateway only signs JWTs and proxies HTTP.

For overall project intent see `../PRD.md` and `../ARCHITECTURE.md`. For the
PR-by-PR build plan see `../TASKS.md`.

## Stack

- Python 3.12, [`uv`](https://docs.astral.sh/uv/) for env / lock / build
- FastAPI + uvicorn
- structlog for structured logging
- Anthropic SDK for LLM calls (PR 9+); LangSmith for tracing (PR 20)
- SQLAlchemy + Alembic against Postgres on Railway (SQLite locally)

## Quickstart

```bash
cd agent-service
uv sync --dev
uv run uvicorn clinical_copilot.main:app --reload --port 8000
# in another shell:
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
```

## Environment Variables

| Var | Required in prod | Default (dev) | Purpose |
|---|---|---|---|
| `APP_ENV` | no | `development` | `development` / `test` / `production`; flips required-var enforcement |
| `LOG_LEVEL` | no | `INFO` | structlog filtering level |
| `COPILOT_HMAC_SECRET` | yes | `dev-insecure-hmac-secret` | HS256 secret shared with PHP gateway (PR 4); rotate together with `copilot_jwt_secret` in OpenEMR globals — see [Shared HMAC secret rotation](#shared-hmac-secret-rotation) |
| `ANTHROPIC_API_KEY` | yes | `""` | Anthropic API key for orchestrator (PR 9+) |
| `FHIR_BASE_URL` | yes | `http://localhost:8300/apis/default/fhir` | OpenEMR FHIR R4 base (PR 5+) |
| `DATABASE_URL` | yes | `sqlite:///./agent.db` | Postgres DSN for traces / eval / audit (PR 2+) |

In `production` (`APP_ENV=production`), missing required variables raise
`ConfigError` at startup so a misconfigured deploy fails loudly instead of
silently running with insecure defaults.

## Local quality gate

The pre-merge gate runs locally — there is no GitLab CI / GitHub Actions /
Railway auto-deploy in MVP scope (see `../TASKS.md`):

```bash
make check    # ruff + mypy + pytest
```

Later PRs add:

- `make eval` — full eval suite, must pass before deploy (PR 24)
- `make deploy` — refuses `railway up` unless eval is green (PR 24)

## Manual deploy

```bash
make check
railway up --service agent-service
```

Production env vars are configured via the Railway dashboard, not via
checked-in config.

## Shared HMAC secret rotation

The PR 4 boundary token is HS256, so the same byte string must live on both
sides:

- PHP gateway (OpenEMR): `copilot_jwt_secret` in OpenEMR globals.
- Agent service (this repo): `COPILOT_HMAC_SECRET` env var (Railway dashboard
  in production).

Rotation is a four-step sequence — both sides briefly run with both old and
new secrets queued, so no in-flight request is dropped:

1. Generate a new secret: `openssl rand -hex 32` (32 bytes / 64 hex chars).
2. Set the new secret on the agent service first (Railway redeploy),
   keeping the *old* `COPILOT_HMAC_SECRET` as the verification fallback for
   the 5-minute JWT lifetime window.
3. Once the agent service is verified healthy on the new secret, update
   `copilot_jwt_secret` in OpenEMR globals — every newly-minted token now
   uses the new secret.
4. After at least 5 minutes (the JWT lifetime), drop the old secret from
   the agent service.

A leak of either side's secret means rotating both immediately. Tokens
already minted with the leaked secret remain valid until `exp`; the JWT
lifetime is intentionally short (5 min) to bound that exposure
(`ARCHITECTURE §4`).

## Layout

```
agent-service/
├── pyproject.toml
├── uv.lock
├── Dockerfile
├── railway.toml
├── Makefile
├── README.md
├── src/clinical_copilot/
│   ├── __init__.py
│   ├── main.py            # FastAPI app, /healthz, /readyz
│   ├── config.py          # env-driven Settings
│   └── logging.py         # structlog configuration
└── tests/
    └── unit/
        └── test_health.py
```

Subdirectories for `auth/`, `tools/`, `orchestrator/`, `verification/`,
`discrepancy/`, `data/`, `observability/`, `audit/`, `db/` arrive in their
respective PRs (see `../TASKS.md`).
