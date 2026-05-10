# W2_ARCHITECTURE.md — Clinical Co-Pilot, Week 2

**Status:** Draft for Week 2 architecture defense
**Last updated:** 2026-05-09
**Companion to:** ARCHITECTURE.md (v1, Week 1) — *not a replacement*. Week 2
extends Week 1; v1 sections that aren't restated here remain in force.

---

## Status as of 2026-05-09 — shipped vs deferred

This document is the architecture target. The Week 2 demo ships a subset;
remaining surfaces are tracked as deferred MRs in TASKS2.md. PRD2.md
carries the product-level status block; TASKS2.md § "Submission timeline"
carries the by-commit breakdown of pre-cutoff vs post-feedback work; this
section is the architecture delta against this document's own §1–§16.

**Post-demo update (2026-05-09).** Six remediation PRs landed:

- **Citations (PR 14)** — `Citation` discriminated union (`SourceCitation` /
  `GuidelineCitation` / `PatientChartCitation`) added as **optional display
  metadata** alongside the canonical opaque `source_id`. Verifier still joins
  on `source_id` strings — `verification/middleware.py` is untouched.
- **Chart-write idempotency (PR 15)** — `ChartWriteCoordinator` with four
  explicit outcomes (AcquiredAndWrote / IdempotentReplay / ConcurrentInFlight
  / DocumentNotFound). Marker columns `chart_written_at` /
  `chart_write_started_at` / `chart_write_summary` on `documents`.
- **Rerank-backend surface (PR 16)** — `AgentResponse.rerank_backend ∈
  {"cohere", "llm_judge", "bm25_only"} | None` populated only on paths that
  invoke a reranker. UI badges non-Cohere backends.
- **Trace wiring (PR 17)** — Alembic `0004_agent_traces_extension` adds
  `retrieval_hits` + `extraction_confidence` to `agent_traces`. `TracesService`
  is fail-open — DB outages never surface to clinicians.
- **Bbox overlay (PR 18, 18b, 18c)** — Click-to-source via per-page PNG cache
  in agent-service (`pypdfium2`) served through a same-origin PHP proxy.
  **Pre-flight #3 reversed:** rendering moved out of PHP because `openemr:flex`
  ships Imagick without Ghostscript. Synthetic monospace renderer covers
  HL7 / docx / xlsx; Tesseract tightens bboxes from row-band coarse to
  per-token at ingest time.
- **Pre-push hook installer (PR 19)** — `composer install` automatically wires
  `.git/hooks/pre-push` against `.pre-commit-config.yaml`. Skips in CI.

Detailed checklists live in TASKS2.md § Post-demo remediation; pre-flight
findings and design rationale archived in
`plans/tasks_copilot_remediation_w2.md`.

**Shipped (live in `agent-service` and the OpenEMR fork at HEAD):**

| Surface | Module / file |
|---|---|
| Document schemas + `ExtractedField[T]` (per-field citations enforced via XOR validator) | `clinical_copilot/documents/schemas/{citation,lab_pdf,intake_form}.py` (citation), full per-field wrapping in lab + intake schemas |
| Vision extractor | `clinical_copilot/documents/extractor.py` |
| Document fetcher / page render | `clinical_copilot/documents/fetcher.py` |
| Extracted-fact JSON store (file-backed; not Postgres) | `clinical_copilot/documents/store.py` |
| Synchronous ingest route (calls `run_extraction()` directly — not the supervisor's `intake_extractor` worker) | `POST /api/agent/internal/ingest` + `GET /api/agent/internal/extracted/{id}` in `main.py` |
| **Hybrid corpus retriever (BM25 + dense via `OpenAIEmbedder`, RRF-fused with `k=60`)** | `clinical_copilot/corpus/{chunker,index,retriever,embedder,scrub,records}.py`; fusion at `corpus/retriever.py` |
| **Cohere `rerank-v3.5` reranker (top-20 re-scored; LLM-judge via Claude Haiku stays as the env-var-gated fallback when `COHERE_API_KEY` is absent or the Cohere call errors)** | `clinical_copilot/corpus/rerank.py` |
| Corpus sources (30 docs / ~262 chunks as of 2026-05-08) | `agent-service/corpus/sources/{uspstf,cdc,nih,aha}/` + `LICENSES.md` |
| Demo CLIs | `clinical_copilot/scripts/{ingest_document,retrieve_evidence}.py` |
| **Supervisor + 2 workers (plain-Python default path)** — exposes `dispatch_intake_extractor` + `dispatch_evidence_retriever` `tool_use` blocks; logs each handoff | `clinical_copilot/orchestrator/supervisor.py`; `clinical_copilot/orchestrator/workers/{intake_extractor,evidence_retriever}.py` |
| **LangGraph supervisor (opt-in)** — `StateGraph` with planner, worker fan-out, synthesizer, critic, verification, and `v1_single` fallback nodes; routed only when `USE_LANGGRAPH=true` | `clinical_copilot/orchestrator/{supervisor_langgraph,state,planner,critic,edges}.py`; `clinical_copilot/orchestrator/nodes/`; gate in `config.py` + `/api/agent/query` |
| Supervisor audit endpoint (handoff rows) | `GET /api/agent/supervisor/audit/{resident_user_id}` in `main.py` |
| **65-case extraction eval gate** (boolean rubrics, threshold-enforced; 28 extraction · 23 retrieval · 6 citations · 4 missing-data · 4 refusals) | `agent-service/evals/extraction/{cases.jsonl,baseline.json}`; runner at `src/clinical_copilot/evals/extraction/runner.py` |
| Pre-push hook for the eval gate | `4a81eca23` |
| GitLab CI pipeline running the gate | `.gitlab-ci.yml` (`24ae138b9`) |
| Verification: confidence-floor abstain | `clinical_copilot/verification/abstention.py` (`LOW_CONFIDENCE`) |
| Chart-side AI entry points (PHP) | `interface/copilot/{upload_lab,lab_review,lab_save_ai,new_patient_with_ai,intake_review,new_patient_save_ai,upload_document,document_review}.php`; menu items in `interface/main/tabs/menu/menus/{standard,front_office}.json`; chart Labs button in `interface/patient_file/summary/labdata_fragment.php` |
| Ingest gateway (PHP) | `src/Services/Copilot/IngestClient.php` (calls the agent-service multipart route) |
| **Chart-write path** (allergies / meds / problems → `lists`; care gaps → `dated_reminders`; lab observations → `procedure_order` chain) | `src/Services/Copilot/ChartWrite/{ChartWriteService,FactsExtractor}.php`; orchestrated by `interface/copilot/api/save_document.php` |
| Fast-lane `get_labs` | `clinical_copilot/app_state.py` lane subset now includes `get_labs` |
| Multimodal-expansion extractors (5 new types) | `clinical_copilot/documents/extractors/{referral_docx,workbook_xlsx,hl7_oru,hl7_adt}.py` + shared `_hl7_common.py`; schemas under `documents/schemas/{referral_docx,fax_tiff,workbook_xlsx,hl7_oru,hl7_adt}.py`. Registry-based dispatch in `documents/extractor.py`. |
| Universal upload UI + format classifier (PHP) | `interface/copilot/upload_document.php` + `interface/copilot/document_review.php`; `src/Services/Copilot/{DocumentClassifier,ClassifierException}.php`; `IngestClient::ingestTyped()` |
| Patient resolver (PHP) | `src/Services/Copilot/PatientMatch/{PatientMatchService,PatientMatchScorer,PatientMatchCandidate}.php`; `interface/copilot/api/patient_match.php` |
| Eval cases (65 total across 5 top-level buckets — extraction 28 / retrieval 23 / citations 6 / missing-data 4 / refusals 4; the extraction bucket subdivides into 7 modality sub-buckets) | `agent-service/evals/extraction/cases.jsonl`; per-case fixtures under `tests/eval/w2_cases/extraction-{lab,intake,fax,referral,workbook,hl7-oru,hl7-adt}/p01..p07.json`; per-case labels under `agent-service/evals/extraction/labels/` |

**Deferred (design captured in §1–§16 below; MR not yet landed):**

| Surface | This doc | Reason it's deferred |
|---|---|---|
| **LangGraph per-node LangSmith spans + proof gates** | §4 | The LangGraph `StateGraph` itself has landed behind `USE_LANGGRAPH=true`, with the plain-Python supervisor as default and fallback. Still deferred: enforced LangSmith `parent_run_id` proof, citation-separation eval pass, and rollout verification with the flag enabled. |
| Documents-subsystem post-upload Symfony listener | §3.1, §1 topology | Replaced for the demo by chart-side PHP page → multipart ingest. Production listener is PR 2; data shape is identical so the swap is internal. |
| Postgres `extraction_jobs` queue (`SKIP LOCKED` worker) | §3.2, §1 | Today's path is synchronous on the ingest request — no async worker. `extracted_facts` table likewise deferred; facts persist as JSON files between extract and review (the chart-write path lands accepted facts durably in OpenEMR tables). |
| `GET /agent/documents/{id}` and `/summary` endpoints | §3.3, §3.4 | Not built. Clinician review happens on `lab_review.php` / `intake_review.php` / `document_review.php`, not a polling side panel. |
| Documents-view side panel + chart summary card | §3.3, §3.4 | Not built. The "primary extraction-state surface" of PRD2 §2 lives on the dedicated review pages instead. |
| OCR strict + degraded path (`_check_document_bbox`) | §7.1 | Not built. `verification/citation_check.py` enforces the v1 citation discipline + the `LOW_CONFIDENCE` floor only. `CitationKind` and the verdict tri-state are not yet in code. |
| `import-linter` package-boundary contract | §10 | Not configured. The chart-tools / corpus separation and the `tools/` / `documents/` / `corpus/` boundary both hold in code today (no cross-imports), but neither is gate-enforced. The LangGraph planner / critic / worker-node implementation depends on the same boundary discipline. |
| Per-stage latency histogram + `latency.stage_p95` rubric | §11 | Not built. Spans carry `latency_ms` but no eval-side aggregation or budget assertion. |
| LangSmith deny-by-default redaction layer | §8 | Not built; demo runs with `LANGSMITH_TRACING=false`. **Scoped as PR 13 in the post-Sunday queue** — deny-by-default per-span allowlist + regex backstop (SSN / MRN / phone / email / DOB / FHIR-bundle name fields). Full plan in TASKS2.md → "PR 13". |
| Eval buckets beyond extraction + retrieval | §9 | 65 cases live as of 2026-05-08 (extraction 28, retrieval 23, citations 6, missing-data 4, refusals 4). The originally-planned `reconciliation`, `citation-separation`, `rbac`, and `abstention` buckets remain at zero; no judge-evaluated rubrics, no budget pre-flight. |

**Conflict resolution.** When this status block disagrees with a section
below, this block reflects what is *deployed*; the section reflects what
the deferred MR will deliver. Appendix-A contracts in PRD2 still bind
whatever ships — fewer surfaces today doesn't relax the citation,
abstention, or RBAC contracts on the surfaces that exist.

---

## Reading order

This document is organized so a reviewer can answer four questions in
order:

1. *What changed structurally from Week 1?* — §0, §1, §2.
2. *How do documents become facts?* — §3, §4, §5.
3. *How does a Week 2 query become a grounded answer?* — §6, §7, §8.
4. *How is quality gated and enforced?* — §9, §10, §11, §12.

Risks, tradeoffs, and protocols live at the end (§13–§16). The binding
contracts cited throughout live in `PRD2.md` Appendix A; this document
explains *how* those contracts are realized in code.

---

## 0. Executive Summary

Week 2 adds **multimodal document ingestion**, a **multi-agent
graph**, **hybrid retrieval over a guideline corpus**, and an
**eval-gated pre-push hook + GitLab CI** to the Week 1 Co-Pilot.
Read this summary as **what shipped today**, with the design target
called out for each surface where shipped ≠ target.

**Shipped today (live in code at HEAD, deployed to Railway):**

- **Multimodal document ingestion** — `POST /api/agent/internal/ingest`
  takes a multipart upload (lab PDF, intake form, referral DOCX, fax
  TIFF, workbook XLSX, HL7 ORU/ADT) and runs synchronous extraction
  via `documents/extractor.py`. Each extracted leaf is wrapped in
  `ExtractedField[T]` with an XOR validator (`citation.py`)
  enforcing every value carries a `SourceCitation` *or* an
  `abstain_reason`, never neither, never both.
- **Two-worker supervisor (plain Python)** — `orchestrator/supervisor.py`
  exposes `dispatch_intake_extractor` + `dispatch_evidence_retriever`
  as Anthropic `tool_use` blocks, the model picks, every dispatch is
  recorded as a `Handoff` and surfaced via
  `GET /api/agent/supervisor/audit/{user_id}`. Wired into the live
  `/api/agent/query` slow-lane path in `main.py`, gated on
  `use_supervisor` (default on), with cross-patient guard +
  chart-pack pre-fetch + v1-orchestrator fallback on supervisor
  exception.
- **Hybrid retrieval over a 30-doc / 262-chunk guideline corpus** —
  BM25 + dense (OpenAI `text-embedding-3-small`) + RRF fusion +
  rerank stage in `corpus/rerank.py` (Cohere `rerank-v3.5` primary
  when `COHERE_API_KEY` is set; LLM-judge fallback otherwise). Dense
  path is gated on the `dense.pkl` artifact + `OPENAI_API_KEY`;
  degrades cleanly to BM25-only on Railway.
- **65-case eval gate + pre-push + GitLab CI** — boolean rubrics
  (`schema_valid = 1.0`, `citation_present ≥ 0.95`,
  `factually_consistent ≥ 0.90`, `safe_refusal = 1.0`,
  `no_phi_in_logs = 1.0`, regression budget 5 pp). Both
  `agent-service/scripts/pre-push.sh` and `.gitlab-ci.yml` invoke
  `make eval-extraction-gate`.
- **Trust boundary unchanged.** v1 §4's PHP-signs-JWT /
  Python-verifies model holds. The PHP gateway is the only writer
  to OpenEMR's `documents` table; the Python service never reads
  OpenEMR's database directly.

**Sunday target / post-Sunday queue (deferred — design captured below):**

- **LangGraph proof gates (PR 8 follow-up).** The `StateGraph`,
  planner, critic, worker wrappers, conditional edges
  (`route_after_planner`, `route_after_critic`), and `USE_LANGGRAPH`
  route gate have landed. Remaining proof work is narrower: per-node
  LangSmith spans with `parent_run_id` linkage, citation-separation
  eval pass, and production rollout verification with the flag enabled.
- **OpenEMR Documents bridge (PR 2).** The Symfony post-upload event
  + Postgres extraction queue + `GET /agent/documents/{id}`
  state-poll endpoint described in §3 are the production target.
  Today's demo replaces them with chart-side PHP pages calling
  `POST /api/agent/internal/ingest` synchronously — works for the
  demo, won't scale to async multi-page packets.
- **OCR citation check (PR 5).** `CitationKind` enum +
  `_check_document_bbox` strict + degraded path described in §7.1
  are the production target. Today's only document-citation gate is
  VLM-confidence < 0.7 → `LOW_CONFIDENCE`.
- **PHI redaction in LangSmith spans (PR 13).** Demo runs with
  `LANGSMITH_TRACING=false`; re-enabling needs the deny-by-default
  span filter described in §8.
- **Cohere rerank backend (PR 7).** *Promoted to Sunday-blocking
  on 2026-05-08; lands before submit.* Cohere `rerank-v3.5` becomes
  the primary reranker behind `COHERE_API_KEY`; the existing
  LLM-judge in `corpus/rerank.py` stays as the env-var-gated
  fallback when the key is absent or the Cohere call errors. Full
  plan in TASKS2.md → "PR 7".
- **Postgres extraction queue + pgvector for the corpus.** Both are
  the production-shape target inside the existing `agent-db`; today
  facts persist as `data/extracted/<id>.json` on the agent-service
  local disk and the dense corpus is a pickled numpy array. No new
  service, no Redis, no S3 — just deferred upgrades within the
  existing two-DB shape.

Three things stayed the same on purpose:

- **The verification trust story.** v1 §3's principle ("deterministic
  where possible, probabilistic only where necessary") expands to
  cover document-extracted facts. The XOR validator + the eval
  gate's `citation_present` threshold are the deterministic layer;
  VLM-confidence floor + the planned OCR check are the probabilistic
  layer with `LOW_CONFIDENCE` over silent rejection.
- **Tool-vs-RAG separation.** Patient facts are tool-mediated only.
  Guideline corpus is RAG-only. No vector index over patient data;
  enforced structurally by package layout (`tools/` vs `corpus/`),
  not procedurally (§10).
- **Deployment shape.** Two Railway services + one Postgres + one
  Redis-free architecture. Week 2 stayed within the v1 shape —
  Postgres extraction queue and pgvector are deferred upgrades, not
  new services.

Major Week-2 tradeoffs documented in §15.

---

## 1. System Topology (delta)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  OpenEMR (PHP / Smarty / Apache)                                        │
│                                                                         │
│   ┌──────────────────┐  ┌──────────────────────┐  ┌─────────────────┐   │
│   │  Daily Brief     │  │  In-Chart Side Panel │  │  Documents View │   │
│   │  (slow lane)     │  │  (fast lane;         │  │  (NEW Week 2    │   │
│   │  Smarty + JS     │  │  + summary card NEW) │  │  side panel for │   │
│   │                  │  │                      │  │  extraction     │   │
│   │                  │  │                      │  │  state)         │   │
│   └────────┬─────────┘  └──────────┬───────────┘  └─────────┬───────┘   │
│            │                       │                        │           │
│            └─────────────┬─────────┴────────────────────────┘           │
│                          │ JSON over HTTPS                              │
│                ┌─────────▼─────────┐                                    │
│                │  PHP Gateway      │    + new endpoints (§3.3, §3.4):   │
│                │  /agent/*         │      GET /agent/documents/{id}     │
│                │  /agent/documents │      GET /agent/documents/summary  │
│                │                   │    + new event listener:           │
│                │                   │      CoPilotDocumentUploaded       │
│                └─────────┬─────────┘                                    │
│                          │                                              │
└──────────────────────────┼──────────────────────────────────────────────┘
                           │ HTTPS + signed JWT (HS256)  (v1 §4 unchanged)
                           ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  Agent Service (Python / FastAPI) — single process                      │
│                                                                         │
│   ┌──────────────────────────────────────────────────────────────────┐  │
│   │ Supervisor (orchestrator/supervisor.py)                          │  │
│   │   ├── Planner (orchestrator/planner.py)        ← NEW Week 2      │  │
│   │   │     └── decompose query → list[SubQuery]                     │  │
│   │   ├── Worker dispatch                                            │  │
│   │   │     ├── chart_tools (tools/*.py — v1 unchanged)              │  │
│   │   │     ├── intake_extractor (documents/*)    ← NEW Week 2       │  │
│   │   │     └── evidence_retriever (corpus/*)     ← NEW Week 2       │  │
│   │   └── Critic (orchestrator/critic.py)         ← NEW Week 2       │  │
│   │         └── gate; max 1 retry per sub-query                      │  │
│   └──────────────────────────────────────────────────────────────────┘  │
│                              │                                          │
│                              ▼                                          │
│   ┌──────────────────────────────────────────────────────────────────┐  │
│   │ Verification Middleware (verification/middleware.py)             │  │
│   │   ├── citation_check.py     ← extended for OCR / bbox path       │  │
│   │   ├── field_check.py        ← v1 unchanged                       │  │
│   │   ├── abstention.py         ← extended enum (Appendix A.1)       │  │
│   │   └── discrepancy/          ← v1 + extended for extracted facts  │  │
│   └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│   ┌──────────────────────────────────────────────────────────────────┐  │
│   │ Async pipeline (off the user's hot path)                         │  │
│   │   ├── documents/queue.py     ← Postgres SKIP LOCKED job table    │  │
│   │   ├── documents/extractor.py ← VLM call + schema validate        │  │
│   │   └── corpus/index.py        ← one-shot indexer (offline)        │  │
│   └──────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
       │                                       │
       │ FHIR / REST + JWT                     │ writes traces, eval, audit,
       ▼                                       │ extraction queue, corpus index
┌──────────────────┐              ┌────────────▼─────────────────────────┐
│ OpenEMR MariaDB  │              │ Agent Postgres                       │
│ (system of       │              │ ├── audit_log (v1)                   │
│  record, PHI,    │              │ ├── eval_baseline (v1 + W2)          │
│  documents       │              │ ├── extraction_jobs    ← NEW W2      │
│  blob storage)   │              │ ├── extracted_facts    ← NEW W2      │
└──────────────────┘              │ └── corpus_index (pgvector)← NEW W2  │
                                  └──────────────────────────────────────┘

External: Anthropic API (Claude Sonnet/Haiku candidates + vision-tier),
          LangSmith (traces, costs, observability — PHI-redacted)
```

### What's genuinely new on the agent service

| New module | Path | Responsibility |
|---|---|---|
| Planner | `orchestrator/planner.py` | Decompose query → `list[SubQuery]`, assign claim type |
| Supervisor | `orchestrator/supervisor.py` | Worker dispatch, result stitching, critic loop |
| Critic | `orchestrator/critic.py` | Sentence-level gate; rejection-reason taxonomy |
| Documents schemas | `documents/schemas/{lab_pdf,intake_form}.py` | Pydantic schema + `ExtractedField[T]` |
| Document fetcher | `documents/fetcher.py` | FHIR `DocumentReference` → `Binary` → page render |
| Extractor worker | `documents/extractor.py` | VLM call + schema validate |
| Job queue | `documents/queue.py` | Postgres `SELECT ... FOR UPDATE SKIP LOCKED` |
| Corpus indexer | `corpus/index.py` | One-shot offline build (BM25 + pgvector) |
| Corpus retriever | `corpus/retriever.py` | Hybrid retrieve → cross-encoder rerank |
| Citation OCR check | `verification/citation_check.py` (extended) | Strict + degraded path (PRD2 §8.2) |
| Eval harness W2 | `evals/w2/` | Cases, rubrics, judge, harness CLI |

### What's reused as-is

The intake-extractor's stored output is just another row in
`extracted_facts`, accessible through the same chart-tool layer
abstraction (a new `tools/extracted_facts.py` reads from agent-db,
shaped like the existing FHIR-backed tools). The existing tool-layer
RBAC discipline (verify JWT → check scope → fetch) carries unchanged;
it's the same enforcement path, the data source happens to be agent-db
rather than FHIR.

---

## 2. Trust Boundaries (delta)

v1 §4's trust boundaries are unchanged. Week 2 adds three.

### 2.1 Document fetch is JWT-bound

The intake-extractor never reads the document blob from disk or
filesystem. Every document binary is fetched via FHIR
`DocumentReference` → `Binary` with the same JWT the supervisor used
to start the turn. The `patient_id` on the JWT must match the
document's `subject` reference; mismatches produce `UNAUTHORIZED` and
an audit row. This makes per-patient access to documents the same
gate as per-patient access to labs — it's checked at OpenEMR's FHIR
layer.

### 2.2 Extraction queue is patient-scoped at enqueue

The post-upload Symfony event listener emits a payload of
`{document_id, patient_id, category_id, uploader_user_id, signed_at}`,
HMAC-signed with the gateway's existing secret. The extractor
verifies the HMAC, then re-fetches the document by FHIR (which checks
RBAC again). The HMAC prevents queue-poisoning by anything that's not
the OpenEMR gateway; the FHIR re-fetch prevents replay against a
different patient context.

### 2.3 Corpus is read-only and patient-free

The corpus index is built **offline** by `corpus/index.py` from a
checked-in `corpus/sources/` directory. The agent service has
read-only access to the index at runtime. There is no path through
which a corpus chunk could contain patient data — the indexer rejects
documents containing PHI-shape regex patterns at build time, with the
build itself logged so any rejection is auditable.

---

## 3. Document Ingestion Flow

> **Status (2026-05-06).** §3.1 (PHP-side listener path), §3.2 (queue +
> async worker), §3.3 (Documents-view side panel polling
> `GET /agent/documents/{id}`), and §3.4 (chart-panel summary card via
> `GET /agent/documents/summary`) are the planned production design and
> have not landed. Today's path is one synchronous round-trip:
>
> 1. Clinician opens the chart's Labs panel and clicks "Upload lab
>    document (AI extract)" (or, for a brand-new patient, picks
>    "Add Patient (with AI)" from the Patient menu).
> 2. `interface/copilot/upload_lab.php` (or `new_patient_with_ai.php`)
>    accepts the file via OpenEMR's standard form, then POSTs the binary
>    as multipart to `POST /api/agent/internal/ingest` on the agent
>    service through `OpenEMR\Services\Copilot\IngestClient`. The HTTP
>    call is service-to-service: it carries an internal-token header,
>    *not* a clinician JWT — the patient binding is supplied as a
>    multipart form field.
> 3. The agent service's `ingest_route` (in `main.py`) calls
>    `documents.extractor.run_extraction` synchronously, persists the
>    facts as JSON via `documents.store`, and returns the parsed facts
>    in the response body alongside `facts_url=
>    /api/agent/internal/extracted/<document_id>` for re-reads.
> 4. The clinician confirms / edits the extracted facts on
>    `lab_review.php` (or `intake_review.php`). On save,
>    `lab_save_ai.php` writes `procedure_order` + `procedure_result`
>    rows; `new_patient_save_ai.php` creates the patient via direct
>    INSERT and seeds `patient_data` + the lists tables.
>
> Re-extraction on a page reload uses
> `GET /api/agent/internal/extracted/{document_id}` so a nav-away-and-
> back doesn't re-spend the VLM call. There is no SKIP-LOCKED worker
> today and no `extraction_jobs` row — the demo's "queue" is the
> request itself.

PRD2 §2.1 has the sequence diagram. This section is the *concrete
mapping* of each lane in that diagram to a module.

### 3.1 Upload path (PHP side)

| Step | Component | File / Symbol |
|---|---|---|
| 1. User drops file in Documents UI | OpenEMR Smarty + Dropzone.js | `templates/documents/general_upload.html` |
| 2. POST `/api/patient/{pid}/document` | OpenEMR REST | `src/RestControllers/DocumentRestController.php::postWithPath` |
| 3. Insert blob + metadata | OpenEMR | `library/classes/Document.class.php::createDocument` |
| 4. Detect Co-Pilot category | NEW Week 2 listener | `src/Events/CoPilotDocumentUploadedListener.php` |
| 5. Sign HMAC payload | Reuse v1 gateway secret | `src/CoPilot/Gateway/HmacSigner.php` (v1) |
| 6. POST to agent-service queue endpoint | NEW Week 2 | `agent-service` `POST /internal/documents/enqueue` |

The category check at step 4 is the **containment boundary**. A
document uploaded to any other category never reaches step 5. This is
the architectural answer to "what about patient X's old documents
that we don't want the agent to see" — they don't have the right
category, so they're invisible to the pipeline.

### 3.2 Extraction path (Python side)

| Step | Component | File / Symbol |
|---|---|---|
| 7. Verify HMAC, write `extraction_jobs` row | `documents/queue.py::enqueue` | |
| 8. Worker picks job (`SKIP LOCKED`) | `documents/queue.py::claim` | |
| 9. Fetch binary via FHIR | `documents/fetcher.py::fetch_binary` | system-scoped JWT (v1 SMART Backend Services pattern); patient binding comes from the HMAC-signed payload checked at step 7 (§2.2), not from a session — extraction is async and the user's session may be gone |
| 10. Render PDF page → image | `documents/fetcher.py::render_page` | pypdfium2, 300 DPI |
| 11. Dispatch to schema | `documents/extractor.py::extract` | type-tag selects `lab_pdf` vs `intake_form` |
| 12. VLM call | `documents/extractor.py::_call_vlm` | Anthropic vision SDK |
| 13. Validate against schema | Pydantic `model_validate` | rejects `OUT_OF_SCHEMA` fields |
| 14. Per-field citation OCR check | `verification/citation_check.py` (extended) | strict path + degraded path (PRD2 §8.2) |
| 15. Domain-rule pass | `discrepancy/rules.py` (v1, extended) | value-sanity, unit, range |
| 16. Persist `extracted_facts` row | `documents/extractor.py::_persist` | per-field, with citation + abstain_reason |
| 17. Update `extraction_jobs.state = 'extracted'` | `documents/queue.py::complete` | |

The async pipeline is **at-most-once** per (`document_id`, `version`).
The job table's primary key includes a content-hash of the binary;
re-uploading the same bytes is a no-op. Re-uploading edited bytes
produces a new job with a new version; the side panel renders the
latest extracted version and links the prior versions for audit.

### 3.3 Render path — Documents-view side panel (canonical)

The Documents-view side panel polls
`GET /agent/documents/{id}` every 3 seconds while
`extraction_jobs.state ∈ {queued, extracting}`. The endpoint reads
from `extracted_facts` only; **it never re-fetches the binary** and
never invokes the extractor. Cost-control by construction: a
clinician opening the panel while extraction is in-flight cannot
trigger duplicate VLM calls.

This is the **canonical extraction-state surface** per PRD2 §2 — the
panel where per-field detail, click-to-source, and per-document
state live.

### 3.4 Render path — chart side panel summary card (secondary)

PRD2 §3 designates the chart side panel as the *secondary,
summary-only* surface. Its rollup card calls a separate endpoint:

`GET /agent/documents/summary?patient_id={pid}` returns
`{queued, extracting, extracted, abstained, failed,
latest_extracted_at}` — counts only, no per-field detail.

Ownership boundaries the endpoint upholds:

- **JWT-bound and patient-scoped.** The requesting JWT's `patient_id`
  must equal the query's `patient_id`; otherwise `UNAUTHORIZED` plus
  audit row, same gate as `GET /agent/documents/{id}`.
- **No per-field data.** The card surfaces aggregate counts plus the
  most recent extraction timestamp. Clicking through deep-links to
  the Documents view, which then renders details via §3.3. The
  summary endpoint structurally cannot leak field-level content —
  the response shape doesn't carry it.
- **Same data store, different aggregation.** Reads from the same
  `extracted_facts` + `extraction_jobs` rows the §3.3 endpoint
  reads. No second source of truth.

Implementation: `agent-service/src/clinical_copilot/tools/
extraction_summary.py` for the aggregation; route registered in
`main.py` next to `GET /agent/documents/{id}`. Polling cadence: same
3-second interval the chart side panel uses for its other v1 widgets.

---

## 4. Multi-Agent Graph

> **Status (2026-05-09).** **Wired into the live query path.** The
> default slow-lane path is still the plain-Python two-worker supervisor,
> gated on `resolved_settings.use_supervisor` (default on) with a
> cross-patient guard, chart-pack pre-fetch, and v1-orchestrator
> fallback on supervisor exception. `clinical_copilot/orchestrator/
> supervisor.py` exposes `dispatch_intake_extractor` and
> `dispatch_evidence_retriever` as Anthropic `tool_use` blocks, the
> model picks, each handoff is logged as a `Handoff` row surfaced via
> `GET /api/agent/supervisor/audit/{resident_user_id}`, and end-to-end
> coverage lives in `tests/integration/test_supervisor.py`.
> Fast-lane requests still go through the v1 single-loop orchestrator
> (`clinical_copilot/orchestrator/agent.py` + `lanes.py`) by design.
>
> **PR 8 LangGraph framing — shipped / fallback / remaining proof.**
>
> * **Shipped implementation.** `langgraph>=0.2,<0.3`,
>   `supervisor_langgraph.py`, `state.py`, `planner.py`, `critic.py`,
>   `edges.py`, and `orchestrator/nodes/*` are in code. `/api/agent/query`
>   routes through the StateGraph only when `USE_LANGGRAPH=true` and the
>   required collaborators are wired.
> * **Fallback posture.** `USE_LANGGRAPH` defaults false, so the
>   production demo remains on the plain-Python supervisor unless the
>   flag is deliberately flipped. LangGraph exceptions fall back to the
>   plain-Python supervisor, which still falls back to v1.
> * **Still open.** LangSmith per-node span linkage, citation-separation
>   eval pass, and the `import-linter` boundary contract remain proof
>   gates rather than shipped enforcement.
> The `tools/extracted_facts.py` and `tools/guideline_evidence.py`
> wrappers that would let the v1 agent reach into Week 2 stores are
> likewise not yet built. Extraction and corpus retrieval are reachable
> today through the demo CLIs, the `POST /api/agent/internal/ingest`
> route (which calls `run_extraction()` directly), the default
> plain-Python supervisor, and the opt-in LangGraph path — not through
> the v1 chat agent's tool registry.

Four LangGraph nodes — supervisor (with planner as the entry-point
node), intake-extractor, evidence-retriever, critic — plus the
verification middleware as a final node. PRD2 §5 has the topology
and the note on rubric framing (rubric §4 lists the critic as
extension; we ship it as core because the action-suggestion
blacklist is load-bearing safety per v3 §5). This section is the
concrete LangGraph wiring and the *invariants* the StateGraph
upholds.

**Framework.** `langgraph>=0.2,<0.3`. We use LangGraph **minimally** —
`StateGraph`, `add_node`, `add_edge`, `add_conditional_edges`, and
typed state. We do **not** use LangChain agents, ReAct loops,
LangChain `Tool` wrappers, or any framework abstraction beyond the
graph orchestrator itself. Node bodies are plain Python that read
and write a typed state dict.

### 4.1 State + Graph definition

**File:** `orchestrator/supervisor.py`. **Replaces** v1's
`orchestrator/agent.py` for Week 2 turns; v1's `agent.py` remains
reachable as a leaf node for the §4.5 single-claim short-circuit.

State shape:

```python
# Pseudocode — actual TypedDict lives in orchestrator/state.py
class TurnState(TypedDict):
    user_query: str
    session: Session                       # JWT, patient_id, history
    sub_queries: list[SubQuery]            # filled by planner node
    drafts: list[Draft]                    # filled by worker nodes
    retry_counts: dict[str, int]           # sub_query_id → count, max 1
    final_response: Response | None        # filled by verification node
```

Graph definition (declarative; declared once at process start):

```python
# Pseudocode — actual code in orchestrator/supervisor.py
graph = StateGraph(TurnState)

graph.add_node("planner",            planner_node)             # §4.2
graph.add_node("chart_tools",        chart_tools_node)         # §4.3
graph.add_node("intake_extractor",   intake_extractor_node)    # §4.3
graph.add_node("evidence_retriever", evidence_retriever_node)  # §4.3
graph.add_node("critic",             critic_node)              # §4.4
graph.add_node("verification",       verification_node)        # §7
graph.add_node("v1_single",          v1_single_node)           # §4.5 leaf

graph.set_entry_point("planner")

# Planner classifies and routes
graph.add_conditional_edges("planner", route_after_planner, {
    "v1_single":          "v1_single",          # §4.5 short-circuit
    "fan_out":            "fan_out_dispatch",   # multi-worker case
})

# Each worker node returns to the critic, which decides retry vs.
# pass-through to verification.
graph.add_conditional_edges("chart_tools",        next_after_worker, {...})
graph.add_conditional_edges("intake_extractor",   next_after_worker, {...})
graph.add_conditional_edges("evidence_retriever", next_after_worker, {...})

# Critic loops back on rejection (max 1 retry per sub-query); accepts
# route to verification.
graph.add_conditional_edges("critic", route_after_critic, {
    "retry":         "fan_out_dispatch",
    "verification":  "verification",
    "abstain":       "verification",   # critic-rejected sub-query
                                       # collapses to abstain
})

graph.add_edge("verification", END)
graph.add_edge("v1_single",    "verification")

compiled = graph.compile()
```

Statelessness invariant: the supervisor (compiled graph) does not
retain memory across turns. Conversation history is held by the v1
`sessions.py` module and *injected* into the planner node's input via
`TurnState.session`; the graph itself is stateless across invocations.

### 4.2 Planner (node)

**File:** `orchestrator/planner.py`. Wraps a single LLM call as a
LangGraph node body — reads `state["user_query"]` and
`state["session"]`, writes `state["sub_queries"]`.

Single LLM call (Haiku-tier, structured output). Input:
`{user_query, session_history, patient_summary}`. Output:
`list[SubQuery]` with `text`, `claim_type`, `target_worker`.

The planner is the *only* place claim type is assigned. The mapping
`claim_type → target_worker` is fixed in code:

```python
CLAIM_TYPE_TO_WORKER: Final[Mapping[ClaimType, Worker]] = {
    ClaimType.CHART_FACT: Worker.CHART_TOOLS,
    ClaimType.DOC_FACT:   Worker.INTAKE_EXTRACTOR,
    ClaimType.GUIDELINE:  Worker.EVIDENCE_RETRIEVER,
}
```

The LLM picks claim type; routing is deterministic. This is what makes
the planner reviewable: you can audit the routing without understanding
prompt internals.

**Rejected alternative.** Letting the LLM emit the worker name
directly. We tried this informally on a smaller test set in week 1
prototyping; the LLM got the routing right but the structured output
was harder to reason about (worker names drift across prompt
revisions). Splitting concerns — LLM picks meaning, code picks address
— gave us a stable artifact for `planner.decompose` spans.

### 4.3 Worker nodes

Each is a LangGraph node — a plain Python function that reads from
`TurnState` and appends to `state["drafts"]`. Module locations and
data sources:

| Worker node | Module | Reads from | Writes to | LLM calls |
|---|---|---|---|---|
| `chart_tools` | `tools/{allergies,labs,…}.py` (v1) wrapped in `orchestrator/nodes/chart_tools.py` | OpenEMR FHIR/REST | `state["drafts"]` only | none (retrieval-first per v1 §3) |
| `intake_extractor` | `orchestrator/nodes/intake_extractor.py` reads `tools/extracted_facts.py` | agent-db `extracted_facts` | `state["drafts"]` only | none at query time |
| `evidence_retriever` | `orchestrator/nodes/evidence_retriever.py` reads `corpus/retriever.py` | corpus index (read-only) | `state["drafts"]` only | embedding model + cross-encoder (local) |

Note that the `intake_extractor` worker **at query time** is a pure
read against `extracted_facts`. The expensive VLM call already happened
in the async pipeline (§3.2); the synthesis path never invokes the
VLM. This is the architectural answer to "how do we keep document
extraction off the latency-budgeted hot path" — by structurally not
having it on the hot path.

### 4.4 Critic (node)

**File:** `orchestrator/critic.py`. Wraps the contract in
Appendix A.6 as a LangGraph node. Reads `state["drafts"]` and
`state["sub_queries"]`; emits a verdict that `route_after_critic`
turns into either `retry` (back to `fan_out_dispatch`) or
`verification`.

Implementation is **two-tier**:

1. **Deterministic checks first** (no LLM call):
   - Citation existence: every prose claim has a `source_id` or
     `corpus_id`.
   - Citation type matches planner-assigned claim type (chart claim →
     `source_id`; guideline claim → `corpus_id`).
   - Action-suggestion blacklist: regex over the rejection-trigger
     verb set ("start", "stop", "increase", "decrease", "switch to",
     "discontinue", "recommend [verb-ing]").
   - Confidence floor: `ExtractedField.citation.confidence ≥ 0.7`.

2. **LLM judge call only if (1) passes**, asking "does the cited
   record actually support the claim?" — the v1 §3 step 4 check, but
   on the synthesized prose rather than the raw draft. This is the
   one place a probabilistic check sits in the path because the
   alternative (full-text NLI implemented deterministically) is
   beyond MVP scope.

The two-tier ordering matters: the deterministic checks catch most
rejections, so the LLM judge runs on a small fraction of claims.
This keeps the critic's p95 within the 1.5s cap (PRD2 §10.1).

### 4.5 Post-planner short-circuit (LangGraph conditional edge)

**The planner always runs first** (per PRD2 §5.1 and Appendix A.5 —
"the planner runs on every turn unconditionally"). This section is
about what happens *after* the planner node has populated
`state["sub_queries"]`, not about skipping it.

The short-circuit is a **conditional edge** in the StateGraph:
`route_after_planner(state)` returns `"v1_single"` when
`state["sub_queries"]` has length 1 and that sub-query's `claim_type`
is `CHART_FACT`; otherwise `"fan_out"`. The `v1_single` node wraps
v1's `orchestrator/agent.py` and emits a single draft into
`state["drafts"]`, then routes directly to `verification` (skipping
the worker fan-out and the critic).

For everything else — composite queries, document-fact sub-queries,
guideline sub-queries — the `fan_out` path invokes the worker nodes
in parallel (LangGraph's parallel-fan-out semantics), each appending
to `state["drafts"]`, then routes through the critic to verification.

The planner cost is paid on every turn, the rest of the graph is
not. This is *not* a design hedge — it's a latency-budget
realization. A four-node-with-evidence-retrieval pass for a
one-claim query spends retrieval and corpus-rerank latency the
answer doesn't need. The split is documented and tested in eval
bucket `latency.single_claim_passthrough`.

---

## 5. Vision Extraction & Schemas

### 5.1 Schema layout

```
agent-service/src/clinical_copilot/documents/schemas/
├── __init__.py
├── abstain.py          # imports v1 abstention enum, extends per Appendix A.1.a
├── citation.py         # SourceCitation + ExtractedField[T]
├── lab_pdf.py          # LabPdfFacts (Observation-shaped fields)
└── intake_form.py      # IntakeFormFacts (chief complaint, meds, allergies, ...)
```

`ExtractedField[T]` is a generic over the field's value type, with the
`value_xor_abstain` validator (PRD2 §6). All field types are nullable —
`None` paired with an `abstain_reason` is a first-class outcome, not an
error.

### 5.2 VLM call shape

`documents/extractor.py::_call_vlm` constructs an Anthropic Messages
API call with:

- A **system prompt** that contains *only* the schema (rendered as
  TypeScript types via `pydantic.TypeAdapter` for token efficiency)
  and the citation contract. No instruction to "be careful" or
  "don't hallucinate" — the schema itself is the constraint.
- A **user message** with two content blocks: an `image` block (the
  rendered PDF page) and a `text` block stating the document type
  (`"This is a lab PDF. Extract the Observation values."`).
- `tool_choice` set to a single tool corresponding to the schema, so
  the only valid model output is a schema-conforming tool call. This
  uses Anthropic's structured-output feature; `OUT_OF_SCHEMA` failures
  are caught at the SDK boundary, not at our validator.

### 5.3 Multi-page handling

For multi-page documents, each page is extracted independently and
results are merged at the schema level. Two cases:

- **Stateless schemas** (`lab_pdf`): each Observation is independent;
  we concatenate per-page extraction lists and dedupe by
  `(code, effective_date, value, page)`.
- **Stateful schemas** (`intake_form`): the form has a fixed shape;
  we extract page 1 and only invoke later pages if any required field
  came back `NO_DATA`. This bounds VLM cost for typical 1–2-page
  intake forms.

The merge logic lives in `documents/merge.py` and is unit-tested
against fixture documents that span page boundaries. **No LLM is
involved in the merge** — it's deterministic.

---

## 6. Hybrid RAG

> **Status (2026-05-08).** Corpus + indexer + **hybrid retriever
> (BM25 + dense via OpenAI `text-embedding-3-small`, RRF-fused with
> `k=60`)** are shipped and **wired into the live
> `/api/agent/query` slow-lane path** through the supervisor's
> `evidence_retriever` worker — see `corpus/retriever.py` for
> the fusion, `corpus/rerank.py` for the rerank, and
> `/api/agent/query` in `main.py` for the supervisor wiring. The rerank stage is
> two-tier: **Cohere `rerank-v3.5`** is the Sunday-target primary
> backend (PR 7 landing before submit, promoted from the
> post-Sunday queue 2026-05-08); the existing **LLM-judge
> implementation** (Claude Haiku, ~600 ms p50) stays as the
> env-var-gated fallback when `COHERE_API_KEY` is absent or the
> Cohere call errors. `corpus/sources/` now holds **30 Markdown
> excerpts** (~262 chunks) under `uspstf/`, `cdc/`, `nih/`, `aha/`,
> each a *synthetic excerpt adapted from public guidance* per
> `corpus/sources/LICENSES.md`. Coverage spans screening *and*
> management for every condition / medication / lab surfaced in the
> W1 chart fixtures and the cohort-5-week-2-assets-v2 set (HFrEF
> GDMT, hypertension management, statin intensity, CAD secondary
> prevention, ACEi monitoring, CKD staging, atrial fibrillation
> anticoagulation, basal-insulin initiation, asthma in pregnancy,
> alcohol use disorder, CBC and CMP interpretation, and the W1
> anchors — diabetes management, metformin monitoring, AOM,
> iron-deficiency anemia, thyroid). The dense path is gated on the
> `dense.pkl` artifact + `OPENAI_API_KEY` and degrades cleanly to
> BM25-only when absent (the deployed Railway demo runs BM25-only
> today; the dense artifact rebuild lands when the key is in the
> deploy environment). Local cross-encoder alternatives
> (`bge-reranker-base` via `sentence-transformers`) and the Jina
> rerank API remain documented as cheap follow-ons in TASKS2.md →
> "PR 7" but are not the Sunday backend.

### 6.1 Corpus structure

```
agent-service/corpus/
├── sources/                       # checked-in source documents (Markdown)
│   ├── uspstf/
│   ├── cdc/
│   └── nih/
├── LICENSES.md                    # required: per-source permission basis
└── chunks/                        # generated by index.py, gitignored
```

Chunking. `corpus/chunker.py` produces sentence-window chunks (window
size 3, stride 1), with parent-document metadata preserved as YAML
frontmatter. Chunk size targets 200–400 tokens; longer sentences
collapse the window to fit the upper bound. Chunker is deterministic
and tested against fixture corpus documents.

### 6.2 Index build

`corpus/index.py` is a one-shot CLI invoked manually
(`python -m clinical_copilot.corpus.index --rebuild`). It writes:

- A BM25 index (`rank_bm25.BM25Okapi` pickled to disk) under
  `agent-service/data/corpus/bm25.pkl`.
- pgvector embeddings (`text-embedding-3-small`, 1536 dims) into the
  `corpus_index` table in agent-db.
- A manifest (`agent-service/data/corpus/manifest.json`) with the
  source-document checksums, license attestations, and build
  timestamp.

The build runs offline; it is not invoked by the agent service at
runtime. Re-builds are operational events with their own
runbook (§16).

### 6.3 Retrieval

`corpus/retriever.py::hybrid_retrieve` runs in three stages:

1. **Query rewrite (LLM, optional).** A Haiku-tier call expands the
   user's intent given the active patient summary — e.g., turning
   *"what does the guideline say"* into *"USPSTF screening
   recommendations for adults with diabetes type 2"*. Skipped when
   the planner emits a sub-query that's already specific enough
   (heuristic: ≥4 medical-vocabulary tokens).
2. **Parallel first-stage retrieval.** BM25 returns top-20; pgvector
   cosine-similarity returns top-20; results are deduped by
   `chunk_id` and unioned.
3. **Rerank stage.** Cohere `rerank-v3.5` reranks the union to
   top-K=5 when `COHERE_API_KEY` is configured (the Sunday-target
   primary, ~80 ms p50, ~$2/1k searches; PR 7). The LLM-judge
   implementation in `corpus/rerank.py` (Claude Haiku scoring each
   candidate, ~600 ms p50) stays as the env-var-gated fallback when
   the key is absent or the Cohere call errors so the rerank stage
   is best-effort end-to-end. The supervisor's `evidence_retriever`
   worker reports which backend actually ran via the
   `rerank_backend` field on `EvidenceRetrieverOutput` (`"cohere"` |
   `"llm-judge"` | `"none"`).

Output: `list[CorpusSnippet]` where each carries `chunk_id`,
`corpus_id`, `source_url`, `version`, `score`. The supervisor cites
by `corpus_id` + `chunk_id`; the critic verifies citation type per
the planner's claim assignment.

### 6.4 Why hybrid + rerank

- **BM25 alone** misses paraphrase. *"first-line therapy for type 2
  diabetes"* and *"initial pharmacologic management of T2DM"* don't
  share enough tokens.
- **Dense alone** loses on rare medical terminology — drug names,
  ICD codes — where exact-token match is the right inductive bias.
- **Rerank** fuses both into one ranking the synthesis call uses.

Each layer is unit-testable in isolation (`tests/unit/corpus/`)
against fixture queries with hand-labeled gold chunks.

---

## 7. Verification Middleware (extensions)

v1's `verification/middleware.py` is unchanged in structure. Three
files extend with Week-2-specific paths:

### 7.1 `verification/citation_check.py` (extended)

> **Status (2026-05-08).** The `CitationKind` enum and
> `_check_document_bbox` strict-+-degraded path are **scoped as
> PR 5 in the post-Sunday queue**. Today, document fields
> abstain on confidence floor only (`< 0.7 → LOW_CONFIDENCE`).
> `CITATION_INVALID` is a member of the runtime abstain enum but is
> unreachable until the OCR check ships. No Tesseract pass exists in
> `agent-service`; `pyproject.toml` does not include `pytesseract`.
> `_check_corpus_chunk` is also not yet wired. Full plan (Tesseract
> in Dockerfile, pytesseract wrap, fuzzy-match threshold,
> 10-fixture false-reject set) in TASKS2.md → "PR 5".

Existing v1 logic checks structured-fact citations against
FHIR-resolved records. Week 2 adds **document citations**:

```python
class CitationKind(StrEnum):
    STRUCTURED_FACT = "structured_fact"  # v1; resolves via FHIR
    DOCUMENT_BBOX   = "document_bbox"    # W2; resolves via OCR check
    CORPUS_CHUNK    = "corpus_chunk"     # W2; resolves via index lookup

def check(citation: Citation) -> CitationVerdict:
    match citation.kind:
        case CitationKind.STRUCTURED_FACT:
            return _check_structured(citation)        # v1
        case CitationKind.DOCUMENT_BBOX:
            return _check_document_bbox(citation)     # W2; PRD2 §8.2
        case CitationKind.CORPUS_CHUNK:
            return _check_corpus_chunk(citation)      # W2
```

`_check_document_bbox` implements the strict-path-then-degraded-path
rule from PRD2 §8.2, returning one of `valid`, `low_confidence`,
`invalid`. The thresholds (0.85 token-set, 0.4 OCR confidence,
0.5%/60% area bounds) are constants in the module, with a
threshold-revision protocol in §16.

`_check_corpus_chunk` confirms the cited chunk exists in the
manifest, its `corpus_id` is on the permitted-source list, and the
manifest checksum hasn't drifted since index build.

### 7.2 `verification/abstention.py` (extended)

Imports the v1 four-state enum and extends it with the Week-2
runtime states. The eval-only `JUDGE_INCONCLUSIVE` lives in a
separate module (`evals/case_state.py`) per Appendix A.1; the two
modules do not cross-import. An `import-linter` contract enforces
this (§10).

### 7.3 `verification/middleware.py` (no behavior change)

Composition order unchanged from v1: claims arrive structured,
citation existence is checked first, citation resolution next,
field-level value check next, abstention decision last. The
addition of new `CitationKind` variants is invisible to
`middleware.py` — it only sees the verdict.

---

## 8. Observability & PHI Safety

PRD2 §9 sets the policy: never log raw document text, never log
patient identifiers, fail-closed under PHI-detection. This section
specifies *how* it's enforced.

### 8.1 Span shape

Every Week-2 span carries:

- `run_id`, `parent_run_id` (for handoff lineage).
- `worker` (one of `planner`, `chart_tools`, `intake_extractor`,
  `evidence_retriever`, `critic`, `verification`).
- Hashes only (`input_sha256`, `output_sha256`) — not the values.
- `latency_ms`, `decision` (accept / reject / abstain), and the
  decision's reason code (when applicable; e.g., `NO_CITATION`).
- A `pseudonym` field for patient identity: HMAC-SHA256 of
  `patient_id` with a salt from `COPILOT_PSEUDONYM_SALT` env var.
  Reversible only inside the agent service.

### 8.2 Redaction layer

All LangSmith calls go through
`observability/langsmith_client.py::send_span`, which applies a
**deny-by-default** filter:

- Whitelisted keys: the structural fields above.
- Any other key is dropped with a `redacted=true` flag.
- A regex layer scans every string value for PHI signals (SSN,
  MRN-shape, phone, email, raw chart text patterns); a match drops
  the whole span and emits a metric (`spans_redacted_total`).

### 8.3 Eval-side enforcement

A `phi.span_redaction` rubric class runs the eval suite with span
capture enabled, then asserts that **zero** captured spans contain
any PHI signal. A single signal fails the run (Appendix A.2 clause
6). This is the regression net — code review is a fallback, not
the primary control.

---

## 9. Eval Harness

> **Status (2026-05-08).** A **65-case extraction eval gate** is
> shipped: cases at `agent-service/evals/extraction/cases.jsonl`,
> runner at `src/clinical_copilot/evals/extraction/runner.py`
> (exits non-zero on threshold breach or > 5 pp regression), thresholds
> in `evals/extraction/baseline.json`. The pre-push hook
> (`agent-service/scripts/pre-push.sh`) and the **GitLab CI pipeline**
> (`.gitlab-ci.yml`) both invoke `make eval-extraction-gate`.
> Cases span five buckets: `extraction` (28 cases — lab, intake,
> fax, referral, workbook, hl7-oru, hl7-adt), `retrieval` (23
> cases — rt01–rt23 covering screening + management for every
> demo condition / medication / lab), `citations` (6),
> `missing-data` (4), `refusals` (4). Boolean-rubric discipline of
> PRD2 §8 is enforced (`observation_count_min`, `field_equals`,
> `field_present`, `field_abstains`, `list_min`, citation-presence,
> schema-validity, safe-refusal, factually-consistent,
> no-PHI-in-logs).
>
> **Still deferred:** the 3-of-3 unanimous judge wrapper, the budget
> pre-flight, the quarantine ceiling, and the
> `latency.stage_p95` / `phi.span_redaction` rubric classes. The full harness
> package layout below — `clinical_copilot/evals/{harness,rubrics,
> judge,budget,results,case_state}.py` + `evals/w2/{cases.jsonl,
> judge.yaml,fixtures,corpus_freeze,results}/` — is the design target;
> what shipped is a leaner subset rooted at
> `src/clinical_copilot/evals/extraction/` + `agent-service/evals/
> extraction/`. The `make eval` target chains
> `make check` (lint + type + pytest) → `tests/eval/runner.py` (the
> v1 Q&A suite), and `make eval-extraction-gate` runs the 65-case
> extraction gate.

### 9.1 Layout

```
agent-service/src/clinical_copilot/evals/
├── __init__.py
├── case_state.py           # JUDGE_INCONCLUSIVE; eval-only enum
├── harness.py              # CLI entry: make copilot-eval
├── rubrics.py              # all rubric definitions
├── judge.py                # 3-of-3 unanimous judge wrapper
├── budget.py               # token-budget pre-flight gate
├── results.py              # JSON + Markdown writers
└── w2/
    ├── cases.jsonl         # 65 cases, line-per-case
    ├── fixtures/           # fixture documents (lab PDFs, intake forms)
    ├── corpus_freeze/      # frozen corpus snapshot for reproducibility
    └── results/            # committed run outputs (Markdown + JSON)
```

`agent-service/Makefile` adds:

```make
copilot-eval:
	uv run python -m clinical_copilot.evals.harness \
	  --bucket-set w2 \
	  --budget-cap 5.00 \
	  --baseline main
```

### 9.2 Rubric authoring

Rubrics are functions:

```python
@rubric(class_="extraction", id_="field_present")
def extraction_field_present(case: Case, response: Response) -> bool:
    expected = case.expected.lab_pdf.fields
    actual = response.intake_extractor.lab_pdf.fields
    return all(f in actual and not actual[f].is_abstain for f in expected)
```

Each rubric returns `bool` (deterministic) or `Literal["pass",
"fail", "judge_inconclusive"]` (judge-evaluated). The harness
collects rubrics by class (`extraction`, `retrieval`, `citation`,
`reconciliation`, `rbac`, `abstention`, `latency`, `phi`) and
applies the per-class rules from Appendix A.2.

### 9.3 Budget enforcement

`evals/budget.py::preflight` estimates token spend by:

1. Loading all 65 cases.
2. Counting expected VLM calls per case (1 per fixture document).
3. Multiplying by per-call token estimates (input + output) at the
   active VLM tier's per-token price.

If projected spend exceeds the cap (`$5` default per pre-push eval
run; overridable via `--budget-cap` for local exploration), the run
aborts with `BUDGET_EXCEEDED` *before* any live calls. This is the
architectural answer to "how do we prevent a developer from
accidentally running a $50 eval" — by structurally not letting them
start one.

### 9.4 Flake handling

Per PRD2 §8.1: per-case retry up to 2 on transient infra
(`TOOL_FAILURE` exit), 3-of-3 unanimous judge for judge-evaluated
rubrics, quarantine ceiling 5%. Implementation lives in
`evals/judge.py` (judge wrapper) and `evals/harness.py::run_case`
(retry loop). Each retry is logged so flake patterns are visible
in the results Markdown.

### 9.5 Pre-push hook split

`.pre-commit-config.yaml` has two hooks:

- `copilot-eval-fast` (stage `pre-commit`) — runs only the
  deterministic rubric classes (`schema`, `citation`, `rbac`).
  Cached LLM responses; offline; <30s.
- `copilot-eval-full` (stage `pre-push`) — runs the full 65-case
  suite per Appendix A.2.

Both target `make copilot-eval` with different flags. The fast
hook keeps the per-commit loop tight; the push hook is the gate.

---

## 10. Tool-vs-RAG Boundary Enforcement

> **Status (2026-05-09).** The package boundary (`clinical_copilot/
> tools/`, `clinical_copilot/documents/`, `clinical_copilot/corpus/`)
> exists in code and is respected today — `corpus/` does not import
> from `tools/` or `documents/`, and vice versa. The
> `import-linter`-as-gate enforcement in §10.1 is **not configured**:
> there is no `agent-service/.importlinter` file and the Makefile has
> no `import-check` target. PR 8 added distinct planner / critic /
> worker-node modules plus typed state, but the enforceable structural
> guarantee PRD2 §5.3 promises still needs the import-linter gate; until
> then, the boundary holds by convention, audited by reading the package
> layout.

PRD2 §5.3 + Appendix A.5 are the *what*. This section is the *how*.

### 10.1 Module structure as the boundary

The Python package layout is the first line of enforcement. The
two retrieval surfaces live in *different* top-level packages with
no shared submodule:

```
clinical_copilot/
├── tools/         # patient-fact retrieval (v1; FHIR-mediated)
├── corpus/        # guideline retrieval (W2; RAG-mediated)
├── documents/     # extracted-fact production + read
└── orchestrator/  # supervisor; the only place the two surfaces meet
```

Cross-package imports are restricted by an `import-linter` contract:

```ini
# agent-service/.importlinter
[importlinter:contract:tool-vs-rag]
name = Tool and corpus packages must not cross
type = forbidden
source_modules =
    clinical_copilot.corpus
forbidden_modules =
    clinical_copilot.tools
    clinical_copilot.documents

[importlinter:contract:tool-vs-rag-reverse]
name = Tool/document packages must not import corpus
type = forbidden
source_modules =
    clinical_copilot.tools
    clinical_copilot.documents
forbidden_modules =
    clinical_copilot.corpus
```

`make import-check` runs this contract; the pre-push hook calls it.
A violation fails the push.

### 10.2 Index-time PHI scrub

The corpus indexer (`corpus/index.py`) runs every source document
through `corpus/scrub.py::detect_phi` before chunking. Detector
covers: SSN, MRN-shape, raw phone, email, and a handful of name
patterns. Match → reject the document, log to `index_build.log`,
manual review required to add. This is auditable: the manifest
records every accepted document; rejected documents leave a log
entry but no chunk.

### 10.3 Worker-level invariants

Each worker has a single read source asserted in its constructor:

```python
class IntakeExtractor:
    def __init__(self, store: ExtractedFactsStore) -> None:
        self._store = store        # only access path
        # Type system prevents passing a CorpusIndex here.

class EvidenceRetriever:
    def __init__(self, index: CorpusIndex) -> None:
        self._index = index        # only access path
        # Type system prevents passing an ExtractedFactsStore here.
```

`ExtractedFactsStore` and `CorpusIndex` are nominally distinct types
with no shared base class. Mistakenly passing the wrong store fails
mypy, not just runtime. This is the structural-not-procedural
enforcement PRD2 §5.3 promises.

---

## 11. Latency Budgets

> **Status (2026-05-06).** Per-stage histogram aggregation
> (`observability/latency.py`) and the `latency.stage_p95` rubric class
> are deferred (PR 12 / PR 13). The §10.1 budgets in PRD2 are the
> design target; nothing in the deployed agent enforces them today.
> Spans carry `latency_ms` already, but no eval-side aggregation
> writes the per-stage report below.

PRD2 §10.1 has the per-stage budgets. This section is enforcement.

### 11.1 Per-stage timer instrumentation

Every span emits `latency_ms`. `observability/latency.py` aggregates
spans into a per-stage histogram per run. The eval harness writes
the histogram to results, so each pre-push run produces:

```
extraction.queue_lag_p95_ms: 24500   (budget: 30000) ✓
extraction.vlm_p95_ms:       38200   (budget: 45000) ✓
synthesis.planner_p95_ms:    1450    (budget: 1500)  ✓
synthesis.critic_p95_ms:     1620    (budget: 1500)  ✗
```

### 11.2 Eval rubric tie-in

`rubrics.py` defines a `latency.stage_p95` rubric that asserts each
budget. Failures are eval failures; the gate (Appendix A.2)
includes them in clause 3 (rubric class regression > 5pp).

### 11.3 Hot-path enforcement

For hot-path stages (planner, retrieval, synthesis, critic,
verification), exceeding the p95 cap *during a real turn* aborts
the response with `TOOL_FAILURE`. The implementation is a
`asyncio.wait_for` wrapper at the supervisor level; the timeout
constants are imported from the same module the eval-rubric reads
from, so eval and runtime see the same number.

---

## 12. Threshold-Revision Protocol

A handful of thresholds in this architecture (citation token-set
0.85, OCR confidence 0.4, bbox area 0.5%/60%, latency p95 budgets,
quarantine ceiling 5%) are *starting points*. PRD2 says they're
revisable in W2_ARCHITECTURE.md "with a written justification." This
section is the protocol.

A revision PR must:

1. **State the current value and the proposed value.** No "raise
   slightly"; the value is a number.
2. **Cite eval data.** A revision is justified by a rubric outcome
   on the current main-branch baseline — typically
   `citation.false_reject_rate > 5%` or `latency.stage_p95` overshoot.
3. **Run a baseline-refresh.** After the revision, the eval harness
   is run with `--rebuild-baseline`, and the resulting baseline is
   committed alongside the threshold change.
4. **Attach a record** to `W2_ARCHITECTURE.md` §12.X (this section
   is the changelog for threshold revisions).

Without a record, the threshold change won't pass review. Silent
threshold drift is the failure mode this protocol exists to
prevent.

---

## 13. Risks and Audit-Dependent Assumptions

PRD2 §13 lists the assumptions; this section says how each shows
up in the architecture and what the architecture does if the
assumption fails.

| Assumption | Failure mode | Architectural fallback |
|---|---|---|
| FHIR `DocumentReference` / `Binary` returns blobs with full RBAC | Custom CCD-only export | Drop-in replacement: a custom PHP gateway endpoint that wraps `DocumentService::getFile()`; fetcher.py points at the new URL |
| Symfony post-upload event fires for both UI and REST upload | Only UI fires | Add an explicit hook in `DocumentRestController::postWithPath` mirroring the listener |
| Anthropic vision quality on scanned PDFs ≥ 80% field accuracy | Drops to ~60% | (a) Pre-process: deskew + contrast normalization in `documents/preprocess.py`, (b) fallback to OCR-first → text-LLM extraction (closed open-question — see PRD2 §16 Q3) |
| 200-doc corpus produces useful retrieval | Eval `retrieval.guideline_in_top_k` < 70% | Curate, don't expand — this is a content problem, not a retrieval-tuning problem |
| Tesseract citation-check FP rate ≤ 5% | FP rate higher | Threshold-revision protocol (§12) — raise to 0.80; if still high, replace with a coarser bbox-presence check |
| Category-as-boundary contains the agent's reach | Misfile in category | Documents subsystem ACL is upstream; the failure is at OpenEMR, not us. Audit confirms upstream ACL is sound |

---

## 14. What This Document Does Not Cover

To keep this document operational, three classes of detail live
elsewhere:

- **Per-MR file lists, dependencies, and test counts.** These are
  in `TASKS.md` (Week 2 block). PRD2 §15.1 has the matrix; TASKS.md
  has the order.
- **Per-token cost math, projected production spend, latency
  measurements.** These are in `./COST_LATENCY.md` (top-level
  Week-2 cost & latency report, shipped 2026-05-08).
- **What the demo script shows on screen, in what order.** Demo
  script is in `README.md` § Week 2 Demo, not here.

If this document grows to cover those, it has lost its job.

---

## 15. Major Tradeoffs

| Tradeoff | Choice | Cost we accepted |
|---|---|---|
| Single orchestrator vs. multi-agent | **Multi-agent (4 nodes, LangGraph StateGraph)** | Coordination latency + verification of handoffs + LangGraph dep; mitigated by planner-deterministic routing, structurally bounded retry, and using LangGraph minimally (graph + nodes only, no agents/ReAct) |
| Procedural vs. structural tool-vs-RAG separation | **Structural (import-linter, type system)** | Build-time enforcement infrastructure; mitigated by it being a one-time cost |
| OCR strict-only vs. degraded-path fallback | **Degraded path with `LOW_CONFIDENCE`** | Higher false-accept risk on bad scans; mitigated by click-to-source forcing visual verification before trusting |
| LLM-judge eval vs. boolean-only | **Boolean rubrics + 3-of-3 unanimous judges** | Slower eval, more cost; mitigated by judge calls only on the small judge-eval rubric class |
| Sentence-level vs. whole-answer rejection | **Sentence-level on slow lane, whole on fast** | Slow-lane responses can be partially-rejected (worse UX) but recover more often; fast lane keeps v1's whole-answer trust story |
| Anthropic Claude as VLM vs. swap to GPT-4o-vision | **Stay on Anthropic** | Possibly suboptimal vision quality; mitigated by single-vendor BAA story being a higher-order win for the trust narrative |
| Tesseract citation check vs. trust the VLM | **Tesseract second-pass check** | Extra dep, extra cost on degraded scans; mitigated by it being batch-only and never on the hot path |
| Local pre-push hook vs. remote CI | **Local pre-push** | Bypassable; mitigated by the bypass policy and reviewer artifact request (PRD2 §8) |

---

## 16. Operational Runbooks (pointers)

These are referenced from this architecture but live as separate
runbook files. Each is a sequence of commands a human runs.

- `agent-service/runbooks/corpus-rebuild.md` — when and how to
  re-index the corpus.
- `agent-service/runbooks/eval-baseline-refresh.md` — when and how
  to refresh the eval baseline (per §12 threshold protocol).
- `agent-service/runbooks/extraction-queue-drain.md` — what to do
  if the extraction queue backs up.
- `agent-service/runbooks/phi-leak-response.md` — what to do if
  the `phi.span_redaction` eval rubric fires (a redaction-layer
  bug → rotate the LangSmith API key, scrub spans, post-mortem).

These are written when the corresponding situation first occurs in
practice; they are not preemptively authored.

---

## 17. Coverage of Assignment-Cited Pitfalls

The Week 2 assignment lists five Common Pitfalls. Each is addressed
structurally — not by a coding convention or code review — and this
table maps each to where the architecture handles it.

| Assignment pitfall | Where it's addressed |
|---|---|
| *"Trying to support five document types before two work reliably."* | PRD2 §12 non-goals: "More than two document schemas in MVP." Stretch types (referral fax, med list) are explicitly backlog (TASKS2 BL-1). |
| *"Using a VLM answer directly without schema validation or source metadata."* | PRD2 §6 schema contract — `ExtractedField[T]` with `value_xor_abstain` validator; W2_ARCH §5.2 — VLM call uses Anthropic structured output with `tool_choice` set to the schema tool, so out-of-schema fields fail at the SDK boundary. |
| *"Letting the supervisor become a black box. Handoffs must be logged and explainable."* | PRD2 §5 handoff logging; W2_ARCH §4 — the plain-Python supervisor records handoff rows, while the opt-in LangGraph path still needs enforced LangSmith per-node `parent_run_id` proof before that part of the rubric is fully closed. |
| *"Using llm-as-a-judge without clear rubric. Use boolean rubrics so failures are actionable."* | PRD2 §8 — boolean rubrics only; PRD2 Appendix A.2 — fail-fast on boolean rubric outcomes; PRD2 §8.1 — judge-evaluated rubrics use 3-of-3 unanimity, never a continuous score. |
| *"Logging raw document text, patient identifiers, or screenshots to SaaS observability tools."* | PRD2 §9; W2_ARCH §8 — deny-by-default span filter, regex layer for PHI signals, `phi.span_redaction` rubric class fail-closes the pre-push gate (Appendix A.2 clause 6). |

Each row is verifiable against a specific section, contract, or
rubric — none is a "we'll be careful" item.

---

## Appendix W — Mapping to PRD2

Reviewers comparing this document to PRD2 can use the following
cross-reference. Every PRD2 section that produced a Week-2 design
decision lands somewhere here.

| PRD2 § | Topic | This doc § |
|---|---|---|
| §2 / §2.1 | Existing Documents subsystem reuse + sequence | §1, §3 |
| §3 | Target user moments | (no new architecture; v1 §1) |
| §4 | Use cases | (PRD; no architecture entry) |
| §5 / §5.1 / §5.2 / §5.3 | Multi-agent graph | §4, §10 |
| §6 | Schemas & VLM | §5 |
| §7 | Hybrid RAG corpus | §6 |
| §8 / §8.1 / §8.2 | Eval gate / flake / citation | §9, §7.1, §11 |
| §9 | Observability + PHI | §8 |
| §10 / §10.1 | Stack + latency budgets | §1 (stack), §11 |
| §11 | Failure modes | §13 |
| §12 | Non-goals | (PRD; no architecture entry) |
| §13 | Risks | §13 |
| §14 | Success criteria | (PRD; this doc explains how each is realized) |
| §15 / §15.1 | Submission mapping + test matrix | (TASKS.md) |
| §16 | Open questions | (PRD; tracked there until closed) |
| Appendix A.1–A.6 | Normative contracts | §4, §7, §9, §10 |
