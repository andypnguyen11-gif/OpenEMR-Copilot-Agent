# W2_ARCHITECTURE.md — Clinical Co-Pilot, Week 2

**Status:** Draft for Week 2 architecture defense
**Last updated:** 2026-05-04
**Companion to:** ARCHITECTURE.md (v1, Week 1) — *not a replacement*. Week 2
extends Week 1; v1 sections that aren't restated here remain in force.

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

Week 2 adds **multimodal document ingestion**, a **small multi-agent
graph**, **hybrid retrieval over a guideline corpus**, and an
**eval-gated pre-push hook** to the Week 1 Co-Pilot. Three things stayed
the same on purpose:

- **The verification trust story.** v1 §3's principle ("deterministic
  where possible, probabilistic only where necessary") expands to cover
  document-extracted facts, with a degraded-OCR path that prefers
  `LOW_CONFIDENCE` over silent rejection.
- **The trust boundary.** v1 §4's PHP-signs-JWT / Python-verifies model
  unchanged. The PHP gateway remains the only writer to OpenEMR's
  `documents` table; the Python service never reads OpenEMR's database
  directly.
- **The deployment shape.** Three Railway services + two managed
  databases (v1 §9). Week 2 adds a Postgres-backed extraction queue and
  pgvector for the corpus — both inside the existing `agent-db`. No new
  service, no Redis, no S3, no vector DB.

Three things changed structurally:

- **Single orchestrator → small multi-agent graph (LangGraph).**
  Four nodes: supervisor (with planner as an entry-point node),
  intake-extractor, evidence-retriever, and critic. Built on
  **LangGraph** (`langgraph>=0.2,<0.3`); the Week 2 rubric §4 names
  LangGraph as a permitted choice and its StateGraph +
  conditional-edge primitives match the topology exactly. We use
  LangGraph minimally — graph + nodes only, no LangChain agents or
  ReAct loops. Every node is a logged span; nodes cannot call other
  nodes (Appendix A.5). *(Rubric note: §4 lists the critic as
  extension; we ship it as core because the action-suggestion
  blacklist is load-bearing safety per v3 §5, not optional polish.
  PRD2 §5 documents the rationale; the rest of these docs treat the
  critic as core.)*
- **Tool-only retrieval → tool + RAG retrieval, with a hard
  separation.** Patient facts are tool-mediated only. Guideline corpus
  is RAG-only. There is no vector index over patient data; this is
  enforced structurally, not procedurally (§10).
- **Document subsystem becomes a fact source.** OpenEMR's existing
  Documents UI + `documents` table + FHIR `DocumentReference` /
  `Binary` endpoints are reused as-is; a new category, a Symfony
  post-upload event, and an extraction worker convert lab PDFs and
  intake forms into structured facts that flow through the same
  verification middleware as Week 1's structured-data claims.

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
3. **Cross-encoder rerank.** `cross-encoder/ms-marco-MiniLM-L-6-v2`
   (loaded once at process start, ~80 MB) reranks the union to
   top-K=5.

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
    ├── cases.jsonl         # 50 cases, line-per-case
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

1. Loading all 50 cases.
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
- `copilot-eval-full` (stage `pre-push`) — runs the full 50-case
  suite per Appendix A.2.

Both target `make copilot-eval` with different flags. The fast
hook keeps the per-commit loop tight; the push hook is the gate.

---

## 10. Tool-vs-RAG Boundary Enforcement

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
  measurements.** These are in `COST.md` (Week 2 section).
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
| *"Trying to support five document types before two work reliably."* | PRD2 §12 non-goals: "More than two document schemas in MVP." Stretch types (referral fax, med list) are explicitly backlog (TASKS2 W2-R1). |
| *"Using a VLM answer directly without schema validation or source metadata."* | PRD2 §6 schema contract — `ExtractedField[T]` with `value_xor_abstain` validator; W2_ARCH §5.2 — VLM call uses Anthropic structured output with `tool_choice` set to the schema tool, so out-of-schema fields fail at the SDK boundary. |
| *"Letting the supervisor become a black box. Handoffs must be logged and explainable."* | PRD2 §5 handoff logging; W2_ARCH §4 — every LangGraph node emits a span with `parent_run_id`; agent audit table holds a row per node for durability beyond LangSmith retention. |
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
