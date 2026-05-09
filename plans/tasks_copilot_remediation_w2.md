# Copilot submission remediation — Week 2 task tracker

Companion to `plans/copilot_submission_remediation.md`. Source-of-truth for which
PR each item ships in. Check boxes only after the code lands AND the listed
verification command passes — per memory, checkboxes drift if marked optimistically.

PR sizing target: each PR should be reviewable in one sitting (~300-600 LOC of
substantive changes excluding fixtures/tests). Backfill fixtures count as data,
not code, for sizing.

---

## File structure (everything this work touches)

```
openemr/                                              (repo root)
├── plans/
│   ├── copilot_submission_remediation.md            [existing — plan]
│   └── tasks_copilot_remediation_w2.md              [this file]
├── db/Migrations/
│   └── Version<ts>.php                              [NEW — PR 2]
├── interface/copilot/
│   ├── api/
│   │   ├── save_document.php                        [EDIT — PR 2]
│   │   └── document_page.php                        [NEW — PR 5]
│   ├── partials/                                    [NEW dir — PR 5]
│   │   └── citation_overlay.php                     [NEW — PR 5]
│   ├── lab_review.php                               [EDIT — PR 3, PR 5]
│   └── document_review.php                          [EDIT — PR 3, PR 5]
├── src/Services/Copilot/
│   ├── AgentResponse.php                            [EDIT — PR 1, PR 3]
│   ├── ExtractedFieldHelper.php                     [EDIT — PR 1, PR 5]
│   └── ChartWrite/
│       ├── ChartWriteOrchestrator.php               [maybe EDIT — PR 2 (Pre-flight #6)]
│       └── ChartWriteService.php                    [maybe EDIT — PR 2 (Pre-flight #6)]
├── tests/Tests/Services/Copilot/ChartWrite/
│   ├── ChartWriteServiceTest.php                    [existing — must stay green]
│   └── SaveDocumentEndpointIdempotencyTest.php      [NEW — PR 2]
├── scripts/
│   └── install-pre-push-hook.php                    [NEW — PR 6]
├── composer.json                                    [EDIT — PR 6]
├── CONTRIBUTING.md                                  [EDIT — PR 6]
└── agent-service/
    ├── data/extracted/*.json (~30 files)            [EDIT — PR 1 backfill]
    ├── scripts/
    │   └── backfill_citation_fields.py              [NEW — PR 1]
    ├── src/clinical_copilot/
    │   ├── app_state.py                             [EDIT — PR 3 boot log]
    │   ├── corpus/rerank.py                         [EDIT — PR 3 labels]
    │   ├── db/migrations/versions/
    │   │   └── 0004_agent_traces_extension.py       [NEW — PR 4]
    │   ├── main.py                                  [EDIT — PR 1, PR 3, PR 4 (`_supervisor_to_agent_response` lives here)]
    │   ├── db/
    │   │   └── models.py                             [EDIT — PR 4 (`AgentTrace` columns)]
    │   ├── documents/
    │   │   ├── schemas/citation.py                  [EDIT — PR 1]
    │   │   ├── extractor.py                         [EDIT — PR 1]
    │   │   └── extractors/{referral_docx,workbook_xlsx,hl7_adt,hl7_oru,_hl7_common}.py
    │   │                                            [EDIT — PR 1 path threading]
    │   ├── observability/
    │   │   ├── metrics.py                           [reference — template for traces]
    │   │   └── traces.py                            [NEW — PR 4]
    │   ├── verification/
    │   │   └── middleware.py                        [maybe EDIT — PR 1 if resolution shape changes]
    │   └── orchestrator/
    │       ├── agent.py                             [EDIT — PR 4 call site + trace-writer constructor injection]
    │       ├── llm_gateway.py                       [EDIT — PR 4 token threading]
    │       ├── schemas.py                           [EDIT — PR 1, PR 3]
    │       ├── supervisor.py                        [EDIT — PR 4 (token aggregation; not the response builder)]
    │       ├── supervisor_langgraph.py              [EDIT — PR 4]
    │       └── workers/evidence_retriever.py        [EDIT — PR 1, PR 3, PR 4]
    └── tests/
        ├── unit/
        │   ├── documents/test_citation.py           [EDIT — PR 1]
        │   ├── observability/                       [NEW dir — PR 4]
        │   │   └── test_traces.py                   [NEW — PR 4]
        │   ├── orchestrator/test_evidence_retriever_worker.py [EDIT — PR 3]
        │   └── verification/                        [maybe EDIT — PR 1 if middleware changes]
        └── integration/
            ├── test_agent_query_writes_trace.py     [NEW — PR 4]
            └── test_document_ingest_writes_trace.py [NEW — PR 4]
```

Pre-flight #1 confirmed: Doctrine migrations live at `db/Migrations/` (sole
existing version `Version00000000000000.php`).
Pre-flight #2 confirmed: latest Alembic version is `0003_request_outcomes.py`,
so PR 4 adds `0004_agent_traces_extension.py`.

---

## PR 0 — Pre-flight discovery (no code; produces findings)

Goal: answer the six pre-flight questions in the plan before any PR opens.
Findings get pasted into this file under each PR's "Decisions from pre-flight"
line so reviewers see what shaped the design.

- [x] **Pre-flight #1** — Doctrine migrations path: `db/Migrations/`
- [x] **Pre-flight #2** — latest Alembic version is `0003_request_outcomes.py`; new file follows `0004_<short_name>.py` convention
- [x] **Pre-flight #3** — page-image source decision (agent-service cache vs. OpenEMR render)
- [x] **Pre-flight #4** — enumerate current rerank-backend label strings verbatim from `evidence_retriever.py` and `corpus/rerank.py`
- [x] **Pre-flight #5** — identify the exact citation field path(s) on `AgentResponse` in `agent-service/src/clinical_copilot/orchestrator/schemas.py`; confirm `extra="forbid"`
- [x] **Pre-flight #6** — verify `ChartWriteOrchestrator` connection invariant; decide whether PR 2 ships TTL clause or drops it

**Decisions captured here:**
- **Pre-flight #3 result:** Render PHP-side from OpenEMR document blob storage (`DocumentService::getFile()` at `src/Services/DocumentService.php:162-177`). agent-service VLM extraction renders pages in-memory only via pypdfium2 → BytesIO → Anthropic vision API → discarded (`agent-service/src/clinical_copilot/documents/fetcher.py:44-155`); the temp input file is deleted in `finally` (`main.py:929-953`). No durable cache to expose. PR 5's `document_page.php` becomes a renderer, not a proxy.
- **Pre-flight #4 old-label list:**
  - `agent-service/src/clinical_copilot/orchestrator/workers/evidence_retriever.py:114` `"cohere"` (keep)
  - `agent-service/src/clinical_copilot/orchestrator/workers/evidence_retriever.py:128` `"llm-judge"` → `"llm_judge"`
  - `agent-service/src/clinical_copilot/orchestrator/workers/evidence_retriever.py:54,132` `"none"` → `"bm25_only"`
  - `agent-service/src/clinical_copilot/evals/extraction/runner.py:166` `"bm25-only"` → `"bm25_only"`
  - `agent-service/src/clinical_copilot/corpus/rerank.py` log event names already underscored (`corpus.rerank.llm_judge_ok`, `corpus.rerank.cohere_ok`) — no change.
  - Tests to update: `tests/unit/orchestrator/test_evidence_retriever_worker.py` lines 85, 91, 106, 124, 139, 141; `tests/unit/corpus/test_rerank.py:140,161`; `tests/unit/corpus/test_rerank_cohere.py:199,220`.
- **Pre-flight #5 citation field path:** **Citations are not on `AgentResponse` today.** The wire shape carries opaque `source_id` strings only:
  - `CitedClaim.source_id: str` (`agent-service/src/clinical_copilot/orchestrator/schemas.py:71`)
  - `Card.source_ids: list[str]` (`agent-service/src/clinical_copilot/orchestrator/schemas.py:54`)
  - `ToolResult.records[i].source_id: str` (record types in `tools/records.py`)
  - `extra="forbid"` confirmed via `_Frozen` base class: `model_config = ConfigDict(frozen=True, extra="forbid")` at `orchestrator/schemas.py:23`. `AgentResponse`, `CitedClaim`, `Card` all inherit `_Frozen`.
  - `_supervisor_to_agent_response` lives at `agent-service/src/clinical_copilot/main.py:406`; populates `cards`, `prose`, `tool_results` at `:472-474`.
  - `SourceCitation` exists only in the extraction layer (`documents/schemas/citation.py:20-52`), attached to `ExtractedField[T].citation`. It never reaches the wire.
  - **This reshapes Gap 1 design — see "Design decision (Pre-flight #5 reframe)" inside PR 1 below.**
- **Pre-flight #6 connection wiring:** Same global ADODB handle (`OEGlobalsBag`) everywhere — `ChartWriteOrchestrator`/`ChartWriteService` writers all dispatch through `QueryUtils::sqlInsert()` → `QueryUtils::getADODB()` → `OEGlobalsBag::getInstance()->get('adodb')['db']`. **No split-connection risk.** TTL clause is **safe to ship**.
  - Caveat: today nothing wraps `ChartWriteOrchestrator->run()` in an explicit transaction. Existing-patient path at `interface/copilot/api/save_document.php:241-246` runs the documents UPDATE + chart writes in autocommit; new-patient path COMMITs at `:637` *before* `ChartWriteOrchestrator->run()` at `:643`.
  - **Fix in PR 2 (no separate prerequisite PR):** move both `run()` call sites inside the marker-UPDATE/finalizing-UPDATE BEGIN/COMMIT block. No constructor refactor of `ChartWriteOrchestrator` needed since the global handle is already shared.

---

## PR 1 — Citation schema: three-class discriminated union (Gap 1)

**Branch suggestion:** `feat/copilot-citation-discriminated-union`
**Depends on:** Pre-flight #5
**Unblocks:** PR 5 (bbox overlay needs `field_or_chunk_id` + `bbox` on `SourceCitation`)

### Design decision (Pre-flight #5 reframe — Option 1: add `citation` alongside `source_id`)

Pre-flight #5 found that `AgentResponse` carries no citation objects today — only opaque
`source_id: str` references on `CitedClaim`, `Card.source_ids`, and `ToolResult.records[i]`.
The verification middleware (`citation_check.py`, `field_check.py`) joins on these strings.

**Resolution:** keep `source_id` as the canonical verification key; add a new optional
`citation: Citation | None` (and `Card.citations: list[Citation] = Field(default_factory=list)`)
as **display/enrichment metadata only**. The verifier is untouched in PR 1. PHP renders
`citation` metadata when present and falls back to `source_id` when absent (fast-lane / v1
responses can leave it `None` during rollout).

```python
class CitedClaim(_Frozen):
    text: str
    source_id: str                # unchanged — canonical verification key
    citation: Citation | None = None     # NEW — display metadata
    source_field: str | None = None
    expected_value: str | None = None

class Card(_Frozen):
    title: str
    kind: str
    source_ids: list[str]         # unchanged — canonical
    citations: list[Citation] = Field(default_factory=list)   # NEW — display metadata
```

**Drift guard:** unit-test invariant — when `citation` is present, its canonical id must
match `source_id`. Mapping per type:
- `PatientChartCitation`: `field_or_chunk_id == source_id` (e.g. `Observation/123`)
- `GuidelineCitation`: `field_or_chunk_id == chunk_id == source_id`
- `SourceCitation`: mapping deferred until field-path scheme is locked in this PR; assert in
  tests but not at construction time.

Not a Pydantic `model_validator` — a runtime failure here would abort a clinician-facing
response. CI-only enforcement is sufficient and safer.

**What does NOT change in PR 1:**
- `verification/middleware.py`, `citation_check.py`, `field_check.py` — they still join on
  `source_id` strings.
- The wire-protocol contract for existing consumers — `citation` is additive and nullable.

### Files
- NEW: `agent-service/scripts/backfill_citation_fields.py`
- NEW (test): cases added to `agent-service/tests/unit/documents/test_citation.py`
- EDIT: `agent-service/src/clinical_copilot/documents/schemas/citation.py`
- EDIT: `agent-service/src/clinical_copilot/documents/extractor.py`
- EDIT: `agent-service/src/clinical_copilot/documents/extractors/{referral_docx,workbook_xlsx,hl7_adt,hl7_oru,_hl7_common}.py` (per-format adapters — path threading)
- EDIT: `agent-service/src/clinical_copilot/orchestrator/schemas.py` (add `citation` to `CitedClaim`, `citations` to `Card` — Option 1; `source_id`/`source_ids` untouched)
- EDIT: `agent-service/src/clinical_copilot/main.py` (`_supervisor_to_agent_response` at `main.py:406` — populate the new optional `citation`/`citations` fields where source metadata is available)
- EDIT: `agent-service/src/clinical_copilot/orchestrator/workers/evidence_retriever.py` (construct `GuidelineCitation` at the retrieval-results boundary)
- EDIT: `agent-service/src/clinical_copilot/orchestrator/chart_pack.py` (construct `PatientChartCitation` from chart-pack records)
- EDIT: `agent-service/data/extracted/*.json` (~30 files — backfill output)
- EDIT: `src/Services/Copilot/ExtractedFieldHelper.php` (surface `source_type`, `field_or_chunk_id`)
- EDIT: `src/Services/Copilot/AgentResponse.php` (parse optional `citation` / `citations` per discriminator; tolerate absence)

**Verification middleware is untouched in PR 1.** `citation_check.py` / `field_check.py` /
`verification/middleware.py` keep joining on `source_id` / `source_ids` strings exactly as
today — Option 1's whole point is to avoid touching the verifier. If a future PR moves
verification onto `citation` objects, that is a separate piece of work.

**Note on `corpus/rerank.py`:** keep that module focused on ranking. Citation
construction belongs at the retrieval-results boundary (`evidence_retriever.py`)
or at the chart-pack producer (`orchestrator/chart_pack.py`), where the source
metadata is already in hand.

### Subtasks (must run in this order — fixture rollout)
- [ ] Add `CitationSourceType` StrEnum + new `GuidelineCitation` and `PatientChartCitation` classes to `citation.py`; add `source_type` and `field_or_chunk_id` to `SourceCitation` **with temporary defaults** so existing fixtures don't break
- [ ] Write `backfill_citation_fields.py`; run against `agent-service/data/extracted/*.json`; commit fixtures
- [ ] Tighten `field_or_chunk_id` to required on `SourceCitation` (drop the temporary default)
- [ ] Add `Citation = Annotated[Union[...], Field(discriminator="source_type")]` alias; export from `citation.py`
- [ ] Thread schema-walk path as parameter into a single `build_extracted_citation(path, ...)` helper; update extractor + 7 adapters (do NOT duplicate wiring at each call site)
- [ ] Add `citation: Citation | None = None` to `CitedClaim` and `citations: list[Citation] = Field(default_factory=list)` to `Card` in `orchestrator/schemas.py` (Option 1 — alongside `source_id` / `source_ids`)
- [ ] Update `_supervisor_to_agent_response()` in `main.py:406` to populate `citation`/`citations` from the in-scope source records; leave `None` / `[]` when no metadata is available (fast-lane, no-retrieval — drives Risk 2's `rerank_backend: None` in PR 3)
- [ ] Construct `GuidelineCitation` in `workers/evidence_retriever.py` (retrieval-results boundary)
- [ ] Construct `PatientChartCitation` in `orchestrator/chart_pack.py` (chart-pack source records)
- [ ] Leave `corpus/rerank.py` ranking-only — no citation construction here
- [ ] Confirm `PatientChartCitation.display_summary` is one-line label, NOT verbatim resource text (PHI surface)
- [ ] **Drift-guard test (CI only, NOT a Pydantic `model_validator`):** at every site that constructs a `CitedClaim` or `Card` with non-None citations, the test asserts `citation.field_or_chunk_id == source_id` (and the per-class mapping listed in the design block above) for `PatientChartCitation` and `GuidelineCitation`. `SourceCitation` mapping deferred to PR 5.
- [ ] Update `tests/unit/documents/test_citation.py`: round-trip each class, mixed-list discriminator round-trip, cross-class rejection
- [ ] Update PHP `ExtractedFieldHelper` to surface new fields; update `AgentResponse.php` parser for the three citation shapes (and for `citation` being absent on legacy / fast-lane responses)
- [ ] If Pydantic raises a discriminator-resolution error, fall back to bare-string `Literal[...]` (per the StrEnum gotcha note in plan §Gap 1)

### Verification (acceptance criteria from plan §Gap 1)
- [ ] `pytest agent-service/tests/unit/documents/test_citation.py -v` green
- [ ] `pytest agent-service/tests/ -k extract` green
- [ ] `pytest agent-service/tests/ -k "agent_response or supervisor"` green
- [ ] `pytest agent-service/tests/unit/verification/ -v` still green **without modification** (Option 1 invariant: middleware is untouched)
- [ ] Drift-guard test: `pytest agent-service/tests/ -k citation_canonical_id` green
- [ ] `composer phpstan` passes with no new baseline entries
- [ ] Manual: load lipid-panel review, inspect Network tab; `citation` objects appear on supervisor responses where source metadata is available; absent (`null` / `[]`) where it isn't, with PHP falling back to `source_id`

---

## PR 2 — Idempotent chart-write save endpoint (Gap 2)

**Branch suggestion:** `fix/copilot-save-document-idempotency`
**Depends on:** Pre-flight #6 result
**Highest-severity correctness item.**

### Files
- NEW: `db/Migrations/Version<timestamp>.php` (Doctrine — adds `chart_written_at`, `chart_write_started_at`, `chart_write_summary`)
- NEW: `tests/Tests/Services/Copilot/ChartWrite/SaveDocumentEndpointIdempotencyTest.php`
- EDIT: `interface/copilot/api/save_document.php`
- maybe EDIT: `src/Services/Copilot/ChartWrite/ChartWriteOrchestrator.php` (only if Pre-flight #6 finds a separate connection)
- maybe EDIT: `src/Services/Copilot/ChartWrite/ChartWriteService.php` (same condition)
- existing — must stay green: `tests/Tests/Services/Copilot/ChartWrite/ChartWriteServiceTest.php`

### Implementation notes (from this PR)
- Lock-acquire / chart-write / finalize cycle was lifted into a new
  `ChartWriteCoordinator` (`src/Services/Copilot/ChartWrite/ChartWriteCoordinator.php`)
  with the four outcomes encoded as `SaveOutcomeKind` enum cases
  (AcquiredAndWrote / IdempotentReplay / ConcurrentInFlight /
  DocumentNotFound). The endpoint stays a thin shell; the test exercises
  the coordinator directly against the real DB.
- Pre-flight #6 confirmed the global ADODB handle is shared across all
  writers, so the TTL clause shipped (default 300 seconds via the
  coordinator's `lockTtlSeconds` constructor arg). No
  `ChartWriteOrchestrator` constructor changes were needed.
- Migration uses LONGTEXT for `chart_write_summary` (not the MariaDB
  JSON alias) so the column reads back as a plain string regardless of
  driver flags. Encode/decode via `json_encode`/`json_decode`; no
  server-side JSON path queries.
- New-patient idempotency recovery: when a retry finds
  `documents.foreign_id` already set to the previously-created pid, the
  endpoint short-circuits past the demographic-extraction +
  duplicate-patient guard and reuses that pid — keeping the
  patient-INSERT side and the chart-write side independently
  recoverable across partial failures.
- `chart_write_summary` JSON shape: `pid`, `patient_created`,
  `document_type`, `document_id`, `selected_sections`, `counts`. The
  endpoint reconstructs the success-page redirect from these on replay
  (rather than storing a denormalised `redirect_target`, which would
  bake `webroot` into the DB).

### Subtasks
- [x] Pre-flight #6 result documented at top of this PR's branch description (see above)
- [x] If split-connection: thread active connection into `ChartWriteOrchestrator` constructor + writer dispatch; update all callers — **before** the endpoint change → not needed (Pre-flight #6: shared global handle)
- [x] If split-connection cannot be cleanly fixed: drop the TTL `OR chart_write_started_at < NOW() - INTERVAL 5 MINUTE` clause; document the manual-cleanup path → not needed; TTL clause shipped
- [x] Doctrine migration: up adds three columns, down drops them (never empty `down()`); pick `JSON` vs `LONGTEXT` after confirming MariaDB JSON support on this branch → `db/Migrations/Version20260509201506.php`, LONGTEXT
- [x] Implement endpoint sequence in `save_document.php`:
  - [x] PUT validated facts to agent-service (HTTP, outside txn)
  - [x] BEGIN
  - [x] atomic conditional UPDATE on `documents` (the lock acquisition)
  - [x] check `affected_rows`; branch into 200/409/200-idempotent based on row state
  - [x] ChartWriteOrchestrator->run() **on the same connection**
  - [x] finalizing UPDATE setting `chart_written_at` + `chart_write_summary`
  - [x] COMMIT
- [x] `chart_write_summary` JSON shape includes: `pid`, `patient_created`, `document_type`, `selected_sections`, `row_counts` (`counts`), `document_id` (replacing `redirect_target` — see Implementation notes)
- [x] On caught Throwable inside txn: ROLLBACK, generic 500 to user (no `$e->getMessage()` leaked) → handled by `QueryUtils::inTransaction()` rolling back + rethrowing; the endpoint's outer catch returns the generic 500
- [x] Single-submit + identical re-submit test: second response has `idempotent: true`; row counts unchanged; `chart_write_summary` round-trips → `testIdenticalResubmitReturnsIdempotentReplayWithoutDoubleWriting`, `testFirstSubmitAcquiresLockAndWritesChart`
- [x] Concurrent-submit test: two near-simultaneous POSTs; exactly one wins; loser gets 409 or `idempotent: true` per timing → `testConcurrentSubmitReturnsConcurrentInFlightOutcome`, `testRecentLockWithinTtlIsRejected`
- [x] Stale-lock test (only if TTL clause shipped): manually backdate `chart_write_started_at` past TTL; new submit acquires lock and completes → `testStaleLockOlderThanTtlIsClaimedByNewSubmit`

### Verification
- [x] `vendor/bin/phpunit -c phpunit.xml --testsuite=services --filter ChartWrite` green (25 tests, 95 assertions)
- [x] `vendor/bin/phpunit ... --filter testWriteAllergiesDoesNotDedupeOnRepeatCall` still green (1 test, 3 assertions)
- [x] `composer phpstan` clean (no new baseline entries)
- [ ] Manual: re-submit lipid-panel review twice → second shows `idempotent=1`; MySQL row counts unchanged
- [ ] Manual concurrent: two browser tabs, click Save in both within ~50ms → exactly one set of rows
- [ ] `patient_choice=new` retry → same `pid` and redirect target (recovery branch in `save_document.php`)

---

## PR 3 — Surface active rerank backend (Risk 2)

**Branch suggestion:** `feat/copilot-rerank-backend-flag`
**Depends on:** Pre-flight #4 (verbatim old-label list), Pre-flight #5 (`extra="forbid"` confirmation)
**Cheap demo protection — bumped ahead of Gaps 3-4 per plan §Execution order.**

### Files
- EDIT: `agent-service/src/clinical_copilot/orchestrator/workers/evidence_retriever.py`
- EDIT: `agent-service/src/clinical_copilot/corpus/rerank.py` (label string emission only)
- EDIT: `agent-service/src/clinical_copilot/orchestrator/schemas.py` (`AgentResponse.rerank_backend`)
- EDIT: `agent-service/src/clinical_copilot/main.py` (`_supervisor_to_agent_response` at `main.py:406`)
- EDIT: `agent-service/src/clinical_copilot/app_state.py` (boot log line)
- EDIT: `agent-service/tests/unit/orchestrator/test_evidence_retriever_worker.py`
- EDIT: `interface/copilot/chat.php` (UI badge — chat panel is the `/api/agent/query` consumer)
- EDIT: `interface/copilot/side_panel.php` (same — slow-lane responses surface here)
- EDIT: `src/Services/Copilot/AgentResponse.php` (parse new top-level field)

**Note:** `lab_review.php` / `document_review.php` consume the *extraction* response,
not `/api/agent/query`. They don't carry `rerank_backend` and don't get the badge.

### Subtasks
- [x] Use Pre-flight #4 list to grep + replace old labels → canonical `"cohere" | "llm_judge" | "bm25_only"` (note: underscored)
- [x] Add `rerank_backend: Optional[Literal["cohere", "llm_judge", "bm25_only"]] = None` to `AgentResponse`
- [x] Wire through `_supervisor_to_agent_response()` in `main.py:406`; populate ONLY when a reranker actually ran (None on fast-lane / no-retrieval) — also threaded through `SupervisorResponse` (plain-Python + LangGraph) and the new TurnState `rerank_backend` slot
- [x] Boot log: `supervisor.rerank_backend_resolved=<label>` in `app_state.py` based on Cohere + Anthropic key presence
- [x] Per-request log `corpus.rerank.backend_used=<label>` only from paths that invoke a reranker (LangGraph evidence-retriever node + plain-Python `supervisor.synth`)
- [x] UI badge in `chat.js` and `side_panel.js` (the JS that renders `/api/agent/query` results — `chat.php`/`side_panel.php` are the shells): show only when non-null AND not `"cohere"`; copy: `"rerank: llm-judge fallback"` / `"rerank: BM25 only — degraded"`
- [x] Worker test asserts each of three labels under matching mock conditions
- [ ] Boot-log test: `bm25_only` resolved when neither client configured — deferred (harness needs `build_app_state` integration scaffolding; the boot log itself emits via existing `structlog` wiring)
- [x] PHP test: response parser accepts `rerank_backend` top-level field without throwing — `AgentResponse` stores body as-is, so `QueryControllerTest`'s existing `new AgentResponse(...)` constructions cover the path

### Deployment (manual — NOT in PR scope; flag for user)
- [ ] Set `COHERE_API_KEY` on Railway agent-service env
- [ ] Verify post-deploy: `curl -X POST https://<railway-host>/api/agent/query ...` shows `"rerank_backend": "cohere"`

### Verification
- [x] `pytest agent-service/tests/unit/orchestrator/test_evidence_retriever_worker.py -v` green (also `pytest tests/` 654 passed locally)
- [x] `composer phpstan` clean (no new baseline entries)
- [ ] Local: vary env vars, hit `/api/agent/query`, verify response field + UI badge for each of three states
- [ ] Fast-lane response has `"rerank_backend": null`, no badge

---

## PR 4 — Wire usage + retrieval + confidence into `agent_traces` (Gap 3)

**Branch suggestion:** `feat/copilot-agent-traces-wiring`
**Depends on:** Pre-flight #2

### Files
- NEW: `agent-service/src/clinical_copilot/db/migrations/versions/0004_agent_traces_extension.py`
- NEW: `agent-service/src/clinical_copilot/observability/traces.py`
- NEW dir: `agent-service/tests/unit/observability/`
- NEW: `agent-service/tests/unit/observability/test_traces.py`
- NEW: `agent-service/tests/integration/test_agent_query_writes_trace.py`
- NEW: `agent-service/tests/integration/test_document_ingest_writes_trace.py` (v5 expanded traces beyond `/api/agent/query`)
- EDIT: `agent-service/src/clinical_copilot/db/models.py` (add `retrieval_hits`, `extraction_confidence` columns to `AgentTrace` ORM class)
- EDIT: `agent-service/src/clinical_copilot/app_state.py` (construct `TracesService` with shared `session_factory`; expose for injection)
- EDIT: `agent-service/src/clinical_copilot/main.py` (aggregate `UsageTotals` into the response build at `_supervisor_to_agent_response`; emit trace at `/api/agent/query` and document-ingest entry points)
- EDIT: `agent-service/src/clinical_copilot/orchestrator/agent.py` (constructor accepts `TracesService`; existing `self._metrics.record(...)` site at `:217` gets a sibling `self._traces.record(...)`)
- EDIT: `agent-service/src/clinical_copilot/orchestrator/llm_gateway.py` (add `input_tokens`/`output_tokens` to `LlmTurn`)
- EDIT: `agent-service/src/clinical_copilot/orchestrator/supervisor.py` (token aggregation only — response-build is in `main.py`)
- EDIT: `agent-service/src/clinical_copilot/orchestrator/supervisor_langgraph.py`
- EDIT: `agent-service/src/clinical_copilot/corpus/rerank.py` (capture tokens at any Anthropic critic/judge call)
- EDIT: document-ingest route handler that triggers `documents/extractor.py` (locate via `rg "extractor.py" agent-service/src/clinical_copilot/`)
- reference (template only): `agent-service/src/clinical_copilot/observability/metrics.py`

### Subtasks
- [x] Alembic migration `0004_agent_traces_extension.py`: up adds `retrieval_hits INTEGER NULL`, `extraction_confidence REAL NULL`; down drops them — verified upgrade head + downgrade to 0003 round-trip cleanly against SQLite
- [x] Do NOT edit `0001_initial.py`
- [x] Update `db/models.py` `AgentTrace` ORM class with the two new columns (must match migration column types/nullability or SQLAlchemy will mismap)
- [x] Add `input_tokens: int`, `output_tokens: int` fields to `LlmTurn` in `llm_gateway.py`; populated from `response.usage` in `AnthropicLlmGateway.complete`
- [x] Populate at every Anthropic call site: v1 LLM gateway, plain-Python supervisor, `corpus/rerank.py` LLM-judge (returns `(chunks, UsageTotals)`), `planner.py` (returns `(sub_queries, UsageTotals)`), `critic.py` (`judge`/`_run_judge` return `(Verdict, UsageTotals)`), and `supervisor_langgraph.py` synthesizer node — all surfaced via the LangGraph state's new `usage_totals` key with the `_add_usage` reducer in `state.py`
- [x] Aggregate into a `UsageTotals` field on the supervisor response (defined in `observability/traces.py`; defined `__add__` so node-level partials can fold)
- [x] Write `traces.py` mirroring `metrics.py` (fail-open, never raises into clinician path); same `session_factory` from `app_state.py`
- [x] Method signature: `record(TraceRecord(request_id, user_id, role, lane, latency_ms, token_in, token_out, model_tier, retrieval_hits, extraction_confidence))`
- [x] Construct `TracesService` in `app_state.py` and inject into the orchestrator (`agent.py` constructor) and the supervisor branch via `AppState.traces_service`
- [x] Wire `self._traces.record(...)` at every request entry point that runs extraction or retrieval — `/api/agent/query` v1 fallback + supervisor (plain-Python and LangGraph) branches + `/api/agent/internal/ingest`
- [x] NULL semantics are independent per column (slow-lane retrieval-only → `extraction_confidence` NULL; doc-ingest extraction-only → `retrieval_hits` NULL); v1 fallback writes both NULL since it never invokes either subsystem
- [x] Unit test: token totals threaded through the v1 LlmTurn loop (covered by `test_agent_query_writes_one_trace_row`)
- [x] Unit test: fail-open on DB error (`test_db_exception_does_not_propagate`)
- [x] Unit test: NULL handling per independent-column semantics (`test_extraction_confidence_only_row_keeps_retrieval_hits_null` + sibling)
- [x] Integration test: POST `/api/agent/query` → exactly one row in `agent_traces` with summed token counts (`test_agent_query_writes_one_trace_row`)
- [x] Integration test: document-ingest path → exactly one row with `extraction_confidence` populated and `retrieval_hits` NULL (`test_document_ingest_writes_trace.py`) — monkeypatches `clinical_copilot.main.run_extraction` with a stub returning a real `LabPdfFacts` so we don't hit Anthropic

### Verification
- [x] `pytest agent-service/tests/unit/observability/ -v` green (15 tests)
- [x] `pytest agent-service/tests/integration/test_agent_query_writes_trace.py -v` green
- [x] `pytest agent-service/tests/integration/test_document_ingest_writes_trace.py -v` green
- [ ] Local doc-ingest of lipid-panel fixture → one new `agent_traces` row with `extraction_confidence` populated, `retrieval_hits` NULL
- [ ] Local slow-lane `/api/agent/query` → row with `retrieval_hits` populated, `extraction_confidence` NULL
- [x] DB outage simulation does not surface error to clinician (covered by `test_db_exception_does_not_propagate`)

---

## PR 5 — Bbox canvas overlay MVP (Gap 4)

**Branch suggestion:** `feat/copilot-bbox-overlay`
**Depends on:** PR 1 (needs `field_or_chunk_id` + `bbox` on `SourceCitation`), Pre-flight #3

### Files
- NEW: `interface/copilot/api/document_page.php` (PHP-side renderer — fetches OpenEMR blob via `DocumentService::getFile()`, renders the requested page to PNG, streams it back; copilot session-gated; same-origin URL `/interface/copilot/api/document_page.php?document_id=...&page=...`)
- NEW dir: `interface/copilot/partials/`
- NEW: `interface/copilot/partials/citation_overlay.php` (img + canvas + JS)
- EDIT: `interface/copilot/lab_review.php` (replace placeholder `include` partial)
- EDIT: `interface/copilot/document_review.php` (same)
- EDIT: `src/Services/Copilot/ExtractedFieldHelper.php` (surface full `SourceCitation` for JS payload — bbox + page + source_type + field_or_chunk_id)
- NEW (test): PHPUnit test for `document_page.php` auth gate + DTO carrying bbox

**No agent-service changes in PR 5.** Per Pre-flight #3, agent-service does not durably
cache rendered pages. PHP renders from `DocumentService::getFile()` (`src/Services/DocumentService.php:162-177`)
on demand. Rendering library: pick from existing OpenEMR PDF deps (likely `mPDF` is already
present; if a separate PDF→PNG renderer is needed, prefer Imagick over a new dep — confirm
during implementation). Cache the rendered PNG on disk keyed by `(document_id, page)` with
a TTL aligned with OpenEMR's existing document cache (or no cache for v1 if rendering is
fast enough).

### Subtasks
- [ ] Pre-flight #3 decision documented in PR description (PHP-side render from OpenEMR blob)
- [ ] Pick PDF→PNG renderer from existing deps; document the choice in PR description
- [ ] `document_page.php`: copilot session/auth gate (mirror `interface/copilot/upload_document.php`); fetch document via `DocumentService::getFile()`; render requested page to PNG; stream with `Content-Type: image/png`
- [ ] (Optional v1) cache rendered PNG to disk keyed `(document_id, page)`; document the cache directory choice
- [ ] `citation_overlay.php`: `<img>` + absolutely-positioned `<canvas>` sized to image intrinsic dimensions; JS draws each `SourceCitation.bbox` scaled to image dims
- [ ] Skip `GuidelineCitation` and `PatientChartCitation` in overlay (no `bbox`); they appear elsewhere in slow-lane source list
- [ ] Click-to-highlight interaction: click row in field list → color-flip the box. Defer two-pane hover-sync (`plans/copilot_bbox_preview.md`)
- [ ] Replace line-14 placeholder note in `lab_review.php` with `include` of partial
- [ ] Same in `document_review.php`
- [ ] PHPUnit: projected DTO carries `bbox` + `page` on `SourceCitation`-shaped payloads
- [ ] PHPUnit: `document_page.php` returns 401/403 without session, 200 + PNG content-type with valid session

### Verification
- [ ] `curl -b "PHPSESSID=<session>" 'https://localhost:9300/interface/copilot/api/document_page.php?document_id=<id>&page=1'` returns PNG
- [ ] `curl 'https://...'` (no session) returns 401/403
- [ ] Manual: upload lipid-panel fixture → six rectangles render at expected positions; click row → matching highlight
- [ ] Network tab: only OpenEMR-origin image loads (no agent-service requests at all — Pre-flight #3 confirms agent-service never serves page images)

---

## PR 6 — Activate prek pre-push hook (Risk 1)

**Branch suggestion:** `chore/copilot-prek-pre-push-installer`
**Trivial; ship anytime once decided.**

### Files
- NEW: `scripts/install-pre-push-hook.php`
- EDIT: `composer.json` (`scripts.post-install-cmd`)
- EDIT: `CONTRIBUTING.md` (around line 73-75)

### Subtasks
- [ ] Write `scripts/install-pre-push-hook.php`: skip if `getenv('CI') === 'true'`; skip if `.git` absent (tarball install); run `prek install --hook-type pre-push` if `prek` on PATH; single-line WARN to stderr if missing; never exit non-zero
- [ ] `composer.json`: `scripts.post-install-cmd` runs `@php scripts/install-pre-push-hook.php`
- [ ] `CONTRIBUTING.md`: explicit pre-push paragraph, manual install command, fallback if prek unavailable

### Verification
- [ ] Fresh clone in temp dir + `composer install` (with prek on PATH) → `.git/hooks/pre-push` exists pointing at prek shim
- [ ] `CI=true composer install` → no hook installed, exit 0
- [ ] Without prek: install completes, single WARN line on stderr, exit 0

---

## End-to-end verification (after all six PRs land)

- [ ] Fresh clone → `composer install` → pre-push hook installed (or WARN on missing prek)
- [ ] `pytest agent-service/tests/ -v` and `docker compose exec openemr /root/devtools clean-sweep-tests` both green
- [ ] Local upload of lipid-panel fixture → review UI shows bbox rectangles via proxy → save → row counts match expectations
- [ ] Re-submit same document → `idempotent: true`; row counts unchanged; pid + redirect target match
- [ ] Two concurrent submits → exactly one set of rows; loser gets 409 or `idempotent: true`
- [ ] Slow-lane retrieval-only query produces `agent_traces` row with `retrieval_hits` populated, `extraction_confidence` NULL
- [ ] Doc-ingest produces row with `extraction_confidence` populated, `retrieval_hits` NULL
- [ ] `/api/agent/query` response includes `"rerank_backend": "cohere"` after Railway env set; UI shows no fallback badge
- [ ] Mixed three-citation-type response payload parses cleanly via `source_type` discriminator on PHP side

---

## Notes for the GitLab MR workflow

- One MR per PR section above. Title: `<type>(copilot): <short>` per Conventional Commits. Do NOT reference "PR N" in commit messages — internal numbering only (per memory).
- Add `Assisted-by: Claude Code` trailer when AI-assisted (CLAUDE.md §AI Assistance Trailer).
- Leave the `# claudeMd` checkboxes in this file unchecked until after the verification command for that subtask passes locally.
- Each MR description should paste in the relevant Pre-flight finding so reviewers see the decision rationale without opening this file.
