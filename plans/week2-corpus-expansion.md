# Plan — Expand the guideline corpus to cover every W1 + W2 fixture entity

## Context

The Week 2 demo question is **"What changed, what should I pay attention to,
and what evidence supports the recommendation?"** Every recommendation the
copilot prints must cite either a chart `source_id` or a corpus chunk. Today
the corpus has 15 markdown sources (90 chunks) — heavy on **screening**, light
on **management**. Several conditions and medications that appear in the
fixtures and on the demo patient panel have *no* guideline backing in the
index, so the supervisor's `evidence_retriever` worker either returns
unrelated chunks or the answer falls back to abstention.

We expand the corpus first because (a) it's the cheapest unblocker for the
"evidence supports the recommendation" rubric and the open-ended chat path,
and (b) it has no architectural risk — the indexing pipeline is stable, the
retriever is already wired into the supervisor, and the eval gate already
asserts on `expected_source_doc_ids`.

## Coverage gap (demo entity → current coverage → action)

| Demo entity (from fixtures + demo patients) | In corpus today? | Action |
|---|---|---|
| Type 2 diabetes — **management** (ADA glycemic targets, metformin first-line, SGLT2/GLP-1 step-up) | Screening only (`uspstf/prediabetes_diabetes_screening.md`) | **ADD** `ada/diabetes_management.md` |
| Hypertension — **management** (BP < 130/80 in high-risk, first-line ACEi/ARB/CCB/thiazide) | Screening only (`uspstf/hypertension_screening.md`) | **ADD** `acc_aha/hypertension_management.md` |
| Statin intensity for ASCVD risk + secondary prevention | Primary-prevention only (`uspstf/statin_primary_prevention.md`, `nih/cholesterol_thresholds.md`) | **ADD** `acc_aha/cholesterol_management.md` |
| CAD secondary prevention (post-MI DAPT, β-blocker, ACEi/ARB) | None | **ADD** `acc_aha/cad_secondary_prevention.md` |
| CKD staging by eGFR + ACEi/ARB indication | None (only eGFR appears in CMP fixture) | **ADD** `kdigo/ckd_staging_management.md` |
| Metformin monitoring (B12, eGFR < 30 contraindicated, 30-45 dose-adjust) | None | **ADD** `ada/metformin_monitoring.md` |
| ACEi monitoring (K⁺, creatinine bump tolerance, cough) | None | **ADD** `acc_aha/acei_monitoring.md` |
| Acute otitis media (Amoxicillin first-line, dosing, observation) | None | **ADD** `aap/acute_otitis_media.md` |
| CBC interpretation cutoffs (anemia, leukocytosis, thrombocytopenia) | Iron-deficiency only (`cdc/iron_deficiency_anemia.md`) | **ADD** `nih/cbc_interpretation.md` |
| CMP electrolyte interpretation (Na⁺/K⁺ action thresholds, AKI definition) | None | **ADD** `nih/cmp_interpretation.md` |
| Adequate today: aspirin primary, breast/colorectal/lung screening, immunizations, chest pain (AHA), thyroid, anemia, tobacco | Covered | no-op |

10 new source markdowns. Conservative estimate ~60 new chunks
(window=3 / stride=1 chunker), pushing corpus from 90 → ~150 chunks.

## Plan

### 1. Extend the allowed-sources list

The manifest's `permitted_sources` is currently
`[AHA, CDC, NIH/NHLBI, NIH/NIDDK, USPSTF]`. Adding `ADA`, `ACC/AHA`,
`KDIGO`, `AAP` requires:

- Confirm where `permitted_sources` is enforced — grep `permitted_sources`
  inside `agent-service/src/clinical_copilot/corpus/`. If it's only a
  manifest annotation and not a hard scrub gate, no code change is needed
  beyond the new frontmatter. If `scrub.py` or `index.py` enforces it,
  extend the allow-list there.
- Confirm scrub patterns at `agent-service/src/clinical_copilot/corpus/scrub.py:18-26`
  (SSN / phone / email / MRN) won't false-positive on the new docs. They're
  conservative regex; risk is low.

### 2. Author 10 new source markdowns

Pattern (matches existing files like `corpus/sources/uspstf/aspirin_primary_prevention.md`):

```markdown
---
title: <One-line topic>
source: <ADA | ACC/AHA | KDIGO | AAP | NIH/NHLBI | etc.>
source_url: <official URL>
date: <YYYY-MM-DD of guideline version>
topics: [<keyword>, <keyword>, ...]
---

<3-6 paragraphs of plain-language management guidance with concrete
thresholds, drug names, monitoring intervals. No PHI. ~1-2 KB each.>
```

Each new file goes under `agent-service/corpus/sources/<source_dir>/<topic>.md`:

- `corpus/sources/ada/diabetes_management.md`
- `corpus/sources/ada/metformin_monitoring.md`
- `corpus/sources/acc_aha/hypertension_management.md`
- `corpus/sources/acc_aha/cholesterol_management.md`
- `corpus/sources/acc_aha/cad_secondary_prevention.md`
- `corpus/sources/acc_aha/acei_monitoring.md`
- `corpus/sources/kdigo/ckd_staging_management.md`
- `corpus/sources/aap/acute_otitis_media.md`
- `corpus/sources/nih/cbc_interpretation.md`
- `corpus/sources/nih/cmp_interpretation.md`

Each markdown should mention the fixture-relevant lab names and drug names
verbatim (TSH, HbA1c, eGFR, LDL, BUN, creatinine, metformin, lisinopril,
atorvastatin, metoprolol, amlodipine, amoxicillin, etc.) so BM25 picks them
up cleanly. Frontmatter `topics:` should include both clinical topic
keywords and entity names so query rewrites still hit.

### 3. Rebuild the index

Single command:

```bash
cd agent-service
uv run python -m clinical_copilot.corpus.index --rebuild
```

This regenerates `data/corpus/manifest.json`, `data/corpus/bm25.pkl`, and —
when `OPENAI_API_KEY` is exported — `data/corpus/dense.pkl`. Without the
key, dense path skips and retrieval is BM25-only (gracefully). Per the
existing memory note about credentials, the user exports the key in their
shell; we don't ask for it.

Verify post-rebuild:

- `manifest.json` `doc_count` increases from 15 → 25
- `chunk_count` increases from 90 to roughly 150
- `dense_built` is `true` (or `false` deliberately, if reindexing BM25-only)

### 4. Extend the retrieval eval bucket

Today: 8 retrieval cases (rt01-rt08) at
`agent-service/evals/extraction/labels/retrieval/` each asserting one
`expected_source_doc_ids` entry. Add 10 new cases — one per new source —
following the existing JSON shape. Naming: `rt09` … `rt18`. Each case asks
a natural-language question that matches the new doc's topic:

| Case | Query | expected_source_doc_ids |
|---|---|---|
| rt09 | "What's the A1c target for type 2 diabetes management?" | `ada/diabetes_management` |
| rt10 | "Should this patient on metformin be screened for B12 deficiency?" | `ada/metformin_monitoring` |
| rt11 | "What's the BP target on antihypertensive therapy?" | `acc_aha/hypertension_management` |
| rt12 | "Is this patient a candidate for high-intensity statin?" | `acc_aha/cholesterol_management` |
| rt13 | "What secondary-prevention meds are indicated after MI?" | `acc_aha/cad_secondary_prevention` |
| rt14 | "How do I monitor potassium and creatinine on lisinopril?" | `acc_aha/acei_monitoring` |
| rt15 | "What CKD stage is eGFR 42 and what does it imply for dosing?" | `kdigo/ckd_staging_management` |
| rt16 | "First-line antibiotic and duration for acute otitis media?" | `aap/acute_otitis_media` |
| rt17 | "Hemoglobin 10.5 — does this meet anemia threshold?" | `nih/cbc_interpretation` |
| rt18 | "Potassium 5.6 — when do I act on this CMP result?" | `nih/cmp_interpretation` |

### 5. Smoke-test against the live supervisor

With `use_supervisor=true` (default) and the rebuilt index in place:

```bash
# Local: hit /api/agent/query with the demo question.
curl -sS -X POST http://localhost:8000/api/agent/query \
  -H 'Authorization: Bearer <dev-token>' \
  -H 'content-type: application/json' \
  -d '{"query":"What changed, what should I pay attention to, and what evidence supports the recommendation?","lane":"slow","session_id":null}' \
  | jq '.prose, .cards, .abstention'
```

Eyeball: `cards` should reference at least one chunk from the new sources
when the bound patient has diabetes/hypertension/CAD; abstention should be
`null` instead of `NO_DATA`.

## Critical files

- **NEW** `agent-service/corpus/sources/{ada,acc_aha,kdigo,aap,nih}/*.md` — 10 files
- **NEW** `agent-service/evals/extraction/labels/retrieval/rt0{9,...,18}.json` — 10 files
- **REBUILT** `agent-service/data/corpus/manifest.json`, `data/corpus/bm25.pkl`, `data/corpus/dense.pkl`
- **NO CODE CHANGES** expected — existing
  `agent-service/src/clinical_copilot/corpus/{index,chunker,scrub,retriever,rerank}.py`
  handle the new files unchanged. Only edit if step 1 finds an enforced
  allow-list.

## Reused existing utilities

- Frontmatter loader at `agent-service/src/clinical_copilot/corpus/index.py:71-93`
- Sentence-window chunker at `agent-service/src/clinical_copilot/corpus/chunker.py:47-90`
- BM25 + RRF fusion at `agent-service/src/clinical_copilot/corpus/retriever.py:62-146`
- LLM-judge rerank at `agent-service/src/clinical_copilot/corpus/rerank.py`
- Eval runner at `agent-service/src/clinical_copilot/evals/extraction/runner.py`

## Verification

1. `uv run python -m clinical_copilot.corpus.index --rebuild` exits 0 and
   manifest doc_count = 25, chunk_count ≈ 150.
2. `cd agent-service && uv run pytest tests/unit/corpus/test_retriever.py`
   stays green (the pure-BM25 unit test should be unaffected; if it loads
   `bm25.pkl` it'll pick up the new corpus).
3. `make eval-extraction-gate` (or whatever the runner CLI is — see
   `agent-service/Makefile`) passes the retrieval bucket. The 10 new
   retrieval cases each return their target doc in top-k.
4. Live smoke (above): `/api/agent/query` against the supervisor branch
   for a diabetic + hypertensive demo patient cites at least one of the
   new corpus chunks in the response.
5. No new PHI scrub failures — `index.py` exits 0, no `PhiInCorpusError`.
6. Dense path still loads when `OPENAI_API_KEY` is set (or BM25-only
   gracefully when it isn't).

## Backlog after this lands (not in this plan, but tracked here so we don't lose them)

The user surfaced four additional concerns during planning. We sequence
them after the corpus expansion is verified:

1. **PR doc alignment to the assignment PRD** — TASKS2.md, PRD2.md,
   W2_ARCHITECTURE.md need a pass to line up with the Sunday-afternoon
   submission spec the user pasted (Core/Extension/Stretch tiers, eval-CI
   gate language, demo-video deliverable, cost-and-latency report,
   common-pitfalls checklist). Update doc surface, no code.
2. **Re-pick the next TASKS2.md PR.** After (1), with the corpus solid,
   the user wants to choose between the recovery-checklist items in
   TASKS2.md:115-278 (chart-write confirmation surface vs supervisor
   wiring vs LangGraph rewrite vs submission narrative).
3. **Timeout on open-ended multi-part questions.** Three-part queries can
   chew through all 4 supervisor iterations; `client.messages.create`
   has no per-call timeout; workers dispatch sequentially even when the
   model emits multiple `tool_use` blocks in one assistant turn
   (`agent-service/src/clinical_copilot/orchestrator/supervisor.py:313-322`).
   Likely fixes: parallel `tool_use` dispatch via
   `asyncio.gather`, per-call timeout on `client.messages.create`,
   raise iteration cap, optional SSE streaming on `/api/agent/query`.
   Plan separately.
4. **Login-revert on universal upload submit.** Symptom seen 1-2× in the
   chart-side universal upload flow (`interface/copilot/upload_document.php`
   and friends). Likely candidates: stale CSRF token after long synchronous
   extract, session expiry while VLM round-trip is in flight, or PHP
   `post_max_size` truncating multipart and tripping the unauth redirect.
   Investigate the submit handler, the AclMain check, and the
   `apicsrftoken` rotation pattern. Plan separately.
5. **Full LangGraph W2-07 rewrite, flag-gated** — the originally-asked
   item. State graph + planner + critic + edges + LangSmith spans + 5
   test files + 6 citation-separation eval cases. Coexists with the
   current tool_use supervisor behind a new `use_langgraph` flag.
   Already-scoped in the AskUserQuestion answer; plan separately.
