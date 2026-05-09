# Submission feedback remediation plan (v5)

## Context

A reviewer flagged four gaps and two passing-with-risk items on the Co-Pilot submission. The extraction pipeline, XOR validator, and hybrid retrieval stack were affirmed; the work below closes the gaps without rebuilding any of the affirmed pieces.

v5 incorporates five rounds of plan review:
- v2 fixed a TOCTOU race in idempotency, the retry-response shape, the page-image source decision, response-schema compatibility, and the rerank vocabulary.
- v3 fixed the Alembic-vs-Doctrine migration conflation, citation-shape semantics (separate retrieval class), `retrieval_hits` NULL conditions, the bbox verification URL, stale-lock recovery, and rerank-vocab enumeration.
- v4 split the retrieval citation class into `GuidelineCitation` and `PatientChartCitation` (chart sources are FHIR resources, not retrieval chunks), named the exact field on `AgentResponse` that becomes the discriminated union (no more hand-waving "source list"), strengthened the idempotency single-connection invariant from soft to non-negotiable, and added a Pydantic StrEnum-discriminator gotcha note.
- v5 reframes `PatientChartCitation.raw_text` as a one-line display summary (not verbatim source text — PHI surface), broadens trace writes beyond `/api/agent/query` to every extraction/retrieval call site (doc uploads are encounters too), and makes `rerank_backend` nullable so fast-lane / no-retrieval responses don't have to fabricate a value.

Out of scope: any reshape of the affirmed extraction or retrieval code paths, prod deploys (testers are using the deployed agent-service — flag, don't push), and any change to the `ChartWriteService`-level "no row dedup" lockdown (`tests/Tests/Services/Copilot/ChartWrite/ChartWriteServiceTest.php:127-138`). Idempotency lives at the endpoint, not the service.

Mirror this plan into `/Users/andynguyen/Desktop/OpenEMR/openemr/plans/` once execution begins (per repo convention).

---

## Pre-flight (must be done before any item below)

These are small fact-checks that change which path is correct for several items. Do them first; do not guess.

1. **Locate the OpenEMR Doctrine migration directory** (Gap 2 only). CLAUDE.md says "New schema changes use Doctrine Migrations" but does not give the path. Confirm by reading at least one recent copilot migration (`git log --diff-filter=A -- '**/Version*.php'` or `rg -l 'Doctrine\\Migrations\\AbstractMigration'`).

2. **Locate the latest agent-service Alembic version** (Gap 3 only). agent-service has its own DB; migrations live under `agent-service/src/clinical_copilot/db/migrations/versions/`. Read the most recent version file (`0001_initial.py` is known; check for newer ones) and follow that convention for the `agent_traces` extension. **This is independent of #1** — different DB, different migration system.

3. **Identify the canonical source of rendered page images** (Gap 4). Two candidates: (a) OpenEMR document blob storage (bytes available to PHP, render server-side), (b) agent-service extraction cache (already has rendered pages from VLM extraction; needs an HTTP route + an OpenEMR proxy route so the browser stays same-origin). Pick (b) if the cache is durable across restarts; (a) otherwise.

4. **Enumerate current rerank-backend label values verbatim** (Risk 2). Read `agent-service/src/clinical_copilot/orchestrator/workers/evidence_retriever.py` and write down the *exact* strings the worker emits today (e.g. `"cohere"`, `"llm-judge"` with hyphen, `"none"`). v4 standardizes on `"cohere" | "llm_judge" | "bm25_only"` (underscored), so the rename pass needs the precise old strings to grep for. Also enumerate the values in `corpus/rerank.py` log event names — those are likely already underscored (`corpus.rerank.llm_judge_ok`) but the backend label string passed to the response may differ.

5. **Identify the exact citation field(s) on `AgentResponse`** (Gap 1 — most important). Read `agent-service/src/clinical_copilot/orchestrator/schemas.py` end to end. Citations may live in any of:
   - a top-level `sources: list[...]` field,
   - inside `CitedClaim.citation` or `CitedClaim.sources`,
   - embedded in tool-result payloads (`tool_results[i].citations`),
   - or split across multiple of the above.
   Write down the exact field path(s) carrying citations today. Gap 1's discriminated-union work changes those fields' types — nothing else. Confirm `AgentResponse` is `extra="forbid"` while you're there (drives Risk 2's response-field addition).

6. **Verify `ChartWriteOrchestrator` connection invariant** (Gap 2 — non-negotiable). Read `src/Services/Copilot/ChartWrite/ChartWriteOrchestrator.php` and the `ChartWriteService` writers it calls. Confirm whether they execute on the connection holding the open transaction or on a separate handle. This is a **blocker**: if they use a separate connection, Gap 2's stale-lock TTL turns into a duplication bug (see Gap 2 design notes). Either fix the connection wiring before Gap 2, or drop the TTL clause.

---

## Gap 1 — Citation schema: three citation types in a discriminated union

### Design decision: three citation types, not two

Patient-chart citations point at FHIR resources (`Observation/123`, `MedicationRequest/456`); guideline citations are retrieval chunks with embeddings (`chunk_id`, `source_url`); extracted-document citations carry `page`/`bbox`/`raw_text`. These are three semantically different shapes — collapsing the latter two into one `RetrievalCitation` would force chart citations to carry a fake `chunk_id`. v4 uses three classes:

- **`SourceCitation`** (existing, kept document-shaped) — `page`, `bbox`, `raw_text`, plus the new `source_type=EXTRACTED_DOCUMENT` and `field_or_chunk_id` (JSON-pointer-style path).
- **`GuidelineCitation`** (new) — `chunk_id`, `source_doc_id`, `source_url`, `raw_text`, `confidence`, plus `source_type=GUIDELINE` and `field_or_chunk_id` (= chunk id).
- **`PatientChartCitation`** (new) — `resource_type` (`"Observation"`, `"MedicationRequest"`, etc.), `resource_id` (the FHIR id), optional `raw_text`, plus `source_type=PATIENT_CHART` and `field_or_chunk_id` (= `f"{resource_type}/{resource_id}"`, mirroring FHIR reference syntax).

Each class stays internally consistent. No optional fields invented to span types.

### Discriminated union — exactly where it lands

After Pre-flight #5 identifies the citation field(s) on `AgentResponse`, change those fields' element types from the current concrete type (likely `SourceCitation` today) to:

```python
Citation = Annotated[
    Union[SourceCitation, GuidelineCitation, PatientChartCitation],
    Field(discriminator="source_type"),
]
```

And update every site that builds those fields. **Do not add a new "sources" field** unless Pre-flight #5 reveals there is no existing place for citations to live (which would be surprising — the lipid-panel review UI is already showing citations).

### Pydantic StrEnum-discriminator gotcha (implementation note)

Pydantic v2 usually accepts `Literal[CitationSourceType.EXTRACTED_DOCUMENT]` as a discriminator value when the enum is `StrEnum`, but resolution can be fussy in edge cases (notably with `Field(discriminator=...)` and union members defined across modules). If Pydantic raises a discriminator-resolution error during implementation, fall back to bare string literals:

```python
class SourceCitation(BaseModel):
    source_type: Literal["extracted_document"] = "extracted_document"
    ...
```

…and keep `CitationSourceType` as a runtime-only enum (used by callers to set the value) rather than embedding it in the type system. Document the chosen approach in the module docstring.

### Field path strategy

For `SourceCitation`, `field_or_chunk_id` is a stable JSON-pointer-style path threaded by the schema traversal — e.g. `observations[0].value`, `medications[2].dose`, not just the leaf key. The extractor's recursive walk already knows the path; thread it as an explicit argument to a new helper `build_extracted_citation(path: str, ...)` rather than duplicating the wiring at each call site.

For `GuidelineCitation`, `field_or_chunk_id` is the chunk id (already present in `evidence_retriever.py` chunk objects).

For `PatientChartCitation`, `field_or_chunk_id` is `f"{resource_type}/{resource_id}"`.

### Changes

1. **`agent-service/src/clinical_copilot/documents/schemas/citation.py`**
   - Add `class CitationSourceType(StrEnum)` with `EXTRACTED_DOCUMENT`, `PATIENT_CHART`, `GUIDELINE`.
   - On `SourceCitation` (existing class — preserve `page` and `bbox` as required):
     - Add `source_type` (literal of `EXTRACTED_DOCUMENT` per the gotcha note above).
     - Add `field_or_chunk_id: str` (required).
   - **New** `class GuidelineCitation(BaseModel)`:
     - `source_type` (literal of `GUIDELINE`).
     - `field_or_chunk_id: str`
     - `source_doc_id: str`
     - `chunk_id: str`
     - `source_url: Optional[str]`
     - `confidence: float` (validate ∈ [0,1])
     - `raw_text: str`
     - Verify the actual chunk-shape fields by reading `evidence_retriever.py` and `corpus/` first; the list above is from exploration notes and may need adjustment.
   - **New** `class PatientChartCitation(BaseModel)`:
     - `source_type` (literal of `PATIENT_CHART`).
     - `field_or_chunk_id: str`
     - `resource_type: str` (e.g. `"Observation"`, `"MedicationRequest"`).
     - `resource_id: str`
     - `display_summary: Optional[str]` — **one-line display label** (e.g. `"Glucose 142 mg/dL on 2026-04-15"`), NOT verbatim source text. The full FHIR resource is re-fetched on demand via `resource_type/resource_id`. Storing the verbatim resource here would create a PHI-redaction surface and duplicate data the UI doesn't render. Document this in the field's docstring so future contributors don't pipe raw resource text in.
     - Verify the actual chart-pack source shape in the chart-pack producer code (search for "chart pack" or `source_id` near the FHIR fetch path); confirm the producer can supply a one-line summary, or add a small formatter there.
   - Export a `Citation` alias for the discriminated union (see "Discriminated union — exactly where it lands" above).

2. **Plug the union into the AgentResponse field(s) identified by Pre-flight #5.** Update:
   - The Pydantic model field type(s) in `schemas.py`.
   - `_supervisor_to_agent_response()` and any other site that constructs the field(s).
   - All existing route/integration tests that assert on the response shape.
   - The PHP client parsing path (search for `json_decode` of agent-service responses in `interface/copilot/`); add discriminator-aware parsing.

3. **Extractor field-path threading** — modify the schema-walk in `agent-service/src/clinical_copilot/documents/extractor.py` and the per-format adapters (`referral_docx.py`, `_hl7_common.py`, `workbook_xlsx.py`) to pass the path as a parameter into the citation builder. Add a single helper `build_extracted_citation(path: str, ...)`; do not duplicate the wiring at each of the 5 call sites.

4. **Retrieval-side citation construction** — in `agent-service/src/clinical_copilot/corpus/` and the evidence retriever worker, construct `GuidelineCitation` for guideline chunks and `PatientChartCitation` for chart-pack sources based on the chunk's existing `corpus`/`source` metadata.

5. **Tests — `agent-service/tests/unit/documents/test_citation.py`**
   - Round-trip both new fields on `SourceCitation` through `model_dump`/`model_validate`.
   - New cases: `GuidelineCitation` and `PatientChartCitation` round-trip independently.
   - Discriminated-union test: a list of mixed citation types round-trips intact through whichever `AgentResponse` field carries them.
   - Invalid `source_type` rejected (cross-class — e.g., `GuidelineCitation` JSON with `source_type="extracted_document"` is rejected).

6. **Fixtures — `agent-service/data/extracted/*.json` (~30 files)**
   - One-shot updater script `scripts/copilot/backfill_citation_fields.py`. Sets `source_type=extracted_document`, derives `field_or_chunk_id` from the surrounding key path.
   - **Run order matters:** the script must run and fixtures must commit *before* the Pydantic schema change lands, or use temporary defaults on the new fields during the rollout window. Otherwise CI red-lines on the first commit.

7. **PHP wire-shape consumer — `src/Services/Copilot/ExtractedFieldHelper.php`**
   - Surface `source_type` and `field_or_chunk_id` on the projected DTO for `SourceCitation`-shaped payloads.
   - For pages that consume the answer-payload citations (the slow-lane response), update parsing to handle all three shapes via the discriminator field.
   - PHPStan level 10 risk: changing the array shape can ripple. Run `composer phpstan` and resolve any new findings at the source — do not add baseline entries.

### Acceptance criteria
- `SourceCitation` round-trips with `page` and `bbox` populated and `source_type=EXTRACTED_DOCUMENT`.
- `GuidelineCitation` round-trips with `chunk_id` and `source_url` populated and `source_type=GUIDELINE`.
- `PatientChartCitation` round-trips with `resource_type`, `resource_id`, and (when present) a one-line `display_summary` populated; `source_type=PATIENT_CHART`.
- The `AgentResponse` field(s) carrying citations parse a mixed list of all three citation types via the `source_type` discriminator.
- A guideline chunk producing JSON with `source_type="extracted_document"` is rejected.
- Existing extracted-document fixtures parse without errors after the backfill.
- `composer phpstan` passes without new baseline entries.

### Verification
- `pytest agent-service/tests/unit/documents/test_citation.py -v`
- `pytest agent-service/tests/ -k extract`
- `pytest agent-service/tests/ -k "agent_response or supervisor"` (whatever test set covers the citation-bearing field)
- `composer phpstan`

---

## Gap 2 — Idempotent chart-write save endpoint

### Design decision: atomic-marker UPDATE + single-connection invariant (non-negotiable)

The v1 design (SELECT → write → UPDATE) is TOCTOU-racy: two double-click submits both pass the pre-check and duplicate rows. The fix uses an atomic conditional UPDATE as the lock acquisition, executed on the same DB connection that runs the chart writes.

The agent-service PUT is an HTTP call to a separate service and **cannot live inside the MySQL transaction.** Sequence:

```
1. PUT validated facts to agent-service       (HTTP, outside txn; idempotent on agent side already)
2. BEGIN
3. UPDATE documents
       SET chart_write_started_at = NOW()
     WHERE id = ?
       AND chart_written_at IS NULL
       AND (chart_write_started_at IS NULL
            OR chart_write_started_at < NOW() - INTERVAL 5 MINUTE)  -- stale-lock recovery, see invariant below
   -- check affected_rows == 1; if 0, another request holds a fresh lock or already finished
4. ChartWriteOrchestrator->run()                                    -- MUST run on this connection
5. UPDATE documents
       SET chart_written_at = NOW(),
           chart_write_summary = ?         -- JSON: pid, created, document_type, sections, row_counts
     WHERE id = ?
6. COMMIT
   -- on any failure inside the txn: ROLLBACK clears chart_write_started_at automatically
```

If `affected_rows == 0` at step 3:
- If `chart_written_at IS NOT NULL`: idempotent retry. Read `chart_write_summary`, return it with `idempotent: true`.
- If `chart_write_started_at` is fresh (within 5 minutes): a write is in progress. Return 409 Conflict with a retry-after hint.
- Stale `chart_write_started_at` (older than 5 minutes) is reclaimed by the OR clause above — no separate code path needed.

### Single-connection invariant (Pre-flight #6 — non-negotiable)

`ChartWriteOrchestrator->run()` and every `ChartWriteService` writer it dispatches to **MUST execute on the same DB connection holding the open transaction.** This is not "belt-and-suspenders" — it is a correctness blocker for the TTL clause:

If the marker UPDATE commits on connection A while chart writes happen on connection B, then:
- A retry after 5 minutes sees `chart_write_started_at` on connection A as stale, reclaims the lock, and fires another full set of inserts.
- The original writes on connection B may have already committed partially. Result: duplicates, exactly what the lock was supposed to prevent.

Pre-flight #6 verifies the connection wiring. If `ChartWriteOrchestrator` opens its own connection, **fix that before Gap 2 ships**, or **drop the TTL clause** (a stuck marker is then a stuck marker, requiring manual cleanup but not causing duplication). Do not ship the TTL with split connections.

### Retry response shape

`chart_write_summary` (JSON column on `documents`) must store enough for the retry response to reproduce the original success state:

```json
{
  "pid": 42,
  "patient_created": true,
  "document_type": "lab_pdf",
  "selected_sections": ["allergies", "lab_observations"],
  "row_counts": { "allergies": 0, "lab_observations": 6, "...": "..." },
  "redirect_target": "/interface/patient_file/summary/demographics.php?pid=42"
}
```

A retry without these fields would correctly avoid duplicating chart rows but would fail to redirect a `patient_choice=new` submit to the right patient page.

### Changes

1. **Doctrine migration** (after Pre-flight #1 confirms the path):
   - `chart_written_at DATETIME NULL`
   - `chart_write_started_at DATETIME NULL`
   - `chart_write_summary JSON NULL` (or LONGTEXT if MariaDB JSON support is unreliable for this branch — confirm).
   - **Down migration** drops all three columns; never an empty `down()`.

2. **`interface/copilot/api/save_document.php`**
   - Implement the sequence above on a single connection.
   - Wrap MySQL work in a transaction via `QueryUtils` (or whichever the existing copilot endpoints use; do not invent a new transaction wrapper).
   - On caught Throwable inside the txn: ROLLBACK, log via PSR-3 with context, return a generic 500 (no `$e->getMessage()` to the user — see CLAUDE.md).

3. **Connection-context wiring** — based on Pre-flight #6's findings:
   - If `ChartWriteOrchestrator` already uses a passed-in connection or the same global handle: no change.
   - If it opens its own: add a constructor parameter accepting the active connection, thread it through the writers, and update all callers. This is a prerequisite for Gap 2, not an afterthought.

4. **Tests**
   - `tests/Tests/Services/Copilot/ChartWrite/SaveDocumentEndpointIdempotencyTest.php` (new): single submit then identical re-submit; second response has `idempotent: true`; row counts in `lists` and `procedure_*` did not double; `chart_write_summary` round-trips.
   - Concurrent-submit test: two near-simultaneous POSTs (use threads or async) for the same `document_id`; exactly one wins; the loser gets 409 or `idempotent: true` depending on timing; total inserted rows match a single submit.
   - Stale-lock test: manually set `chart_write_started_at` to 6 minutes ago; submit; expect the new submit to acquire the lock and complete.
   - **Keep `ChartWriteServiceTest::testWriteAllergiesDoesNotDedupeOnRepeatCall` passing** — it exercises the service directly, below the endpoint guard.

### Acceptance criteria
- Pre-flight #6 has confirmed `ChartWriteOrchestrator` runs on the active connection (or the wiring has been updated to make it do so, or the TTL clause has been dropped).
- Two concurrent POSTs for the same `document_id` produce exactly one set of chart rows.
- The losing submit gets either `idempotent: true` (if it lost to a completed write) or 409 (if it lost to an in-flight write); never a partial duplicate.
- A retry of a `patient_choice=new` submit returns the same `pid` and redirect target as the original.
- Failed chart writes ROLLBACK and clear `chart_write_started_at`, allowing a legitimate retry.
- A stale `chart_write_started_at` (>5 min) does not permanently block retries (only if TTL clause shipped).

### Verification
- `docker compose exec openemr /root/devtools services-test --filter=ChartWrite`
- Manual: re-submit the lipid-panel review twice; second response shows `idempotent: true`; MySQL row counts unchanged.
- Manual concurrent: open two browser tabs, click Save in both within ~50ms; verify exactly one set of rows.

---

## Gap 3 — Wire usage + retrieval + confidence into `agent_traces`

### What's already there

`agent-service/src/clinical_copilot/db/models.py:35-57` defines `agent_traces` with `request_id`, `user_id`, `role`, `lane`, `latency_ms`, `token_in`, `token_out`, `model_tier`, `created_at`. Table is **not currently written to anywhere.**

This is the agent-service's own DB (Alembic, separate from OpenEMR's Doctrine). All migration work for this gap belongs under `agent-service/src/clinical_copilot/db/migrations/versions/` — see Pre-flight #2.

### Capture sites (broader than one gateway)

Token usage must be captured at every `client.messages.create()` call site, not just the v1 gateway:

- `agent-service/src/clinical_copilot/orchestrator/llm_gateway.py:142` — v1 `AnthropicLlmGateway.complete()`
- `agent-service/src/clinical_copilot/orchestrator/supervisor.py:285` — supervisor planner
- `agent-service/src/clinical_copilot/orchestrator/supervisor_langgraph.py:231` — LangGraph planner + synthesizer
- Any critic / judge call sites in `corpus/rerank.py` that hit Anthropic

Each receives `response.usage.input_tokens` / `output_tokens` and discards it today. The pattern: add `input_tokens`/`output_tokens` to the dataclass each path returns (start with `LlmTurn`), aggregate per-request into the supervisor response, write once at the end.

### Changes

1. **Alembic migration** (after Pre-flight #2 confirms the latest version file):
   - Extend `agent_traces`: `retrieval_hits INTEGER NULL`, `extraction_confidence REAL NULL`.
   - Up migration adds columns; **down migration drops them.**
   - **Do not edit** `0001_initial.py` — new version file following the existing naming convention.

2. **Thread token usage**
   - Add `input_tokens: int`, `output_tokens: int` to `LlmTurn` (`llm_gateway.py:65-78`).
   - Populate at every Anthropic call site listed above.
   - Aggregate into a `UsageTotals` field on the supervisor response.

3. **Trace writer service** — `agent-service/src/clinical_copilot/observability/traces.py` (new)
   - Mirror `observability/metrics.py:174-232` (existing `MetricsService` is the template — fail-open, never raises into clinician path).
   - Method: `record(request_id, user_id, role, lane, latency_ms, token_in, token_out, model_tier, retrieval_hits, extraction_confidence)`.
   - Same `session_factory` from `app_state.py:213-219`.

4. **Call sites — every request path that performs extraction or retrieval, not just `/api/agent/query`:**
   - `agent-service/src/clinical_copilot/orchestrator/agent.py:217` (alongside the existing `self._metrics.record(...)`) — slow-lane query path.
   - The document-ingest path (search for the route handler that triggers `documents/extractor.py`; the lipid-panel upload the reviewer demoed flows through this, not `/api/agent/query`). A doc upload is an encounter for trace-data purposes.
   - Any other supervisor entry point that runs Anthropic calls.
   Each call site invokes `self._traces.record(...)` with values aggregated from the supervisor response (or extractor result, for ingest) and retriever output. NULL the columns that don't apply per request type — the writer accepts NULLs.

5. **Tests**
   - `tests/unit/observability/test_traces.py` — happy path, fail-open on DB error, NULL handling per the independent-column semantics below.
   - `tests/integration/test_agent_query_writes_trace.py` — POST `/api/agent/query`; assert one row in `agent_traces` with non-zero token counts.
   - Unit: assert each Anthropic call site contributes its tokens to the aggregate (mock `client.messages.create` return value with known `usage`).

### Acceptance criteria

NULL conditions are independent per column (slow-lane retrieval queries retrieve without extracting, so `retrieval_hits` is populated while `extraction_confidence` is NULL):

- After every request path that runs extraction or retrieval (`/api/agent/query`, document ingest, and any other supervisor entry point), exactly one row appears in `agent_traces`.
- `token_in` and `token_out` are non-zero and equal the sum across all Anthropic calls for the request.
- `latency_ms` is populated.
- `retrieval_hits` is the post-rerank chunk count whenever a retrieval worker ran; NULL only when no retrieval ran.
- `extraction_confidence` is the mean across extracted-field confidences whenever an extraction ran; NULL only when no extraction ran.
- A slow-lane query that retrieves without extracting: `retrieval_hits` populated, `extraction_confidence` NULL.
- A document-ingest request that extracts without retrieving: `retrieval_hits` NULL, `extraction_confidence` populated.
- A DB outage does not surface an error to the clinician.

### Verification
- `pytest agent-service/tests/unit/observability/ -v`
- `pytest agent-service/tests/integration/test_agent_query_writes_trace.py -v`
- Integration: hit the document-ingest route locally (lipid-panel fixture upload); assert one new `agent_traces` row with `extraction_confidence` populated and `retrieval_hits` NULL.
- Live: hit `/api/agent/query` locally with a slow-lane retrieval; `SELECT * FROM agent_traces ORDER BY created_at DESC LIMIT 1` shows `retrieval_hits` populated and `extraction_confidence` NULL.

---

## Gap 4 — Minimum-viable bbox canvas overlay

### Design decision: agent-service serves cached page images, OpenEMR proxies them same-origin

The extraction pipeline already renders pages during VLM extraction and caches them. After Pre-flight #3 confirms the cache is durable, expose those images via a new agent-service route. **Add an OpenEMR proxy route** so the browser only ever talks to OpenEMR's origin (avoids CORS, keeps auth uniform, lets the existing PHP session gate access).

If Pre-flight #3 instead picks the OpenEMR-side render path (because the agent-service cache is ephemeral or sandboxed), the overlay still works the same — only the rendering source changes; the proxy route stays.

### Changes

1. **Page render route on agent-service** — `GET /api/agent/internal/document/{document_id}/page/{page}.png`
   - Returns the cached PNG, or renders on demand from the source PDF/image stored alongside the extraction artifacts.
   - Cache key: `(document_id, page)`. Cache TTL: aligned with the existing extraction artifact TTL.

2. **OpenEMR proxy route** — `interface/copilot/api/document_page.php` (new)
   - Authenticates via the existing copilot session/auth pattern.
   - Proxies `GET /api/agent/internal/document/{document_id}/page/{page}.png` to the agent-service, streams the PNG back.
   - Browser image src is same-origin: `/interface/copilot/api/document_page.php?document_id=...&page=1`.

3. **PHP partial — `interface/copilot/partials/citation_overlay.php` (new)**
   - `<img>` of the page (pointing at the proxy URL), `<canvas>` absolutely positioned on top, sized to the rendered image's intrinsic dimensions.
   - JS reads the citations array passed in by the host page, draws each `bbox` as a rectangle scaled to the image dimensions. Only `SourceCitation`-shaped citations have `bbox`; `GuidelineCitation` and `PatientChartCitation` are skipped here — they're handled elsewhere in the slow-lane source list.
   - Single interaction: clicking a citation row in the field list highlights (color-flips) the corresponding box. Two-pane hover-sync from `plans/copilot_bbox_preview.md` is deferred.

4. **Wire into review pages**
   - `interface/copilot/lab_review.php` — replace the line-14 placeholder note with an `include` of the partial.
   - `interface/copilot/document_review.php` — same.
   - `ExtractedFieldHelper` (or sibling) exposes the full `SourceCitation` including `bbox`, `page`, `source_type`, `field_or_chunk_id` for the JS payload.

5. **Tests**
   - PHPUnit: assert the projected DTO carries bbox + page on `SourceCitation`-shaped payloads.
   - PHPUnit: the proxy route returns 401/403 without a valid session and 200 with a PNG content-type when authorized.
   - Manual visual check (see Acceptance criteria).

### Acceptance criteria
- Uploading the lipid-panel fixture and opening the review UI shows the rendered page image with six rectangles drawn at the bbox coordinates of the six observations.
- Clicking a row in the review list highlights the corresponding rectangle.
- The browser only loads images from the OpenEMR origin (no direct agent-service requests in the network tab).
- Unauthenticated proxy requests are rejected.

### Verification
- `curl -b "PHPSESSID=<session>" 'https://localhost:9300/interface/copilot/api/document_page.php?document_id=<id>&page=1'` returns a PNG.
- `curl 'https://localhost:9300/interface/copilot/api/document_page.php?...'` (no session) returns 401/403.
- Manual: upload lipid-panel fixture; six rectangles render at expected positions; click each row, see the matching highlight.

---

## Risk 1 — Activate the prek pre-push hook on fresh clones

### Design decision: small PHP installer script, not inline composer shell_exec

Inline composer `shell_exec` quoting is brittle and hard to test. Replace with a dedicated installer.

### Changes

1. **`scripts/install-pre-push-hook.php` (new)** — small, focused:
   - Skip if `getenv('CI') === 'true'` (Docker / CI shouldn't auto-install hooks).
   - Skip if `.git` is not present (working from an extracted tarball).
   - Run `prek install --hook-type pre-push` if `prek` is on PATH.
   - Print a single-line WARN to stderr if `prek` is missing, pointing to CONTRIBUTING.md. Never exit non-zero — `composer install` must keep working.

2. **`composer.json`** — `scripts.post-install-cmd` runs `@php scripts/install-pre-push-hook.php`.

3. **`CONTRIBUTING.md:73-75`** — explicit pre-push paragraph, manual install command, what to do if prek is unavailable.

### Acceptance criteria
- Fresh clone + `composer install` on a contributor machine with prek on PATH: `.git/hooks/pre-push` exists and points at the prek runner.
- Same flow without prek installed: `composer install` exits 0; one WARN line on stderr.
- Inside Docker / CI (`CI=true`): installer skips silently.

### Verification
- Fresh clone in a temp dir, `composer install`, `cat .git/hooks/pre-push` shows the prek shim.
- `CI=true composer install` produces no hook installation.
- Without prek: install completes, WARN line appears.

---

## Risk 2 — Surface the active rerank backend so silent BM25 fallback can't hide

### Design decision: canonical vocabulary `"cohere" | "llm_judge" | "bm25_only"`, nullable on responses without retrieval

Pre-flight #4 enumerates the current values verbatim (likely a mix of `"cohere"`, `"llm-judge"` with hyphen, and `"none"`). v5 standardizes on the underscored set everywhere: worker output, response schema, tests, UI, eval-runner startup print. No new strings invented downstream.

Fast-lane requests and any other path that doesn't invoke a reranker have no honest value here. Make the field nullable rather than fabricate a label: `rerank_backend: Optional[Literal["cohere", "llm_judge", "bm25_only"]] = None`. UI and eval-runner only render/print the backend when it's non-null.

### Adding a top-level response field

Pre-flight #5 confirms `AgentResponse` is `extra="forbid"`. Adding `rerank_backend` to the response requires:
- Update the Pydantic model in `agent-service/src/clinical_copilot/orchestrator/schemas.py`.
- Update `_supervisor_to_agent_response()` to populate it.
- Update existing route tests that assert on the response shape.
- Update the PHP client parsing path in `interface/copilot/` that consumes the response (search for `json_decode` of agent-service responses).

### Changes

1. **Worker**
   - `agent-service/src/clinical_copilot/orchestrator/workers/evidence_retriever.py:101-130` — emit canonical labels. Use Pre-flight #4's enumerated old values to drive the rename: e.g. `"none"` → `"bm25_only"`, `"llm-judge"` (if hyphenated) → `"llm_judge"`.
   - Each branch's existing structured log keeps its current event name; only the `backend` label string changes.

2. **Response schema**
   - Add `rerank_backend: Optional[Literal["cohere", "llm_judge", "bm25_only"]] = None` to `AgentResponse`.
   - Wire through `_supervisor_to_agent_response()` — populate only when the request actually ran a reranker; leave None otherwise (fast-lane, no-retrieval).

3. **Startup log**
   - In agent-service boot (`app_state.py:341-358`), emit a single structured log line: `supervisor.rerank_backend_resolved=<label>` based on whether Cohere client + Anthropic key are configured.
   - Mirrors the eval-gate pattern at `evals/extraction/runner.py:166-167`.
   - Per-request: only emit `corpus.rerank.backend_used=<label>` from code paths that actually invoke a reranker. Don't emit a "no rerank" line for paths that skip retrieval entirely.

4. **UI**
   - In review pane(s), render a small inline badge **only** when `rerank_backend` is non-null AND not `"cohere"`:
     - `"llm_judge"` → "rerank: llm-judge fallback"
     - `"bm25_only"` → "rerank: BM25 only — degraded"
   - Null `rerank_backend` (fast-lane, no-retrieval) renders no badge.

5. **Tests**
   - `tests/unit/orchestrator/test_evidence_retriever_worker.py` — extend to assert the response carries each of the three canonical backend labels under matching mock conditions.
   - New: assert boot log emits `supervisor.rerank_backend_resolved=bm25_only` when neither client is configured.
   - PHP test: the response parser handles the new top-level field without throwing.

6. **Deployment step (manual — outside code scope)**
   - Set `COHERE_API_KEY` on the Railway agent-service environment.
   - Verification command: `curl -X POST https://<railway-host>/api/agent/query -d '...'` and assert `response.rerank_backend == "cohere"`.

### Acceptance criteria
- With `COHERE_API_KEY` set: a slow-lane `/api/agent/query` response has `"rerank_backend": "cohere"`; no UI badge.
- Without Cohere but with Anthropic: `"rerank_backend": "llm_judge"`; UI shows fallback badge.
- With neither: `"rerank_backend": "bm25_only"`; UI shows degraded badge; boot log carries the resolved-backend line.
- A fast-lane response (no retrieval) has `"rerank_backend": null`; UI renders no badge.
- All three non-null vocab values appear consistently in worker, response schema, tests, UI, and eval-runner output.

### Verification
- `pytest agent-service/tests/unit/orchestrator/test_evidence_retriever_worker.py -v`
- Local: vary env vars, hit `/api/agent/query`, verify response field + UI badge.
- Railway: after setting `COHERE_API_KEY` and redeploying, `curl` confirms `"rerank_backend": "cohere"`.

---

## Execution order

The reviewer's priority order is the right baseline. v4 reorders only one item — Risk 2 jumps ahead of Gaps 3-4 — because the rerank flag is a ~2-hour change that protects every demo from this point forward, and the cost of being demo-blind is asymmetric.

1. **Pre-flight checks** (1, 2, 3, 4, 5, 6) — small, branchless, must come before anything else. **Pre-flight #6 may surface a connection-wiring fix** that has to land before Gap 2.
2. **Gap 1** — citation schema (three-way union). Unblocks Gap 4. Run the fixture backfill before the Pydantic change lands.
3. **Gap 2** — idempotency (atomic UPDATE pattern + retry shape + single-connection invariant + stale-lock TTL). Highest-severity correctness bug.
4. **Risk 2** — rerank backend flag. Cheap demo protection; bumped ahead of remaining gaps.
5. **Gap 3** — agent_traces wiring + Alembic migration extension.
6. **Gap 4** — bbox overlay MVP + OpenEMR proxy route.
7. **Risk 1** — prek installer. Trivial; ship anytime once #1 is decided.

## Critical files

- `agent-service/src/clinical_copilot/documents/schemas/citation.py`
- `agent-service/src/clinical_copilot/documents/extractor.py` and per-format adapters
- `agent-service/src/clinical_copilot/orchestrator/llm_gateway.py`
- `agent-service/src/clinical_copilot/orchestrator/supervisor.py`, `supervisor_langgraph.py`
- `agent-service/src/clinical_copilot/orchestrator/workers/evidence_retriever.py`
- `agent-service/src/clinical_copilot/orchestrator/schemas.py` (`AgentResponse`, citation field(s) identified by Pre-flight #5)
- `agent-service/src/clinical_copilot/observability/metrics.py` (template for `traces.py`)
- `agent-service/src/clinical_copilot/db/models.py` and `db/migrations/versions/` (Alembic)
- `interface/copilot/api/save_document.php`
- `interface/copilot/api/document_page.php` (new — proxy route)
- `interface/copilot/lab_review.php`, `interface/copilot/document_review.php`
- `interface/copilot/partials/citation_overlay.php` (new)
- `src/Services/Copilot/ExtractedFieldHelper.php`
- `src/Services/Copilot/ChartWrite/ChartWriteOrchestrator.php` and writer classes (verify single-connection invariant)
- `.pre-commit-config.yaml`, `composer.json`, `scripts/install-pre-push-hook.php` (new), `CONTRIBUTING.md`

## End-to-end verification (after all six items land)

1. Fresh clone → `composer install` → pre-push hook installed (or WARN on missing prek).
2. `pytest agent-service/tests/ -v` and `docker compose exec openemr /root/devtools clean-sweep-tests` both green.
3. Local upload of the lipid-panel fixture → review UI shows bbox rectangles via the proxy route → save → row counts in `lists`/`procedure_*` match expectations.
4. Re-submit same document → response carries `idempotent: true`; row counts unchanged; `pid` and redirect target match the original submit.
5. Two concurrent submits → exactly one set of chart rows; the loser gets 409 or `idempotent: true`.
6. A slow-lane retrieval-only query and an extraction-only query each produce one `agent_traces` row with the expected NULL pattern per column (retrieval_hits / extraction_confidence are independently NULLable).
7. `/api/agent/query` response includes `"rerank_backend": "cohere"` after Railway env is set; UI shows no fallback badge.
8. A response payload containing all three citation types parses cleanly on the PHP side via the `source_type` discriminator.
