# Copilot E2E (Playwright)

End-to-end tests for the Clinical Co-Pilot chat surface. Drives a real
browser through OpenEMR → PHP gateway → agent-service → Anthropic, so
you need the full stack running locally before invoking these.

## Prerequisites

1. **OpenEMR dev-easy stack up:**
   ```bash
   docker compose -f docker/development-easy/docker-compose.yml up --detach --wait
   ```
   App must be reachable at `http://localhost:8300`.

2. **agent-service running on host:8000** with a matching HMAC secret
   and an Anthropic key. The dev fallback baked into
   `sites/default/config.php` is `dev-insecure-shared-secret-32-bytes!`,
   so the simplest local setup is:
   ```bash
   cd agent-service
   # .env is gitignored — paste your ANTHROPIC_API_KEY there
   set -a; source .env; set +a
   uv run uvicorn clinical_copilot.main:app --reload --host 0.0.0.0 --port 8000
   ```

3. **Browser binary** (one-time):
   ```bash
   npm --prefix e2e exec playwright install chromium
   ```

## Run

From repo root:

```bash
npm --prefix e2e test            # headless, all scenarios
npm --prefix e2e run test:ui     # Playwright UI mode
npm --prefix e2e run report      # open last HTML report
```

A single scenario:

```bash
npm --prefix e2e exec playwright test multi-turn-continuity
```

## Layout

```
e2e/
├── playwright.config.ts          # baseURL=http://localhost:8300, workers=1
├── specs/copilot-sessions.md     # human-readable test plan (PR9)
├── tests/
│   ├── auth.setup.ts             # logs in once, persists state to .auth/
│   └── copilot/
│       ├── _helpers.ts           # `ask()`, `selectPatient()`, `captureSessionDelete()`
│       ├── multi-turn-continuity.spec.ts
│       ├── patient-switch-drops-history.spec.ts
│       ├── clear-chat-drops-history.spec.ts
│       └── session-id-roundtrip.spec.ts
└── .claude/agents/               # Playwright Test Agent definitions (planner/generator/healer)
```

## Authoring new scenarios via the Test Agents

`init-agents` (already run; see `.claude/agents/`) installed three
sub-agents that author tests through the `playwright-test` MCP server.
To use them in a new Claude Code session:

1. Start Claude Code from this directory so `.mcp.json` is picked up:
   ```bash
   cd e2e && claude
   ```
2. Have the planner draft a plan into `specs/`:
   > Use the `playwright-test-planner` agent to create a plan for
   > \<feature you want to cover\>.
3. Have the generator turn each scenario into a spec file:
   > Use the `playwright-test-generator` agent to author each scenario
   > in `specs/<plan>.md`.
4. Run the new specs:
   ```bash
   npm test
   ```

## Stack assumptions baked into specs

- Patient 101 has two conditions (T2DM 2019-04-12 + HTN 2018-11-03).
- Patient 102 has one condition (T2DM 2017-06-22).
- `dev-insecure-shared-secret-32-bytes!` is the shared HMAC secret
  between the PHP gateway (config.php fallback) and agent-service
  (`COPILOT_HMAC_SECRET`).

If any of those drift, the assertions need a coordinated update.
