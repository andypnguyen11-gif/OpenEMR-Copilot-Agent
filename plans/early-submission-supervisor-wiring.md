# Wire Supervisor + Hybrid Retriever into `/api/agent/query` and Demo End-to-End

## Context

**Why this work, now.** The grader's W2 feedback explicitly calls out five items: supervisor/orchestrator routing observable, hybrid retrieval truly sparse + dense + rerank, citations reliable and visible, eval gate meaningful with 50+ grounded cases, and the physician workflow visible end-to-end "without needing to infer behavior from the repo."

The user's own `TASKS2.md:188-199` and `plans/week2-early-submission.md` flag the largest open item: **wire the shipped Supervisor + 2 workers + hybrid retriever into the live `/api/agent/query` path**. Code is committed (commit `39f487aaf` for supervisor + workers; `corpus/retriever.py`/`corpus/rerank.py` for hybrid stack), exercised by tests, but never on the production chat route — `main.py:283` still calls `Orchestrator.run()` (v1 single-loop, FHIR tools only). `data/corpus/dense.pkl` doesn't exist, so even the corpus retriever degrades to BM25-only on prod.

**Locked decisions** from `plans/week2-early-submission.md:17-30`:
- 2 workers only (intake_extractor + evidence_retriever); no `chart_tools` worker
- Supervisor in plain Python via Anthropic `tool_use` (no LangGraph migration)
- Hybrid RAG: BM25 + dense + LLM-judge rerank live in production
- Observability: structlog handoff log (LangSmith stays disabled)
- Feature flag `USE_SUPERVISOR` defaults ON in prod; chart-data questions abstaining is acceptable

**Outcome.** Deployed Co-Pilot chat at `openemr-production-6c31.up.railway.app` routes the full-chat-page traffic (slow lane) through the W2 Supervisor; clinician asks a question, the supervisor dispatches `evidence_retriever` (and/or `intake_extractor`) via Anthropic tool_use, evidence_retriever runs BM25 + dense + LLM-judge rerank against the curated corpus, citations render in the chat, and structlog `supervisor.handoff` rows show the routing decisions. A 3-5 min demo video walks through upload → extraction → chart write → grounded chat response with citations, linked from `README.md`.

**Lane split (load-bearing, not arbitrary).**
- **Full chat page** (`interface/copilot/chat.php` → no `lane` param → `Lane.SLOW`) → **Supervisor**. Hosts the deeper guideline / evidence queries; SLOW-lane SLO accommodates the extra Anthropic round-trips (supervisor tool_use + evidence_retriever + rerank LLM-judge).
- **Side panel chat** (`interface/copilot/side_panel.php` → explicit `lane: 'fast'`, see `public/copilot/side_panel.js:53,189`) → **v1 Orchestrator** (unchanged). Engineered for ≤5s p50 (PR 17 acceptance), Haiku-backed, dispatches FHIR chart tools. Putting supervisor there would (a) blow the latency budget by ~2-3s of LLM-judge rerank, and (b) abstain on every chart-data query because supervisor has no `chart_tools` worker per locked plan. Side panel keeps its existing v1 behavior.
- **Demo video records using the full chat page**, not the side panel.

**Budget.** ~5 hours. Sub-task estimates inside.

---

## Time budget (target ~5h)

| Phase | Estimate |
|---|---|
| -1. Mirror this plan into `<repo>/plans/` | 2 min |
| 0. Pre-flight (deploys land, env vars set) | 15 min |
| 1. Build `dense.pkl` locally + commit | 20 min |
| 2. Wire Supervisor into `/api/agent/query` | 2h 0m |
| 3. Deploy + smoke-test live | 30 min |
| 4. Multi-citation polish in adapter (optional) | 30 min |
| 5. Record demo video | 90 min |
| 6. README update + final push | 15 min |
| **Total** | **~5h** |

If overrunning, cut Phase 4 first (single-anchor citation is acceptable per locked plan; adapter still surfaces one cite).

---

## Phase -1 — Mirror plan into repo (2 min)

Copy this file into the repo's `plans/` directory so future sessions / collaborators can resume from the repo alone:

```
cp /Users/andynguyen/.claude/plans/help-me-plan-out-abundant-emerson.md \
   /Users/andynguyen/Desktop/OpenEMR/openemr/plans/early-submission-supervisor-wiring.md
```

No commit needed in this phase — the file lands alongside `plans/week2-early-submission.md` and gets picked up by the next commit that sweeps the working tree.

---

## Phase 0 — Pre-flight (15 min)

Verify the in-flight deploys land before starting any code work, and that prod env vars are correct.

- Confirm `git rev-parse HEAD` matches the openemr Railway deployment's commit SHA.
- Confirm agent-service Railway deployment is on the latest agent-service commit (the one with HL7/TIFF/DOCX/XLSX extractors + supervisor code).
- On the **agent-service Railway env vars** page, confirm or set:
  - `ANTHROPIC_API_KEY` (already required)
  - `OPENAI_API_KEY` — **needed for the dense embedding path**. If absent, retriever silently degrades to BM25-only on prod.
  - `USE_SUPERVISOR=true` — set explicitly so the value is visible in the dashboard (also our rollback toggle).
- Smoke-test current deployed `/api/agent/healthz` returns 200.

**Rollback for everything in this plan:** flip `USE_SUPERVISOR=false` on Railway → service restarts → `/api/agent/query` resumes v1 Orchestrator. ~30 second rollback, no redeploy needed.

---

## Phase 1 — Build `dense.pkl` (20 min)

The corpus retriever's dense path is gated on `data/corpus/dense.pkl`. Without it, we ship "BM25 only" and miss the grader's "truly sparse + dense" bar.

```bash
cd /Users/andynguyen/Desktop/OpenEMR/openemr/agent-service
uv run python -m clinical_copilot.corpus.index --rebuild
```

The CLI is at `agent-service/src/clinical_copilot/corpus/index.py:258-289`. It reads `OPENAI_API_KEY` from `os.environ` (loaded via `dotenv` in `config.py:38-39`), embeds 58 corpus chunks with `text-embedding-3-small` (1536 dims), and writes `data/corpus/dense.pkl` (`(list[ChunkRecord], np.ndarray(N, 1536))`, ~360 KB on disk). Cost: ~$0.0001. Time: 5-10 sec.

**Ship mode:** commit `dense.pkl` to git (override `agent-service/.gitignore:25-27` for this single file). 360 KB binary in the repo is fine; the alternative — building at image-startup — adds boot-time complexity and a hard `OPENAI_API_KEY` dependency at deploy time. Edit `.gitignore` to add a negation:

```gitignore
data/corpus/
!data/corpus/bm25.pkl
!data/corpus/dense.pkl
!data/corpus/manifest.json
```

`bm25.pkl` is already shipped via the same pattern. Commit message: `feat(copilot): ship dense.pkl so prod retriever runs hybrid BM25+dense+rerank`.

---

## Phase 2 — Wire Supervisor into `/api/agent/query` (2h)

Five sub-steps. All in `agent-service/`. Critical path.

### 2.1 — Settings flag (10 min)

Edit `agent-service/src/clinical_copilot/config.py`:
- Add field `use_supervisor: bool` to the frozen `Settings` dataclass (after `internal_token`, ~line 89).
- In `_load()`, parse `USE_SUPERVISOR` env var with default `"true"`:
  ```python
  use_supervisor = _optional("USE_SUPERVISOR", "true").lower() in ("true", "1", "yes")
  ```
- Pass `use_supervisor=use_supervisor` to `Settings(...)` constructor (~line 130).

### 2.2 — Wire workers into `AppState` (40 min)

Edit `agent-service/src/clinical_copilot/app_state.py`:

- Add four `| None` fields to `AppState` (lines 109-138):
  - `supervisor_anthropic: Anthropic | None = None`
  - `supervisor_intake_extractor: IntakeExtractorFn | None = None`
  - `supervisor_evidence_retriever: EvidenceRetrieverFn | None = None`
  - `supervisor_model: str | None = None`
- In `build_app_state()`, after the existing `client = Anthropic(...)` (line 278), construct:
  - `CorpusRetriever()` inside `try: except FileNotFoundError:` (graceful degrade if pickle missing)
  - `evidence_partial` lambda that calls `run_evidence_retriever(retriever=corpus_retriever, rerank_client=client, rerank_model=settings.model_fast, **kwargs).to_tool_result()`
  - `intake_partial` lambda that calls `run_intake_extractor(client=client, model=settings.model_slow, **kwargs).to_tool_result()`
- Pass all four through to the `AppState(...)` constructor (~line 306). Test/fixture path leaves them `None`.

**Wiring complication noted:** `run_intake_extractor` requires a `document_path` from the model's tool_use input. On a chat query the model has no path to invent, so this worker will rarely fire usefully. Acceptable per locked plan ("chart-data questions abstaining is acceptable"); document with one inline comment. Bridging to `facts_store.read()` is a follow-on change.

### 2.3 — Branch the route + adapter (40 min)

Edit `agent-service/src/clinical_copilot/main.py`:

- Add imports near existing `from clinical_copilot.orchestrator...`:
  - `from clinical_copilot.orchestrator.supervisor import SupervisorResponse, run as supervisor_run`
  - `from clinical_copilot.orchestrator.schemas import CitedClaim`
  - `from clinical_copilot.verification.abstention import Abstention, AbstentionState`
- Add adapter helper `_supervisor_to_agent_response(sup, *, session_id)`:
  - If `sup.abstention_reason`: return `AgentResponse` with `Abstention(state=ABSTAINED, reason=...)`.
  - Otherwise walk `handoffs` for the first available citation source_id (`handoff.output["chunks"][0]["chunk_id"]` for evidence_retriever; `["citations"][0]["source_doc_id"]` for intake).
  - If no citation found OR no synthesized text: abstain with `NO_DATA` (cannot return prose with empty source_id — `CitedClaim.source_id` has `min_length=1`).
  - Otherwise return `AgentResponse(prose=[CitedClaim(text=sup.synthesized_text, source_id=anchor_source_id)], cards=[], tool_results=[], session_id=session_id)`.
  - **Note:** `AgentResponse` has `extra="forbid"` — do NOT add a top-level `handoffs` field. Handoffs stay structlog-only.
- Branch in `query_route` (line 282-290):
  ```python
  if (
      resolved_settings.use_supervisor
      and resolved_state.supervisor_anthropic is not None
      and resolved_state.supervisor_evidence_retriever is not None
      and body.lane == Lane.SLOW   # fast lane stays on v1
  ):
      try:
          sup = supervisor_run(
              client=resolved_state.supervisor_anthropic,
              model=resolved_state.supervisor_model,
              query=body.query,
              intake_extractor=resolved_state.supervisor_intake_extractor,
              evidence_retriever=resolved_state.supervisor_evidence_retriever,
              request_id=request_id,
          )
      except Exception as exc:
          get_logger(__name__).warning(
              "supervisor.fallback_to_v1", request_id=request_id,
              error=f"{type(exc).__name__}: {exc}",
          )
      else:
          return _supervisor_to_agent_response(sup, session_id=...)
  # Fallback / use_supervisor=false / fast lane:
  return resolved_state.orchestrator.run(...)
  ```
  Fast lane (≤5s p50 budget) skips supervisor — supervisor adds an extra Anthropic round-trip.

### 2.4 — Tests (30 min)

New file `agent-service/tests/integration/test_query_route_supervisor.py`. Five tests, pattern follows existing `tests/integration/test_supervisor.py` and `tests/unit/test_query_route.py`:

1. `test_query_route_uses_supervisor_when_flagged` — flag ON, mock Anthropic to script tool_use → text turns, assert `prose[0].text` contains synthesized text and structlog records `supervisor.handoff`.
2. `test_query_route_supervisor_off_uses_v1_orchestrator` — flag OFF, assert v1 orchestrator was called.
3. `test_query_route_evidence_only_query` — script one `dispatch_evidence_retriever` tool_use + final text; assert `evidence_partial` was called.
4. `test_query_route_supervisor_error_falls_back_to_v1` — supervisor raises; assert v1 orchestrator was used.
5. `test_query_route_fast_lane_skips_supervisor` — `lane=Lane.FAST` flag ON; assert supervisor never invoked.

Run locally: `cd agent-service && uv run pytest tests/integration/test_query_route_supervisor.py -v`. Must be green before deploy.

### 2.5 — Commit + push (5 min)

Two commits:
- `feat(copilot): wire Supervisor into /api/agent/query (W2-07)` — Settings flag, AppState wiring, route branch, adapter
- `test(copilot): integration coverage for supervisor on chat path` — the 5 tests

Push triggers `agent-service-pytest` + `agent-service-eval-gate` + `copilot-prod-push-confirm` (already restricted to pre-push only after `03fa14c0d`). Answer `y` at the prompt. Push lands on GitLab → openemr Railway service auto-redeploys (note: agent-service does NOT auto-redeploy from this push; phase 3 handles that).

---

## Phase 3 — Deploy + smoke-test live (30 min)

```bash
cd /Users/andynguyen/Desktop/OpenEMR/openemr/agent-service && railway up
```

Wait for `/healthz` 200. Then verify `/readyz` shows `corpus_retriever_loaded: true` (or check structlog for `corpus.retriever.dense_loaded` on startup).

Smoke test from terminal (use existing internal-token + JWT mint pattern):

```bash
# Query that should fire evidence_retriever:
curl -X POST $AGENT_URL/api/agent/query \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"query": "What screenings are recommended for a 55-year-old smoker?", "lane": "slow"}' \
  | jq .
```

Expected: `prose[0].text` non-empty with corpus-grounded answer; `prose[0].source_id` references a USPSTF/CDC chunk. Check Railway logs (or `railway logs --service agent-service`) for `supervisor.handoff` events with `worker=evidence_retriever`.

Also test from the OpenEMR chat UI directly to confirm end-to-end. If anything breaks, flip `USE_SUPERVISOR=false` in Railway and triage offline.

---

## Phase 4 — Multi-citation polish in adapter (optional, 30 min)

Single-citation anchor (Phase 2.3) is the minimum bar. If time permits, walk all handoff outputs and surface one `CitedClaim` per cited chunk:

- For each evidence_retriever handoff, iterate `handoff.output["chunks"]` and emit one `CitedClaim(text=chunk["text"][:200], source_id=chunk["chunk_id"])`.
- Place the synthesized `sup.synthesized_text` as a leading prose entry tied to the first citation; subsequent entries are excerpt+cite pairs.
- Existing chat UI already renders prose with citations (`interface/copilot/chat.php` + `chat-render.js`).

Cut this phase if running long; the locked plan considers single-citation acceptable for early submission.

---

## Phase 5 — Demo video (90 min)

Per `plans/week2-early-submission.md:159-167`, target 3-5 minutes. Adapt scene 6 to GitLab MR pipeline (your CI is GitLab, not GitHub Actions).

**Shot list:**
1. **Upload a lab PDF** in the chart UI (use one of the cohort-5 example PDFs); show the universal upload page → review page rendering extracted facts with citation hints (existing UI from `lab_review.php`).
2. **Confirm and attach** to the chart; show the chart's allergies/meds/problems/labs tabs updating with the extracted rows (chart-write path from commit `3146bd42a`).
3. **Open Co-Pilot chat**; ask: *"What does the recent lab say and what guidelines apply for a 55-year-old smoker?"*. Expected: supervisor dispatches both workers (or at minimum evidence_retriever); the response renders with corpus citations.
4. **Side terminal pane**: tail Railway logs for `supervisor.handoff` events to show the routing decisions in real time. (`railway logs --service agent-service | grep supervisor.handoff`)
5. **Eval gate demo**: in another terminal, `cd agent-service && make eval-extraction-gate` (cached path, fast). Show pass-rates per bucket. Optionally demonstrate regression detection by reverting one fix and re-running.
6. **GitLab MR pipeline**: show the most recent MR's `agent-service:test` + `agent-service:eval-gate` stages green in the GitLab CI/CD page. (Substituted from the original "GitHub Actions" line in the locked plan since CI is GitLab-only.)

**Recording tools:** Loom (fastest path) or QuickTime + YouTube unlisted. Loom auto-generates a sharable URL. Keep first cut tight; one retake max.

---

## Phase 6 — README update + final push (15 min)

Edit `README.md`:
- Update Week 2 section with deployed app URL (already `openemr-production-6c31.up.railway.app`).
- Add demo video link (Loom/YouTube unlisted URL from Phase 5).
- One-line note that `USE_SUPERVISOR` controls the chat engine and defaults ON for the grading window.

Commit `docs(copilot): demo video link + supervisor routing note`. Push. Auto-redeploys openemr (no agent-service rebuild needed since no agent-service code changed).

---

## Critical files

**Edit:**
- `agent-service/src/clinical_copilot/config.py` — `Settings.use_supervisor` flag
- `agent-service/src/clinical_copilot/app_state.py` — supervisor wiring fields + worker partials
- `agent-service/src/clinical_copilot/main.py` — `query_route` branch + `_supervisor_to_agent_response` adapter
- `agent-service/.gitignore` — un-ignore `data/corpus/dense.pkl`
- `README.md` — deployed URL + video link

**Create:**
- `agent-service/data/corpus/dense.pkl` — generated by Phase 1 build (~360 KB)
- `agent-service/tests/integration/test_query_route_supervisor.py` — 5 integration tests

**Reuse (no edits):**
- `agent-service/src/clinical_copilot/orchestrator/supervisor.py` — already implements `Supervisor.run()` + handoff structlog
- `agent-service/src/clinical_copilot/orchestrator/workers/intake_extractor.py` — `run_intake_extractor()`
- `agent-service/src/clinical_copilot/orchestrator/workers/evidence_retriever.py` — `run_evidence_retriever()` accepts `rerank_client`
- `agent-service/src/clinical_copilot/corpus/retriever.py` — `CorpusRetriever` (BM25+dense fusion via RRF)
- `agent-service/src/clinical_copilot/corpus/rerank.py` — `rerank_with_llm()`
- `agent-service/src/clinical_copilot/corpus/index.py` — `--rebuild` CLI for Phase 1
- `agent-service/src/clinical_copilot/corpus/embedder.py` — `OpenAIEmbedder` (1536-dim, reads `OPENAI_API_KEY`)

---

## Verification

**End-to-end checklist** before recording the demo video:

1. `cd agent-service && uv run pytest -q` → 565+ passed (existing) + 5 new integration tests passing.
2. Deployed `agent-service` health: `curl $AGENT_URL/healthz` → 200.
3. Deployed query exercises supervisor:
   ```
   curl -X POST $AGENT_URL/api/agent/query \
     -H "Authorization: Bearer $JWT" \
     -d '{"query":"What screenings for a 55yo smoker?","lane":"slow"}' \
     | jq '.prose[0]'
   ```
   Returns prose with non-empty `source_id` referencing a corpus chunk.
4. Railway `agent-service` logs show `supervisor.handoff` rows with `worker=evidence_retriever` for the same request_id.
5. Deployed openemr at `openemr-production-6c31.up.railway.app`: log in as admin, open Co-Pilot chat, ask the same question, see the answer render with citation chip.
6. `cd agent-service && make eval-extraction-gate` → all 50 cases pass, baseline regression check green.
7. GitLab CI/CD pipeline for the latest MR: `agent-service:test` + `agent-service:eval-gate` both green.

**Rubric callout coverage:**
- Supervisor routing observable → ✅ structlog handoff log + supervisor on `/api/agent/query`
- Hybrid retrieval truly sparse + dense + rerank → ✅ `dense.pkl` shipped, `rerank_client` wired
- Citations reliable and visible → ✅ adapter surfaces source_id; UI already renders citations (✓ Phase 4 polish if time)
- Eval gate 50+ grounded cases → ✅ already shipped, CI blocks on regression
- Polish physician workflow + clearly visible without inferring from repo → ✅ deployed app + 3-5 min demo video linked from README

---

## Risks / known gaps to call out in submission narrative

- **`intake_extractor` worker rarely fires on chat queries** (no `document_path` for the model to invent). Locked plan accepts this; bridge to saved facts is full-submission work.
- **Audit-log silent on supervisor branch** — by design (no PHI tools called). One-line comment in route.
- **Adapter surfaces single citation** unless Phase 4 lands. Locked plan accepts; multi-citation polish is a 30-min stretch.
- **Demo video scene 6 substitutes GitLab CI for the originally-planned GitHub Actions screenshot** — your repo's CI is GitLab-only.
- **`USE_SUPERVISOR=false` instant rollback** if anything regresses for testers during the grading window.
