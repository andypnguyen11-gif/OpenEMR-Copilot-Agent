# Stage 4A - Extraction Eval Core

## Summary

Build the extraction eval system first. This is the hard, load-bearing work: 50 human-reviewed cases, cached predictions, boolean rubrics, category thresholds, regression detection, and PHI-log checks. It must run locally without PHP, FastAPI, OpenEMR, Postgres, `AGENT_BASE_URL`, or network access.

The output of this phase is a working command:

```bash
cd agent-service
make eval-extraction-gate
```

That command is the source of truth. Hook wiring comes later and should only call this command.

## Current Repo State

- Existing document extraction code lives in `agent-service/src/clinical_copilot/documents/`.
- Existing schemas are already the right library contract:
  - `documents/schemas/citation.py`
  - `documents/schemas/lab_pdf.py`
  - `documents/schemas/intake_form.py`
- Existing live extractor is `documents/extractor.py::extract`, but Anthropic calling is embedded directly and not cleanly replayable.
- Existing extraction eval runner is `agent-service/tests/eval/extraction_runner.py`; it has only 10 live cases and simple field/list assertions.
- Existing deployed-agent eval runner targets `/api/agent/query`; keep it separate.

## Implementation

### Replayable Extraction Boundary

Keep `clinical_copilot.documents` as the lightweight library boundary.

Add an injectable backend layer:

- `clinical_copilot.documents.backends.ExtractionBackend`
  - Protocol that accepts rendered page payloads, `document_type`, and tool metadata, then returns raw page-level tool JSON.
- `AnthropicVisionBackend`
  - Moves the current Anthropic Messages API call logic out of `_call_vlm`.
  - Used by existing CLI/FastAPI ingest paths.
- `ReplayVisionBackend`
  - Loads cached raw tool outputs from eval prediction files.
  - Used by deterministic eval.

Refactor `documents/extractor.py` so the reusable flow is:

1. Render document.
2. Ask backend for raw page-level tool outputs.
3. Validate raw outputs against the existing raw Pydantic models.
4. Convert to `LabPdfFacts` or `IntakeFormFacts`.

Keep a compatibility wrapper named `extract(...)` that constructs `AnthropicVisionBackend`, so existing call sites keep working.

### Eval Package

Create `agent-service/src/clinical_copilot/evals/extraction/`:

- `cases.py` - typed case manifest models and exact-50 loading.
- `labels.py` - human-reviewed label schema.
- `predictions.py` - cached raw model-output load/write helpers.
- `rubrics.py` - boolean rubric evaluators.
- `runner.py` - replay gate, live smoke, full live refresh, and baseline commands.
- `results.py` - JSON and Markdown summaries.
- `phi.py` - PHI-shape scanning for captured logs/results.

Create eval data under `agent-service/evals/extraction/`:

- `cases.jsonl`
- `labels/`
- `predictions/`
- `baseline.json`
- `results/`

Generated result contents should be gitignored unless a deliberate summary is committed later. Do not commit raw live logs containing document text.

### Case Manifest

Each case in `cases.jsonl` includes:

- `case_id`
- `bucket`
- `document_type`: `lab_pdf`, `intake_form`, `retrieval`, or `refusal`
- `document_path` when document-backed
- `query` when retrieval/refusal-backed
- `label_path`
- `prediction_path`
- `rubric_categories`
- `live_smoke`
- `description`

The runner fails if:

- Loaded case count is not exactly 50.
- Any `case_id` is duplicated.
- Any selected-mode artifact is missing.
- Any label is not human-reviewed.

### Label Schema

Use required-field labels instead of brittle full snapshots.

Support:

- `required_fields`
  - Field paths such as `observations[].display.value`, `chief_complaint.value`, `reported_allergies[].substance.value`.
  - Case-insensitive normalized string exact match by default.
  - Numeric absolute tolerance.
  - Any-order list row matching by declared key fields.
- `must_abstain`
  - Field paths and required reasons such as `LOW_CONFIDENCE`, `NO_DATA`, or `CITATION_INVALID`.
- `required_citations`
  - Required citation presence by field path.
  - Validate `document_id`, `page >= 1`, nondegenerate bbox in `[0,1]`, confidence in `[0,1]`, and non-empty `raw_text`.
- `expected_retrieval`
  - Gold `source_doc_id` or `chunk_id` in top-k.
  - Required `source`, `source_url`, and chunk citation fields.
- `safe_refusal`
  - Expected refusal/abstention reason and forbidden fact patterns.
- `metadata`
  - `review_status`
  - `reviewed_by`
  - `reviewed_at`
  - `source_notes`

Do not build a review UI in this phase. JSON labels plus clear validation errors are enough.

### Boolean Rubrics

Implement these category names exactly:

- `schema_valid`
  - Facts validate against `LabPdfFacts` or `IntakeFormFacts`; retrieval/refusal outputs validate against eval-local models.
- `citation_present`
  - Every label-required fact or retrieval hit has a valid citation.
- `factually_consistent`
  - Required facts match labels within tolerance; retrieval returns gold source/chunk within top-k.
- `safe_refusal`
  - Missing-data and unsupported cases abstain/refuse and do not emit forbidden fabricated facts.
- `no_phi_in_logs`
  - Captured eval logs, result summaries, and span-like records contain no PHI sentinels or PHI-shaped values.

Each rubric returns a boolean plus a concise failure reason. No scalar ratings.

### 50-Case Inventory

Author exactly 50 synthetic/demo cases:

- 14 lab extraction cases:
  - Existing examples plus synthetic variants.
  - Cover clean PDFs, PNG scans, rotated/low-quality images, missing reference ranges, missing flags, low-confidence values, and date parsing edge cases.
- 14 intake extraction cases:
  - Existing examples plus synthetic variants.
  - Cover NKDA, explicit allergies, meds, active problems, family history, missing demographics, phone/email, and ambiguous/low-confidence fields.
- 8 evidence retrieval cases:
  - Query the committed guideline corpus.
  - Label expected source doc/chunk in top 5.
  - Include no-match query behavior.
- 6 citation cases:
  - Exercise document citation presence and retrieval citation presence.
  - Include invalid bbox, empty raw text, wrong document id, and missing citation via cached bad predictions.
- 4 missing-data cases:
  - Documents/forms omit required data and should produce `NO_DATA` or absent optional fields.
- 4 safe-refusal cases:
  - Prompt/prediction shapes try to infer unsupported values.
  - Assert refusal/abstention and forbidden fabricated patterns.

Reuse `agent-service/tests/fixtures/build_pdfs.py` where possible. New fixtures should be synthetic and committed under the eval fixture tree.

### Gate Rules

Create `baseline.json` with accepted pass rates by category.

Default thresholds:

- `schema_valid`: 100%
- `no_phi_in_logs`: 100%
- `safe_refusal`: 100%
- `citation_present`: 95%
- `factually_consistent`: 90%

The gate fails if any category:

- Drops below its threshold.
- Regresses by more than 5.0 percentage points versus `baseline.json`.

The gate also fails if:

- Case count is not exactly 50.
- Labels are unreviewed.
- Baseline is missing unless `--write-baseline` is explicitly passed.
- Any required artifact is missing.
- PHI appears in logs/results.

### Make Targets

Add these targets to `agent-service/Makefile`:

- `eval-extraction-gate`
  - Runs replay mode.
  - This is the eventual PR-blocking command.
- `eval-extraction-smoke`
  - Runs only cases marked `live_smoke=true`.
  - Requires `ANTHROPIC_API_KEY`; skip with a clear message when not configured unless `REQUIRE_LIVE=1`.
- `eval-extraction-live`
  - Runs full live extraction/retrieval and writes candidate prediction files.
  - Never updates human labels automatically.
- `eval-labels-validate`
  - Validates manifests and human-reviewed labels.

Keep existing `make eval` untouched for deployed-agent evals.

## Tests

Add unit tests under `agent-service/tests/unit/evals/extraction/`:

- Case manifest loader:
  - Exact 50 count enforcement.
  - Duplicate id rejection.
  - Missing artifact rejection.
- Label schema:
  - Unreviewed label fails.
  - Required field paths validate.
  - Numeric/string tolerance behavior.
  - Any-order list matching.
- Rubrics:
  - One targeted passing fixture per category.
  - One targeted failing fixture per category.
  - Failure reasons include case id, rubric category, and field path.
- Baseline gate:
  - Below threshold fails.
  - Regression over 5 percentage points fails.
  - Regression at exactly 5 percentage points passes.
  - Hard 100% categories fail on any failure.
- Replay backend:
  - Cached raw tool outputs replay through existing conversion code.
  - Bad raw output fails `schema_valid`.
- PHI scan:
  - Sentinels are caught.
  - Redacted summaries pass.

Add an offline integration-style test:

- Run replay gate over a small temporary case set.
- Assert result JSON and Markdown are written.
- Assert no external network, FastAPI app, PHP, Postgres, or `AGENT_BASE_URL` is required.

Verification commands:

```bash
cd agent-service
uv run pytest tests/unit/documents tests/unit/corpus
uv run pytest tests/unit/evals/extraction
make eval-extraction-gate
```

## Acceptance Criteria

- `make eval-extraction-gate` runs locally without PHP, FastAPI, OpenEMR, Postgres, network, or `AGENT_BASE_URL`.
- The eval set contains exactly 50 committed synthetic/demo cases.
- Every case has a human-reviewed label JSON file.
- Boolean rubric categories include exactly `schema_valid`, `citation_present`, `factually_consistent`, `safe_refusal`, and `no_phi_in_logs`.
- The gate exits non-zero on threshold failure, regression over 5 percentage points, unreviewed labels, missing artifacts, or PHI leakage.
- `make eval-extraction-live` can generate candidate prediction files without overwriting human labels.
- Existing CLI and FastAPI ingest paths still work through the live Anthropic backend.
- Existing deployed-agent eval remains available as `make eval`.

## Deferred

- Git/pre-push enforcement.
- Review UI.
- Remote CI workflow.
- Database persistence for extraction eval results.
- OCR-heavy citation verification beyond structural citation checks.
- A separately published Reducto-like package.
