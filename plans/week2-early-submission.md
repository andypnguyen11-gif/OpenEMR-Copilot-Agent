# Plan — Week 2 Early Submission (Thursday 2026-05-07 23:59)

## Context

The Week 2 early-submission rubric requires by Thursday 23:59:

1. **Supervisor + 2 workers**
2. **50-case eval suite** with boolean rubrics across extraction / retrieval / citations / refusals / missing-data
3. **PR-blocking CI** that fails on meaningful regression
4. **Deployed app**
5. **3-5 min demo video**

Today is Wednesday 2026-05-06; effective working time is ~24 hours.

`PRD2.md` and `W2_ARCHITECTURE.md` are aligned with this scope and do **not** need updating before submission. `TASKS2.md` "Landing status as of 2026-05-06" is the current truth: extraction works in a synchronous single-loop path (no LangGraph), the corpus retriever is BM25-only, the eval suite is 10 boolean-rubric cases, observability runs with LangSmith disabled. The Thursday submission closes the gap from there to the rubric's Core scope.

## Locked decisions

1. **Eval suite: Stage 4A.** The plan in `plans/stage-4a-extraction-eval-core.md` is the eval strategy. 50 cases distributed: extraction 28 (14 lab + 14 intake), retrieval 8, citations 6, refusals 4, missing-data 4. Boolean rubrics: `schema_valid`, `citation_present`, `factually_consistent`, `safe_refusal`, `no_phi_in_logs`. `baseline.json` + 5pp regression threshold gates CI.
2. **Workers (PRD-named):** `intake_extractor` (multimodal — lab PDF + intake form) and `evidence_retriever` (hybrid RAG over corpus). No `chart_tools` worker for early submission.
3. **Supervisor: plain Python.** Anthropic Messages call with two `tool_use` tools (`dispatch_intake_extractor`, `dispatch_evidence_retriever`). No LangGraph. W2-07 deferred to full submission.
4. **Critic: deferred** (PRD lists as Extension).
5. **Hybrid RAG: BM25 + dense + LLM-judge rerank.** Cross-encoder rerank deferred to full submission.
6. **Observability: structlog handoff log only.** LangSmith stays disabled (avoids the W2-12 PHI-redaction dependency and the explicit PHI-leak pitfall).
7. **Stage 4A cuts for the 24h window:**
   - `ExtractionBackend` protocol refactor / replay backend → deferred to full submission. CI runs cases live; cost ~$2-3/run is acceptable for Thursday.
   - PHI-as-separate-rubric → collapsed into a single sentinel-scan smoke check on result JSON.
   - Granular per-rubric unit tests → replaced by one integration smoke test.
   - Boolean rubrics + baseline.json + regression check → kept (PRD-mandated).

## Architecture

```
                                       ┌─────────────────────────┐
                                       │ Supervisor              │
  user query ──► JWT + patient ───────►│ orchestrator/           │
                                       │   supervisor.py         │
                                       │                         │
                                       │ Anthropic Messages call │
                                       │ with two tools:         │
                                       │  • dispatch_intake_..   │
                                       │  • dispatch_evidence_.. │
                                       └────┬────────────────┬───┘
                                            │                │
                       ┌────────────────────┘                └────────────────────┐
                       ▼                                                          ▼
            ┌──────────────────────┐                              ┌──────────────────────────┐
            │ intake_extractor     │                              │ evidence_retriever       │
            │ orchestrator/        │                              │ orchestrator/workers/    │
            │   workers/intake.py  │                              │   evidence.py            │
            │                      │                              │                          │
            │ wraps                │                              │ wraps corpus/retriever:  │
            │ documents/extractor  │                              │   BM25 + dense + LLM     │
            │   ::extract          │                              │   rerank                 │
            │                      │                              │                          │
            │ → ExtractedField[T]  │                              │ → top-k chunks +         │
            │   with citations     │                              │   source citations       │
            └─────────┬────────────┘                              └────────────┬─────────────┘
                      │                                                        │
                      └────────────────────┬───────────────────────────────────┘
                                           ▼
                              Supervisor synthesis
                              (abstain on uncited / schema-invalid)
                                           │
                                           ▼
                              structlog handoff log entries:
                              { worker_dispatched, worker_returned,
                                citation_count, latency_ms, request_id }
```

## File-level work

Executed in the order below. Eval is first because it's the longest single phase and largely independent of supervisor + RAG; doing it first de-risks the deadline.

### Phase 1 — Eval harness + 42 non-retrieval cases (~10h)

**New (per `plans/stage-4a-extraction-eval-core.md` with the cuts above):**
- `agent-service/src/clinical_copilot/evals/__init__.py`
- `agent-service/src/clinical_copilot/evals/extraction/cases.py` — case manifest loader, exact-50 enforcement, duplicate-id check, missing-artifact check.
- `agent-service/src/clinical_copilot/evals/extraction/labels.py` — required-field schema (`required_fields`, `must_abstain`, `required_citations`, `expected_retrieval`, `safe_refusal`, `metadata`).
- `agent-service/src/clinical_copilot/evals/extraction/rubrics.py` — five boolean rubrics.
- `agent-service/src/clinical_copilot/evals/extraction/runner.py` — live-mode gate; `--write-baseline`; regression check vs `baseline.json`.
- `agent-service/src/clinical_copilot/evals/extraction/results.py` — JSON + Markdown summaries.
- `agent-service/src/clinical_copilot/evals/extraction/phi.py` — sentinel scan for PHI patterns in results.

**Eval data (this phase: 42 of 50 cases):**
- `agent-service/evals/extraction/cases.jsonl` — 42 case manifests so far.
- `agent-service/evals/extraction/labels/` — 42 human-reviewed label JSONs.
- `agent-service/evals/extraction/results/` (gitignored).

**42 cases:**
- Migrate the existing 10 (`tests/eval/w2_cases/extraction-{lab,intake}/`) into the new manifest. Schema is different — translate, don't copy.
- 18 more extraction cases via `tests/fixtures/build_pdfs.py` synthetic variants (clean, scanned, rotated, low-confidence, missing-fields).
- 6 citation cases (structural — bad bbox, empty raw_text, wrong document_id) via cached bad predictions.
- 4 missing-data + 4 safe-refusal: hand-author.
- AI-draft initial labels, human-review every one before baseline write.

**Make targets** (`agent-service/Makefile`):
- `eval-extraction-gate` — runs the gate live (Thursday).
- `eval-extraction-smoke` — `live_smoke=true` cases only.
- `eval-labels-validate` — manifest + label schema validation.

**Tests:**
- `agent-service/tests/unit/evals/extraction/test_loader.py` — exact-50 enforcement, duplicate-id rejection, missing-artifact rejection.
- `agent-service/tests/unit/evals/extraction/test_rubrics.py` — one passing + one failing fixture per rubric.
- `agent-service/tests/unit/evals/extraction/test_baseline.py` — below-threshold fails, regression >5pp fails, exactly 5pp passes.
- `agent-service/tests/integration/test_eval_gate.py` — single integration smoke over a 5-case temp manifest.

### Phase 2 — Supervisor + 2 workers (~6-8h)

**New:**
- `agent-service/src/clinical_copilot/orchestrator/supervisor.py` — top-level Anthropic Messages call with two `tool_use` tools; structlog handoff logging; result synthesis with abstention on missing citations.
- `agent-service/src/clinical_copilot/orchestrator/workers/__init__.py`
- `agent-service/src/clinical_copilot/orchestrator/workers/intake.py` — supervisor-callable wrapper around `documents/extractor.py::extract`. Input: `document_id`. Output: `LabPdfFacts | IntakeFormFacts` JSON + flattened citation list.
- `agent-service/src/clinical_copilot/orchestrator/workers/evidence.py` — wraps `corpus/retriever.py`. Input: `query`. Output: top-k chunks + source citations.

**Edit:**
- `agent-service/src/clinical_copilot/main.py` — slow lane on `/api/agent/query` routes through `supervisor.run()`. Fast lane unchanged (still uses `agent.run()`).

**Tests:**
- `agent-service/tests/integration/test_supervisor.py` — three end-to-end cases (chart-only, document-only, mixed). Assert handoff log entries exist for the right workers. Mock the Anthropic call.

### Phase 3 — Hybrid RAG (~3h)

**New:**
- `agent-service/src/clinical_copilot/corpus/embeddings.py` — embed corpus chunks at startup or load cached `.npy`; embed queries at retrieval time. Use `sentence-transformers/all-MiniLM-L6-v2` (local, no API key) or `text-embedding-3-small` if simpler.

**Edit:**
- `agent-service/src/clinical_copilot/corpus/retriever.py` — add dense step alongside existing BM25; combine via Reciprocal Rank Fusion; rerank top-N with a Haiku call returning 0-1 relevance scores.

**Tests:**
- `agent-service/tests/unit/corpus/test_hybrid.py` — assert dense-only works, BM25-only works, hybrid is at-least-as-good on a 5-case microbenchmark.

### Phase 4 — Retrieval cases + baseline write (~2-3h)

The remaining 8 of 50 cases. Authored after hybrid RAG so expected top-k chunk IDs reflect the production retrieval pipeline.

**Eval data (closes the suite at 50):**
- 8 retrieval cases hand-authored against the 11-source corpus. Each case lists `expected_retrieval.source_doc_id` (or `chunk_id`) and asserts gold-in-top-k.
- Run `make eval-extraction-gate --write-baseline` to write the initial `agent-service/evals/extraction/baseline.json`. From this point forward, regression detection is live.

**Verify:** `make eval-extraction-gate` returns exit 0 with the full 50-case suite. Manifest loader's exact-50 enforcement passes for the first time.

### Phase 5 — PR-blocking CI (~2h)

**New:**
- `.github/workflows/agent-service-eval.yml` — on PR + push to main, runs `cd agent-service && make check && make eval-extraction-gate`. Required check on `main`. Documents `ANTHROPIC_API_KEY` as required repo secret.

**Edit:**
- `agent-service/Makefile` — wire eval targets to be CI-callable.
- Document `make eval-extraction-gate` cost (~$2-3/run) in `agent-service/README.md`.

### Phase 6 — Deployment (~1h)

- From `agent-service/` (NOT repo root, per saved memory): `railway up --service agent-service`.
- Verify `/healthz` returns 200 on the public URL.
- Smoke-test supervisor with a curl POST that triggers both workers.

### Phase 7 — Demo video (~2h)

- Record 3-5 min showing:
  1. Upload a lab PDF in the chart UI; show structlog handoff entries.
  2. Ask a query that triggers both workers ("what does the recent lab say and what guidelines apply"); show supervisor's tool_use call sequence in the log.
  3. Show extracted facts with citations rendered.
  4. Show evidence-retriever results with corpus citations.
  5. Run `make eval-extraction-gate` locally; show pass rates per category and the regression-detection demo (revert a change, watch CI block).
  6. Show GitHub Actions PR-blocking CI page screenshot.

## Time budget

| Phase | Estimate |
|---|---|
| 1. Eval harness + 42 non-retrieval cases + tests | 10h |
| 2. Supervisor + 2 workers + tests | 6-8h |
| 3. Hybrid RAG | 3h |
| 4. Retrieval cases (8) + baseline write | 2-3h |
| 5. PR-blocking CI workflow | 2h |
| 6. Deployment + smoke | 1h |
| 7. Demo video | 2h |
| **Total** | **26-29h** |

Tight. Bottleneck is 50-case authoring. If it overruns 6h, mitigations in priority order:
1. Drop 4 of the 18 synthetic extraction variants (reuse existing 10 + AI-draft 16 = 26 extraction cases instead of 28; rebalance retrieval to 10).
2. AI-draft labels with shallow review for retrieval cases (still human-confirm pass/fail outcomes).
3. Skip the integration smoke test for the gate; rely on running the gate live.

## Verification (end-to-end)

```bash
# 1. Supervisor smoke
cd agent-service
uv run uvicorn clinical_copilot.main:app --reload &
JWT=$(uv run python -m clinical_copilot.scripts.mint_dev_jwt)
curl -X POST http://localhost:8000/api/agent/query \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"query":"what does the latest lab say and what guidelines apply","lane":"slow","patient_id":"<pid>"}'
# Expect: 200, response with synthesis + citations; handoff log shows
#         intake_extractor + evidence_retriever dispatched.

# 2. Eval gate (live)
make eval-extraction-gate
# Expect: 50 cases run, all rubric categories within thresholds, exit 0.

# 3. Regression detection
# Revert FhirAllergyIntoleranceService::filter or extractor confidence default,
# rerun gate; expect factually_consistent or safe_refusal below threshold,
# exit non-zero.

# 4. CI gate
git push origin <branch>
# Expect: GitHub Actions runs eval workflow, passes on green; PR-blocking on
#         main protected branch settings.

# 5. Deployed app
curl https://openemr-production-6c31.up.railway.app/healthz
# Expect: 200 OK.
curl -X POST https://openemr-production-6c31.up.railway.app/api/agent/query \
  -H "Authorization: Bearer $JWT" -d '...'
# Expect: same shape as local, real Synthea data.
```

## Out of scope (full submission, post-Thursday)

- W2-07 LangGraph migration (planner / supervisor / critic graph as proper StateGraph).
- Critic agent.
- ExtractionBackend protocol refactor + replay backend (live CI for Thursday is acceptable; replay is the cost-sustainable answer for ongoing PR work).
- Click-to-source UI / Documents-view side panel (W2-10).
- Cross-encoder rerank (LLM rerank is the early-submission stand-in).
- LangSmith re-enable + W2-12 PHI redaction MR.
- Cost & latency report (PRD lists as full-submission deliverable; not in early submission ask).
- W2_ARCHITECTURE.md update reflecting the early submission's "no LangGraph, no critic" reality. Doc currently describes the design target; update before full submission.

## Risks

- **50-case authoring overruns Thursday morning.** Mitigation: AI-draft + human-review pattern; mitigations listed above.
- **Hybrid RAG dense embeddings model selection.** Local `sentence-transformers` adds a heavy dep but is one-shot install. `text-embedding-3-small` adds a paid call per query. Pick local at start; flip to OpenAI if local model is too slow for CI.
- **Live CI cost (~$2-3/run).** Acceptable for Thursday; flag in submission notes; replay is the right answer for ongoing CI.
- **Demo upload flow on deployed app.** Verify chart-side upload works end-to-end on Railway before recording.
- **Railway env vars.** Per saved memory: `OAUTH_PRIVATE_KEY_PEM` must include PEM markers; check before deploying. `cd agent-service` before `railway up` to avoid shipping the wrong Dockerfile.
