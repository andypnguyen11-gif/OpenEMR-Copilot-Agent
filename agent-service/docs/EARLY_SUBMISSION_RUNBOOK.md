# Week 2 Early Submission — Demo Runbook

3-5 minute walkthrough hitting the rubric's five Core deliverables:

1. **Supervisor + 2 workers** — `intake_extractor` + `evidence_retriever`.
2. **50-case eval suite** — boolean rubrics, baseline regression detection.
3. **PR-blocking CI** — `.github/workflows/agent-service-eval.yml`.
4. **Deployed app** — existing Railway deployment (multimodal upload flow).
5. **Demo video** — this script.

## Pre-recording checklist

```bash
cd /Users/andynguyen/Desktop/OpenEMR/openemr/agent-service

# 1. ANTHROPIC_API_KEY in .env (already set).
grep -q "^ANTHROPIC_API_KEY=" .env && echo "key present" || echo "MISSING"

# 2. Synthetic fixtures + corpus index already built (committed).
test -f tests/fixtures/lab_pdf/glucose_panel.pdf && echo "lab fixtures OK"
test -f data/corpus/bm25.pkl && echo "corpus index OK"

# 3. New harness loads cleanly.
uv run python -m clinical_copilot.evals.extraction.runner --validate-only

# 4. New tests all green.
uv run pytest tests/unit/evals tests/unit/corpus/test_rerank.py tests/integration/test_supervisor.py -v
```

## Recording script (~4 minutes)

### Beat 1 — Supervisor + 2 workers (~60s)

> "Week 2 ships a multi-agent graph: a supervisor that routes work between
> two workers — intake_extractor for multimodal documents, evidence_retriever
> for guideline RAG. Plain Python, no LangGraph. Every handoff is logged."

Run:

```bash
uv run python scripts/demo_supervisor.py \
  --document tests/fixtures/lab_pdf/glucose_panel.pdf \
  --document-type lab_pdf \
  --query "What's notable in this lab and what guideline applies?"
```

**What to point at**:

- `=== HANDOFFS` — both `intake_extractor` and `evidence_retriever`
  fired, with latencies.
- `=== SYNTHESIZED RESPONSE` — the supervisor cited extracted facts
  AND a corpus chunk.
- structlog lines on stderr show `supervisor.handoff` per dispatch.

### Beat 2 — 50-case eval suite (~90s)

> "Every claim the agent makes must be testable. The eval suite is 50
> synthetic cases across the five PRD categories — extraction, retrieval,
> citations, refusals, missing-data — judged with boolean rubrics:
> schema_valid, citation_present, factually_consistent, safe_refusal,
> no_phi_in_logs. The gate runs in CI and blocks PRs on a >5pp regression
> against `baseline.json`."

Show the cases inventory:

```bash
wc -l evals/extraction/cases.jsonl   # 50
ls evals/extraction/labels/          # extraction citations missing-data refusals retrieval
cat evals/extraction/baseline.json   # category thresholds
```

Then run the offline subset (free, ~5 sec):

```bash
make eval-extraction-cached
```

**What to point at**: the `summary` block at the end:

```
schema_valid               18/18   100.00%
citation_present            6/6    100.00%
factually_consistent       12/12   100.00%
safe_refusal                8/8    100.00%
no_phi_in_logs             22/22   100.00%

== gate PASSED
```

(Optional, if budget allows: `make eval-extraction-gate` runs all 50
including the 28 live extraction cases — costs ~$2-3 and takes ~5 min.)

### Beat 3 — Regression detection (~30s)

> "Now break it on purpose."

Edit `evals/extraction/labels/refusals/r03_unauthorized_request.json`
to require a different abstention reason, save, re-run:

```bash
make eval-extraction-cached
```

**Point at**: `safe_refusal` drops from 100% → < 100% → "below threshold"
gate failure with non-zero exit.

Revert the edit to restore green.

### Beat 4 — PR-blocking CI (~30s)

> "That same gate runs in GitLab CI on every PR."

Show:

- `.gitlab-ci.yml` — two stages: `test` then `eval`. The eval stage
  reads ANTHROPIC_API_KEY from a masked + protected CI/CD variable.
- Optional: navigate to the GitLab Pipelines page on the open MR and
  show the pipeline run.

### Beat 5 — Deployed app (~30s)

> "The deployed agent-service on Railway already serves the multimodal
> upload flow. New users can hit it today."

Show:

```bash
curl -s "$AGENT_BASE_URL/healthz"
# → {"ok":true}
```

Open the deployed OpenEMR demo, navigate to a patient's chart, click
"Upload lab document (AI extract)" on the Labs panel, upload one of
the example PDFs.

## What to NOT do

- Don't run `railway up` during the recording. Existing deployment is
  the demo target.
- Don't run the full live `eval-extraction-gate` mid-recording unless
  the recording is 5+ minutes — ~5 min wall time, ~$3.
- Don't mention LangSmith. Tracing is intentionally off (PHI leak
  pitfall per PRD); structlog handoff log is the observability surface
  for early submission.

## Where each deliverable lives

| Deliverable | Path |
|---|---|
| Supervisor | `agent-service/src/clinical_copilot/orchestrator/supervisor.py` |
| intake_extractor worker | `agent-service/src/clinical_copilot/orchestrator/workers/intake_extractor.py` |
| evidence_retriever worker | `agent-service/src/clinical_copilot/orchestrator/workers/evidence_retriever.py` |
| LLM-judge rerank | `agent-service/src/clinical_copilot/corpus/rerank.py` |
| Eval harness | `agent-service/src/clinical_copilot/evals/extraction/` |
| 50-case manifest | `agent-service/evals/extraction/cases.jsonl` |
| Labels | `agent-service/evals/extraction/labels/` |
| Baseline | `agent-service/evals/extraction/baseline.json` |
| CI workflow | `.gitlab-ci.yml` |
| Demo CLI | `agent-service/scripts/demo_supervisor.py` |
| Plan | `plans/week2-early-submission.md` |

## Known gaps (out of scope for early; documented for full submission)

- **Critic agent** — PRD lists as Extension. Skipped for early.
- **LangGraph migration** — W2-07 deferred; supervisor runs as plain
  Python. Doc target unchanged.
- **Cross-encoder rerank** — replaced with LLM-judge rerank for time;
  cross-encoder lands with the full submission.
- **LangSmith tracing** — disabled; W2-12 PHI redaction MR is the
  prerequisite to re-enable.
- **Replay backend / `ExtractionBackend` protocol** — CI runs cases
  live for early submission (~$2-3/run). Replay stage planned for
  full submission to make per-PR CI cost-sustainable.
