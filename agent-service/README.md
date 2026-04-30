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
| `COPILOT_AUDIT_SALT` | yes | `dev-insecure-audit-salt` | HMAC salt for patient-ID hashing in audit log (PR 2) |
| `OAUTH_CLIENT_ID` | yes | `""` | Confidential OAuth2 client ID provisioned in OpenEMR (PR 5); see [OpenEMR OAuth2 client registration](#openemr-oauth2-client-registration) |
| `OAUTH_CLIENT_SECRET` | yes | `""` | Client secret paired with `OAUTH_CLIENT_ID` (PR 5) |
| `OAUTH_TOKEN_URL` | yes | `http://localhost:8300/oauth2/default/token` | OpenEMR OAuth2 token endpoint (PR 5) — note this is the *site root* path, not under `/apis/default/` |

In `production` (`APP_ENV=production`), missing required variables raise
`ConfigError` at startup so a misconfigured deploy fails loudly instead of
silently running with insecure defaults.

## Local quality gate

The pre-merge gate runs locally — there is no GitLab CI / GitHub Actions /
Railway auto-deploy in MVP scope (see `../TASKS.md`):

```bash
make check    # ruff + mypy + pytest (offline; integration tests skipped)
```

Integration tests live under `tests/integration/` and are tagged with the
`integration` pytest marker. They are skipped by default and only run when
`OPENEMR_INTEGRATION=1` is set in the environment, plus the test-specific
env vars listed in the test file's docstring (e.g. PR 5's OAuth + FHIR
fetch needs `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_TOKEN_URL`,
`FHIR_BASE_URL`, `OPENEMR_TEST_PATIENT_ID`):

```bash
OPENEMR_INTEGRATION=1 OAUTH_CLIENT_ID=... OAUTH_CLIENT_SECRET=... \
  uv run pytest tests/integration
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

## OpenEMR OAuth2 client registration

The agent service authenticates to OpenEMR's FHIR endpoint as a backend
service using `client_credentials` against `OAUTH_TOKEN_URL`. This is the
second of the two trust layers described in `../ARCHITECTURE.md` §4 —
per-clinician trust is established by the JWT minted in PR 4, and per-tool
RBAC is enforced at the agent's tool layer (PR 7) against the JWT claims.
The OAuth token is intentionally coarse, scoped to the read surface the
agent will ever need.

One-time setup (per environment):

1. Log in to OpenEMR as admin and open **Admin → System → API Clients**
   (the registered OAuth2 / FHIR clients management page).
2. Click **Register New App** and fill in:
   - **Name:** `Clinical Co-Pilot Agent Service`
   - **Application Type:** `confidential` (server-side, holds a secret)
   - **Grant Types:** `client_credentials` only
   - **Scopes:** check the eight `system/*` read scopes —
     `Patient`, `Condition`, `MedicationRequest`, `MedicationStatement`,
     `AllergyIntolerance`, `Observation`, `Encounter`, `DocumentReference`
     (each as `.read`)
3. After registration, OpenEMR shows the `client_id` and `client_secret`
   exactly once. Copy both into the agent service's environment as
   `OAUTH_CLIENT_ID` and `OAUTH_CLIENT_SECRET`. There is no recovery if
   the secret is lost — re-register the client and rotate.
4. In OpenEMR globals, ensure **OAuth2 Server Enabled** is on and the
   **Token Lifetime** matches the cache assumption in
   `auth/oauth_client.py` (1 hour is the OpenEMR default and the value
   the cache is sized for).
5. Verify with the integration test:

   ```bash
   OPENEMR_INTEGRATION=1 \
     OAUTH_CLIENT_ID=... OAUTH_CLIENT_SECRET=... \
     OAUTH_TOKEN_URL=http://localhost:8300/oauth2/default/token \
     FHIR_BASE_URL=http://localhost:8300/apis/default/fhir \
     OPENEMR_TEST_PATIENT_ID=<fhir-uuid-from-patient_data> \
     uv run pytest tests/integration/test_oauth_client.py
   ```

   The test fetches a token, asserts the cache returns the same token on
   a second call, then `GET`s `Patient/$id` with the bearer.

In production (Railway), set `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, and
`OAUTH_TOKEN_URL` via the dashboard — never check them in. Rotation is
unilateral on the agent side: re-register a client in OpenEMR, update the
two env vars, and redeploy. There's no shared-key window to coordinate
because the secret is held only by OpenEMR's auth server, not by the PHP
gateway.

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
├── alembic.ini
├── src/clinical_copilot/
│   ├── __init__.py
│   ├── main.py             # FastAPI app, /healthz, /readyz
│   ├── config.py           # env-driven Settings
│   ├── logging.py          # structlog configuration
│   ├── auth/               # PR 4 (JWT verifier) + PR 5 (OAuth2 client)
│   ├── audit/              # PR 2 (fail-closed audit-log writer)
│   └── db/                 # PR 2 (SQLAlchemy models, Alembic migrations)
└── tests/
    ├── unit/               # offline; default `make check`
    └── integration/        # gated by OPENEMR_INTEGRATION=1
```

Subdirectories for `tools/`, `orchestrator/`, `verification/`,
`discrepancy/`, `data/`, `observability/` arrive in their respective PRs
(see `../TASKS.md`).
