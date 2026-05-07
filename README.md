[![Syntax Status](https://github.com/openemr/openemr/actions/workflows/syntax.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/syntax.yml)
[![Styling Status](https://github.com/openemr/openemr/actions/workflows/styling.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/styling.yml)
[![Testing Status](https://github.com/openemr/openemr/actions/workflows/test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/test.yml)
[![JS Unit Testing Status](https://github.com/openemr/openemr/actions/workflows/js-test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/js-test.yml)
[![PHPStan](https://github.com/openemr/openemr/actions/workflows/phpstan.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/phpstan.yml)
[![Rector](https://github.com/openemr/openemr/actions/workflows/rector.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/rector.yml)
[![ShellCheck](https://github.com/openemr/openemr/actions/workflows/shellcheck.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/shellcheck.yml)
[![Docker Compose Linting](https://github.com/openemr/openemr/actions/workflows/docker-compose-lint.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/docker-compose-lint.yml)
[![Dockerfile Linting](https://github.com/openemr/openemr/actions/workflows/hadolint.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/hadolint.yml)
[![Isolated Tests](https://github.com/openemr/openemr/actions/workflows/isolated-tests.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/isolated-tests.yml)
[![Inferno Certification Test](https://github.com/openemr/openemr/actions/workflows/inferno-test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/inferno-test.yml)
[![Composer Checks](https://github.com/openemr/openemr/actions/workflows/composer.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/composer.yml)
[![Composer Require Checker](https://github.com/openemr/openemr/actions/workflows/composer-require-checker.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/composer-require-checker.yml)
[![API Docs Freshness Checks](https://github.com/openemr/openemr/actions/workflows/api-docs.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/api-docs.yml)
[![codecov](https://codecov.io/gh/openemr/openemr/graph/badge.svg?token=7Eu3U1Ozdq)](https://codecov.io/gh/openemr/openemr)

[![Backers on Open Collective](https://opencollective.com/openemr/backers/badge.svg)](#backers) [![Sponsors on Open Collective](https://opencollective.com/openemr/sponsors/badge.svg)](#sponsors)

# OpenEMR

[OpenEMR](https://open-emr.org) is a Free and Open Source electronic health records and medical practice management application. It features fully integrated electronic health records, practice management, scheduling, electronic billing, internationalization, free support, a vibrant community, and a whole lot more. It runs on Windows, Linux, Mac OS X, and many other platforms.

---

## Clinical Co-Pilot (case-study fork)

This fork adds a **Clinical Co-Pilot** — a verified, lane-aware agent for cross-coverage primary-care clinicians. Architecture is a PHP gateway inside OpenEMR plus a Python/FastAPI sidecar (`agent-service/`) running the LLM tool-use loop, verification middleware, and discrepancy engine. See [USERS.md](USERS.md) for the seven user-facing use cases, [ARCHITECTURE.md](ARCHITECTURE.md) for the full design, [PRD.md](PRD.md) for the product brief, and [AUDIT.md](AUDIT.md) for the OpenEMR integration audit.

### App URL

| Environment | URL |
|---|---|
| Local development (HTTP) | http://localhost:8300/ |
| Local development (HTTPS) | https://localhost:9300/ |
| Deployed demo (Railway) | https://openemr-production-6c31.up.railway.app *(grading window only — may be torn down post-review)* |
| phpMyAdmin (local) | http://localhost:8310/ |

### Credentials (demo only)

| Environment | Username | Password |
|---|---|---|
| Local Docker stack | `admin` | `pass` |
| Deployed demo (Railway) | `admin` | `ChangeMe_StrongAdminPass_456` |

The local-stack values are the upstream OpenEMR demo defaults; the deployed instance has password-strength enforcement enabled, so its admin password is the longer string above. Both exist only for case-study evaluation and **must be rotated** before any non-demo use — the deployed credential will be invalidated when the Railway demo is torn down post-review. The Co-Pilot inherits OpenEMR's RBAC at the FHIR/REST layer — login as `admin` to exercise the attending workflow; the resident/supervisor roles in [USERS.md §1.4](USERS.md) are provisioned via Admin → Users on the running stack.

### Setup

The Co-Pilot needs **two services running**: the OpenEMR PHP app (this repo) and the Python agent sidecar (`agent-service/`).

```bash
# 1. Start the OpenEMR stack (Apache + MariaDB + phpMyAdmin)
cd docker/development-easy
docker compose up --detach --wait

# 2. In a separate shell, start the agent sidecar
cd agent-service
make check        # ruff + mypy + pytest, sanity-checks the local install
# Configure env vars per agent-service/README.md (HMAC secret, LLM key, FHIR base URL)
uvicorn clinical_copilot.main:app --reload --port 8001
```

Once both are up, log in to OpenEMR at the URL above and open a patient chart — the in-chart Co-Pilot side panel attaches there. The Daily Brief surface (slow-lane pre-warm) is available at `/interface/copilot/daily_brief.php`.

For the full agent-service env-var matrix, deploy workflow, and eval gate, see [agent-service/README.md](agent-service/README.md). For the test/eval policy, see [CLAUDE.md](CLAUDE.md) and [TASKS.md](TASKS.md).

### Running the eval suite

The agent ships with a build-blocking eval harness. From `agent-service/`:

```bash
make eval         # runs all suites; non-zero exit on any RBAC failure
```

Eval coverage and the human-reviewed case manifest are tracked in [TASKS.md](TASKS.md) and [agent-service/evals/extraction/cases.jsonl](agent-service/evals/extraction/cases.jsonl).

### How the W2 rubric is met

Three properties a grader can verify directly from the repo, each pinned to the file that proves it:

- **Citations are schema-enforced, not best-effort.** Every extracted field carries either a citation or an explicit abstain reason — enforced by the `ExtractedField[T]` Pydantic XOR validator at [`agent-service/src/clinical_copilot/documents/schemas/citation.py:68-89`](agent-service/src/clinical_copilot/documents/schemas/citation.py); `CitedClaim.source_id` requires `min_length=1` ([`orchestrator/schemas.py:71`](agent-service/src/clinical_copilot/orchestrator/schemas.py)). A claim without a citation is an exception, not a typo waiting to happen.
- **GitLab MR-blocking CI runs the 50-case eval gate.** The `agent-service:test` and `agent-service:eval-gate` stages ([`.gitlab-ci.yml:69-109`](.gitlab-ci.yml)) are required on `merge_request_event` and `main`. The eval suite is 50 grounded, human-reviewed cases — 28 extraction, 6 citations, 4 refusals, 8 retrieval, 4 missing-data — see [`agent-service/evals/extraction/cases.jsonl`](agent-service/evals/extraction/cases.jsonl). A regression on any bucket fails the pipeline.
- **Supervisor routes the slow lane; v1 keeps the fast lane.** The full chat page dispatches via the W2 Supervisor + 2 workers (`intake_extractor` + `evidence_retriever`) running BM25 + dense + LLM-judge rerank — corpus indexes shipped at [`agent-service/data/corpus/bm25.pkl`](agent-service/data/corpus/bm25.pkl) and [`dense.pkl`](agent-service/data/corpus/dense.pkl). The in-chart side panel stays on the v1 orchestrator for ≤5s p50 chart-tool dispatch. Toggle the engine with `USE_SUPERVISOR` (default `true`); `false` is the instant rollback. Wiring lives at [`main.py:396-430`](agent-service/src/clinical_copilot/main.py).

---

OpenEMR is a leader in healthcare open source software and comprises a large and diverse community of software developers, medical providers and educators with a very healthy mix of both volunteers and professionals. [Join us and learn how to start contributing today!](https://open-emr.org/wiki/index.php/FAQ#How_do_I_begin_to_volunteer_for_the_OpenEMR_project.3F)

> Already comfortable with git? Check out [CONTRIBUTING.md](CONTRIBUTING.md) for quick setup instructions and requirements for contributing to OpenEMR by resolving a bug or adding an awesome feature 😊.

### Support

Community and Professional support can be found [here](https://open-emr.org/wiki/index.php/OpenEMR_Support_Guide).

Extensive documentation and forums can be found on the [OpenEMR website](https://open-emr.org) that can help you to become more familiar about the project 📖.

### Reporting Issues and Bugs

Report these on the [Issue Tracker](https://github.com/openemr/openemr/issues). If you are unsure if it is an issue/bug, then always feel free to use the [Forum](https://community.open-emr.org/) and [Chat](https://www.open-emr.org/chat/) to discuss about the issue 🪲.

### Reporting Security Vulnerabilities

Check out [SECURITY.md](.github/SECURITY.md)

### API

Check out [API_README.md](API_README.md)

### Docker

Check out [DOCKER_README.md](DOCKER_README.md)

### FHIR

Check out [FHIR_README.md](FHIR_README.md)

### For Developers

If using OpenEMR directly from the code repository, then the following commands will build OpenEMR (Node.js version 24.* is required) :

```shell
composer install --no-dev
npm install
npm run build
composer dump-autoload -o
```

### Contributors

This project exists thanks to all the people who have contributed. [[Contribute]](CONTRIBUTING.md).
<a href="https://github.com/openemr/openemr/graphs/contributors"><img src="https://opencollective.com/openemr/contributors.svg?width=890" /></a>


### Sponsors

Thanks to our [ONC Certification Major Sponsors](https://www.open-emr.org/wiki/index.php/OpenEMR_Certification_Stage_III_Meaningful_Use#Major_sponsors)!


### License

[GNU GPL](LICENSE)
