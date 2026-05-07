# Multimodal upload intake + suggested patient routing — Thursday early-submission plan

## Context

You're shipping a Week 2 early-submission Thursday evening (~30h from now, today is 2026-05-06 Wed). You want to expand the Co-Pilot upload intake from 2 doc types (lab PDF, intake form) to 6 by adding the cohort-5 assets:
- `docx` referral letters
- `hl7v2` ADT-A08 (demographics) and ORU-R01 (lab results)
- `tiff` fax packets (multi-page scans)
- `xlsx` patient workbooks

The supervisor agent should **suggest** which patient each upload belongs to (existing match vs new patient) and surface that on a review page. Existing chart-side PHP review pages still mediate every DB write (your "Keep clinician-confirm" choice). No autonomous writes.

This is aggressive scope for the window. The plan below sequences work by **smallest-blast-radius-first** so if you run out of time at any point, what's already shipped is clean and demoable. Cut order is documented at the bottom.

---

## Architecture: what's added vs what stays

**Stays (do not touch):**
- `agent-service/src/clinical_copilot/orchestrator/supervisor.py` — keep single-loop v1, just register new tools
- `interface/copilot/lab_review.php`, `intake_review.php`, `lab_save_ai.php`, `new_patient_save_ai.php` — these are the write-mediation pages, reuse them
- `documents/schemas/{lab_pdf,intake_form,citation}.py` — existing schemas unchanged
- `tests/eval/extraction_runner.py` — extend, don't rewrite

**Added:**
1. **Universal upload entrypoint** `interface/copilot/upload_document.php` — accepts any of the 6 file types, classifies via MIME + sniff, routes to the right extractor
2. **Per-type schemas** under `documents/schemas/` — `referral_docx.py`, `hl7_adt.py`, `hl7_oru.py`, `fax_tiff.py`, `workbook_xlsx.py`. All reuse `ExtractedField[T]` + `SourceCitation`.
3. **Per-type extractors** in `documents/extractors/` (refactor `extractor.py` into a package). Routing inside `agent-service/.../main.py:491` (`/internal/ingest`) keys off `document_type`.
4. **Patient resolution worker** `agent-service/.../orchestrator/workers/patient_resolver.py` — wraps a new PHP endpoint that scores patients by name+DOB+MRN.
5. **PHP patient-match endpoint** `interface/copilot/api/patient_match.php` — wraps `PatientService::search()`, returns ranked candidates.
6. **Document review router** `interface/copilot/document_review.php` — single review surface that shows extracted facts + best-match patient suggestion + `[Confirm match] [Pick different] [Create new]` actions, then forwards to the appropriate existing save page.
7. **Supervisor tool registration** — one new dispatcher per type (`dispatch_*_extractor`) plus `dispatch_patient_resolver`. Same handoff dataclass pattern.

**Not added (explicit non-goals for Thursday):**
- LangGraph migration (W2-07) — single-loop continues
- ADT-driven autonomous demographics merge — clinician confirms every field change on review page
- ORU-driven autonomous lab post — extracted observations land in the existing lab review queue, clinician confirms before `procedure_result` write
- Reconciliation (W2-08), full RBAC scoping (W2-09), abstention side panel (W2-10), PHI redaction (W2-12) remain deferred

---

## Sequenced work (ship-on-each-step ordering)

Each step ends with a green pre-push hook and is independently demoable. Stop anywhere and what's behind you is shippable.

### Step 0 — Eval scaffolding (1h, do first)
Extend `tests/eval/extraction_runner.py` to accept new `document_type` values and add per-type case loaders. Fail closed: unknown types return `UNSUPPORTED_TYPE` abstain. **Why first:** every new extractor lands behind the gate.

### Step 1 — Universal upload UI + router (2h)
- Add `interface/copilot/upload_document.php` with `<input type="file" accept=".pdf,.tiff,.png,.jpg,.docx,.xlsx,.hl7,.txt">`
- Server-side classifier: MIME sniff + extension + first-bytes (HL7 starts with `MSH|`, xlsx is zip with `xl/`, docx is zip with `word/`)
- Route to existing `/api/agent/internal/ingest` with `document_type` set
- Keep `upload_lab.php` and `upload_intake.php` working (don't break)

### Step 2 — TIFF fax packet (3h, lowest new risk)
TIFF is already in the agent-service accepted MIME list. Work needed:
- Verify multi-page TIFF rendering in `documents/fetcher.py` (pypdfium2 doesn't handle TIFF — use Pillow page iteration, render each page to JPEG)
- New schema `fax_tiff.py` modeling cover-page metadata + N body pages classified loosely (referral / lab / intake / unknown)
- Extractor calls Claude vision per page with a coarse classifier prompt; per-page facts attached with page-index citation
- Demo cases: 7 cohort-5 TIFFs → 7 eval cases
- Review page: surfaces page-by-page classification, lets clinician pick which pages to attach to chart

### Step 3 — DOCX referral (3h, no VLM)
- `python-docx` extracts paragraphs with paragraph-index as citation anchor
- Schema `referral_docx.py`: referring_provider, reason_for_referral, history_summary, requested_actions
- Pure text extraction, deterministic — eval cases run cheap and reliably
- 7 cohort-5 docx → 7 eval cases
- Review page: facts + patient match suggestion → confirm → attaches doc + writes referral note via existing `Documents` insert

### Step 4 — Patient resolver worker + PHP endpoint (3h)
- `interface/copilot/api/patient_match.php`: POST extracted demographics, returns top-3 candidates from `PatientService::search()` with match scores (exact-DOB + last-name = high; fuzzy first-name = medium; MRN match = override)
- `orchestrator/workers/patient_resolver.py`: new dispatcher tool the supervisor can call
- `document_review.php` calls this when extractor returns demographic fields
- Score thresholds: ≥0.9 → "Confirm match" preselected; 0.6–0.9 → "Review candidates"; <0.6 → "Create new"
- **Critical:** never auto-confirm. Clinician click is mandatory.

### Step 5 — XLSX workbook (3h, no VLM)
- `openpyxl` + cell-coordinate citations (e.g., `Sheet1!B7`)
- Schema `workbook_xlsx.py`: structured sections (Demographics, Vitals, Labs, Meds) keyed off canonical cohort-5 layout
- Eval cases: 7 cohort-5 xlsx → 7 cases
- Review page: section-by-section confirm; labs route to existing `lab_review.php` flow

### Step 6 — HL7 ORU-R01 (4h)
- `aranyasen/hl7` PHP library is already in vendor (per OpenEMR exploration). Use it for parsing.
- New schema `hl7_oru.py` modeling OBR + OBX segments → maps to same `LabObservation` shape used by lab_pdf
- Extractor in agent-service parses HL7 text → emits `LabPdfFacts`-shape facts so existing `lab_review.php` handles it unchanged
- Citations: segment+field index (e.g., `OBX|3|...`)
- 7 cohort-5 ORU files → 7 eval cases
- **Do NOT call OpenEMR's `receive_hl7_results.inc.php` directly** — that bypasses the review step. Extract → review → existing save page writes.

### Step 7 — HL7 ADT-A08 demographics (4h, save for last)
- Same parser, schema `hl7_adt.py` modeling PID/PD1 segments
- Extractor returns demographics in the same shape as `intake_form` `IntakeFormFacts.demographics`
- Routes through patient resolver: high match → demographics-update review page (same shape as intake review); low match → create-new-patient
- 7 cohort-5 ADT files → 7 eval cases

### Step 8 — Eval gate top-up + demo + cost report (3h)
- Aim for ~50 cases total (10 existing + 35 new across 5 types). If short, document the gap and ship.
- Demo video script: upload 1 of each type, show extraction → patient suggestion → confirm → chart updated
- Cost/latency report: instrument new tool dispatches; report p50/p95 per doc type

---

## Critical files to modify

**Python (agent-service):**
- `agent-service/src/clinical_copilot/main.py:491` — extend `/internal/ingest` routing
- `agent-service/src/clinical_copilot/documents/extractor.py` → refactor to `documents/extractors/` package
- `agent-service/src/clinical_copilot/documents/schemas/` — 5 new files
- `agent-service/src/clinical_copilot/documents/fetcher.py` — TIFF multi-page support
- `agent-service/src/clinical_copilot/orchestrator/supervisor.py` — register new tools
- `agent-service/src/clinical_copilot/orchestrator/workers/` — `patient_resolver.py` (new); per-type extractors stay inside `documents/`
- `tests/eval/extraction_runner.py` — case loaders for new types

**PHP (OpenEMR):**
- `interface/copilot/upload_document.php` (new)
- `interface/copilot/document_review.php` (new — generic review surface)
- `interface/copilot/api/patient_match.php` (new)
- `src/Services/Copilot/IngestClient.php` — accept new doc types
- `src/Services/Copilot/PatientMatchService.php` (new — wraps `PatientService::search()`)

**Docs:**
- `W2_ARCHITECTURE.md` — add §"Multimodal extension" with the 5 new extractors + patient resolver worker
- `TASKS2.md` — add W2-13 through W2-18, update status block
- `PRD2.md` §12 non-goals — strike "more than two doc types"

## Existing utilities to reuse (do not rebuild)

- `ExtractedField[T]`, `SourceCitation` in `documents/schemas/citation.py`
- `PatientService::search()` in `src/Services/PatientService.php` (line 418)
- `DocumentService::insertAtPath()` in `src/Services/DocumentService.php` (line 127)
- `Document::createDocument()` for chart-side attachment
- `lab_save_ai.php`, `new_patient_save_ai.php` — existing write-back pages, route to them, don't replace
- `aranyasen/hl7` PHP library — already in vendor for HL7 parsing
- `python-docx`, `openpyxl`, `pillow`, `hl7` Python libs — add to `agent-service/pyproject.toml`

---

## Cut order if time runs short (drop from the bottom)

If clock pressure mounts, drop in this order:
1. **First to cut:** Step 7 (HL7 ADT-A08) — most novel work, narrowest demo flair
2. **Second:** Step 6 (HL7 ORU-R01) — the existing OpenEMR ingester already handles ORU; stretch to phase 2
3. **Third:** Step 5 (XLSX) — workbook is somewhat artificial demo data
4. **Fourth:** Step 4 (patient resolver) — fall back to "patient_id passed by URL" like today
5. **Hold:** Steps 0–3 (eval scaffolding + universal upload + TIFF + DOCX) are the minimum to claim "multimodal" credibly

Anything Step 4 and below stays clinician-confirmed regardless of cuts.

---

## Verification

**Per-step (run after each step lands):**
- `cd agent-service && pytest -x` — unit + integration green
- `composer phpunit-isolated` — host-side PHP isolated tests
- `python tests/eval/extraction_runner.py --type <new-type>` — new type's eval cases pass
- `make copilot-eval` (or pre-push hook equivalent) — full gate green, no rubric regression

**End-to-end smoke (each new doc type):**
- `curl -F file=@cohort-5-week-2-assets-v2/<type>/p01-chen-*.* -F document_type=<type> /api/agent/internal/ingest` → 200 + facts + citations
- Open `upload_document.php`, drop one cohort-5 file of each type, observe → review page → confirm → chart shows attached doc / updated demographics / lab in queue

**Demo video checklist:**
- Upload one of each of the 6 types (lab PDF + intake form + 4 new)
- Show patient-match suggestion on review page
- Confirm new patient flow on at least one (e.g., ADT for an unknown patient)
- Open patient chart, show all attached docs
- Run `make copilot-eval`, show gate passing

---

## Risks (own these explicitly in W2_ARCHITECTURE.md)

- **TIFF rendering edge cases:** old fax scans may have skewed pages, color profiles. Pillow handles most; document fallback as "manual review" abstain.
- **HL7 segment heterogeneity:** cohort-5 files may not exercise every segment variant. Eval cases must include at least one off-spec example per type.
- **Patient match false positives:** name+DOB collisions are real (twins, common names). Score threshold 0.9 is conservative; clinician click still required.
- **Eval gate slip:** if total cases < 50 by Thursday, document delta and propose Phase 2 top-up. Don't ship a fake 50-case suite.
- **Time risk:** plan totals ~26h plus buffer; real wall-clock will be tighter. Steps 4–7 are most likely to slip — accept the cut order.
