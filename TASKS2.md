# TASKS2.md — Clinical Co-Pilot Week 2 Build Plan

**Status:** Working task list, derived from PRD2.md + W2_ARCHITECTURE.md
**Last updated:** 2026-05-04
**Owner:** [you]

This is an MR-by-MR build checklist for the Week 2 Clinical Co-Pilot scope.
Each top-level item is one GitLab merge request. Sub-tasks are the work
inside that MR. Files marked **NEW** are created in the MR; files marked
**EDIT** are existing files modified in the MR.

MR identifiers (`W2-01` … `W2-12`) match the test matrix in
`PRD2.md §15.1` exactly so the cross-references between PRD2,
W2_ARCHITECTURE.md, and this file stay coherent. **MR identifiers are not
build order.** See §"Recommended sequencing" below.

---

## How to use this document

- **One MR per W2-XX block.** Subtasks are the work inside that MR.
  When you push the MR, copy the block's `Acceptance` lines into the MR
  description so the reviewer / grader can scan them.
- **Files: NEW vs EDIT.** Every block lists files in two groups. NEW
  files come into existence in this MR; EDIT files already exist
  (typically v1 modules being extended).
- **Checkbox discipline.** Tick a box when the item is *committed and
  passing on the branch*, not when it's "in progress." Stale checked
  boxes hide regressions.
- **Tests ship in the same MR.** Per CLAUDE.md test policy: the test
  files listed under each MR are required for that MR to merge. Never
  defer tests to a follow-up MR.
- **Commit messages do not reference W2-XX.** Identifiers in this file
  are for tracking; commits stand on their own. The MR description is
  the right place to link back to the W2-XX block.
- **Bypass policy.** A `git push --no-verify` for a Week 2 MR requires
  the eval-gate run artifact in the MR description (PRD2 §8 / Appendix
  A.2). Reviewers refuse bypassed MRs without that artifact.

---

## Recommended sequencing

The W2-XX numbers match PRD2 §15.1, but the **build order is different**
because two MRs are test-first per CLAUDE.md ("test-first is required for
high-risk behaviors: JWT verification, audit-log fail-closed path, RBAC /
scope enforcement, PHI redaction to LangSmith"). Build in this order:

| Order | MR | Why this order |
|---|---|---|
| 1 | **W2-01** Schemas + abstain enum | Foundation — every later MR imports these types |
| 2 | **W2-12** PHI redaction | Test-first; lands before any module that emits LangSmith spans containing extracted text |
| 3 | **W2-09** RBAC scope tests for documents | Test-first; gate documents access before any extraction MR ships |
| 4 | **W2-02** OpenEMR category + event hook + state-poll endpoint | Bridge from existing Documents subsystem to the agent service |
| 5 | **W2-05** Citation OCR check (strict + degraded path) | Required by the extractor; lands before extraction MRs so they can call it |
| 6 | **W2-03** lab_pdf VLM extraction worker | First end-to-end document → extracted_facts path |
| 7 | **W2-04** intake_form extraction | Same module, second schema |
| 8 | **W2-10** Abstention rendering + chart-side summary card | UX surfaces (Documents-view canonical, chart-panel summary-only secondary) |
| 9 | **W2-06** Evidence retriever (corpus + RAG) | Independent from W2-03/04; parallel-safe but typically lands after |
| 10 | **W2-07** Supervisor + planner + critic | Composes the prior workers; needs W2-03/06 in place |
| 11 | **W2-08** Reconciliation extension | Needs extracted facts (W2-03) and existing chart tools |
| 12 | **W2-11** Pre-push eval hook + Makefile + flake policy + README + COST.md | Wraps the suite; all 50 cases must be authored by here |

Parallel-safe pairs once foundation is in: (W2-05, W2-09), (W2-06, anything
in 4–8). Hard blockers stay sequential: nothing past W2-04 can be
*completed* without the extractor producing real facts.

---

## Cross-cutting acceptance gates

These apply to every Week 2 MR. The MR description should affirm them.

- [ ] All new Python files start with `from __future__ import annotations`
      and pass `mypy --strict` (project default).
- [ ] No new entries to the PHPStan baseline (CLAUDE.md rule). If you must
      add one, the underlying type error is fixed in the same MR or the
      addition is justified in the MR description.
- [ ] No `mixed`, no inline `@var` casts, no `@phpstan-ignore` in new PHP
      code (CLAUDE.md type-system rules).
- [ ] No raw document text, no patient identifiers, no chart-fact strings
      sent to LangSmith. Spans pass the W2-12 redaction layer.
- [ ] Every new public function has a return type. Every parameter has a
      native type declaration. PSR-3 logging context, never string
      concatenation (CLAUDE.md).
- [ ] `make import-check` passes (W2-01 introduces the contract; every
      later MR keeps it green).
- [ ] `make copilot-eval-fast` passes locally before push (available
      from W2-11 onward; earlier MRs use `make copilot-eval BUCKET=<name>`
      skeleton from W2-01 to verify their own bucket).

---

## Repository layout (target)

This is the file layout the plan builds toward. Items marked **NEW** are
created during Week 2; everything else is v1 territory that we extend.

```
agent-service/
├── Makefile                                          [EDIT — copilot-eval, import-check targets]
├── .importlinter                                     [NEW — tool-vs-RAG contract]
├── runbooks/                                         [NEW]
│   ├── corpus-rebuild.md
│   ├── eval-baseline-refresh.md
│   ├── extraction-queue-drain.md
│   └── phi-leak-response.md
├── corpus/                                           [NEW]
│   ├── sources/
│   │   ├── uspstf/
│   │   ├── cdc/
│   │   ├── nih/
│   │   └── LICENSES.md
│   └── (chunks/, generated, gitignored)
├── data/corpus/
│   ├── bm25.pkl                                      [generated, gitignored]
│   └── manifest.json                                 [generated, gitignored]
├── src/clinical_copilot/
│   ├── schemas/
│   │   ├── __init__.py
│   │   └── abstain.py                                [NEW — runtime AbstainReason; shared with v1]
│   ├── orchestrator/
│   │   ├── agent.py                                  [v1 — wrapped by nodes/v1_single.py]
│   │   ├── lanes.py                                  [v1]
│   │   ├── llm_gateway.py                            [v1]
│   │   ├── cross_patient_guard.py                    [v1]
│   │   ├── sessions.py                               [v1]
│   │   ├── schemas.py                                [v1]
│   │   ├── prompts/                                  [v1, +new W2 prompts below]
│   │   │   ├── planner.txt                           [NEW]
│   │   │   └── critic.txt                            [NEW]
│   │   ├── state.py                                  [NEW — TurnState TypedDict]
│   │   ├── supervisor.py                             [NEW — StateGraph build + compile]
│   │   ├── planner.py                                [NEW — planner node body]
│   │   ├── critic.py                                 [NEW — critic node body]
│   │   ├── edges.py                                  [NEW — route_after_planner / _critic]
│   │   └── nodes/                                    [NEW]
│   │       ├── __init__.py
│   │       ├── chart_tools.py                        [wraps v1 tool registry]
│   │       ├── intake_extractor.py                   [wraps tools/extracted_facts.py]
│   │       ├── evidence_retriever.py                 [wraps corpus/retriever.py]
│   │       └── v1_single.py                          [wraps v1 agent.py for §4.5]
│   ├── documents/                                    [NEW package]
│   │   ├── __init__.py
│   │   ├── fetcher.py
│   │   ├── extractor.py
│   │   ├── queue.py
│   │   ├── merge.py
│   │   ├── preprocess.py                             [optional, if VLM-fail fallback chosen]
│   │   └── schemas/
│   │       ├── __init__.py
│   │       ├── citation.py
│   │       ├── lab_pdf.py
│   │       └── intake_form.py
│   ├── corpus/                                       [NEW package]
│   │   ├── __init__.py
│   │   ├── chunker.py
│   │   ├── index.py
│   │   ├── retriever.py
│   │   └── scrub.py
│   ├── tools/
│   │   ├── extracted_facts.py                        [NEW — query-time read of agent-db]
│   │   ├── guideline_evidence.py                     [NEW — evidence_retriever as a tool]
│   │   ├── extraction_summary.py                     [NEW — chart-panel rollup aggregation]
│   │   └── ...                                       [v1 unchanged]
│   ├── verification/
│   │   ├── citation_check.py                         [EDIT — extend for OCR + corpus]
│   │   ├── abstention.py                             [EDIT — add W2 reasons]
│   │   ├── field_check.py                            [v1]
│   │   └── middleware.py                             [v1]
│   ├── observability/
│   │   ├── langsmith_client.py                       [EDIT — deny-by-default redaction layer]
│   │   └── latency.py                                [NEW — per-stage histogram]
│   ├── evals/                                        [NEW package]
│   │   ├── __init__.py                               [W2-01]
│   │   ├── case_state.py                             [W2-01 — JUDGE_INCONCLUSIVE]
│   │   ├── harness.py                                [W2-01 skeleton; W2-11 extends]
│   │   ├── rubrics.py                                [W2-01 registry; later MRs register theirs]
│   │   ├── judge.py                                  [W2-11]
│   │   ├── budget.py                                 [W2-11]
│   │   ├── results.py                                [W2-11]
│   │   └── w2/
│   │       ├── cases.jsonl
│   │       ├── judge.yaml                            [W2-11]
│   │       ├── fixtures/                             [hand-built lab PDFs + intake forms]
│   │       ├── corpus_freeze/                        [snapshot for reproducibility]
│   │       └── results/                              [committed Markdown + JSON]
│   ├── discrepancy/
│   │   ├── rules.py                                  [EDIT — extend for extracted facts]
│   │   └── ...                                       [v1]
│   ├── db/
│   │   └── alembic/versions/
│   │       ├── XXXX_extraction_jobs.py               [NEW]
│   │       ├── XXXX_extracted_facts.py               [NEW]
│   │       └── XXXX_corpus_index.py                  [NEW]
│   └── main.py                                       [EDIT — new endpoints]
└── tests/
    ├── unit/
    │   ├── schemas/                                  [NEW]
    │   ├── documents/                                [NEW]
    │   ├── corpus/                                   [NEW]
    │   ├── orchestrator/                             [EDIT — new tests for planner/supervisor/critic]
    │   ├── verification/                             [EDIT]
    │   └── observability/                            [NEW]
    ├── integration/
    │   ├── ingestion_flow_test.py                    [NEW]
    │   ├── synthesis_flow_test.py                    [NEW]
    │   ├── rbac_documents_test.py                    [NEW]
    │   └── phi_redaction_test.py                     [NEW]
    └── eval/w2/                                      [NEW — bucket subdirs]

# OpenEMR fork (PHP side)
src/CoPilot/
├── Gateway/
│   └── HmacSigner.php                                [v1]
├── Documents/                                        [NEW package]
│   ├── CategoryProvider.php
│   ├── EnqueuePayloadBuilder.php
│   └── Routes.php                                    [W2-02: GET /agent/documents/{id};
│                                                       W2-10: GET /agent/documents/summary]
src/Events/
└── CoPilotDocumentUploadedListener.php               [NEW]
sql/migrations/
└── XXXX_copilot_document_category.sql                [NEW — installs Co-Pilot category]
templates/documents/
├── general_upload.html                               [unchanged]
└── copilot_panel.html                                [NEW — extraction-state side panel; primary surface]
templates/copilot/
└── chart_summary_card.html                           [NEW — chart-panel rollup; secondary surface]

# Repo root
.pre-commit-config.yaml                               [EDIT — copilot-eval-fast + copilot-eval-full hooks]
README.md                                             [EDIT — Week 2 section, demo URL, demo video link]
COST.md                                               [EDIT — Week 2 section appended]
```

---

## W2-01 — Schemas + abstain enum (foundation)

**Goal.** Create the type contracts every later MR imports: the runtime
abstention enum (shared with v1's verification layer), the document-
schema package (`SourceCitation`, `ExtractedField[T]`, `lab_pdf`,
`intake_form`), the eval-only case-state enum, and the import-linter
contract that enforces the tool-vs-RAG boundary structurally.

**Depends on.** Nothing.

**Binding contracts.** PRD2 Appendix A.1 (canonical enum), Appendix A.5
(worker isolation + tool-vs-RAG), §6 (schema contract); W2_ARCHITECTURE
§5.1, §10.1.

**NEW**

- `agent-service/src/clinical_copilot/schemas/__init__.py`
- `agent-service/src/clinical_copilot/schemas/abstain.py` — `RuntimeAbstainReason`
  (Appendix A.1.a, 7 members). Re-exports v1's existing reasons; adds
  `LOW_CONFIDENCE`, `OUT_OF_SCHEMA`, `CITATION_INVALID`.
- `agent-service/src/clinical_copilot/documents/__init__.py`
- `agent-service/src/clinical_copilot/documents/schemas/__init__.py`
- `agent-service/src/clinical_copilot/documents/schemas/citation.py` — `SourceCitation`,
  `ExtractedField[T]` with the `value_xor_abstain` validator.
- `agent-service/src/clinical_copilot/documents/schemas/lab_pdf.py` —
  `LabPdfFacts` (list of Observation-shaped `ExtractedField`s).
- `agent-service/src/clinical_copilot/documents/schemas/intake_form.py` —
  `IntakeFormFacts` (chief complaint, current meds, reported allergies,
  social/family history flags, pain scale).
- `agent-service/src/clinical_copilot/evals/__init__.py`
- `agent-service/src/clinical_copilot/evals/case_state.py` — `EvalCaseState`
  (Appendix A.1.b, single member `JUDGE_INCONCLUSIVE`). Module must NOT
  import from `clinical_copilot.schemas.abstain`.
- `agent-service/src/clinical_copilot/evals/harness.py` — **minimal
  skeleton.** Bucket discovery (`evals/w2/<bucket>/*.json`), rubric
  registry collection, sequential case run, prints per-case pass/fail
  to stdout, exits non-zero on any failure. NO judge wrapper, NO budget
  pre-flight, NO results writer, NO retry — those land in W2-11. The
  goal is just enough to let later MRs run their own bucket via
  `make copilot-eval BUCKET=<name>` and verify acceptance locally.
- `agent-service/src/clinical_copilot/evals/rubrics.py` — registry
  decorator (`@rubric(class_=..., id_=...)`) + base rubric type. No
  rubric implementations yet — each later MR registers the rubrics it
  needs. The registry is the contract; W2-11 extends with the judge
  variant and the cross-bucket rubric classes (`latency.stage_p95`,
  `phi.span_redaction`).
- `agent-service/.importlinter` — initial contract:
  - `evidence_retriever` package may not import from `chart_tools` /
    `intake_extractor` read paths.
  - `evals.case_state` may not import from `schemas.abstain`, and vice
    versa.
- `agent-service/Makefile` — two skeleton targets:
  - `import-check` — runs `import-linter`.
  - `copilot-eval BUCKET=<name>` — invokes the W2-01 harness skeleton
    on a single bucket. Exits 0 when the bucket is empty (so empty
    buckets in early MRs are not failures).
  - W2-11 extends with `copilot-eval` (full suite), `copilot-eval-fast`
    (deterministic-only), and the budget gate. **Do not over-build
    here** — keep this MR scoped to "skeleton runs, exits sanely on
    empty buckets."
- `agent-service/tests/unit/schemas/test_abstain.py`
- `agent-service/tests/unit/documents/test_citation.py`
- `agent-service/tests/unit/documents/test_lab_pdf.py`
- `agent-service/tests/unit/documents/test_intake_form.py`

**EDIT**

- `agent-service/src/clinical_copilot/verification/abstention.py` — re-export
  `RuntimeAbstainReason` from the new `schemas/abstain.py` so v1 callers
  keep working without churn.
- `agent-service/pyproject.toml` — add `import-linter` dev dep.

**Subtasks**

- [ ] Define `RuntimeAbstainReason` `StrEnum` with all 7 canonical members.
      Verify v1's existing 4 reasons map by name.
- [ ] Define `SourceCitation` (document_id, page, bbox 4-tuple normalized
      0..1, confidence, raw_text). All fields required.
- [ ] Define `ExtractedField[T]` generic with `value: T | None`,
      `citation: SourceCitation | None`, `abstain_reason:
      RuntimeAbstainReason | None`.
- [ ] Implement `value_xor_abstain` model validator: non-null value → non-null
      citation; null value → non-null abstain_reason.
- [ ] Define `LabPdfFacts` and `IntakeFormFacts` per PRD2 §6.
- [ ] Define `EvalCaseState` in `evals/case_state.py`. Module-level
      assertion: must not import `schemas.abstain`.
- [ ] Author `.importlinter` contracts. Run `make import-check` and confirm
      it passes.
- [ ] Author `evals/harness.py` skeleton: discover bucket dir, load
      `*.json` cases, collect registered rubrics, run, print summary,
      exit non-zero on any failure. ~80–120 lines.
- [ ] Author `evals/rubrics.py` registry: `@rubric` decorator,
      `RubricResult` type, `registry: dict[str, list[Rubric]]`. No
      built-in rubrics yet.
- [ ] Author `Makefile` skeleton: `make copilot-eval BUCKET=foo`
      runs the harness on `evals/w2/foo/`. Empty bucket → exit 0.
- [ ] Tests: round-trip a known fixture through each schema; verify
      `value_xor_abstain` rejects invalid combinations; verify enum
      members exhaustively (regression net for member additions);
      verify `make copilot-eval BUCKET=__empty__` exits 0.

**Acceptance**

- [ ] `mypy --strict` clean on every new module.
- [ ] `make import-check` green.
- [ ] `make copilot-eval BUCKET=__empty__` exits 0 (skeleton works).
- [ ] All new test files pass; ≥ 12 test cases total.
- [ ] No PHP changes in this MR.

---

## W2-02 — OpenEMR Documents category + event hook + state-poll endpoint

**Goal.** Add the bridge from OpenEMR's existing Documents subsystem to
the agent service: a new document category that scopes ingestion, a
post-upload Symfony event listener that signs an HMAC payload and
enqueues a job in agent-db, and the read-only `GET /agent/documents/{id}`
endpoint the side-panel polls.

**Depends on.** W2-01 (imports schemas). Otherwise lifts the existing
`HmacSigner.php`, `Document.class.php`, FHIR DocumentReference / Binary
endpoints from v1 territory unchanged.

**Binding contracts.** PRD2 §2 (existing-subsystem reuse),
§2.1 (sequence), §2.2 (HMAC + FHIR re-fetch); W2_ARCHITECTURE §3.1, §3.3,
§2.2.

**NEW** — PHP side

- `src/CoPilot/Documents/CategoryProvider.php` — installs and idempotently
  detects the "Co-Pilot — Source Documents" category in OpenEMR's
  `categories` table. Must NOT modify the categories tree shape; only
  inserts one new node under "Patient Information" (or analogous).
- `src/CoPilot/Documents/EnqueuePayloadBuilder.php` — builds the HMAC
  payload (`document_id`, `patient_id`, `category_id`, `uploader_user_id`,
  `signed_at`). Reuses the existing `HmacSigner.php` from v1.
- `src/CoPilot/Documents/Routes.php` — registers `GET /agent/documents/{id}`
  on the existing `/agent/*` gateway prefix. (W2-10 adds a sibling
  `GET /agent/documents/summary` route to this same file for the
  chart-panel rollup; not part of W2-02 scope.)
- `src/Events/CoPilotDocumentUploadedListener.php` — Symfony EventDispatcher
  listener; fires on document insert; only acts when category matches the
  Co-Pilot category; calls `EnqueuePayloadBuilder` then POSTs to
  `agent-service` `POST /internal/documents/enqueue`.
- `sql/migrations/XXXX_copilot_document_category.sql` — installs the
  category. Idempotent (CHECK NOT EXISTS).

**NEW** — Python side

- `agent-service/src/clinical_copilot/documents/queue.py` — `enqueue` (HMAC
  verify + insert into `extraction_jobs`), `claim` (`SELECT ... FOR UPDATE
  SKIP LOCKED`), `complete`, `fail`. Stub `claim` for now; real worker
  arrives in W2-03.
- `agent-service/src/clinical_copilot/db/alembic/versions/XXXX_extraction_jobs.py`
  — schema: `id`, `document_id`, `patient_id`, `category_id`,
  `uploader_user_id`, `signed_at`, `payload_hmac`, `state ∈ {queued,
  claimed, extracting, extracted, failed}`, `version` (content hash),
  `created_at`, `updated_at`.

**EDIT** — Python side

- `agent-service/src/clinical_copilot/main.py` — add two endpoints:
  - `POST /internal/documents/enqueue` (HMAC-bound; verifies signature
    against the same secret v1 uses for the JWT signing key chain).
  - `GET /agent/documents/{id}` — reads from `extraction_jobs` +
    (eventually) `extracted_facts` (W2-03). For this MR, returns
    `{state, ready=false}` only.

**EDIT** — PHP side

- `interface/main/main_screen.php` (or analogous) — register the new
  Symfony listener. Light touch only.
- `templates/documents/general_list.html` — add a side-panel slot for
  the Co-Pilot category. Empty state for now; the panel content lands
  in W2-10.

**Tests**

- `agent-service/tests/unit/documents/test_queue.py` — unit tests for
  `enqueue` (HMAC valid / invalid / replayed), `claim` (lock semantics
  on a single connection — SKIP LOCKED is integration-tested below).
- `agent-service/tests/integration/ingestion_flow_test.py` — uploads a
  document via the OpenEMR REST endpoint *and* via the UI form path;
  asserts both fire the event, sign the payload, and write a row to
  `extraction_jobs`. Two cases (REST + UI) so neither path silently
  stops working.
- `tests/php/CoPilot/Documents/CategoryProviderTest.php` — installs and
  re-installs the category; asserts idempotence.

**Subtasks**

- [ ] Author the SQL migration. Verify it runs against a clean DB and
      against a DB with the category already present.
- [ ] Author `CategoryProvider.php` + unit test.
- [ ] Author `EnqueuePayloadBuilder.php`. Reuse `HmacSigner.php`.
- [ ] Author `CoPilotDocumentUploadedListener.php`. Wire to the
      EventDispatcher in the bootstrap.
- [ ] Author `Routes.php` registering `GET /agent/documents/{id}`.
- [ ] Add stub side-panel slot to `general_list.html`.
- [ ] Author `documents/queue.py::enqueue` + alembic migration.
- [ ] Author `POST /internal/documents/enqueue` (HMAC verify, write
      `extraction_jobs` row).
- [ ] Author `GET /agent/documents/{id}` (state-only response).
- [ ] Integration test: upload via REST + UI, assert event fires both ways.
- [ ] Confirm an upload to a *different* category does NOT enqueue
      (containment boundary).

**Acceptance**

- [ ] Co-Pilot category exists after migration; uploading to it writes
      `extraction_jobs.queued`; uploading to any other category does not.
- [ ] HMAC mismatch returns 401 from `POST /internal/documents/enqueue`.
- [ ] `GET /agent/documents/{id}` returns `{state, ready=false}` for any
      job not yet extracted; returns 404 for unknown ids; returns
      `UNAUTHORIZED` for ids whose patient_id doesn't match the JWT.
- [ ] All new tests pass; integration tests run against a clean
      docker-compose stack.

---

## W2-03 — lab_pdf VLM extraction worker

**Goal.** First end-to-end document → extracted_facts path. Worker
claims a job from the queue, fetches the binary via FHIR, renders the
page, calls the VLM with the `lab_pdf` schema, validates the response,
runs the citation OCR check (W2-05 required for this), runs the
domain-rule pass, and persists facts to agent-db.

**Depends on.** W2-01 (schemas), W2-02 (queue), W2-05 (citation OCR
check). W2-12 must already have shipped (PHI redaction) — extraction
emits LangSmith spans with content that requires the redaction layer.

**Binding contracts.** PRD2 §6 (vision extraction), §10.1 async pipeline
budgets, Appendix A.5 worker isolation; W2_ARCHITECTURE §3.2, §5.1,
§5.2.

**NEW**

- `agent-service/src/clinical_copilot/documents/fetcher.py` — `fetch_binary`
  (system-scoped JWT, FHIR DocumentReference → Binary), `render_page`
  (pypdfium2, 300 DPI, returns image + page number).
- `agent-service/src/clinical_copilot/documents/extractor.py` — entry
  point `extract(job_id)`; dispatches by document type; calls VLM via
  Anthropic SDK with `tool_choice` set to the schema-tool; persists.
- `agent-service/src/clinical_copilot/documents/merge.py` — multi-page
  merge logic for `lab_pdf` (concat + dedupe by `(code, effective_date,
  value, page)`). Stateless; pure function.
- `agent-service/src/clinical_copilot/db/alembic/versions/XXXX_extracted_facts.py`
  — schema: `id`, `document_id`, `field_path` (e.g.
  `observations[3].value`), `value` (JSON), `citation` (JSONB), `abstain_reason`,
  `confidence`, `extracted_at`, `version`.
- `agent-service/src/clinical_copilot/tools/extracted_facts.py` — query-time
  read of `extracted_facts`. **Pure read** — no VLM call, no FHIR fetch
  (W2_ARCHITECTURE §4.3 contract).
- `agent-service/runbooks/extraction-queue-drain.md` — what to do when
  the queue backs up.

**EDIT**

- `agent-service/src/clinical_copilot/documents/queue.py` — implement
  `claim` (`SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`), `complete`,
  `fail`. Add at-most-once invariant check on `(document_id, version)`.
- `agent-service/src/clinical_copilot/main.py` — `GET /agent/documents/{id}`
  now returns `{state, ready, facts, citations}` when `state == extracted`.
- `agent-service/src/clinical_copilot/discrepancy/rules.py` — add
  value-sanity rules for extracted lab values. Reuses v1 rule shape.

**Tests**

- `agent-service/tests/unit/documents/test_fetcher.py` — `render_page`
  against fixture PDFs (clean scan, low-contrast scan, multi-page).
- `agent-service/tests/unit/documents/test_extractor.py` — extractor
  with VLM call mocked (cassetted Anthropic responses). Verifies schema
  validation rejects out-of-schema fields.
- `agent-service/tests/unit/documents/test_merge.py` — multi-page merge
  edge cases.
- `agent-service/tests/integration/ingestion_flow_test.py` — extends
  W2-02 integration test: full upload → queue → extract → side-panel
  reads `state == extracted` with citations.
- `agent-service/tests/eval/w2/extraction-lab/` — **12 eval cases**
  (PRD2 §15.1 row W2-03). Each case: fixture lab PDF +
  ground-truth extraction; rubrics `extraction.field_present`,
  `extraction.citation_resolves`, `extraction.value_in_range`.

**Subtasks**

- [ ] Author the `extracted_facts` migration; verify FK to
      `extraction_jobs(id)`.
- [ ] Author `fetcher.py::fetch_binary` using v1's HTTP client. Confirm
      system-scoped JWT works against the FHIR Binary endpoint locally.
- [ ] Author `fetcher.py::render_page` with pypdfium2 at 300 DPI. Tests
      against three fixture PDFs.
- [ ] Author `extractor.py`: dispatch by document type → schema; call
      VLM with `tool_choice` = schema tool; validate via
      `model_validate`; emit per-field citations.
- [ ] Author `merge.py` for `lab_pdf`. Pure function, deterministic.
- [ ] Wire citation OCR check (from W2-05) into the per-field pipeline.
      Reject `OUT_OF_SCHEMA` early; mark `LOW_CONFIDENCE` when
      confidence < 0.7.
- [ ] Author `extracted_facts` persistence. One row per field, not one
      row per document.
- [ ] Implement `tools/extracted_facts.py` query-time read.
- [ ] Hand-build **10 lab PDF fixtures** (varying scan quality;
      hand-validated ground truth) for the eval bucket. Author the 12
      eval cases.
- [ ] Author `runbooks/extraction-queue-drain.md`.

**Acceptance**

- [ ] Upload of a Co-Pilot-category lab PDF to a clean stack produces
      an `extracted_facts` row set within the §10.1 async pipeline
      budget (p95 ≤ 90s end-to-end).
- [ ] `GET /agent/documents/{id}` returns the persisted facts with
      citations after extraction completes.
- [ ] All 12 eval cases pass against committed fixtures (run
      `make copilot-eval BUCKET=extraction-lab`).
- [ ] No worker → worker calls (Appendix A.5 worker isolation).
- [ ] No raw document text in any LangSmith span (W2-12 redaction
      layer holds).

---

## W2-04 — intake_form extraction

**Goal.** Second document schema. Extends the W2-03 extractor with the
`intake_form` dispatch path and the multi-page-with-required-field-fallback
strategy from W2_ARCHITECTURE §5.3.

**Depends on.** W2-03.

**Binding contracts.** Same as W2-03; schema contract per PRD2 §6.

**NEW**

- (none — schema already in W2-01)
- `agent-service/tests/eval/w2/extraction-intake/` — **10 eval cases**.

**EDIT**

- `agent-service/src/clinical_copilot/documents/extractor.py` — add the
  `intake_form` dispatch branch.
- `agent-service/src/clinical_copilot/documents/merge.py` — add the
  stateful merge strategy for `intake_form` (page 1 first; later pages
  only when required fields came back `NO_DATA`).
- `agent-service/src/clinical_copilot/discrepancy/rules.py` — add
  reported-allergy / current-med plausibility rules.

**Tests**

- `agent-service/tests/unit/documents/test_extractor.py` — extend with
  `intake_form` cases.
- `agent-service/tests/unit/documents/test_merge.py` — stateful merge
  edge cases (page 1 has all required fields → page 2 not invoked;
  page 1 missing chief complaint → page 2 invoked; both pages partial
  → merge picks the more complete value).
- `agent-service/tests/eval/w2/extraction-intake/` — 10 cases against
  hand-built intake form fixtures (typed forms, handwritten forms,
  partially-complete forms, denial-shape responses like "denies
  allergies / NKDA").

**Subtasks**

- [ ] Hand-build **10 intake-form fixtures**: 4 typed, 4 handwritten,
      2 partially complete. Hand-validated ground truth.
- [ ] Author the `intake_form` extractor branch.
- [ ] Author the stateful merge for `intake_form`.
- [ ] Add domain-rule plausibility checks for reported allergies and
      meds.
- [ ] Author the 10 eval cases.

**Acceptance**

- [ ] All 10 eval cases pass.
- [ ] Stateful merge correctly bounds VLM calls: a 1-page complete form
      generates exactly one VLM call.
- [ ] Negation-shape inputs ("NKDA", "denies allergies") do not produce
      false-positive allergy entries.

---

## W2-05 — Citation OCR check (strict + degraded path)

**Goal.** Implement the formal citation-validity rule from PRD2 §8.2 +
Appendix A.4. This is what makes "citation valid" mean something
auditable. Used by the extractor (W2-03/04) per-field after VLM call.

**Depends on.** W2-01 (`SourceCitation` schema). Independent of W2-02
and prior; ships *before* W2-03 so the extractor wires it directly.

**Binding contracts.** PRD2 §8.2, Appendix A.4 (threshold contract);
W2_ARCHITECTURE §7.1.

**NEW**

- `agent-service/src/clinical_copilot/verification/ocr.py` — Tesseract
  wrapper (`pytesseract`); produces `{text, mean_word_confidence}`.
  Single source for both citation check and any future OCR-fallback
  use.
- `agent-service/tests/unit/verification/test_citation_check.py`

**EDIT**

- `agent-service/src/clinical_copilot/verification/citation_check.py` —
  add the `CitationKind` enum (`STRUCTURED_FACT`, `DOCUMENT_BBOX`,
  `CORPUS_CHUNK`); implement `_check_document_bbox` per §8.2 strict
  + degraded path; expose verdict (`valid`, `low_confidence`,
  `invalid`).
- `agent-service/pyproject.toml` — add `pytesseract`, `rapidfuzz`,
  `pypdfium2` deps.

**Tests**

- `tests/unit/verification/test_citation_check.py` — strict path:
  high-confidence match passes, sub-threshold fails. Degraded path:
  empty OCR + plausible bbox → `low_confidence`; empty OCR + zero-area
  bbox → `invalid`; ≥60% page area → `invalid`. False-reject set:
  10 hand-validated correct extractions; assert ≤5% reject rate
  (PRD2 §8.2 `citation.false_reject_rate`).

**Subtasks**

- [ ] Author `verification/ocr.py` wrapping `pytesseract.image_to_data`
      with a margin parameter.
- [ ] Author `_check_document_bbox`: render bbox + 5% margin; OCR;
      `rapidfuzz.token_set_ratio` ≥ 0.85 → valid. Else degraded path:
      bbox plausibility checks (area ≥ 0.5%, ≤ 60% of page, within
      page bounds) → `low_confidence` or `invalid`.
- [ ] Author `_check_corpus_chunk`: confirm chunk exists in manifest,
      `corpus_id` is on permitted-source list, manifest checksum
      matches build.
- [ ] Build the 10-fixture false-reject set and the boolean rubric.
- [ ] All thresholds as module constants. Imported from a single place
      so eval and runtime see the same numbers.

**Acceptance**

- [ ] Unit tests cover both paths and all three verdict values.
- [ ] False-reject rate ≤ 5% on the curated 10-fixture set.
- [ ] Tesseract dep declared in pyproject and installed in the Docker
      image (Dockerfile EDIT if needed).

---

## W2-06 — Evidence retriever (corpus + hybrid RAG)

**Goal.** Stand up the guideline corpus + indexer + hybrid retriever.
Independent of W2-03/04; can land in parallel.

**Depends on.** W2-01 (schemas). Otherwise standalone.

**Binding contracts.** PRD2 §7 (corpus + permitted sources), §5.3 + Appendix
A.5 (tool-vs-RAG boundary); W2_ARCHITECTURE §6, §10.

**NEW**

- `agent-service/corpus/sources/uspstf/` — initial USPSTF screening
  recommendations (Markdown with YAML frontmatter).
- `agent-service/corpus/sources/cdc/` — initial CDC vaccine schedule +
  selected clinical guidance.
- `agent-service/corpus/sources/nih/` — initial NIH/NHLBI/NIDDK
  guides.
- `agent-service/corpus/sources/LICENSES.md` — per-source permission
  basis (PRD2 §7 corpus licensing table).
- `agent-service/src/clinical_copilot/corpus/__init__.py`
- `agent-service/src/clinical_copilot/corpus/chunker.py` — sentence-window
  chunks (window 3, stride 1; 200–400 token target).
- `agent-service/src/clinical_copilot/corpus/scrub.py` — index-time
  PHI-shape regex detector (SSN, MRN, phone, email, name patterns).
- `agent-service/src/clinical_copilot/corpus/index.py` — one-shot CLI:
  reads `corpus/sources/`, scrubs, chunks, builds BM25
  (`rank_bm25.BM25Okapi`) → `data/corpus/bm25.pkl`, embeds → pgvector
  `corpus_index` table, writes `data/corpus/manifest.json`.
- `agent-service/src/clinical_copilot/corpus/retriever.py` — hybrid
  retrieve (BM25 top-20 + dense top-20 dedup) → cross-encoder rerank
  (`cross-encoder/ms-marco-MiniLM-L-6-v2`) → top-K=5.
- `agent-service/src/clinical_copilot/tools/guideline_evidence.py` —
  exposes the evidence retriever as a tool the supervisor invokes.
- `agent-service/src/clinical_copilot/db/alembic/versions/XXXX_corpus_index.py`
  — pgvector table.
- `agent-service/runbooks/corpus-rebuild.md`.

**EDIT**

- `agent-service/.importlinter` — confirm contract still passes;
  `corpus` package must not import from `tools` or `documents`.
- `agent-service/pyproject.toml` — add `rank-bm25`,
  `sentence-transformers` (for the cross-encoder), `pgvector`.
- `agent-service/src/clinical_copilot/verification/citation_check.py` —
  flesh out `_check_corpus_chunk` against the live manifest.

**Tests**

- `agent-service/tests/unit/corpus/test_chunker.py` — deterministic
  chunking against fixture documents.
- `agent-service/tests/unit/corpus/test_scrub.py` — PHI-shape detector
  passes clean docs, rejects docs with seeded PHI patterns.
- `agent-service/tests/unit/corpus/test_retriever.py` — BM25 alone,
  dense alone, hybrid+rerank against fixture corpus with hand-labeled
  gold chunks.
- `agent-service/tests/eval/w2/retrieval/` — **8 eval cases**;
  rubrics `retrieval.guideline_in_top_k`, `retrieval.citation_form`.

**Subtasks**

- [ ] Curate the initial corpus (~200 docs split across USPSTF / CDC /
      NIH). Author `LICENSES.md` with the permission basis for each
      source.
- [ ] Author `chunker.py` (deterministic; pure function on bytes).
- [ ] Author `scrub.py` (regex layer + manifest-rejection log).
- [ ] Author `index.py` CLI: `python -m clinical_copilot.corpus.index
      --rebuild`. Build BM25 + pgvector + manifest.
- [ ] Author `retriever.py::hybrid_retrieve`. Cross-encoder loads once
      at process start.
- [ ] Author the `query rewrite` heuristic (skip rewrite when ≥ 4
      medical-vocabulary tokens already in the sub-query).
- [ ] Wire `tools/guideline_evidence.py` so the supervisor can invoke it.
- [ ] Author the corpus-rebuild runbook.
- [ ] Author the 8 eval cases with hand-labeled gold chunks.

**Acceptance**

- [ ] Index builds reproducibly (manifest checksum stable across two
      builds on the same source set).
- [ ] `make import-check` still green (no cross-package import drift).
- [ ] All 8 eval cases pass.
- [ ] `corpus/scrub.py` rejects test docs with seeded PHI patterns.

---

## W2-07 — Supervisor + planner + critic + handoff logging (LangGraph)

**Goal.** Compose the multi-agent graph using **LangGraph**
(`langgraph>=0.2,<0.3`). Implements the planner node (query
decomposition), the worker fan-out, the critic node + retry
conditional edge, the §4.5 post-planner short-circuit (also a
conditional edge), and the LangGraph tracing callback that emits
every node execution as a span with `parent_run_id` linkage.

We use LangGraph **minimally** — `StateGraph`, `add_node`,
`add_edge`, `add_conditional_edges`, `compile`. No LangChain agent
packages, no ReAct loops, no LangChain `Tool` wrappers; node bodies
are plain Python that read/write the typed `TurnState` dict.

**Depends on.** W2-01, W2-03, W2-04, W2-06. Last MR before integration
gets real.

**Binding contracts.** PRD2 §5, §5.1, §5.2, §5.3, Appendix A.5, A.6;
W2_ARCHITECTURE §4, §10.

**NEW**

- `agent-service/src/clinical_copilot/orchestrator/state.py` — `TurnState`
  TypedDict (W2_ARCHITECTURE §4.1): `user_query`, `session`,
  `sub_queries`, `drafts`, `retry_counts`, `final_response`. Single
  source of truth for the LangGraph state shape.
- `agent-service/src/clinical_copilot/orchestrator/supervisor.py` —
  builds and compiles the `StateGraph` once at process start. Declares
  nodes (`planner`, `chart_tools`, `intake_extractor`,
  `evidence_retriever`, `critic`, `verification`, `v1_single`),
  conditional edges (`route_after_planner`, `route_after_critic`),
  and the `END` edge. Exposes `run_turn(state) -> Response` as the
  thin wrapper FastAPI calls.
- `agent-service/src/clinical_copilot/orchestrator/planner.py` — node
  body: Haiku call returning `list[SubQuery]` with `text`,
  `claim_type`, `target_worker`. Routing map (claim_type → node name)
  is fixed in code per Appendix A.5; the planner emits claim type
  only.
- `agent-service/src/clinical_copilot/orchestrator/critic.py` — node
  body. Two-tier: deterministic checks (citation existence,
  citation-type vs claim-type, action-suggestion blacklist,
  confidence floor) then LLM judge call. Latency cap 1.5s p95
  (timeout → abstain). Emits a verdict that
  `route_after_critic` turns into either `retry` or `verification`.
- `agent-service/src/clinical_copilot/orchestrator/nodes/__init__.py`
- `agent-service/src/clinical_copilot/orchestrator/nodes/chart_tools.py`
  — wraps the v1 tool registry as a LangGraph node body.
- `agent-service/src/clinical_copilot/orchestrator/nodes/intake_extractor.py`
  — wraps `tools/extracted_facts.py` (W2-03) as a LangGraph node body.
- `agent-service/src/clinical_copilot/orchestrator/nodes/evidence_retriever.py`
  — wraps `corpus/retriever.py` (W2-06) as a LangGraph node body.
- `agent-service/src/clinical_copilot/orchestrator/nodes/v1_single.py` —
  wraps the v1 single-orchestrator path (`orchestrator/agent.py`) for
  the §4.5 short-circuit conditional edge.
- `agent-service/src/clinical_copilot/orchestrator/edges.py` —
  `route_after_planner` and `route_after_critic` predicates. Pure
  functions on state; unit-tested in isolation.
- `agent-service/src/clinical_copilot/orchestrator/prompts/planner.txt`
- `agent-service/src/clinical_copilot/orchestrator/prompts/critic.txt`
- `agent-service/tests/integration/synthesis_flow_test.py`

**EDIT**

- `agent-service/pyproject.toml` — add `langgraph>=0.2,<0.3`.
- `agent-service/src/clinical_copilot/orchestrator/agent.py` — keep v1
  single-orchestrator path; expose it as a callable that
  `nodes/v1_single.py` wraps.
- `agent-service/src/clinical_copilot/main.py` — turn-handling endpoint
  invokes the compiled supervisor graph instead of v1's `agent.py`.
  v1 path is reachable only through the StateGraph's `v1_single` node.
- `agent-service/src/clinical_copilot/observability/langsmith_client.py` —
  configure LangGraph's tracing callback so every node emits a span
  with `parent_run_id` linkage automatically; remove any redundant
  manual span emission in node bodies.

**Tests**

- `agent-service/tests/unit/orchestrator/test_planner.py` — composite
  asks decompose to ≥ 2 sub-queries; single-claim asks decompose to
  1-element lists; claim-type-to-worker mapping is fixed (LLM picks
  type, code picks worker).
- `agent-service/tests/unit/orchestrator/test_critic.py` — each rejection
  reason fires under its trigger condition; deterministic checks short-
  circuit before judge call; latency cap forces abstain.
- `agent-service/tests/unit/orchestrator/test_edges.py` — pure-function
  tests for `route_after_planner` (single-CHART_FACT case →
  `v1_single`; multi-claim → `fan_out`; doc-fact-only →
  `fan_out`) and `route_after_critic` (accept → `verification`;
  rejection with retry budget → `retry`; rejection without budget
  → `abstain`).
- `agent-service/tests/unit/orchestrator/test_supervisor.py` — graph
  compiles; invoking `run_turn` against fixture states executes
  nodes in the expected order; retry semantics (max 1 per
  sub-query, tracked in `state["retry_counts"]`); fast-lane
  whole-answer abstain; slow-lane sentence-level rejection.
- `agent-service/tests/integration/synthesis_flow_test.py` — full turn
  end-to-end through the compiled StateGraph. Asserts every node
  execution is a logged span with `parent_run_id` linkage.
- `agent-service/tests/eval/w2/citation-separation/` — **6 eval cases**
  testing planner-claim-type vs citation-type matching.

**Subtasks**

- [ ] Add `langgraph>=0.2,<0.3` to `pyproject.toml`; verify lockfile
      regenerates cleanly.
- [ ] Author `state.py` `TurnState` TypedDict.
- [ ] Author the planner prompt and the structured-output Pydantic
      model for the planner's output.
- [ ] Implement `planner.py` node body (single Anthropic call →
      `state["sub_queries"]`).
- [ ] Implement worker node bodies in `orchestrator/nodes/`. Each is
      a thin wrapper that reads `state["sub_queries"]` and appends to
      `state["drafts"]`.
- [ ] Implement `critic.py` node body. Deterministic checks first;
      `asyncio.wait_for(judge_call, timeout=1.5)` for the LLM judge.
- [ ] Implement `edges.py` predicates. `route_after_planner` returns
      `"v1_single"` only when there is exactly one CHART_FACT
      sub-query; otherwise `"fan_out"`. `route_after_critic` returns
      `"retry"` (max 1 per sub-query — track in
      `state["retry_counts"]`), `"verification"`, or `"abstain"`.
- [ ] Wire `supervisor.py`: build the StateGraph at module load,
      compile once. Expose `run_turn(state) -> Response`.
- [ ] Wrap v1 `orchestrator/agent.py` in `nodes/v1_single.py` for the
      §4.5 short-circuit conditional edge. Eval bucket
      `latency.single_claim_passthrough` covers it.
- [ ] Configure LangGraph's tracing callback so every node emits a
      span with `parent_run_id` automatically; remove any redundant
      manual span emission in node bodies.
- [ ] Author the 6 citation-separation eval cases. Each case asserts
      both that the right citation type is used AND that a violation
      would be caught by the critic.

**Acceptance**

- [ ] `langgraph` is the only new runtime dep; no LangChain agent
      packages, no ReAct deps. (Grep `pyproject.toml` to verify.)
- [ ] Every node execution is a logged span with `parent_run_id`
      linkage; verifiable from LangSmith traces (also asserted in
      `test_supervisor.py`).
- [ ] Critic latency cap holds: synthetic case forcing the judge over
      1.5s aborts with `VERIFICATION_FAILED`.
- [ ] Action-suggestion blacklist hits abort the response in both lanes.
- [ ] All 6 citation-separation eval cases pass.
- [ ] Planner runs unconditionally per Appendix A.5 (the §4.5
      short-circuit is a *post-planner* conditional edge; verified by
      trace assertion that `planner` span fires before any `v1_single`
      span).
- [ ] Single-claim CHART_FACT queries route through `v1_single` (not
      through the worker fan-out + critic), preserving v1 fast-lane
      latency. Eval bucket `latency.single_claim_passthrough` asserts.

---

## W2-08 — Reconciliation extension (extracted facts vs chart)

**Goal.** Generalize v1's discrepancy engine to compare extracted-doc
facts against structured chart facts (the use-case 6 differentiating
feature in PRD2 §4).

**Depends on.** W2-03 (extracted facts produced), W2-04 (intake_form
allergies/meds for reconciliation against chart allergies/meds).

**Binding contracts.** PRD2 §4 use case 6; W2_ARCHITECTURE §4.3.

**NEW**

- `agent-service/tests/eval/w2/reconciliation/` — **8 eval cases** with
  seeded discrepancies (chart med list says metoprolol, intake form
  reports patient says discontinued; chart shows no penicillin
  allergy, intake form reports rash on amoxicillin; etc.).

**EDIT**

- `agent-service/src/clinical_copilot/discrepancy/rules.py` — add
  cross-source reconciliation rules:
  - `extracted_med_not_in_med_list`
  - `extracted_allergy_not_in_allergy_table`
  - `chart_med_marked_discontinued_on_intake`
  - `extracted_lab_value_outside_chart_range`
- `agent-service/src/clinical_copilot/orchestrator/supervisor.py` — wire
  reconciliation results into `discrepancy` flag emissions when the
  intake-extractor returns facts.

**Tests**

- `agent-service/tests/unit/discrepancy/test_reconciliation.py` —
  per-rule unit tests.
- `agent-service/tests/integration/synthesis_flow_test.py` — extend with
  a reconciliation scenario.
- `agent-service/tests/eval/w2/reconciliation/` — 8 cases.

**Subtasks**

- [ ] Author the four reconciliation rules.
- [ ] Hand-build the 8 eval-case fixtures with seeded discrepancies
      (each case has a known ground-truth flag the agent must raise).
- [ ] Verify the v1 `discrepancy_engine` flag-emission path consumes
      the new rule outputs without modification (it should — same shape).

**Acceptance**

- [ ] All 8 reconciliation eval cases pass.
- [ ] Each rule has at least one positive test case AND one negative
      test case (no false positives on clean fixtures).

---

## W2-09 — RBAC scope test for documents (test-first)

**Goal.** Per CLAUDE.md test-first policy: assert the documents-access
boundary holds before any extraction MR ships. Most "implementation"
in this MR is *tests* against existing OpenEMR FHIR + W2-02 endpoints;
production code only changes if tests reveal a gap.

**Depends on.** W2-02 (state-poll endpoint exists).

**Binding contracts.** PRD2 §14 (RBAC must be 100% pass), Appendix A.2
clause 1; W2_ARCHITECTURE §2.1.

**NEW**

- `agent-service/tests/integration/rbac_documents_test.py` — full RBAC
  matrix:
  - User with patient_id A queries document of patient B → `UNAUTHORIZED`.
  - User logged out / expired JWT queries any document → `UNAUTHORIZED`.
  - User with correct patient_id queries a document not in the Co-Pilot
    category → 404 (not found in the agent surface; the document exists
    but isn't ours).
  - Replay attack: HMAC payload with `signed_at` > 5min old →
    `UNAUTHORIZED` from the enqueue endpoint.
  - Cross-patient injection in the GET path (`/agent/documents/{id}`
    where `{id}` resolves to a different patient than the JWT) →
    `UNAUTHORIZED`.
- `agent-service/tests/eval/w2/rbac/` — **4 eval cases** mirroring the
  integration scenarios as adversarial-prompt cases.

**EDIT**

- `agent-service/src/clinical_copilot/main.py` — only if tests reveal a
  gap (e.g., the GET path doesn't validate patient binding); harden if
  needed.

**Tests**

(All this MR is tests; see NEW above.)

**Subtasks**

- [ ] Author the 5 integration test scenarios.
- [ ] Author the 4 eval cases.
- [ ] Run; if any scenario fails, fix the underlying gap *before* the
      MR merges. (Gaps caught here are the whole point of the MR.)

**Acceptance**

- [ ] All 5 integration tests pass.
- [ ] All 4 eval cases pass — must be 100%, no exceptions, per
      Appendix A.2 clause 1.
- [ ] Audit-log entries exist for every UNAUTHORIZED outcome (verify
      against `audit_log` table).

---

## W2-10 — Abstention rendering + chart-side summary card

**Goal.** Two UX surfaces. (1) **Primary surface — Documents-view
side panel:** UX rendering for the new W2 abstain reasons
(`LOW_CONFIDENCE`, `OUT_OF_SCHEMA`, `CITATION_INVALID`) plus v1
reasons. (2) **Secondary surface — chart side panel summary
rollup card:** a small "N extracted, M abstained" rollup that
deep-links into the Documents view (PRD2 §3 + §2; W2_ARCHITECTURE
§1 — chart panel is summary-only, Documents view is canonical).

**Depends on.** W2-03 (extractor emits abstain reasons), W2-02 (side
panel slot exists in `general_list.html`; chart side panel from v1
already on the patient summary surface).

**Binding contracts.** PRD2 §2 (Documents view canonical), §3 (chart
panel is summary-only secondary surface), §6 abstention rendering
table; Appendix A.1.a.

**NEW**

- `templates/documents/copilot_panel.html` — Documents-view side panel
  content: per-document state row (queued / extracting / extracted /
  abstained / failed) and per-field rendering with the abstention UX
  from PRD2 §6:
  - `LOW_CONFIDENCE` → "Could not read reliably — please verify in
    source" + click-to-source link.
  - `OUT_OF_SCHEMA` → not surfaced (silent omission).
  - `CITATION_INVALID` → "Extraction did not match source — please
    verify" + click-to-source link.
  - Other v1 reasons rendered per v1 §5.
- `templates/copilot/chart_summary_card.html` — chart-side summary
  rollup card. Renders one of:
  - "No documents extracted yet" (NO_DATA-shape, with a quiet styling).
  - "N extracted, M abstained" with a "View documents" button that
    deep-links to the patient's Documents view filtered to the
    Co-Pilot category.
  - "Extracting M documents…" while any job is `queued`/`extracting`.
- `agent-service/src/clinical_copilot/tools/extraction_summary.py` —
  aggregates `extracted_facts` + `extraction_jobs` for one patient
  into the rollup shape (counts only — no per-field detail).

**EDIT**

- `templates/documents/general_list.html` — replace the W2-02 stub
  side-panel slot with the real `copilot_panel.html` include.
- The v1 in-chart side panel template (path TBD; confirm during
  implementation — likely under `templates/patient/` or wherever v1
  added the chart side panel) — include `chart_summary_card.html` at
  the top of the panel. Card is collapsible; default open.
- `agent-service/src/clinical_copilot/main.py` — add
  `GET /agent/documents/summary?patient_id={pid}` returning
  `{queued: int, extracting: int, extracted: int, abstained: int,
  failed: int, latest_extracted_at: datetime | null}`. JWT-bound to
  the requesting user; patient_id must match session-bound patient.
- (Light JS for click-to-source preview, embedded in
  `copilot_panel.html` per PRD2 §7 implementation approach — vanilla /
  Alpine.)

**Tests**

- `tests/Tests/Isolated/Common/Twig/render/copilot_panel_test.php` —
  render fixture for each abstain reason; snapshot the HTML; commit
  the snapshot per CLAUDE.md Twig render-test policy.
  *(If the panel ends up Smarty rather than Twig, use the Smarty
  render-test analog; the panel uses whichever the surrounding
  Documents UI uses.)*
- `tests/Tests/Isolated/Common/Twig/render/chart_summary_card_test.php`
  — snapshots for the three states (empty / extracting / extracted).
- `agent-service/tests/unit/tools/test_extraction_summary.py` —
  aggregation correctness against fixture states (mixed-state patient,
  all-extracted patient, no-documents patient).
- `agent-service/tests/integration/synthesis_flow_test.py` — extend
  with a chart-panel summary card scenario: open the chart panel
  while extraction is in flight, confirm the rollup updates as jobs
  complete (poll-based).
- `agent-service/tests/eval/w2/abstention/` — **2 eval cases**:
  - One case with a low-confidence extraction; assert the response
    surfaces the LOW_CONFIDENCE rendering.
  - One case with an OCR mismatch; assert CITATION_INVALID rendering.

**Subtasks**

- [ ] Author `copilot_panel.html` with the per-state and
      per-abstain-reason rendering paths.
- [ ] Wire click-to-source: on click of a citation, open a modal that
      renders the source PDF page with the `bbox` highlighted.
- [ ] Author `chart_summary_card.html` with the three states.
- [ ] Locate the v1 in-chart side panel template; add the summary card
      include at the top. Verify the card collapses gracefully when
      the patient has zero Co-Pilot documents.
- [ ] Author `tools/extraction_summary.py` aggregation function.
- [ ] Author `GET /agent/documents/summary` endpoint.
- [ ] Author both render-test snapshots.
- [ ] Author the 2 abstention eval cases.

**Acceptance**

- [ ] Render snapshot tests pass for both panel and card.
- [ ] Click-to-source works for both `lab_pdf` and `intake_form`
      fixtures (manual smoke + an integration test that asserts the
      modal endpoint returns the rendered page with the bbox overlay).
- [ ] Chart-side summary card renders the rollup correctly across all
      three states; the "View documents" button deep-links to the
      Documents view filtered to the Co-Pilot category for the active
      patient.
- [ ] `GET /agent/documents/summary` returns 401 cross-patient (RBAC
      check from W2-09 covers it).
- [ ] Both abstention eval cases pass.

---

## W2-11 — Pre-push eval hook + Makefile + flake policy + README + COST.md

**Goal.** Wrap the eval suite, wire the pre-push gate, finalize
documentation. The harness *skeleton* + the rubric registry already
exist from W2-01; this MR layers on the judge wrapper, the budget
gate, the flake/quarantine logic, the results writer, the
`.pre-commit-config.yaml` hooks, and the README + COST.md updates
that close out the deliverables.

**Depends on.** Every other MR (the eval suite cannot be wired before
all 50 cases exist).

**Binding contracts.** PRD2 §8, §8.1, §8.2, Appendix A.2, A.3, A.4;
W2_ARCHITECTURE §9, §11, §12.

**NEW**

- `agent-service/src/clinical_copilot/evals/judge.py` — 3-of-3 unanimous
  judge wrapper per Appendix A.2 clause 4.
- `agent-service/src/clinical_copilot/evals/budget.py` — pre-flight
  token-budget gate; aborts with `BUDGET_EXCEEDED` when projected
  spend > cap.
- `agent-service/src/clinical_copilot/evals/results.py` — JSON +
  Markdown writers; results land in `evals/w2/results/<run_id>.{json,md}`.
- `agent-service/src/clinical_copilot/evals/w2/judge.yaml` — judge
  config (model tier, temperature, system prompt).
- `agent-service/runbooks/eval-baseline-refresh.md` — how to refresh
  the eval baseline after a threshold revision (PRD2 §12 protocol).
- `.pre-commit-config.yaml` (root) — two hooks:
  - `copilot-eval-fast` (stage `pre-commit`) — deterministic rubric
    classes only, cached responses, <30s.
  - `copilot-eval-full` (stage `pre-push`) — full 50-case suite per
    Appendix A.2.

**EDIT (extending W2-01 skeleton)**

- `agent-service/src/clinical_copilot/evals/harness.py` — extend the
  W2-01 skeleton with: per-case retry on `TOOL_FAILURE` (max 2),
  judge invocation for judge-evaluated rubrics (3-of-3 unanimity),
  quarantine-ceiling check (5%), budget pre-flight, results writer
  invocation.
- `agent-service/src/clinical_copilot/evals/rubrics.py` — extend the
  W2-01 registry with the cross-bucket rubric classes
  (`latency.stage_p95`, `phi.span_redaction`,
  `citation.false_reject_rate`, `citation.degraded_path_rate`).
  Per-bucket rubrics were registered by their owning MRs.

**EDIT**

- `agent-service/Makefile` — extend the W2-01 skeleton:
  - `copilot-eval` — full live-VLM run (new).
  - `copilot-eval-fast` — deterministic rubric classes only (new).
  - `copilot-eval BUCKET=<name>` — keep the W2-01 form; now wired
    through the full retry + judge + budget path.
- `README.md` — Week 2 section: app URL, demo video link,
  two-service startup, eval shortcut, **environment-variable matrix**
  (per Week 2 rubric Submission Requirements: "clear
  environment-variable documentation"). Matrix columns:
  variable name / required-or-optional / default / where it's used /
  rotation guidance. Required entries:
  - `ANTHROPIC_API_KEY` — VLM + planner + critic + judge calls.
  - `OPENAI_API_KEY` — corpus embedding model
    (`text-embedding-3-small`); skip if you swap to a local
    embedding model in W2-06.
  - `LANGSMITH_API_KEY` — observability traces.
  - `COPILOT_JWT_SECRET` — PHP gateway HMAC + JWT signing (v1 carries).
  - `COPILOT_PSEUDONYM_SALT` — HMAC salt for span pseudonyms (W2-12).
  - `COPILOT_BUDGET_CAP_USD` — eval-gate budget cap (default 5.00).
  - `COPILOT_CORPUS_INDEX_PATH` — pgvector + BM25 manifest location.
  - `COPILOT_FHIR_BASE_URL` — system-scoped FHIR endpoint (v1).
  - `COPILOT_SMART_PRIVATE_KEY_PATH` — SMART Backend Services system
    JWT key (v1).
  - Database envs (existing v1 connection strings).
  Include the §15.1 test matrix link.
- `COST.md` — append Week 2 section: actual dev spend, projected
  production spend per active patient per day, p50/p95 latency per
  §10.1 stage with measured numbers, identified bottleneck.
- `agent-service/src/clinical_copilot/observability/latency.py` (if
  not yet created in an earlier MR) — finalize the per-stage
  histogram so eval reads `latency.stage_p95`.
- `agent-service/.gitignore` — ignore `evals/w2/results/` *contents*
  except for the latest committed `<run_id>.md` summary (the file
  reviewers / graders see).

**Tests**

- `agent-service/tests/unit/evals/test_harness.py` — retry logic;
  budget gate; flake policy (judge unanimous; quarantine ceiling).
- `agent-service/tests/unit/evals/test_rubrics.py` — each rubric
  evaluator is deterministic on a fixture pair (case + response).
- `agent-service/tests/unit/evals/test_budget.py` — projected-spend
  estimator; abort path.

**Subtasks**

- [ ] Extend `harness.py` (W2-01 skeleton) with retry-on-TOOL_FAILURE
      (max 2), judge invocation, quarantine-ceiling check.
- [ ] Extend `rubrics.py` (W2-01 registry) with the cross-bucket
      rubric classes (`latency.stage_p95`, `phi.span_redaction`,
      `citation.false_reject_rate`, `citation.degraded_path_rate`).
- [ ] Author `judge.py` (Anthropic Haiku judge wrapper) with config
      from `judge.yaml`.
- [ ] Author `budget.py` pre-flight estimator. Default cap $5; CLI
      override via `--budget-cap`.
- [ ] Author `results.py` (JSON + Markdown writers).
- [ ] Wire `agent-service/Makefile` targets.
- [ ] Wire `.pre-commit-config.yaml` hooks. Run `prek install` locally
      to confirm.
- [ ] Author the eval-baseline-refresh runbook.
- [ ] Author the 50th eval case if any bucket is short (sanity check
      total: 12 + 10 + 8 + 8 + 6 + 4 + 2 = 50).
- [ ] Run the full eval against `main` baseline and commit the result
      Markdown to `evals/w2/results/`.
- [ ] Update `README.md` Week 2 section, including the
      environment-variable matrix (rubric: "clear environment-variable
      documentation").
- [ ] Update `COST.md` Week 2 section with measured numbers.
- [ ] Record demo video; link in README.md.

**Acceptance**

- [ ] `make copilot-eval` runs end-to-end and writes a Markdown summary
      to `evals/w2/results/`.
- [ ] Pre-push hook blocks pushes that fail per Appendix A.2; verified
      with a deliberate-fail commit (then reverted).
- [ ] Budget gate aborts with `BUDGET_EXCEEDED` on a synthetic
      over-budget run.
- [ ] All 50 eval cases pass at the configured pass-rate threshold
      (≥ 90% overall, 100% on RBAC).
- [ ] README.md Week 2 section is reachable from the repo root and
      contains the deployed-app URL, demo video, two-service startup,
      eval shortcut, AND a complete environment-variable matrix (every
      env var the agent service or PHP gateway reads, with default,
      use-site, and rotation guidance). Rubric: "clear
      environment-variable documentation."
- [ ] COST.md Week 2 section has measured (not projected) p50/p95 per
      §10.1 stage.

---

## W2-12 — PHI redaction in LangSmith spans (test-first)

**Goal.** Per CLAUDE.md test-first policy: lock down the PHI-redaction
fail-closed path before any module that emits LangSmith spans
containing extracted text ships. This MR is small and lands early in
the build sequence (recommended order #2 after W2-01).

**Depends on.** W2-01 (schemas only — keeps the dependency surface
minimal so this can ship before extraction MRs).

**Binding contracts.** PRD2 §9, Appendix A.2 clause 6;
W2_ARCHITECTURE §8.

**NEW**

- `agent-service/src/clinical_copilot/observability/latency.py` — created
  here (or in W2-11; whichever lands first owns it). Per-stage
  histogram aggregator that the redaction layer composes with.
- `agent-service/src/clinical_copilot/observability/phi_detector.py` —
  regex layer for PHI signals (SSN, MRN-shape, phone, email, raw
  chart-text patterns).
- `agent-service/tests/unit/observability/test_redaction.py`
- `agent-service/tests/integration/phi_redaction_test.py`

**EDIT**

- `agent-service/src/clinical_copilot/observability/langsmith_client.py` —
  introduce `send_span` as the single mandatory entry point; deny-by-
  default key whitelist; regex layer drops spans with PHI signals;
  emits `spans_redacted_total` metric on each drop.

**Tests**

- `tests/unit/observability/test_redaction.py` — every PHI signal
  pattern is detected. Every non-whitelisted key is dropped. Whitelisted
  keys with PHI in the value still drop the span.
- `tests/integration/phi_redaction_test.py` — fail-closed: synthetic
  span with raw chart-text payload reaches `send_span`, gets dropped,
  no LangSmith API call made (verified with mocked HTTP client).
- Eval-side test (lives in `evals/rubrics.py` once W2-11 ships): a
  `phi.span_redaction` rubric that runs the eval suite with span
  capture enabled and asserts zero PHI signals across all spans.

**Subtasks**

- [ ] Author `phi_detector.py` with the canonical regex set. Include
      the FHIR bundle-shape patterns that look like raw chart text
      ("name": "..." nested with PII fields).
- [ ] Author the deny-by-default key whitelist in `langsmith_client.py`.
      Whitelist: structural span keys only (`run_id`, `parent_run_id`,
      `worker`, `latency_ms`, `decision`, `decision_reason`,
      `input_sha256`, `output_sha256`, `pseudonym`).
- [ ] Replace every direct LangSmith API call in v1 with `send_span`
      so nothing bypasses the layer. Grep audit.
- [ ] Author tests covering every PHI pattern and the whitelist drop
      semantics.
- [ ] Pseudonym helper: HMAC-SHA256 of `patient_id` with
      `COPILOT_PSEUDONYM_SALT` env. Reversible only inside the agent.

**Acceptance**

- [ ] Every direct LangSmith call in `agent-service/` routes through
      `send_span`. (Grep test enforced by the pre-push eval gate.)
- [ ] All unit tests pass; integration test confirms no LangSmith
      API call made on a PHI-bearing span.
- [ ] `spans_redacted_total` metric increments on each drop.
- [ ] Once W2-11 ships, the `phi.span_redaction` rubric blocks the
      pre-push gate on any future PHI leak (Appendix A.2 clause 6).

---

## Eval-suite bucket inventory (cross-MR view)

For convenience, a single view of the 50 eval cases distributed across
MRs. The harness uses these bucket names directly (`evals/w2/<bucket>/`).

| Bucket | Count | Owner MR |
|---|---|---|
| `extraction-lab` | 12 | W2-03 |
| `extraction-intake` | 10 | W2-04 |
| `reconciliation` | 8 | W2-08 |
| `retrieval` | 8 | W2-06 |
| `citation-separation` | 6 | W2-07 |
| `rbac` | 4 | W2-09 |
| `abstention` | 2 | W2-10 |
| **Total** | **50** | |

`phi.span_redaction` and `latency.stage_p95` are *rubric classes* that
run *across all 50 cases*, not their own bucket. They live in the
W2-11 + W2-12 ownership.

---

## Backlog (post-Week-2)

Tracked here so they don't leak into Week 2 scope. Each is a candidate
for a follow-up MR after Sunday.

- **W2-R1 — Third document type (referral fax or medication list).**
  PRD2 §1 lists this as a stretch goal. Adds one schema +
  `documents/extractor.py` branch + 8 eval cases. Doesn't change
  architecture.
- **W2-R2 — Lab trend chart widget.** Stretch goal from the PRD2
  scenario. Pulls extracted Observation values into a small
  Chart.js / vanilla SVG widget on the chart side panel.
- **W2-R3 — Contextual retrieval improvements.** Better chunking
  strategies (parent-doc retrieval), domain-specific filters,
  per-user-context query rewriting. Only after W2-06 eval data shows
  retrieval is the bottleneck.
- **W2-R4 — Verifier-model second pass.** v3 PRD listed it as
  post-MVP; still post-MVP for Week 2. Reconsider after the
  citation-OCR check + critic combo's eval performance is measured.
- **W2-R5 — User-facing eval dashboard.** PRD2 §12 lists as Phase 2.
  When committed eval results exceed ~5 runs, the Markdown summaries
  become hard to scan; build a dashboard then.
- **W2-R6 — Move to a HIPAA-eligible deploy operator.** v1 carried
  this; Week 2 keeps Railway. Trigger when pilot users move from demo
  to real PHI.

---

## Document map

This file is the build plan; the contracts it implements live elsewhere.

- `PRD.md` (v3) — Week 1 source of truth.
- `PRD2.md` — Week 2 source of truth, including Appendix A normative
  decisions.
- `ARCHITECTURE.md` — Week 1 architecture defense.
- `W2_ARCHITECTURE.md` — Week 2 architecture; how-to to PRD2's what-and-
  why.
- `TASKS.md` — Week 1 build plan (MR R1 / R2 / R3 / R4 follow-ups
  still relevant if not yet shipped).
- `TASKS2.md` (this file) — Week 2 build plan.
- `COST.md` — cost / latency report; Week 2 section appended in W2-11.
- `README.md` — top-level demo, deployed URL, two-service startup;
  Week 2 section appended in W2-11.
- `USERS.md` — primary user persona; unchanged.
- `AUDIT.md` — risks + audit findings; Week 2 audit follow-ups land
  here as risks resolve.
