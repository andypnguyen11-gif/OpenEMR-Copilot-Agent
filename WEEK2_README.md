# Clinical Co-Pilot — Week 2 (Multimodal) Pointer

This repository is a fork of [OpenEMR](https://github.com/openemr/openemr) that
adds the **Clinical Co-Pilot** AI agent. The fork carries two scope layers:

* **Week 1 baseline** — chart-tools-only Q&A, FHIR-backed structured tools,
  verification + abstention, reconciliation against the chart. See
  `PRD.md`, `ARCHITECTURE.md`, `TASKS.md`.
* **Week 2 multimodal** — vision extraction of lab PDFs and intake forms,
  guideline-corpus retrieval, ExtractedField/SourceCitation contract.
  See `PRD2.md`, `W2_ARCHITECTURE.md`, `TASKS2.md`.

**Both layers live on `main`.** No separate branch is needed. The Week 2
code surface is contained to three new Python packages under
`agent-service/src/clinical_copilot/{schemas,documents,corpus}` plus
two new CLIs under `clinical_copilot/scripts/`. None of it touches the
Week 1 chart-tools / orchestrator / discrepancy code paths, so a grader
running the Week 1 flow gets the same behaviour as before.

## Where to go next

* **Running the Week 2 demo (lab PDF ingestion + intake ingestion +
  evidence retrieval):** see
  [`agent-service/README.md` § Week 2 — Multimodal demo](agent-service/README.md#week-2--multimodal-demo).
  That section names the exact services required (just Python + an
  Anthropic API key — no OpenEMR, no FHIR server, no Postgres) and the
  three CLI commands the demo runs end-to-end.
* **Running the Week 1 baseline:** see
  [`agent-service/README.md` § Quickstart](agent-service/README.md#quickstart)
  for the FastAPI sidecar, and `ARCHITECTURE.md` for the chart-tools
  flow.
* **Where the multimodal code lives:**
  - `agent-service/src/clinical_copilot/schemas/abstain.py` — canonical
    `RuntimeAbstainReason` (7 members; the Week 1 4-state enum aliases
    onto this).
  - `agent-service/src/clinical_copilot/documents/` — vision extractor,
    Pydantic schemas, JSON-on-disk persistence.
  - `agent-service/src/clinical_copilot/corpus/` — markdown sources,
    chunker, BM25 indexer, retriever.
  - `agent-service/corpus/sources/` — committed USPSTF / CDC / NIH /
    AHA excerpts (synthetic adaptations; see `LICENSES.md`).
  - `agent-service/tests/fixtures/{lab_pdf,intake_form}/` — committed
    synthetic demo PDFs.

## What is and is not in scope tonight

The Week 2 milestone for tonight is **lab PDF + intake form ingestion
working locally, first extraction, and first evidence retrieval
demo.** The full W2-02 OpenEMR Documents bridge (Symfony listener,
HMAC payload, `extraction_jobs` queue, side-panel state poll), the
W2-05 OCR-based citation check, the W2-07 LangGraph supervisor, and
the eval buckets land in subsequent merges per `TASKS2.md`. The
demo coverage matrix in `agent-service/README.md` lists each W2-XX
block alongside its demo-cut status.
