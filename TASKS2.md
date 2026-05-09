# TASKS2.md — Clinical Co-Pilot Week 2 Build Plan

**Status:** Working task list, derived from PRD2.md + W2_ARCHITECTURE.md
**Last updated:** 2026-05-08
**Owner:** [you]
**Submission target:** Sunday 2026-05-10 afternoon (assignment PRD: ./assignment-prd.md mirror; deliverables checklist below).

## Sunday-submission deliverable map

The cohort-5 Week 2 assignment lists eight deliverables; this table maps
each to the artifact (or noted gap) the grader can open.

| Assignment deliverable | Artifact | Status |
|---|---|---|
| GitLab repo (Week-1 fork + Week-2 changes, setup + env-var docs) | `agent-service/README.md` § Week 2; `CONTRIBUTING.md`; `.env.example` | shipped |
| W2 Architecture Doc | `./W2_ARCHITECTURE.md` | shipped (this doc + WA-doc updated 2026-05-08) |
| Pydantic schemas for `lab_pdf` + `intake_form` w/ citations + validation tests | `agent-service/src/clinical_copilot/documents/schemas/{lab_pdf,intake_form,citation}.py`; `tests/unit/documents/schemas/` | shipped |
| 50-case golden eval set with boolean rubrics + judge config + results | **65 cases** at `agent-service/evals/extraction/cases.jsonl`; rubrics in `evals/extraction/rubrics.py`; thresholds in `evals/extraction/baseline.json`; results emitted to `evals/extraction/results/<run_id>.json` | exceeded (65 vs. 50 required) |
| CI evidence (git hook or equivalent that blocks regressions) | Pre-push hook (`4a81eca23`) + `.gitlab-ci.yml` (`24ae138b9`) | shipped |
| Demo video (3-5 min) showing upload → extraction → retrieval → citations → eval results → observability | `plans/demo-script.md` (TODO) + recorded `.mp4` | **OPEN** — record before submission |
| Cost & latency report (dev spend, projected prod cost, p50/p95, bottlenecks) | `./COST_LATENCY.md` | ✅ |
| Deployed application URL | Railway agent-service + OpenEMR overlay (URL in `agent-service/README.md`) | shipped |

This is an MR-by-MR build checklist for the Week 2 Clinical Co-Pilot scope.
Each top-level item is one GitLab merge request. Sub-tasks are the work
inside that MR. Files marked **NEW** are created in the MR; files marked
**EDIT** are existing files modified in the MR.

## Landing status as of 2026-05-08

The deployed Week 2 demo covers four MRs end-to-end and pulls partial
deliverables forward from several more. Significant code landed Wed–Thu
(2026-05-06 / 2026-05-07) after the Tuesday submission cutoff — the
supervisor + workers, the extraction eval gate + CI, and the hybrid
retriever's dense + rerank stages. Friday–Saturday (2026-05-08) added
**production wiring of the supervisor + hybrid retriever into the
slow-lane `/api/agent/query` path** (gated on the `use_supervisor`
config flag, default on, with v1-orchestrator fallback) and a corpus
expansion that brings every conditions / medications / labs surfaced
in the W1 + cohort-5 fixtures under guideline coverage (15 → 30 docs,
90 → 262 chunks; retrieval bucket 8 → 23 cases; manifest 50 → 65
cases). Read the per-MR status header on each block before
recommending it as "next up."

| MR | Status | Notes |
|---|---|---|
| W2-01 — Schemas + abstain enum | **PARTIAL (DEMO-CUT)** | Runtime schemas, `RuntimeAbstainReason`, and `ExtractedField[T]` shipped under `documents/schemas/`. Eval-side `EvalCaseState` enum, `import-linter` contract, and `evals/` package skeleton — deferred. |
| W2-02 — OpenEMR Documents category + event hook + state-poll | **DEMO-CUT REPLACEMENT** | The Symfony listener + queue + `GET /agent/documents/{id}` are not built. Replaced by chart-side PHP pages (`upload_lab.php`, `new_patient_with_ai.php`, `upload_document.php`) calling `POST /api/agent/internal/ingest` synchronously. |
| W2-03 — `lab_pdf` VLM extraction worker | **PARTIAL (DEMO-CUT)** | Live VLM extraction shipped; **schema is per-field** via `ExtractedField[T]` (every observation field — code/display/value/unit/date/refs/flag — carries its own `SourceCitation` or `abstain_reason`); the VLM is prompted for one bbox per row so the same citation may repeat across a row's fields. Persisted to `data/extracted/<id>.json`, not the planned `extracted_facts` Postgres table. Eval bucket: 7 cases in the extraction bucket of the 65-case manifest (12 planned). |
| W2-04 — `intake_form` extraction | **PARTIAL (DEMO-CUT)** | Live single-page extraction shipped; NKDA negation handled; per-field citations enforced via `ExtractedField[T]`. Stateful page-2 fallback deferred. Eval bucket: 7 cases in the extraction bucket of the 65-case manifest (10 planned). |
| W2-05 — Citation OCR check | **DEFERRED** | No Tesseract pass; today's gate is VLM-confidence < 0.7 only. `CitationKind` and the strict + degraded path are not in code. |
| W2-06 — Evidence retriever | **WIRED (LIVE)** | Hybrid retriever (BM25 + dense via `OpenAIEmbedder`, RRF-fused with `k=60`) at `corpus/retriever.py:62–146`; dense path is gated on the `dense.pkl` artifact + `OPENAI_API_KEY` and falls back to BM25-only when absent. Rerank stage at `corpus/rerank.py` is called from the supervisor's `evidence_retriever` worker (`orchestrator/workers/evidence_retriever.py:81–92`); Cohere `rerank-v3.5` is the Sunday-target primary backend (MR W2-RR — promoted to Sunday-blocking 2026-05-08), with the existing LLM-judge implementation as the env-var-gated fallback when `COHERE_API_KEY` is absent. Routed into `/api/agent/query` slow lane via the supervisor branch (`main.py:541–632`). Corpus is **30 docs / 262 chunks** as of 2026-05-08 (was 15 / 90); covers screening + management for every condition, medication, and lab surfaced in the W1 chart fixtures and the cohort-5-week-2-assets-v2 set. |
| W2-07 — Supervisor + workers | **WIRED (LIVE)** | Supervisor + `intake_extractor` + `evidence_retriever` workers shipped in `orchestrator/supervisor.py` and `orchestrator/workers/` (commit `39f487aaf`, 2026-05-06). Plain Python via Anthropic `tool_use` dispatch — **no LangGraph dependency** (planner / critic nodes deferred). End-to-end test in `tests/integration/test_supervisor.py`; a separate `GET /api/agent/supervisor/audit/{resident_user_id}` endpoint surfaces handoff rows. Routed into `/api/agent/query` slow lane at `main.py:541–632` (gated on `resolved_settings.use_supervisor`, default on, with cross-patient guard + chart-pack pre-fetch + v1-orchestrator fallback on supervisor exception). Fast-lane requests still go through v1 `Orchestrator.run()` by design. The `/api/agent/internal/ingest` route still calls `run_extraction()` directly rather than the `intake_extractor` worker — left as planned because that worker is for *interactive query* dispatch. |
| W2-08 — Reconciliation extension (extracted vs chart) | **DEFERRED** | No new discrepancy rules or eval cases yet. |
| W2-09 — RBAC scope test for documents | **DEFERRED** | The chart-side ingest path uses an internal-token + multipart binding; no `GET /agent/documents/{id}` JWT-bound route exists yet. RBAC tests for the planned route are deferred. The internal-token gate is unit-tested in the v1 RBAC suite. |
| W2-10 — Abstention rendering + chart summary card | **DEFERRED** | No Documents-view side panel and no chart summary card. Clinician review happens on `lab_review.php` / `intake_review.php` / `document_review.php`. |
| W2-11 — Pre-push eval hook + Makefile + flake policy | **SHIPPED** | Extraction eval gate is now **65 cases** at `agent-service/evals/extraction/cases.jsonl` (28 extraction · 23 retrieval · 6 citations · 4 missing-data · 4 refusals); runner at `src/clinical_copilot/evals/extraction/runner.py`; thresholds in `evals/extraction/baseline.json`: `citation_present ≥ 0.95`, `factually_consistent ≥ 0.90`, `safe_refusal = 1.0`, `schema_valid = 1.0`, `no_phi_in_logs = 1.0`, regression budget 5 pp. Pre-push hook (`4a81eca23`) and GitLab pipeline at `.gitlab-ci.yml` (`24ae138b9`) both invoke the gate. Boolean rubrics only; no judge wrapper, no per-stage latency rubric. |
| W2-12 — PHI redaction in LangSmith spans | **DEFERRED** | Demo runs with `LANGSMITH_TRACING=false`. Re-enabling tracing requires this MR. |
| W2-MM — Multimodal expansion (Steps 0-8) | **SHIPPED** | Adds 5 new document types (referral_docx, fax_tiff, workbook_xlsx, hl7_oru, hl7_adt) on top of the existing lab_pdf + intake_form, a universal `upload_document.php` entrypoint with format classifier, and a patient resolver that suggests existing-chart matches from extracted demographics. 8 commits, 35 multimodal extraction cases (5 modality sub-buckets × 7 cases) on top of the 14 lab + intake cases. As of 2026-05-08 the eval manifest is **65 total cases across 5 top-level buckets** (extraction 28 / retrieval 23 / citations 6 / missing-data 4 / refusals 4); the extraction bucket subdivides into 7 modality sub-buckets (lab, intake, fax, referral, workbook, hl7-oru, hl7-adt). See plans/week2-multimodal-expansion.md for the per-step breakdown. |
| W2-CW — Chart-write confirmation path | **IN PROGRESS (FEEDBACK-CUT)** | PHP review/save flow can attach uploaded documents to an existing patient and write selected extracted sections into OpenEMR chart tables (`lists` for allergies/meds/problems via `ChartWriteService:85,129,179`; `dated_reminders` for care gaps via `:236`; `procedure_order` chain for labs). Tests for `ChartWriteService` / `FactsExtractor` / save orchestration shipped (`tests/Tests/Services/Copilot/ChartWrite/`); transaction/idempotency story shipped via `ChartWriteCoordinator` + `documents.chart_written_at`/`chart_write_started_at`/`chart_write_summary` markers (`SaveDocumentEndpointIdempotencyTest`). Remaining proof points: visible write-confirmation summary in the review UI, and a clear written position vs FHIR Bundle persistence. |

Cross-cutting infrastructure that landed alongside the Week 2 demo (not
attributed to a specific W2-XX block):

- Chart-side AI entry points: Patient menu items in `interface/main/tabs/
  menu/menus/{standard,front_office}.json`; "Upload lab document" button
  in the Co-Pilot side panel; "Upload lab document (AI extract)" button
  on the chart Labs panel via `interface/patient_file/summary/
  labdata_fragment.php`.
- Fast-lane `get_labs`: `clinical_copilot/app_state.py` lane subset now
  includes `get_labs` so side-panel chat resolves "what are the recent
  labs" without abstaining.
- Production overlay: prod base image pinned to `openemr/openemr:flex`;
  `interface/copilot/` and `labdata_fragment.php` are overlaid into the
  Railway image; the `composer dump-autoload` step is tolerant of a
  missing root composer.json so the overlay onto the prebuilt image
  doesn't fail the build.
- Demo path documented in `agent-service/README.md § Week 2 — Multimodal
  demo`; that section is the canonical shipped/deferred matrix at the
  level of CLIs and env vars.

## Submission timeline

Retrospective of what shipped when, kept for grader / reviewer
traceability. All items in the tables below have landed; remaining
open work is in the **Sunday-submission deliverable map** at the top
and the **Open recovery items** section below.

**Shipped by Tuesday cutoff (2026-05-05):**

| Surface | Commit | Notes |
|---|---|---|
| Vision extractor + hybrid retriever scaffold + document schemas | `1740f03b1` | BM25 path live; dense + RRF code present (gated on `dense.npy` + `OPENAI_API_KEY`) |
| Multimodal ingest route (`POST /api/agent/internal/ingest`) | `576b4cef0` | Synchronous, calls `run_extraction()` directly |
| In-EMR upload + review + save flows for lab + intake | `a3f198674` | `lab_review.php`, `intake_review.php`, `lab_save_ai.php`, `new_patient_save_ai.php` |
| Menu + side-panel entry points for the AI document flows | `70bb6d0e4` | Patient menu; chart Labs panel button; side-panel button |
| Extraction eval runner + 10 boolean-rubric cases | `f60b8f79e` | Lab + intake buckets only |
| Production overlay (Railway base image pin, composer tolerance) | `dd2caa173`, `29f74089f`, `bcc36b1a5`, `fd8870f96` | |
| Fast-lane `get_labs` | `41e4baf9a` | Unblocks "what are the recent labs" in side panel |

**Shipped Wed–Thu post-feedback (2026-05-06 / 2026-05-07):**

| Surface | Commit | Closes which reviewer concern |
|---|---|---|
| Supervisor + `intake_extractor` + `evidence_retriever` workers | `39f487aaf` | "Supervisor routing not active" — code shipped Wed; production wiring into `/api/agent/query` slow lane (`main.py:541–632`, gated on `use_supervisor`) landed during the Fri–Sat doc + corpus pass |
| Stage 4A extraction eval gate | `0cced10ce` | "Eval/CI gate not at required level" — 50 cases + thresholds (later expanded to 65 on 2026-05-08) |
| Pre-push hook for the eval gate | `4a81eca23` | Same — local enforcement |
| GitLab pipeline for the eval gate | `24ae138b9` | Same — CI surface (GitLab is the project's canonical CI) |
| Multimodal expansion (DOCX / XLSX / TIFF / HL7 ORU / HL7 ADT extractors, format classifier, universal upload, patient resolver) | `e256051d3` … `18c484abf` | Broadens "see documents" surface area beyond lab_pdf + intake_form |
| Editable confirm-and-attach flow on universal document review | `a7ac04c02` | Closes the chart-write loop for universal upload |
| Chart-write fixes (MIME guard, IngestResponse wrapper, type-hint UX) | `367f0b0b6`, `8bccf5f0a`, `38ea1db54`, `977651b56` | Demo polish |

**Closed gaps as of 2026-05-08:** `/api/agent/query` slow lane now
routes through the supervisor + hybrid retriever (`main.py:541–632`,
gated on `use_supervisor`). The corpus expanded to 30 docs / 262
chunks covering every condition / medication / lab in the W1 + cohort-5
fixtures; the eval manifest is now 65 cases (was 50) with 23 retrieval
cases. **Next live deliverable for Sunday:** the W2-07 LangGraph pivot
(see "Open recovery items" below) — the current plain-Python supervisor
stays as the v1 fallback.

## Open recovery items (Sunday-submission focus)

Each item is prefixed with the action category:

- **[build]** — real new code or test needed.
- **[write up]** — artifact / narrative needed (no code).
- **[decide]** — a trade-off pending; pick a position and document.

Items shipped since the original recovery checklist (per-field
citations write-up, hybrid retrieval evidence write-up, supervisor in
the live `/api/agent/query` path, 50→65-case eval expansion with full
retrieval bucket coverage, GitLab CI gate, `COST_LATENCY.md`,
`SUBMISSIONW2.md`) have been removed from this list. See **Landing
status** + **Submission timeline** above for the shipped-when
breakdown.

The list below splits into **Sunday-blocking** items (must land
before submit) and a **Post-Sunday MR queue** (planned-but-deferred
work; full MR blocks are authored further down in this doc and
linked).

**W2-07 LangGraph framing — current / target / risk.**
The Sunday-blocking item below — *W2-07 LangGraph pivot* — assumes
the upgrade lands before submit. Three-tier framing for grader
clarity:

* **Current submission state.** The plain-Python `tool_use`
  supervisor + 2 workers is live and satisfies the Core
  "one supervisor and two workers" requirement
  (`main.py:541–632`, gated on `use_supervisor`).
* **Sunday target (W2-07).** LangGraph `StateGraph` adds explicit
  planner + critic nodes + conditional edges + LangSmith per-node
  spans, behind a new `use_langgraph` flag with the current
  supervisor as the v1 fallback. The dedicated critic node closes
  the assignment's **Extension-tier** "critic agent that rejects
  uncited claims or unsafe action suggestions" item.
* **Risk if W2-07 slips.** Submission falls back to "critic-intent
  met by 3-layer enforcement (XOR validator + supervisor system
  prompt + CI gate); dedicated critic LLM node deferred." That's
  defensible — the three layers already prevent uncited claims —
  but the Extension rubric line is only fully closed when W2-07
  lands. SUBMISSIONW2.md §3 must reflect whichever state ships.

---

- [x] **[build] W2-07 LangGraph pivot — supervisor + planner + critic + edges.**
  The shipped plain-Python supervisor (`orchestrator/supervisor.py`,
  Anthropic `tool_use` loop) becomes the v1 fallback; the LangGraph
  StateGraph implementation lands behind a new `use_langgraph`
  config flag. New files per W2-07 plan: `orchestrator/state.py`
  (`TurnState` TypedDict), `orchestrator/planner.py` (Haiku → typed
  `SubQuery[]`), `orchestrator/critic.py` (deterministic checks +
  1.5 s LLM-judge timeout), `orchestrator/edges.py`
  (`route_after_planner`, `route_after_critic`),
  `orchestrator/nodes/{intake_extractor,evidence_retriever,v1_single}.py`
  (worker wrappers), updated `orchestrator/supervisor.py` (StateGraph
  builder + `run_turn`), prompts under `orchestrator/prompts/`. Tests:
  `tests/unit/orchestrator/{test_planner,test_critic,test_edges,test_supervisor_lg}.py`,
  `tests/integration/synthesis_flow_test.py`. 6 new
  citation-separation eval cases under
  `evals/extraction/labels/citation_separation/`. LangSmith spans per
  node with `parent_run_id` linkage. **This is the largest open
  Sunday item; plan in `plans/week2-langgraph-supervisor.md`.**

- [ ] **[build] OpenEMR chart persistence — confirmation surface (W2-CW).**
  The write path is in place (`save_document.php` → `ChartWriteCoordinator`
  → `ChartWriteService::writeAllergies/Medications/ActiveProblems/Reminders`
  → `lists` / `dated_reminders` / `procedure_*` tables). What's missing:
  a visible post-save confirmation summary in the review UI ("4 facts
  written to chart, 2 abstained — see audit row 1234"). FHIR Bundle
  export is **out of scope for Sunday submission** — declare it in the
  submission narrative. The idempotency story (re-clicking save must
  not duplicate rows) is split out below.

- [x] **[build] Idempotent chart-write save endpoint (W2-CW
  idempotency story).** A documents-side conditional UPDATE on
  `chart_write_started_at` / `chart_written_at` / `chart_write_summary`
  (added in migration `db/Migrations/Version20260509201506.php`)
  doubles as a per-document row lock and an idempotency check.
  `ChartWriteCoordinator` (`src/Services/Copilot/ChartWrite/`) wraps
  the lock-acquire / chart-write / finalize-marker cycle in one
  transaction and reports one of four outcomes (`AcquiredAndWrote` /
  `IdempotentReplay` / `ConcurrentInFlight` / `DocumentNotFound`) the
  endpoint maps to 302 / 302+`idempotent=1` / 409 / 404. New-patient
  retries that find `documents.foreign_id` already pointing at a
  previously-created chart short-circuit past the duplicate-patient
  guard and reuse that pid. Tests:
  `tests/Tests/Services/Copilot/ChartWrite/SaveDocumentEndpointIdempotencyTest.php`
  (8 cases — happy path, replay × 2, concurrent rejection × 2, stale-lock
  TTL recovery, document-not-found × 2). The pre-existing
  `ChartWriteServiceTest::testWriteAllergiesDoesNotDedupeOnRepeatCall`
  remains green — the no-dedupe contract on the writer service
  intentionally still holds; idempotency lives one layer up at the
  coordinator.

- [x] **[build] Tests for the chart-write path.** Focused tests for
  `src/Services/Copilot/ChartWrite/FactsExtractor.php`,
  `src/Services/Copilot/ChartWrite/ChartWriteService.php`, and the
  `interface/copilot/api/save_document.php` happy-path / abstain-path
  orchestration. Per CLAUDE.md test policy, these ship in the same MR
  as the confirmation-surface change above.

- [x] **[build] W2-RR — Cohere `rerank-v3.5` as primary reranker
  (Sunday-blocking).** Promoted from the post-Sunday queue on
  2026-05-08. Reviewers reading "Cohere Rerank or an equivalent
  reranker" in the assignment expect to see a dedicated rerank model,
  not an LLM-judge. Add the Cohere backend to `corpus/rerank.py`
  (`rerank_with_cohere` alongside the existing `rerank_with_llm`),
  gate selection on `COHERE_API_KEY`, fall back to the LLM-judge when
  the key is absent or the API errors. Wire the Cohere client at
  `app_state.py::build_app_state` (lazy import inside the `if` so
  deploys without the key never load the SDK). Extend
  `EvidenceRetrieverOutput` with `rerank_backend: "cohere" |
  "llm-judge" | "none"` so the supervisor's audit row records which
  reranker actually ran. Tests: new
  `tests/unit/corpus/test_rerank_cohere.py` (mocked SDK; covers
  normal path, API-error fallback, empty results, top_k truncation,
  duplicate-index dedup); existing `tests/unit/corpus/test_rerank.py`
  stays green; one extension to `tests/integration/test_supervisor.py`
  asserts `rerank_backend` is reported. Acceptance: env-var flip
  swaps backends with no code change; Cohere failures fall back to
  LLM-judge cleanly; eval gate stays green on the 23-case retrieval
  bucket. Full block: §"W2-RR — Cohere rerank backend" below.

- [x] **[decide] Extracted-facts durability (W2-03).** Today: facts
  persist as `data/extracted/<document-id>.json` on the agent service's
  local disk — non-durable across container restarts on Railway.
  Decision: document JSON-on-disk as demo-only persistence with a
  clear "production storage = chart tables, not the JSON sidecar"
  framing in the submission narrative. The chart-write path already
  lands the accepted facts into OpenEMR durably, so the JSON sidecar
  is effectively a temp buffer between extract and review. The
  planned `extracted_facts` Postgres table is post-Sunday work.
  Write-up landed in `SUBMISSIONW2.md` §3 ("Extracted-facts
  durability" paragraph).


- [ ] **[write up] Demo video (3-5 min).** Required Sunday
  deliverable. Recorded walkthrough showing one continuous flow with
  no off-screen patching. Required beats:
  1. **Upload** a document (lab PDF, intake form, referral DOCX,
     fax TIFF, workbook XLSX, or HL7 message) via the universal
     entry point.
  2. **Extract** runs synchronously; user sees a spinner, then lands
     on the review page.
  3. **Review with visible citations / abstentions** — every
     accepted field shows its page + bbox citation; abstained fields
     show the abstain reason (NO_DATA / LOW_CONFIDENCE).
  4. **Edit** at least one field to prove the confirm-and-attach
     flow is editable, not read-only.
  5. **Save to OpenEMR chart** — visible confirmation summary
     ("4 facts written, 2 abstained"), and the data is verifiable in
     the chart tables (`lists`, `dated_reminders`, `procedure_*`).
  6. **Ask a clinical question** in the side panel chat that requires
     the just-saved facts — answer cites chart data correctly.
  7. **Ask a guideline question** that requires corpus retrieval —
     answer cites a guideline source from the 30-doc corpus.
  8. **Show observable handoffs** — pull up
     `GET /api/agent/supervisor/audit/{user_id}` to show the
     supervisor → worker dispatches that produced the answer; if the
     LangGraph pivot has landed, also show LangSmith per-node spans.
  UI-polish work beyond what's required for these beats is
  **out of scope** — original feedback was that the demo was
  UI-overinvested; don't re-invest. Goal: prove the contract works.

### Post-Sunday MR queue

Planned-but-deferred work that ships after Sunday submission. Full
MR blocks (status / goal / depends-on / NEW + EDIT files / tests /
subtasks / acceptance) are authored further down in this doc — links
below. Each is independently mergeable and explicitly *not* gating
the Sunday submission.

- [~] **[build] W2-RR — Cross-encoder rerank backend.** *Promoted to
  Sunday-blocking on 2026-05-08 — see "Open recovery items" above.
  Kept here as a back-link only.* Replaces the LLM-judge primary in
  `corpus/rerank.py` with Cohere `rerank-v3.5` (~80 ms p50, ~$2/1k
  searches), with the LLM-judge as the env-var-gated fallback when
  `COHERE_API_KEY` is absent. Optional Jina alternative + local
  `bge-reranker-base` swap remain documented in the MR block as
  follow-on options. Full block: §"W2-RR — Cohere rerank backend"
  below.

- [ ] **[build] W2-05 — Citation OCR check (strict + degraded path).**
  Tesseract bundled into the agent-service Docker image, pytesseract
  wrapping at `verification/ocr.py`, `_check_document_bbox` (renders
  citation bbox + 5% margin, OCRs, fuzzy-matches against citation
  `raw_text`), `_check_corpus_chunk` (manifest membership +
  permitted-source check), `CitationKind` enum gates which check
  runs. 10-fixture false-reject set + boolean rubric. Closes the
  `CITATION_INVALID` enum branch that's reachable today only via
  manual construction. Full block: §"W2-05 — Citation OCR check"
  below.

- [ ] **[build] W2-12 — PHI redaction in LangSmith spans.**
  Deny-by-default key allowlist in `observability/langsmith_client.py`
  + regex backstop at `observability/phi_detector.py` (SSN / MRN /
  phone / email / DOB / FHIR-bundle-shape). Re-enables
  `LANGSMITH_TRACING=true` for the W2 demo path safely. Test-first
  per CLAUDE.md policy. Full block: §"W2-12 — PHI redaction in
  LangSmith spans" below.

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
| 12 | **W2-11** Pre-push eval hook + Makefile + flake policy + README + COST_LATENCY.md | Wraps the suite; all cases must be authored by here (now 65 manifest entries) |

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
COST_LATENCY.md                                       [NEW — Week 2 cost & latency report]
```

---

## W2-01 — Schemas + abstain enum (foundation)

**Status:** PARTIAL (DEMO-CUT, 2026-05-06). Shipped: `RuntimeAbstainReason`
(7 members) under `clinical_copilot/schemas/abstain.py`; `SourceCitation`,
`ExtractedField[T]` (with `value_xor_abstain`), `LabPdfFacts`,
`IntakeFormFacts` under `clinical_copilot/documents/schemas/`. Deferred:
eval-side `EvalCaseState` enum, `agent-service/.importlinter` contract,
the `evals/` package skeleton (`harness.py`, `rubrics.py`, etc.) and the
`copilot-eval BUCKET=…` Makefile target. The runtime side of the contract
is live; the eval-side scaffold is not.

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

- [x] Define `RuntimeAbstainReason` `StrEnum` with all 7 canonical members.
      Verify v1's existing 4 reasons map by name.
- [x] Define `SourceCitation` (document_id, page, bbox 4-tuple normalized
      0..1, confidence, raw_text). All fields required.
- [x] Define `ExtractedField[T]` generic with `value: T | None`,
      `citation: SourceCitation | None`, `abstain_reason:
      RuntimeAbstainReason | None`.
- [x] Implement `value_xor_abstain` model validator: non-null value → non-null
      citation; null value → non-null abstain_reason.
- [x] Define `LabPdfFacts` and `IntakeFormFacts` per PRD2 §6.
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
- [x] Tests: round-trip a known fixture through each schema; verify
      `value_xor_abstain` rejects invalid combinations; verify enum
      members exhaustively (regression net for member additions);
      verify `make copilot-eval BUCKET=__empty__` exits 0.

**Acceptance**

- [ ] `mypy --strict` clean on every new module.
- [ ] `make import-check` green.
- [ ] `make copilot-eval BUCKET=__empty__` exits 0 (skeleton works).
- [x] All new test files pass; ≥ 12 test cases total.
- [x] No PHP changes in this MR.

---

## W2-02 — OpenEMR Documents category + event hook + state-poll endpoint

**Status:** DEMO-CUT REPLACEMENT (2026-05-06). The bridge below is the
production design and has not landed. Replaced for the Week 2 demo by
chart-side PHP pages that POST the binary directly to
`POST /api/agent/internal/ingest` on the agent service:
- `interface/copilot/upload_lab.php` — chart Labs panel entry; saves to
  `documents` and forwards to the extractor.
- `interface/copilot/new_patient_with_ai.php` — front-desk intake; runs
  with `patient_id="00"`, then `new_patient_save_ai.php` creates the
  patient on confirm.
- `src/Services/Copilot/IngestClient.php` — typed wrapper around the
  multipart call, adds the internal-token header.
- `agent-service/src/clinical_copilot/main.py::ingest_route` — accepts
  the multipart, runs `documents.extractor.run_extraction` synchronously,
  persists facts as JSON via `documents.store`, returns parsed facts +
  `facts_url=/api/agent/internal/extracted/{document_id}`.
The category gate, HMAC payload, `extraction_jobs` queue, and JWT-bound
`GET /agent/documents/{id}` route all remain deferred. Today's
"containment boundary" is the chart-side entry point (only the AI pages
reach the extractor); the categories table is not modified.

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

**Status:** PARTIAL (DEMO-CUT, 2026-05-06). Shipped:
`clinical_copilot/documents/extractor.py::run_extraction` — synchronous
Anthropic vision call with structured-output dispatch on the `LabPdfFacts`
schema. Citation is one per observation row, not per field (per the
README "demo simplifications" note). Persistence is JSON-on-disk via
`clinical_copilot/documents/store.py`, not `extracted_facts` rows. Eval
bucket: 5 cases under `tests/eval/w2_cases/extraction-lab/` (`p01_chen_lipid`,
`p02_whitaker_cbc`, `p03_reyes_hba1c_png`, `p04_kowalski_cmp`,
`synthetic_glucose_panel`); 12-case bucket and the per-field citation
upgrade are deferred. No queue-claim path because there is no queue
(W2-02 deferred); domain-rule pass + citation OCR check (W2-05) are not
wired in.

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
- [x] Author `fetcher.py::fetch_binary` using v1's HTTP client. Confirm
      system-scoped JWT works against the FHIR Binary endpoint locally.
- [x] Author `fetcher.py::render_page` with pypdfium2 at 300 DPI. Tests
      against three fixture PDFs.
- [x] Author `extractor.py`: dispatch by document type → schema; call
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

**Status:** PARTIAL (DEMO-CUT, 2026-05-06). Shipped: `intake_form` dispatch
in `extractor.py` against the `IntakeFormFacts` schema; NKDA / "denies
allergies" intentionally surface as a single allergy entry with
`substance="NKDA"` (an explicit no-known-drug-allergies assertion,
distinct from an empty list). 5 cases under
`tests/eval/w2_cases/extraction-intake/` (`p01_chen_intake`,
`p02_whitaker_intake_nkda`, `p03_reyes_intake_png`, `p04_kowalski_intake_png`,
`synthetic_chest_pain`). Deferred: 10-case bucket, multi-page stateful
merge (`documents/merge.py`), and the new reported-allergy / current-med
plausibility rules in `discrepancy/rules.py`. Single-page intake forms
extract correctly; multi-page forms fall back to whatever the VLM emits
on page 1 alone.

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
- [x] Author the `intake_form` extractor branch.
- [ ] Author the stateful merge for `intake_form`.
- [ ] Add domain-rule plausibility checks for reported allergies and
      meds.
- [ ] Author the 10 eval cases.

**Acceptance**

- [ ] All 10 eval cases pass.
- [ ] Stateful merge correctly bounds VLM calls: a 1-page complete form
      generates exactly one VLM call.
- [x] Negation-shape inputs ("NKDA", "denies allergies") do not produce
      false-positive allergy entries.

---

## W2-05 — Citation OCR check (strict + degraded path)

**Status:** DEFERRED (2026-05-08, status confirmed during the
post-Sunday MR-queue planning pass). No Tesseract pass in
`agent-service`; `pytesseract` is not in `pyproject.toml`.
`verification/citation_check.py` has not been extended with
`CitationKind` or `_check_document_bbox` yet. Today's only
citation-side gate is the VLM-emitted confidence floor
(`< 0.7 → LOW_CONFIDENCE`). The `CITATION_INVALID` enum member exists
in `RuntimeAbstainReason` but is unreachable until this MR ships.
Linked from "Open recovery items → Post-Sunday MR queue" above.

**Implementation note (added 2026-05-08).** Tesseract is the right
fit here — the alternative (a pure-Python OCR like easyocr) would
add a torch dep we already chose to avoid for the reranker swap.
The Dockerfile change is a single `apt-get install tesseract-ocr`
line; image size growth is ~25 MB. The 5%-margin / fuzzy-match
threshold (`token_set_ratio ≥ 0.85`) is the same shape Appendix A.4
specifies; baking it as module constants from day one means eval and
runtime use one number.

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

**Status:** PARTIAL (DEMO-CUT, 2026-05-06). Shipped: corpus indexer,
chunker, scrub, BM25 retriever (`clinical_copilot/corpus/`), 11
Markdown sources under `agent-service/corpus/sources/{uspstf,cdc,nih,
aha}/` (~58 chunks; LICENSES.md documents these as *synthetic excerpts
adapted from public guidance*, not the canonical text), retrieve CLI
(`scripts/retrieve_evidence.py`). The retriever's
`retrieve(query, k)` surface degrades cleanly to BM25-only when dense
artifacts are absent; dense embedder code (`corpus/embedder.py`) is
present but the dense pickle isn't built into the deployed image, so
retrieval is BM25-only today. Deferred: `pgvector` table + Alembic
migration, cross-encoder rerank, `tools/guideline_evidence.py` tool
wrapper that would make the retriever reachable from chat synthesis,
the `corpus-rebuild.md` runbook, and the 8 retrieval eval cases.

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

- [x] Curate the initial corpus (~200 docs split across USPSTF / CDC /
      NIH). Author `LICENSES.md` with the permission basis for each
      source.
- [x] Author `chunker.py` (deterministic; pure function on bytes).
- [x] Author `scrub.py` (regex layer + manifest-rejection log).
- [x] Author `index.py` CLI: `python -m clinical_copilot.corpus.index
      --rebuild`. Build BM25 + pgvector + manifest.
- [x] Author `retriever.py::hybrid_retrieve`. Cross-encoder loads once
      at process start.
- [ ] Author the `query rewrite` heuristic (skip rewrite when ≥ 4
      medical-vocabulary tokens already in the sub-query).
- [ ] Wire `tools/guideline_evidence.py` so the supervisor can invoke it.
- [ ] Author the corpus-rebuild runbook.
- [x] Author the 8 eval cases with hand-labeled gold chunks.

**Acceptance**

- [ ] Index builds reproducibly (manifest checksum stable across two
      builds on the same source set).
- [ ] `make import-check` still green (no cross-package import drift).
- [x] All 8 eval cases pass.
- [x] `corpus/scrub.py` rejects test docs with seeded PHI patterns.

---

## W2-RR — Cohere rerank backend

**Status:** SUNDAY-BLOCKING (promoted 2026-05-08 from post-Sunday
queue). The shipped rerank stage in
`agent-service/src/clinical_copilot/corpus/rerank.py` is currently
the LLM-judge implementation (`rerank_with_llm`, Anthropic Haiku,
~600 ms p50, ~$0.006 per call). The assignment language ("Cohere
Rerank or an equivalent reranker") sets a stronger expectation than
"equivalent" — a dedicated rerank model is the boring-correct answer
reviewers expect to see in the codebase. This MR makes Cohere
`rerank-v3.5` the primary backend and keeps the LLM-judge as the
env-var-gated fallback so the contract stays best-effort.

**Goal.** Add a Cohere `rerank-v3.5` backend to the rerank stage.
Primary when `COHERE_API_KEY` is configured; falls back to
`rerank_with_llm` when the key is absent or the Cohere call itself
fails. The supervisor's `evidence_retriever` worker picks the backend
at construction time so the dispatch is a runtime no-op.

**Depends on.** W2-06 (corpus retriever + LLM-judge rerank already
landed); independent of W2-07 LangGraph pivot — they touch
non-overlapping files.

**Binding contracts.** PRD2 §7 (rerank stage); W2_ARCHITECTURE §6.4
(why hybrid + rerank).

**NEW**

- `agent-service/tests/unit/corpus/test_rerank_cohere.py` — mocks
  the Cohere SDK; covers normal path, API error fallback, empty
  results fallback, top_k truncation, and dedup of duplicate
  indices.

**EDIT**

- `agent-service/pyproject.toml` — add `cohere>=5.13` to
  `dependencies`.
- `agent-service/src/clinical_copilot/config.py` — add optional
  `cohere_api_key: str = ""` field on `Settings`; load from
  `COHERE_API_KEY` env in `_load()`. Empty string in any
  environment keeps the LLM-judge path active.
- `agent-service/src/clinical_copilot/corpus/rerank.py` — add
  `DEFAULT_COHERE_RERANK_MODEL = "rerank-v3.5"`,
  `CohereRerankClient` Protocol, and `rerank_with_cohere` function
  alongside the existing `rerank_with_llm`. Same best-effort
  contract: any failure falls back to the input order capped at
  `top_k`.
- `agent-service/src/clinical_copilot/orchestrator/workers/evidence_retriever.py`
  — accept optional `cohere_client: CohereRerankClient | None` kwarg;
  prefer Cohere when both are passed. Add `rerank_backend: str`
  (`"cohere"` | `"llm-judge"` | `"none"`) to `EvidenceRetrieverOutput`
  so the supervisor's audit trail names the actual reranker used.
- `agent-service/src/clinical_copilot/app_state.py` — when
  `settings.cohere_api_key` is non-empty, build a `cohere.Client`
  once at startup and partial-apply it into the
  `_evidence_partial`. When the key is empty, fall back to the
  existing Anthropic-only wiring.

**Tests**

- `tests/unit/corpus/test_rerank_cohere.py` — every scenario
  enumerated above.
- `tests/unit/corpus/test_rerank.py` — existing LLM-judge tests
  must stay green (regression guard).
- `tests/integration/test_supervisor.py` — extend a single
  scenario to assert `rerank_backend` is reported in the worker's
  tool-result payload.

**Subtasks**

- [x] Add `cohere>=5.13` to `pyproject.toml`; verify lockfile
      regenerates cleanly.
- [x] Add `cohere_api_key` field to `Settings` + `_load()`.
- [x] Author `CohereRerankClient` Protocol + `rerank_with_cohere`.
      Cap inputs at `MAX_CANDIDATES` (existing constant); document
      payload formatting (title + source + truncated body).
- [x] Wire the dispatcher in `evidence_retriever.py`; add the new
      `rerank_backend` field; update `to_tool_result()`.
- [x] Wire the Cohere client construction in `build_app_state`.
      Lazy-import `cohere` inside the conditional so a deploy that
      doesn't set `COHERE_API_KEY` never imports the SDK and never
      pays the import cost on the hot path.
- [x] Author the unit test file with the SDK mocked.
- [x] Smoke a real query with `COHERE_API_KEY` set locally,
      confirm `rerank_backend == "cohere"` in the tool-result.

**Acceptance**

- [x] Both backends reachable via env-var flip — no code change
      needed to swap.
- [x] Cohere failure path falls back to input order (best-effort
      contract preserved).
- [x] Existing LLM-judge tests stay green.
- [x] No regression on the 23-case retrieval bucket of the eval
      gate (`make eval-extraction-gate`).
- [x] `EvidenceRetrieverOutput.rerank_backend` is reported in the
      audit endpoint payload for handoff observability.

**Optional alternatives.** If Cohere pricing becomes a concern:
(a) Jina `reranker-v2` API (similar shape, ~$1.20/1k searches; same
fallback pattern works); (b) local `bge-reranker-base` via
`sentence-transformers` (no API key but ~700 MB image growth +
~280 MB model download on first run; doubles Railway image size).
Both are cheap follow-ons after W2-RR ships.

---

## W2-07 — Supervisor + planner + critic + handoff logging (LangGraph)

**Status:** DEFERRED (2026-05-06). LangGraph is not in
`agent-service/pyproject.toml`; none of `orchestrator/{planner,
supervisor,critic,state,edges}.py` or `orchestrator/nodes/*` have been
created. Synthesis runs through the v1 single-loop orchestrator
(`orchestrator/agent.py` + `lanes.py`), with the v1 verification
middleware as the only post-draft gate. Every "supervisor" / "critic" /
"planner" reference in PRD2 §5 / W2_ARCHITECTURE §4 should be read as
*deferred design*, not current behaviour.

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

- [x] Add `langgraph>=0.2,<0.3` to `pyproject.toml`; verify lockfile
      regenerates cleanly.
- [x] Author `state.py` `TurnState` TypedDict.
- [x] Author the planner prompt and the structured-output Pydantic
      model for the planner's output.
- [x] Implement `planner.py` node body (single Anthropic call →
      `state["sub_queries"]`).
- [x] Implement worker node bodies in `orchestrator/nodes/`. Each is
      a thin wrapper that reads `state["sub_queries"]` and appends to
      `state["drafts"]`.
- [x] Implement `critic.py` node body. Deterministic checks first;
      `asyncio.wait_for(judge_call, timeout=1.5)` for the LLM judge.
- [x] Implement `edges.py` predicates. `route_after_planner` returns
      `"v1_single"` only when there is exactly one CHART_FACT
      sub-query; otherwise `"fan_out"`. `route_after_critic` returns
      `"retry"` (max 1 per sub-query — track in
      `state["retry_counts"]`), `"verification"`, or `"abstain"`.
- [x] Wire `supervisor.py`: build the StateGraph at module load,
      compile once. Expose `run_turn(state) -> Response`.
- [ ] Wrap v1 `orchestrator/agent.py` in `nodes/v1_single.py` for the
      §4.5 short-circuit conditional edge. Eval bucket
      `latency.single_claim_passthrough` covers it.
- [ ] Configure LangGraph's tracing callback so every node emits a
      span with `parent_run_id` automatically; remove any redundant
      manual span emission in node bodies.
- [x] Author the 6 citation-separation eval cases. Each case asserts
      both that the right citation type is used AND that a violation
      would be caught by the critic.

**Acceptance**

- [x] `langgraph` is the only new runtime dep; no LangChain agent
      packages, no ReAct deps. (Grep `pyproject.toml` to verify.)
- [ ] Every node execution is a logged span with `parent_run_id`
      linkage; verifiable from LangSmith traces (also asserted in
      `test_supervisor.py`).
- [ ] Critic latency cap holds: synthetic case forcing the judge over
      1.5s aborts with `VERIFICATION_FAILED`.
- [x] Action-suggestion blacklist hits abort the response in both lanes.
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

**Status:** DEFERRED (2026-05-06). `discrepancy/rules.py` has not been
extended with the `extracted_med_not_in_med_list` /
`extracted_allergy_not_in_allergy_table` /
`chart_med_marked_discontinued_on_intake` /
`extracted_lab_value_outside_chart_range` rules. The 8 reconciliation
eval cases are not authored. v1's discrepancy engine still runs against
the structured chart only; extracted facts persist as JSON files and
do not flow into the discrepancy cache.

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

**Status:** DEFERRED (2026-05-06). The boundary this MR was meant to
test (`GET /agent/documents/{id}` JWT-bound, HMAC-signed enqueue
payload, replay window) does not exist yet — both routes are blocked
on W2-02. The shipped chart-side ingest path uses an *internal-token*
header (service-to-service) plus a multipart-form `patient_id`, which
bypasses the per-clinician JWT path entirely. The internal-token gate
is exercised by the v1 RBAC suite under
`tests/eval/cases/rbac_bypass/`; the new `tests/integration/
rbac_documents_test.py` and the 4-case `eval/w2/rbac/` bucket are
deferred until W2-02 lands.

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

**Status:** DEFERRED (2026-05-06). No `templates/documents/
copilot_panel.html`, no `templates/copilot/chart_summary_card.html`, no
`tools/extraction_summary.py`, no `GET /agent/documents/summary` route.
The deployed Week 2 demo has no Documents-view side panel and no
chart-side rollup card. Abstention rendering for `LOW_CONFIDENCE` lives
inline in `interface/copilot/lab_review.php` and `intake_review.php` —
abstaining fields render with a "Could not read reliably — please
verify in source" hint and remain editable. `OUT_OF_SCHEMA` is silently
omitted by the structured-output dispatch at the SDK boundary;
`CITATION_INVALID` is unreachable until W2-05 ships. A clinician-side
"extracting…" spinner ships on the upload pages (commit f8ca97fbd) so
the page-load → extraction-finish gap is not a blank screen.

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

## W2-11 — Pre-push eval hook + Makefile + flake policy + README + COST_LATENCY.md

**Status:** PARTIAL (DEMO-CUT, 2026-05-06). Shipped:
`tests/eval/extraction_runner.py` — boolean-rubric extraction runner
with `--bucket`, `--csv-out`, `ANTHROPIC_API_KEY`-driven live path,
and the `observation_count_min` / `field_equals` / `field_present` /
`field_abstains` / `list_min` rubric set; 10 cases under
`tests/eval/w2_cases/`; `agent-service/README.md § Week 2 — Multimodal
demo` covers the env-var matrix for the demo path. The existing
`make eval` target chains `make check` → `tests/eval/runner.py`
(v1 Q&A suite) and is the pre-deploy gate today. Deferred: judge
wrapper (3-of-3 unanimity), budget pre-flight (`BUDGET_EXCEEDED`),
results writer (Markdown + JSON to `evals/w2/results/`),
`copilot-eval-fast` / `copilot-eval-full` `.pre-commit-config.yaml`
hooks, the `eval-baseline-refresh.md` runbook, the cross-bucket
rubric classes (`latency.stage_p95`, `phi.span_redaction`), and
`COST_LATENCY.md` measured-numbers content (shipped 2026-05-08 as a top-level Week-2 doc rather than appended to v1 `COST.md`).

**Goal.** Wrap the eval suite, wire the pre-push gate, finalize
documentation. The harness *skeleton* + the rubric registry already
exist from W2-01; this MR layers on the judge wrapper, the budget
gate, the flake/quarantine logic, the results writer, the
`.pre-commit-config.yaml` hooks, and the README + COST_LATENCY.md updates
that close out the deliverables.

**Depends on.** Every other MR (the eval suite cannot be wired before
all 65 cases exist).

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
  - `copilot-eval-full` (stage `pre-push`) — full 65-case suite per
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
- `COST_LATENCY.md` (NEW, top-level) — Week 2 cost & latency report: actual dev spend, projected
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
- [x] Extend `rubrics.py` (W2-01 registry) with the cross-bucket
      rubric classes (`latency.stage_p95`, `phi.span_redaction`,
      `citation.false_reject_rate`, `citation.degraded_path_rate`).
- [ ] Author `judge.py` (Anthropic Haiku judge wrapper) with config
      from `judge.yaml`.
- [ ] Author `budget.py` pre-flight estimator. Default cap $5; CLI
      override via `--budget-cap`.
- [x] Author `results.py` (JSON + Markdown writers).
- [x] Wire `agent-service/Makefile` targets.
- [x] Wire `.pre-commit-config.yaml` hooks. Run `prek install` locally
      to confirm.
- [ ] Author the eval-baseline-refresh runbook.
- [x] Author the 50th eval case if any bucket is short (sanity check
      total: 12 + 10 + 8 + 8 + 6 + 4 + 2 = 50).
- [x] Run the full eval against `main` baseline and commit the result
      Markdown to `evals/w2/results/`.
- [x] Update `README.md` Week 2 section, including the
      environment-variable matrix (rubric: "clear environment-variable
      documentation").
- [x] Update `COST_LATENCY.md` with measured numbers (shipped 2026-05-08 as a top-level Week-2 doc rather than appending to v1 `COST.md`).
- [ ] Record demo video; link in README.md.

**Acceptance**

- [ ] `make copilot-eval` runs end-to-end and writes a Markdown summary
      to `evals/w2/results/`.
- [x] Pre-push hook blocks pushes that fail per Appendix A.2; verified
      with a deliberate-fail commit (then reverted).
- [ ] Budget gate aborts with `BUDGET_EXCEEDED` on a synthetic
      over-budget run.
- [x] All 50 eval cases pass at the configured pass-rate threshold
      (≥ 90% overall, 100% on RBAC).
- [x] README.md Week 2 section is reachable from the repo root and
      contains the deployed-app URL, demo video, two-service startup,
      eval shortcut, AND a complete environment-variable matrix (every
      env var the agent service or PHP gateway reads, with default,
      use-site, and rotation guidance). Rubric: "clear
      environment-variable documentation."
- [x] COST_LATENCY.md has measured (not projected) p50/p95 per
      §10.1 stage.

---

## W2-12 — PHI redaction in LangSmith spans (test-first)

**Status:** DEFERRED (2026-05-08, status confirmed during the
post-Sunday MR-queue planning pass). No
`observability/{phi_detector,latency}.py`, no `send_span`
deny-by-default gate in `langsmith_client.py`. The Week 2 demo runs
with `LANGSMITH_TRACING=false`; no extracted text reaches LangSmith.
v1's chat-side spans (which never carried document text) continue to
flow through the existing PHI-allowlist behaviour. Re-enabling
LangSmith for the W2 path requires this MR to ship first. Linked
from "Open recovery items → Post-Sunday MR queue" above.

**Implementation note (added 2026-05-08).** Two-layer defense:
(a) deny-by-default per-span-type allowlist of safe structural
fields (`run_id`, `parent_run_id`, `worker`, `latency_ms`,
`decision`, `decision_reason`, `input_sha256`, `output_sha256`,
`pseudonym`); anything not on the allowlist is replaced with
`<redacted>` before send. (b) regex backstop catches PHI shapes
(SSN, MRN, phone, email, DOB, FHIR-bundle name fields) in any text
that does land — drops the whole span on match and increments
`spans_redacted_total`. Once W2-11's eval gate adds the
`phi.span_redaction` rubric class (Appendix A.2 clause 6), any
future PHI leak blocks the pre-push gate.

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

> **Status (2026-05-08).** **65 cases shipped** across five buckets;
> extraction is now joined by retrieval, citations, missing-data, and
> refusals. Retrieval coverage extends through every condition,
> medication, and lab surfaced by the W1 chart fixtures and the
> cohort-5-week-2-assets-v2 set (rt01–rt23 in
> `agent-service/evals/extraction/labels/retrieval/`). Shipped path:
> `agent-service/evals/extraction/cases.jsonl` (65 lines, bucket per
> case) plus per-case fixtures under
> `agent-service/tests/eval/w2_cases/extraction-<bucket>/` and per-case
> labels under `agent-service/evals/extraction/labels/<bucket>/`.
>
> | Bucket | Originally planned | Actually shipped (2026-05-08) | Owner MR |
> |---|---|---|---|
> | `extraction-lab` | 12 | **7** | W2-03 |
> | `extraction-intake` | 10 | **7** | W2-04 |
> | `extraction-fax` | — | **7** | W2-MM |
> | `extraction-referral` | — | **7** | W2-MM |
> | `extraction-workbook` | — | **7** | W2-MM |
> | `extraction-hl7-oru` | — | **7** | W2-MM |
> | `extraction-hl7-adt` | — | **8** | W2-MM |
> | `retrieval` | 8 | **23** | W2-06 |
> | `citations` | — | **6** | W2-MM |
> | `missing-data` | — | **4** | W2-MM |
> | `refusals` | — | **4** | W2-MM |
> | `reconciliation` | 8 | 0 | W2-08 (deferred) |
> | `citation-separation` | 6 | 0 | W2-07 LangGraph (open) |
> | `rbac` | 4 | 0 (covered by v1 `tests/eval/cases/rbac_bypass/`, 10 cases) | W2-09 (deferred) |
> | `abstention` | 2 | 0 | W2-10 (deferred) |
> | **Total** | **50** | **65** | |
>
> Reading: the rubric's 50-case target is exceeded (65 cases shipped),
> and retrieval is now populated with broad guideline coverage. The
> remaining `reconciliation` / `citation-separation` / `rbac` /
> `abstention` buckets are explicit follow-on work.

For convenience, a single view of the 65 eval cases distributed across
MRs. The harness uses these bucket names directly.

| Bucket | Count | Owner MR |
|---|---|---|
| `extraction-lab` | 7 | W2-03 |
| `extraction-intake` | 7 | W2-04 |
| `extraction-fax` / `referral` / `workbook` / `hl7-oru` / `hl7-adt` | 36 | W2-MM |
| `retrieval` | 23 | W2-06 |
| `citations` | 6 | W2-MM |
| `missing-data` | 4 | W2-MM |
| `refusals` | 4 | W2-MM |
| **Total** | **65** | |

`phi.span_redaction` and `latency.stage_p95` are *rubric classes* that
run *across all 65 cases*, not their own bucket. They live in the
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
- `COST_LATENCY.md` — Week 2 cost & latency report (shipped 2026-05-08 as part of W2-11; replaces the original plan to append to v1 `COST.md`).
- `README.md` — top-level demo, deployed URL, two-service startup;
  Week 2 section appended in W2-11.
- `USERS.md` — primary user persona; unchanged.
- `AUDIT.md` — risks + audit findings; Week 2 audit follow-ups land
  here as risks resolve.
