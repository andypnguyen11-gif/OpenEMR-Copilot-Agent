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
| `OAUTH_CLIENT_ID` | yes | `""` | Confidential OAuth2 client ID provisioned in OpenEMR (PR 5.5); see [OpenEMR OAuth2 client registration](#openemr-oauth2-client-registration) |
| `OAUTH_PRIVATE_KEY_PEM` | yes | `""` | Multi-line PKCS8 PEM RSA private key paired with the JWK registered at `OAUTH_CLIENT_ID` (PR 5.5) |
| `OAUTH_KEY_ID` | yes | `""` | `kid` of the registered JWK; embedded in every minted JWT-bearer assertion (PR 5.5) |
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
env vars listed in the test file's docstring (e.g. PR 5.5's JWT-bearer
OAuth + FHIR fetch needs `OAUTH_CLIENT_ID`, `OAUTH_PRIVATE_KEY_PEM`,
`OAUTH_KEY_ID`, `OAUTH_TOKEN_URL`, `FHIR_BASE_URL`, `OPENEMR_TEST_PATIENT_ID`):

```bash
OPENEMR_INTEGRATION=1 OAUTH_CLIENT_ID=... \
  OAUTH_PRIVATE_KEY_PEM="$(cat agent-service-private-key.pem)" \
  OAUTH_KEY_ID=... \
  uv run pytest tests/integration
```

Later PRs add:

- `make eval` — full eval suite, must pass before deploy (PR 24)
- `make deploy` — refuses `railway up` unless eval is green (PR 24)

## Discrepancy cache

`DiscrepancyCache` is two-tier (PR 14): an in-process dict + a `discrepancy_cache`
table. Reads go in-process → durable → recompute, with a 30-minute TTL. After
editing fixtures or seed data the cache will shadow the change for up to that
TTL window. To force a refresh:

```bash
make discrepancy-cache-clear   # wipes the durable tier (Postgres or SQLite)
```

The in-process tier survives in any running agent — restart the service
(`make run`) or call `POST /api/agent/internal/invalidate/{patient_id}` per
affected pid to drop both tiers atomically.

## Manual deploy

```bash
make check
railway up --service agent-service
```

Production env vars are configured via the Railway dashboard, not via
checked-in config.

## OpenEMR OAuth2 client registration

The agent service authenticates to OpenEMR's FHIR endpoint as a backend
service using `client_credentials` against `OAUTH_TOKEN_URL`, with the
JWT-bearer `client_assertion` form of client authentication (RFC 7523 §2.2
+ SMART Backend Services). OpenEMR's confidential-client OAuth2 endpoint
hard-rejects any registration with `system/*` scopes that lacks a registered
JWK (`src/RestControllers/AuthorizationController.php` lines 312–317), so
static-secret authentication is not an option against a real instance.
Algorithm is RS384 — the only one OpenEMR's signer accepts
(`src/Common/Auth/OpenIDConnect/JWT/RsaSha384Signer.php:42`).

This is the second of the two trust layers described in
`../ARCHITECTURE.md` §4 — per-clinician trust is established by the JWT
minted in PR 4, and per-tool RBAC is enforced at the agent's tool layer
(PR 7) against the JWT claims. The OAuth token is intentionally coarse,
scoped to the read surface the agent will ever need.

One-time setup (per environment):

1. Generate the keypair locally:

   ```bash
   uv run python scripts/generate_client_keypair.py \
     --kid agent-service-2026-05 \
     --out agent-service-private-key.pem \
     > public-jwk.json
   ```

   The script writes the PKCS8 PEM private key with mode `0600` and
   prints the JWK to stdout. Pick a `kid` that is stable per key
   *generation* — rotating the key gets a new `kid`.

2. In OpenEMR globals → **Connectors**, enable (and save):
   - **Enable OpenEMR Standard FHIR REST API**
   - **Enable OpenEMR FHIR System Scopes** ← without this, OpenEMR
     silently strips `system/*` scopes at registration
   - **Enable OpenEMR Standard REST API**

3. Register the client by POSTing the public JWK:

   ```bash
   uv run python scripts/register_oauth_client.py \
     --openemr-url https://openemr.example.com \
     --jwk public-jwk.json
   ```

   The registration response includes `client_id`. Save it as
   `OAUTH_CLIENT_ID` on the agent service. The script also writes the
   full response to `oauth-registration.json` for reference; treat that
   file as sensitive (it carries the registration-access token).

4. Set `OAUTH_PRIVATE_KEY_PEM` to the contents of
   `agent-service-private-key.pem` and `OAUTH_KEY_ID` to the same `kid`
   used in step 1.

5. Verify with the integration test:

   ```bash
   OPENEMR_INTEGRATION=1 \
     OAUTH_CLIENT_ID=... \
     OAUTH_PRIVATE_KEY_PEM="$(cat agent-service-private-key.pem)" \
     OAUTH_KEY_ID=agent-service-2026-05 \
     OAUTH_TOKEN_URL=http://localhost:8300/oauth2/default/token \
     FHIR_BASE_URL=http://localhost:8300/apis/default/fhir \
     OPENEMR_TEST_PATIENT_ID=<fhir-uuid-from-patient_data> \
     uv run pytest tests/integration/test_oauth_client.py
   ```

   The test mints a fresh assertion, fetches a token, asserts the cache
   returns the same token on a second call, then `GET`s `Patient/$id`
   with the bearer.

In production (Railway), set `OAUTH_CLIENT_ID`, `OAUTH_PRIVATE_KEY_PEM`,
`OAUTH_KEY_ID`, and `OAUTH_TOKEN_URL` via the dashboard — never check them
in. Rotation is unilateral on the agent side: generate a new keypair, run
`register_oauth_client.py` to register the new public JWK, update the env
vars, and redeploy. The old client can be revoked via the API Clients
admin page once the new one is verified working.

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
