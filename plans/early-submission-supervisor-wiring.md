# Validate Shipped Supervisor + Hybrid Retriever and Submit Demo

## Context

**This plan was originally written before the wiring work landed.** The
wiring is now merged and live:

- Supervisor branch on `/api/agent/query` slow lane —
  `agent-service/src/clinical_copilot/main.py:396-430` (commit
  `649cf9dd8`, "feat(copilot): route slow-lane /api/agent/query through
  Supervisor (W2-07)").
- Workers + Anthropic client wired into `AppState` —
  `agent-service/src/clinical_copilot/app_state.py:154-157, 292-372`.
- `USE_SUPERVISOR=true` default — `config.py:96, 114-120`.
- Pre-built corpus indexes shipped (BM25 + dense) — commit `45046a040`,
  un-ignored at `agent-service/.gitignore:34-36`. Hybrid retrieval +
  LLM-judge rerank run on prod, not BM25-only.
- Integration coverage — commit `771093a02`,
  `agent-service/tests/integration/test_query_route_supervisor.py`.

So the W2 reviewer's biggest backend asks (supervisor routing live,
hybrid retrieval real, eval gate enforcing) are all addressed by code
already on `main`. What's left is **proof artifacts**, not engineering.

**Lane split (still load-bearing for the narrative).**
- **Full chat page** (`interface/copilot/chat.php` → no `lane` param →
  `Lane.SLOW`) → **Supervisor**.
- **Side panel chat** (`public/copilot/side_panel.js:53,189` hardcodes
  `lane: 'fast'`) → **v1 Orchestrator** (unchanged). Side panel keeps
  its ≤5s p50 budget and chart-tool dispatch — supervisor has no
  `chart_tools` worker by design (locked decision), so chart-data
  questions only stay correct on the fast lane.

**Outcome.** A live smoke test confirms the supervisor branch fires on
the deployed slow lane with corpus-grounded citations, the README
narrative explicitly calls out citation enforcement / GitLab CI / lane
split, and the changes are pushed.

**Demo video is out of scope for this plan** — user records it
manually. This plan covers only the parts an agent can edit/verify.

---

## Time budget (target ~1h)

| Phase | Estimate |
|---|---|
| A. Live smoke test (verify supervisor fires on prod) | 30 min |
| C. README narrative (3 sentences) | 15 min |
| D. Final commit + push (with explicit confirmation) | 15 min |
| **Total** | **~1h** |

(Phases labeled A/C/D to match the user's "worry about a c d"
direction; B — the demo video — is intentionally omitted.)

---

## Phase A — Live smoke test (30 min)

Purpose: prove the deployed slow-lane query actually invokes the
supervisor and returns corpus-grounded citations, *before* trusting
the README to make those claims.

Mint a chat JWT the way local dev does (or reuse an existing token
from a recent OpenEMR login session). Then:

```bash
curl -sS -X POST "$AGENT_URL/api/agent/query" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"query":"What screenings are recommended for a 55-year-old smoker?","lane":"slow"}' \
  | jq '.prose[0]'
```

Expected: `prose[0].text` non-empty, `prose[0].source_id` matches a
real chunk_id from `agent-service/data/corpus/manifest.json`.

Then confirm Railway logs show `supervisor.handoff` with
`worker=evidence_retriever` for the same `request_id`:

```bash
railway logs --service agent-service | grep supervisor.handoff
```

If the smoke is red — supervisor not firing, citation missing, or
exception fallback — flip `USE_SUPERVISOR=false` on Railway and
triage offline. Rollback is ~30 sec (env var change → service
restart).

The user provides the prod JWT directly; the agent does not handle
credential paste. (See repo memory: "user exports credential in
shell, I do the rest".)

---

## Phase C — README narrative (15 min)

Edit `README.md` Clinical Co-Pilot section (around lines 25-76). Add
three sentences that map directly to the W2 rubric, each linking to
the file that proves the claim:

1. **Citations are schema-enforced, not best-effort.** Every extracted
   field carries either a citation or an explicit abstain reason —
   enforced by the `ExtractedField[T]` Pydantic XOR validator at
   `agent-service/src/clinical_copilot/documents/schemas/citation.py:68-89`,
   and `CitedClaim.source_id` requires `min_length=1`
   (`agent-service/src/clinical_copilot/orchestrator/schemas.py:71`).

2. **GitLab MR-blocking CI runs the 50-case eval gate.** The
   `agent-service:test` and `agent-service:eval-gate` stages
   (`.gitlab-ci.yml:69-109`) are required on `merge_request_event`
   and `main`; the eval suite is 50 grounded human-reviewed cases
   across extraction (28), citations (6), refusals (4), retrieval
   (8), and missing-data (4) — see `agent-service/eval/manifest.yaml`.

3. **Supervisor routes the slow lane; v1 keeps the fast lane.** The
   full chat page dispatches via the W2 Supervisor + 2 workers
   (BM25 + dense + LLM-judge rerank, indexes shipped at
   `agent-service/data/corpus/{bm25,dense}.pkl`); the in-chart side
   panel stays on the v1 orchestrator for ≤5s chart-tool dispatch.
   `USE_SUPERVISOR=false` is the instant rollback toggle.

Place these as a small "Verification surface" or "How the W2
rubric is met" subsection under the existing Clinical Co-Pilot
section so a grader scanning the README finds them without
hunting through `ARCHITECTURE.md`.

---

## Phase D — Final commit + push (15 min)

Two commits:

1. `docs(copilot): plan refresh for early-submission proof artifacts`
   — covers the rewrite of this very file (already mostly done by
   the time D runs; included if the working tree shows it).
2. `docs(copilot): README narrative for W2 rubric (citations, CI, lanes)`
   — the README edits from Phase C.

**Before pushing, ask the user explicitly.** Pushes to `main`
auto-deploy to Railway, and the user's standing instructions
require confirmation before any prod-facing action (see
`feedback_no_prod_deploy.md` in agent memory).

---

## Critical files

**Edit:**
- `README.md` — Phase C narrative

**Read (no edits, cite in README):**
- `agent-service/src/clinical_copilot/documents/schemas/citation.py:68-89`
- `agent-service/src/clinical_copilot/orchestrator/schemas.py:71`
- `.gitlab-ci.yml:69-109`
- `agent-service/eval/manifest.yaml`
- `agent-service/data/corpus/manifest.json`
- `agent-service/data/corpus/dense.pkl` (binary, just cite)
- `agent-service/data/corpus/bm25.pkl` (binary, just cite)

**Rollback lever (do not touch unless smoke fails):**
- Railway agent-service env: `USE_SUPERVISOR=true` → `false`

---

## Verification

End-to-end checklist:

1. Live smoke confirms `prose[0].source_id` matches a real chunk_id
   from `agent-service/data/corpus/manifest.json`.
2. Railway `agent-service` logs show `supervisor.handoff` rows with
   `worker=evidence_retriever` for the same request_id.
3. README edits land and the three claims are each backed by a file
   path and line range.
4. GitLab pipeline for the README commit is green
   (`agent-service:test` is read-only, eval-gate runs on push to
   main; both should pass since no agent-service code changed).
5. Deployed app at `openemr-production-6c31.up.railway.app` still
   responds 200 on `/api/agent/healthz` after the README push (no
   regression).

**Rubric callout coverage (post-this-plan):**
- Supervisor routing observable → ✅ live smoke + structlog handoffs
- Hybrid retrieval truly sparse + dense + rerank → ✅ both pickles
  shipped, rerank wired
- Citations reliable and visible → ✅ schema-enforced + README cites
  the validator
- Eval gate 50+ grounded cases → ✅ manifest + GitLab gate
- Physician workflow visible end-to-end → handled by the demo video
  (out of scope for this plan)

---

## Risks / known gaps to call out in submission narrative

- **`intake_extractor` rarely fires on chat queries** (model has no
  `document_path` to invent on a chat turn). Locked plan accepts
  this; bridge to saved facts is full-submission work.
- **Audit log silent on supervisor branch** — by design (no PHI
  tools called). One-line comment in route already documents this
  (`main.py:386-395`).
- **Single citation per supervisor turn** unless the multi-citation
  adapter is added later. Locked plan accepts; not in this scope.
- **`USE_SUPERVISOR=false` is the instant rollback** if anything
  regresses for testers during the grading window.
