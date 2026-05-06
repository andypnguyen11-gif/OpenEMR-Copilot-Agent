---
  Clinical Co-Pilot — Product Requirements Document (Week 2)

  Status: Working PRD — extends PRD.md (v3); feeds W2_ARCHITECTURE.md, TASKS.md
  Last updated: 2026-05-06
  Owner: [you]

  Relationship to v3 PRD:
  - PRD.md (v3) remains the source of truth for the Week 1 scope: structured-data
    retrieval, citation middleware, RBAC, two-lane architecture, four use cases.
    Nothing in v3 is rescinded here.
  - This document specifies the Week 2 *additions* — multimodal document
    ingestion, multi-agent decomposition, hybrid-RAG over a guideline corpus,
    and a local pre-push eval gate that blocks regressions before they reach
    the remote. Where Week 2 contradicts a v3 decision (e.g. v3 listed
    multi-agent in Non-Goals), the contradiction is called out explicitly in
    §13.

  ---
  Status as of 2026-05-06 — what shipped vs what is deferred.

  This PRD is the design target. The Week 2 demo ships a focused subset
  end-to-end; the remaining items are tracked in TASKS2.md as deferred MRs.
  When a section below describes a deferred surface, this status block is the
  signal — read the section as design intent, not deployed behaviour.
  `agent-service/README.md § Week 2 — Multimodal demo` carries the canonical
  shipped/deferred matrix; this is the product-facing summary.

  Shipped:
  - **Vision extraction** of `lab_pdf` and `intake_form` documents through a
    direct multipart route on the agent service
    (`POST /api/agent/internal/ingest`). One Anthropic vision call per
    document, structured-output via the Pydantic schema, single citation per
    row / section, persisted as JSON under `data/extracted/<id>.json`.
  - **Chart-side entry points** for the AI flow: an "Upload lab document
    (AI extract)" button on the patient summary's Labs panel
    (`interface/patient_file/summary/labdata_fragment.php`) and a parallel
    button on the in-chart Co-Pilot side panel; an "Add Patient (with AI)"
    item in the Patient menu for front-desk intake.
  - **Page-driven review and save** (`interface/copilot/{upload_lab,
    lab_review,lab_save_ai,new_patient_with_ai,intake_review,
    new_patient_save_ai}.php`). Clinician confirms / edits the extracted
    facts before they write to `procedure_result` (labs) or `patient_data`
    + lists tables (intake).
  - **Hybrid corpus retriever** under `agent-service/corpus/sources/`: 11
    Markdown excerpts adapted from USPSTF / CDC / NIH / AHA public guidance
    (license basis in `corpus/sources/LICENSES.md`), chunked sentence-window
    (3/1), BM25-only retrieval (`rank-bm25`), exposed via
    `clinical_copilot.scripts.retrieve_evidence` CLI.
  - **Extraction eval suite**: 10 boolean-rubric cases under
    `agent-service/tests/eval/w2_cases/{extraction-lab,extraction-intake}/`
    (5 lab + 5 intake), runnable via
    `python -m tests.eval.extraction_runner`. Boolean rubrics only.
  - **Fast-lane `get_labs`**: side-panel chat now resolves "what are the
    recent labs" without abstaining (was NO_DATA before — tool was outside
    the fast-lane subset).

  Deferred (design captured here, MR not yet landed — see TASKS2.md):
  - **Documents-subsystem post-upload event hook** (PRD §2 / §2.1, MR
    W2-02). Replaced for the demo by the chart-side page-driven flow above.
    The category-as-boundary discussion in §2 still applies once the
    listener lands; until then, only documents uploaded *through the AI
    pages* reach the extractor.
  - **Documents-view side panel + chart summary card** (§2 / §3 / §3.4 of
    W2_ARCHITECTURE; MR W2-10). Not built — clinician review happens on
    the dedicated `lab_review.php` / `intake_review.php` pages instead of
    a side panel polling `GET /agent/documents/{id}`.
  - **LangGraph multi-agent graph** (planner / supervisor / critic nodes —
    PRD §5 / §5.1 / §5.2; MR W2-07). Not built — Week 2 ships on top of
    the v1 single-loop orchestrator (`orchestrator/agent.py` +
    `lanes.py`). LangGraph is not a dependency. The chart-tools / corpus
    boundary is preserved structurally by package layout
    (`tools/` vs `corpus/`), not by an `import-linter` contract.
  - **OCR strict + degraded path for citations** (§6 verification step 2,
    §8.2; MR W2-05). The demo uses VLM-emitted confidence only:
    `confidence < 0.7` → `LOW_CONFIDENCE` abstain. No Tesseract pass.
    `CITATION_INVALID` is in the enum but unreachable until the OCR check
    lands.
  - **Dense + cross-encoder rerank** for the corpus (§7 retrieval pipeline,
    MR W2-06). Today the retriever is BM25-only over 11 sources. The
    public surface (`retrieve(query, k)`) matches the planned hybrid
    shape, so the swap is internal.
  - **Eval gate beyond extraction**: only `extraction-lab` and
    `extraction-intake` buckets exist (10 cases, not 50). No
    `reconciliation` / `retrieval` / `citation-separation` / `rbac` /
    `abstention` buckets, no judge-evaluated rubrics, no budget pre-flight.
    The W2-related pre-push hook is still pytest-only
    (`agent-service-pytest`) — not the `make copilot-eval` gate of §8.
    `make eval` runs the v1 Q&A suite + extraction runner against a
    deployed agent, not a 50-case golden suite.
  - **PHI redaction layer** for LangSmith spans (§9; MR W2-12). Demo path
    runs with `LANGSMITH_TRACING=false`; the redaction layer is not yet
    wired. Re-enabling LangSmith requires that MR.
  - **`extraction_jobs` / `extracted_facts` Postgres tables** (§2.1, §10).
    Facts persist as JSON files; no Postgres job queue, no
    `SELECT … FOR UPDATE SKIP LOCKED` worker. The agent service has no
    background worker process — extraction is synchronous on the ingest
    request.

  Conflict-resolution rule for this status block. Where this block and a
  later section disagree (e.g. §5 describes a four-node graph; this block
  says it isn't built), this block is what is *deployed today*; the later
  section is what the deferred MR will deliver. Appendix A still binds
  whatever ships — fewer surfaces today doesn't relax the contracts on
  the surfaces that *do* exist.

  Normative decisions appendix. Where reviewers flagged ambiguity (eval
  fail-fast vs. flake tolerance, abstention enum membership, eval runner
  platform, citation validator threshold), the binding answer is in
  **Appendix A — Normative Decisions**. The body of this PRD describes
  intent; Appendix A is the contract, and conflicts resolve to Appendix A.

  ---
  1. Product Overview (Week 2 delta)

  The Week 1 Co-Pilot reads the chart. The Week 2 Co-Pilot reads the *documents
  attached to the chart.*

  The new scenario. A primary care physician is prepping for a follow-up. The
  structured chart is up to date — but the most relevant recent information lives
  inside a scanned lab PDF and a patient-completed intake form uploaded by the
  front desk. The clinician asks: *what changed, what should I pay attention to,
  and what evidence supports the recommendation?*

  Week 2 adds three capabilities to make that scenario answerable:

  1. **See documents.** Ingest a lab PDF and an intake form, extract structured
     facts (Observation values, allergies, current meds, chief complaint) with
     per-field citations back to a page + bounding region in the source document.
  2. **Route work.** A small multi-agent graph (one supervisor, two workers, one
     critic) decomposes the request: extract from documents → retrieve guideline
     evidence → critique uncited or unsafe claims → return a grounded synthesis.
  3. **Gate quality.** A 50-case golden eval suite with boolean rubrics runs in a
     local pre-push git hook and blocks the push when correctness, citation rate,
     or RBAC behavior regresses (per the rubric's "Git Hook or equivalent"
     allowance — see §8 and Appendix A.2).

  MVP Goal. The deployed Week 1 demo, plus: upload a lab PDF and intake form
  through OpenEMR's existing Documents UI, see the agent extract structured facts
  with click-to-source citations, see retrieved guideline snippets cited
  separately from chart facts, and see the eval gate prevent merging when
  rubrics drop.

  Non-Goal (still). General-purpose clinical chatbot. Diagnostic or treatment
  recommendation. Patient-facing surface. Generation of orders or chart edits.

  Why this matters. Real clinical inputs are messy: scanned PDFs, faxed forms,
  smartphone photos of intake sheets. A Co-Pilot that only reads structured data
  describes a sanitized world the clinician doesn't actually work in. Multimodal
  ingestion is the difference between "demo on a clean panel" and "useful on
  Monday morning."

  ---
  2. Build on what OpenEMR already has

  > **Status (2026-05-06).** The design below — Documents subsystem reuse,
  > new category, post-upload Symfony event, Documents-view side panel —
  > is the planned production path (MR W2-02 + W2-10) and has not landed.
  > The deployed Week 2 demo bypasses the category gate and the listener
  > entirely: clinicians enter the AI flow from chart-side buttons
  > (`labdata_fragment.php` lab panel, Co-Pilot side panel, Patient menu),
  > the PHP page POSTs the binary directly to
  > `POST /api/agent/internal/ingest` on the agent service, and the
  > clinician confirms / edits the extracted facts on `lab_review.php` /
  > `intake_review.php` before any write to `procedure_result` or
  > `patient_data`. The "containment boundary" today is *the entry point
  > itself* (only the AI pages reach the extractor), not a category on
  > the documents table.

  OpenEMR ships a mature document subsystem. **The Week 2 build does not
  reimplement upload, storage, categorization, or access control.** Reuse, do
  not rebuild:

  ┌───────────────────────────────┬──────────────────────────────────────────────┐
  │ Existing OpenEMR component    │ Role in Week 2                               │
  ├───────────────────────────────┼──────────────────────────────────────────────┤
  │ Documents UI (Smarty +        │ User-facing upload surface. No new uploader  │
  │ Dropzone.js, general_upload   │ widget; we add a category + a post-upload    │
  │ template)                     │ hook, nothing more.                          │
  ├───────────────────────────────┼──────────────────────────────────────────────┤
  │ documents table + filesystem  │ System of record for the source blob. The    │
  │ blob storage; categories /    │ agent stores extracted facts in its own      │
  │ categories_to_documents       │ Postgres, but always references back to the  │
  │ tables                        │ document_id + storage path here.             │
  ├───────────────────────────────┼──────────────────────────────────────────────┤
  │ Document.class.php +          │ The Python sidecar fetches binaries via the  │
  │ DocumentService               │ FHIR DocumentReference / Binary endpoints,   │
  │ (src/Services/                │ not by reading the filesystem directly. ACL  │
  │ DocumentService.php)          │ stays at the OpenEMR boundary.               │
  ├───────────────────────────────┼──────────────────────────────────────────────┤
  │ FHIR DocumentReference (and   │ Read API for the agent. List, fetch          │
  │ Binary) endpoints — already   │ metadata, download content. The signed JWT   │
  │ wired in                      │ from §6 of v3 PRD scopes access to the       │
  │ src/RestControllers/FHIR/     │ patient bound to the session.                │
  │ FhirDocumentReferenceRest…    │                                              │
  ├───────────────────────────────┼──────────────────────────────────────────────┤
  │ POST /api/patient/{pid}/      │ Programmatic upload path used by eval        │
  │ document REST endpoint        │ fixtures + integration tests.                │
  └───────────────────────────────┴──────────────────────────────────────────────┘

  What we add (and only this):

  - A new document **category** ("Co-Pilot — Source Documents") that gates which
    uploads are eligible for ingestion. Documents in any other category are
    invisible to the extraction pipeline. This is a containment boundary: a
    historical chart with thousands of documents does not implicitly become
    training context for the agent.
  - A **post-upload hook** (Symfony EventDispatcher, consistent with existing
    OpenEMR patterns) that enqueues an extraction job in the agent service when
    a document lands in that category.
  - A **lightweight side panel** in the Documents view that renders extraction
    state: queued / extracting / extracted / abstained / failed, plus a link to
    the structured fact view and a click-to-source preview. **This is the
    primary Week 2 UI surface for extraction state.** The chart side panel
    referenced in §3 is a *secondary, summary-only* surface — it shows a
    rollup card ("3 documents extracted, 1 abstained") and links into the
    Documents view for detail. No extraction state exists in two places of
    record; the Documents view is canonical.

  Why not a custom uploader? Three reasons. (1) Reusing the existing widget keeps
  RBAC, categories, and audit-log entries on the Documents subsystem we already
  trust. (2) The grading rubric values narrowness; building a parallel uploader
  is a five-day rabbit hole that yields nothing the existing one doesn't already
  do. (3) The Week 1 PRD already deferred "Document and imaging integration" as
  a non-goal — Week 2 lifts that constraint by *integrating with* the existing
  subsystem rather than building new chrome around it.

  ### 2.1 End-to-end ingestion flow

  Sequence from upload to rendered fact, with ownership boundaries explicit
  (each column is owned by exactly one component):

  ```
  User    Documents UI      PHP Gateway       documents      Event /        Extraction   Verification    Agent-DB
   │      (Smarty +         (OpenEMR fork)    table +        Symfony        worker        (citation +     (Postgres)
   │       Dropzone)                          filesystem     dispatcher    (Python)      domain rules)
   │           │                  │                │              │             │              │              │
   ├─upload───▶│                  │                │              │             │             │              │
   │           ├─POST /api/───────▶                │              │             │             │              │
   │           │  patient/{pid}/ │                │              │             │             │              │
   │           │  document       │                │              │             │             │              │
   │           │                  ├─insert blob ──▶              │             │             │              │
   │           │                  ├─dispatch CoPilotDocumentUploaded ─▶        │             │              │
   │           │                  │                │              ├─enqueue ──▶│             │              │
   │           │◀─200 OK ─────────│                │              │             │             │              │
   │           │                  │                │              │             │             │              │
   │           │                  │                │              │             ├─FHIR fetch ─┘              │
   │           │                  │                │              │             │  Binary                    │
   │           │                  │                │              │             ├─Anthropic VLM call          │
   │           │                  │                │              │             ├─schema validate             │
   │           │                  │                │              │             ├──────────────▶              │
   │           │                  │                │              │             │              ├─OCR cite chk│
   │           │                  │                │              │             │              ├─domain rules│
   │           │                  │                │              │             │              ├─store facts ▶
   │           │                  │                │              │             │              │              │
   ├─poll state─▶                  │                │              │             │             │              │
   │           ├─GET /agent/      ▶                │              │             │             │              │
   │           │  documents/{id} │                │              │             │             │              │
   │           │                  ├─read state ─────────────────────────────────────────────────────────────▶│
   │           │◀─{state, facts, citations}                       │             │             │              │
   ◀─render────│                  │                │              │             │             │              │
  ```

  Ownership boundaries the diagram makes binding:

  - The **PHP Gateway** is the *only* writer to `documents`/filesystem. The
    Python service never writes to OpenEMR's database.
  - The **Extraction worker** is the only caller of the VLM. Supervisor
    cannot bypass it to call Anthropic vision directly.
  - The **Verification step** is the only writer of facts to Agent-DB. An
    extraction that fails verification produces an audit row, not a fact row.
  - The **UI poll path** reads from Agent-DB only; it does not re-fetch the
    binary. The binary is fetched once, on the extraction worker's pass.

  ---
  3. Target User & Scenario

  Same primary user as v3 §2 (cross-coverage primary care physician). Week 2
  introduces a third moment in the day:

  ┌──────────────────────────┬──────────────────────────────────────────────────┐
  │ Moment                   │ What's different in Week 2                       │
  ├──────────────────────────┼──────────────────────────────────────────────────┤
  │ Pre-clinic (slow lane)   │ Daily Brief now includes "documents to review"   │
  │                          │ — extracted facts surfaced alongside structured  │
  │                          │ chart context.                                   │
  ├──────────────────────────┼──────────────────────────────────────────────────┤
  │ Between rooms (fast      │ Chart side panel shows a *summary rollup* of the │
  │ lane)                    │ documents subsystem state ("3 extracted, 1      │
  │                          │ abstained") served by GET /agent/documents/      │
  │                          │ summary?patient_id={pid} (counts-only response;  │
  │                          │ no per-field data) and deep-links to the         │
  │                          │ Documents view for detail. Secondary surface     │
  │                          │ only — see §2; endpoint contract in              │
  │                          │ W2_ARCHITECTURE §3.4.                            │
  ├──────────────────────────┼──────────────────────────────────────────────────┤
  │ Doc-anchored Q&A (NEW —  │ "What did the lab from 04/15 actually show?"     │
  │ either lane)             │ "What did the patient write under chief          │
  │                          │ complaint?" Answers cite the source page and    │
  │                          │ region, not just a record id.                    │
  └──────────────────────────┴──────────────────────────────────────────────────┘

  ---
  4. Use Cases (Week 2)

  Each use case must answer: *why an agent and not a static parser?*

  #: 5
  Use case: "Extract this scanned lab PDF into structured Observations."
  Lane: Slow (background extraction, foreground review)
  Why an agent?: Layout-variable scans don't fit a fixed template. The VLM reads
    label + value spatially, but the surrounding agent applies schema validation,
    domain plausibility, and abstain-on-low-confidence rules — a parser cannot.
  ────────────────────────────────────────
  #: 6
  Use case: "Pull facts from this patient intake form and tell me what's new
    relative to the chart."
  Lane: Slow
  Why an agent?: Cross-source reconciliation (extracted intake vs. existing
    chart) — same shape as v3 use case 3, now with a non-structured source on
    one side. The agent flags discrepancies; a parser produces two unreconciled
    lists.
  ────────────────────────────────────────
  #: 7
  Use case: "What does the guideline say about this patient's situation, and
    what evidence supports that?"
  Lane: Slow
  Why an agent?: Hybrid retrieval over a guideline corpus, then synthesis with
    explicit separation between *patient-record fact* and *guideline evidence.*
    Two retrieval surfaces, one synthesis, citation-required output.
  ────────────────────────────────────────
  #: 8
  Use case: "Show me where in the document this fact came from."
  Lane: Either
  Why an agent?: Click-to-source. Extracted facts carry document_id + page +
    bounding region. UI renders the source PDF/image with the region highlighted
    when the clinician clicks the fact. Closes the trust loop on extraction.

  Differentiating-feature thesis. Use case 6 (extract + reconcile against the
  chart) is the Week 2 analogue of v3's use case 3. The discrepancy engine v3
  built for structured-vs-structured comparison generalizes to
  extracted-vs-structured comparison; the value lives in the cross-source
  reasoning, not the extraction itself.

  ---
  5. Multi-Agent Architecture

  > **Status (2026-05-06).** The four-node LangGraph graph below is the
  > planned design for MR W2-07 and **has not landed**. LangGraph is not
  > a dependency of the deployed agent service today. Synthesis still
  > runs through the v1 single-loop orchestrator (`orchestrator/agent.py`
  > + `lanes.py`). The intake-extractor exists *only* as the async-side
  > extractor invoked by `POST /api/agent/internal/ingest`; it is not
  > yet a query-time worker. The evidence-retriever exists as a CLI
  > (`scripts/retrieve_evidence.py`) but is not yet wired into chat
  > responses — the synthesis path does not call the corpus today. The
  > planner / critic / handoff-logging story below describes how the
  > graph will compose once W2-07 ships; until then, the v1 verification
  > middleware remains the only post-draft gate.

  The graph is small on purpose. The grading rubric explicitly warns against
  the supervisor becoming a black box. Four nodes, every handoff logged:
  **supervisor + intake-extractor + evidence-retriever + critic**.

  Note on rubric framing. Week 2 rubric §4 lists the critic as
  "extension work, not core." We ship it as part of the core graph
  because the verification-and-safety story carries forward from
  v3 §5 — the action-suggestion blacklist + citation-type gate are
  load-bearing safety controls in our design, not optional polish.
  Disabling the critic would weaken the trust narrative that
  Week 1 was built around. We acknowledge the rubric's classification
  once here; the rest of this PRD treats the critic as core.

  Framework choice: **LangGraph** (`langgraph>=0.2,<0.3`). The rubric
  permits "LangGraph, the OpenAI Agents SDK, or another inspectable
  orchestration framework." LangGraph is the canonical pick — its
  StateGraph + node + conditional-edge model is exactly the topology
  this graph needs, and its `parent_run_id` linkage gives us the
  handoff explainability the rubric demands without bolt-on
  instrumentation.

  ```
                            ┌──────────────┐
              user query →  │  Supervisor  │  ← session JWT (patient_id-bound)
                            │  (StateGraph)│
                            └──────┬───────┘
                                   │ planner step (a node) →
                                   │ list[SubQuery]
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
    ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
    │ intake_extractor │  │ evidence_        │  │ chart_tools      │
    │ (LangGraph node) │  │ retriever        │  │ (LangGraph node, │
    │ — query-time read│  │ (LangGraph node) │  │ wraps v1 tools)  │
    │ of agent-db      │  │ hybrid RAG +     │  │                  │
    │                  │  │ rerank           │  │                  │
    └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘
             │                     │                     │
             └─────────────────────┼─────────────────────┘
                                   ▼
                            ┌──────────────┐
                            │ critic       │  EXTENSION (rubric §4):
                            │ (LangGraph   │  rejects uncited claims +
                            │ conditional  │  unsafe action suggestions
                            │ edge → retry │
                            │ or END)      │
                            └──────┬───────┘
                                   ▼
                            verification middleware (v3 §5)
                                   ▼
                                  UI
  ```

  Worker responsibilities:

  - **Supervisor** — receives the user query, chooses which workers to invoke
    (intake-extractor for "what does the document say", evidence-retriever for
    "what does the guideline say", chart tools for "what's in the record"),
    and stitches results into a single answer payload. Stateless across turns;
    chooses workers per turn, not per session.
  - **Intake-Extractor** — has two surfaces with different roles:
    1. **Async pipeline (off the user's hot path).** On a post-upload
       event, an extraction worker fetches the binary via FHIR
       DocumentReference / Binary, dispatches to the appropriate Pydantic
       schema (`lab_pdf` or `intake_form`), runs the VLM extraction call,
       validates the response against the schema, runs the citation OCR
       check + domain-rule pass, and persists facts to agent-db. This is
       where the VLM cost lives.
    2. **Query-time worker (on the user's hot path).** A pure read against
       `extracted_facts` in agent-db — no VLM call, no FHIR fetch. The
       supervisor invokes this worker when the planner emits a
       `DOC_FACT` sub-query.

    The split is a latency-budget realization, not an aesthetic choice:
    §10.1's hot-path budgets cannot tolerate a VLM call on the user's
    turn. See W2_ARCHITECTURE.md §3.2 (async pipeline) and §4.3
    (query-time worker) for the concrete module layout.
  - **Evidence-Retriever** — runs hybrid retrieval (BM25 + dense embedding)
    over a small guideline corpus, then a cross-encoder rerank, returning the
    top-K snippets with corpus citations. Patient-record-aware in its query
    rewriting (e.g. expand "this patient's lipid panel" → query terms drawn
    from the active record), not in the corpus itself — the corpus is generic.
  - **Critic** — gating, not generative. Sits as a LangGraph conditional
    edge between the workers' drafts and the verification middleware.
    Rejects any prose claim without a `source_id`, any extracted field
    with confidence below threshold, and any sentence pattern matching
    the action-suggestion blacklist ("recommend starting", "increase
    dose", "switch to"). On rejection: route back to supervisor with a
    structured rationale; supervisor either retries (max 1, §5.2) or
    abstains per the v3 abstention taxonomy. The full critic contract
    lives in §5.2 + Appendix A.6.

  Handoff logging. LangGraph emits a span per node execution with
  `run_id`, `parent_run_id`, node name, latency, and our own
  `decision` field appended. We also write a row to the agent audit
  table per node so the explainability surface the rubric demands is
  durable beyond LangSmith retention. *No node may call another node
  directly* — coordination flows through the StateGraph's edges, which
  the supervisor declares.

  Framework rationale. LangGraph's StateGraph is the right shape:
  nodes for the four core workers (planner-as-entry, intake-extractor,
  evidence-retriever, critic), conditional
  edges for the §5.2 critic loop, a typed state object that carries
  `sub_queries`, `drafts`, `retry_counts`, and the final response.
  We deliberately use LangGraph as a *small* framework — node
  functions are plain Python that read/write the state dict; we are
  not adopting LangChain agents, ReAct loops, or any framework
  features beyond the graph orchestrator itself. The `inspectable`
  bar the rubric sets is met because every node is a logged span and
  every edge is declared in code. See W2_ARCHITECTURE §4 for the
  StateGraph wiring.

  ### 5.1 Planner step (query decomposition)

  Composite asks ("what did the labs show, and what does the guideline say?")
  must not be dispatched to a single worker. The supervisor's first action on
  every turn is a **planner step** that decomposes the user query into a list
  of sub-queries, each tagged with its claim type and routed worker.

  Planner contract (binding):

  - Output shape: `list[SubQuery]` where each `SubQuery` carries
    `text`, `claim_type ∈ {chart_fact, doc_fact, guideline}`, and
    `target_worker ∈ {chart_tools, intake_extractor, evidence_retriever}`.
    The mapping `claim_type → target_worker` is fixed (not LLM-chosen) once
    the claim type is decided, so the LLM is choosing claim type, not
    routing.
  - Single-claim queries decompose to a 1-element list. The planner runs on
    every turn unconditionally — no fast path that skips it.
  - The planner is the *only* place claim type is assigned. Workers do not
    re-classify their own output; the critic checks against the
    planner-assigned claim type.
  - Planner output is logged as a single span (`planner.decompose`) with
    `{user_query, sub_queries[], rejected_alternatives[]}`. Reviewers can
    audit every routing decision from logs alone.
  - Critic-rejected sub-queries can be retried at most **once** (see §5.2);
    repeated rejection of the same sub-query forces a whole-answer abstain.

  Why this matters. Without a planner step, the supervisor either
  short-circuits to a single worker (losing the corpus citation) or
  invokes all workers eagerly (wasting tokens and inviting the critic
  to reject corpus-derived claims tagged as patient facts). Explicit
  decomposition makes the routing reviewable.

  ### 5.2 Critic contract (normative)

  Lifts the §5 critic description into a binding contract. Any
  ambiguity in this section resolves to Appendix A.6.

  Retry cap. **Max one retry per sub-query.** On critic rejection of a
  sub-query, the supervisor may retry the same sub-query exactly once
  with the rejection rationale appended to the worker prompt. A second
  rejection forces abstain on that sub-query. There is no "retry the
  whole turn" mechanic; retries are sub-query-scoped to keep latency
  bounded.

  Rejection granularity (lane-dependent, mirrors v3 §5):

  - **Slow lane (use cases 5–7):** sentence-level rejection. The critic
    can reject individual sentences; the supervisor renders the
    surviving sentences and marks rejected ones inline as
    `VERIFICATION_FAILED ("unverified — please check chart")`. A
    whole-answer abstain is forced **only** when one of:
    1. ≥50% of sentences are rejected (signal that the synthesis is
       structurally unsound).
    2. Any sentence triggers the *action-suggestion blacklist* — "start",
       "stop", "increase", "decrease", "switch to", "discontinue",
       "recommend [verb-ing]". These are P0 safety failures; one
       hit aborts the response.
    3. Any sub-query fails its retry (per the cap above).
    4. The planner-assigned claim type and the citation type disagree
       (chart claim citing `corpus_id`, or vice versa).

  - **Fast lane (use cases mapped from v3):** whole-answer abstain on
    *any* critic rejection. v3 §5 rationale carries: nuance is unread
    between rooms.

  Latency bound. The critic itself must run within 1.5s p95; if it
  times out, the response abstains with `VERIFICATION_FAILED` rather
  than rendering unchecked content. (This bound is referenced in
  §11.1.)

  Rejection logging. Every critic rejection emits a `critic.reject`
  span with `{sub_query_id, sentence_index, rejection_reason ∈
  {NO_CITATION, CITATION_TYPE_MISMATCH, ACTION_BLACKLIST,
  CONFIDENCE_FLOOR}}`. Eval rubrics assert the expected reason for
  adversarial cases, so the rejection-reason taxonomy is itself part
  of the contract.

  ### 5.3 Tool vs. RAG boundary (hard rule)

  This is the architectural rule that makes the whole verification
  story tractable. **Patient-fact retrieval is tool-mediated;
  external-corpus retrieval is RAG-mediated. The two never cross.**

  - Structured patient facts (meds, allergies, labs, problems, visits,
    notes, extracted document fields stored in agent-db) are reachable
    only via the chart-tools layer or the intake-extractor's stored
    output. RBAC is enforced at the tool layer per v3 §6 — every tool
    call verifies the JWT and re-checks scope before fetching.
  - Guideline / corpus retrieval is reachable only via the
    evidence-retriever's hybrid index (BM25 + dense + rerank) over the
    permitted-source corpus from §7. The corpus contains zero patient
    data and no embeddings of patient notes are ever computed.

  Concrete prohibitions, enforced as Appendix A.5 import-graph rules:

  - **No vector index over patient data.** Embedding patient notes for
    semantic search bypasses RBAC at retrieval time and is rejected as
    a design.
  - **No corpus chunk contains patient identifiers.** Curation step
    rejects any guideline document that happens to embed patient
    examples with identifying detail.
  - **No worker reads from both stores.** The intake-extractor reads
    documents (and writes to agent-db); the evidence-retriever reads
    the corpus. Cross-store reads happen only at the supervisor, after
    each store has emitted its citation.

  Why a hard rule. Architecture interviews and trust/safety reviews
  consistently push on the question "what stops a clinician's note
  from being retrieved by similarity to a guideline query?" The
  answer is structural, not procedural: there is no index to retrieve
  from. Procedural rules ("the agent shouldn't do this") are weaker
  than structural ones ("there is no path through which this could
  happen").

  ---
  6. Vision Extraction & Schemas

  Principle (extends v3 §5): *Deterministic where possible. Probabilistic
  only where necessary.* For documents, "deterministic" means the schema is
  the contract — the VLM can produce only what the schema describes, and
  every field carries provenance.

  Schema contract (Pydantic, mirrored to Zod for any TS surfaces). The
  `AbstainReason` enum is canonical — every member appears here, in the
  validator, and in eval rubrics. See Appendix A.1 for the full contract.

  ```python
  class AbstainReason(StrEnum):
      NO_DATA          = "NO_DATA"           # field empty in source
      LOW_CONFIDENCE   = "LOW_CONFIDENCE"    # VLM confidence below floor
      OUT_OF_SCHEMA    = "OUT_OF_SCHEMA"     # VLM emitted unknown field
      CITATION_INVALID = "CITATION_INVALID"  # OCR check disagrees with VLM
      TOOL_FAILURE     = "TOOL_FAILURE"      # transient infra failure

  class SourceCitation(BaseModel):
      document_id: str               # OpenEMR documents.id
      page: int                      # 1-indexed
      bbox: tuple[float, float, float, float]  # normalized 0..1
      confidence: float              # 0..1, model-emitted
      raw_text: str                  # the substring the model claims to have read

  class ExtractedField(BaseModel, Generic[T]):
      value: T | None                # None when abstained
      citation: SourceCitation | None
      abstain_reason: AbstainReason | None

      @model_validator(mode='after')
      def value_xor_abstain(self) -> Self:
          if self.value is not None and self.citation is None:
              raise ValueError("Extracted value without citation")
          if self.value is None and self.abstain_reason is None:
              raise ValueError("Missing value must carry an abstain_reason")
          return self
  ```

  Two document schemas for MVP:

  - `lab_pdf` — list of Observation-shaped entries (`code`, `display`, `value`,
    `unit`, `reference_range`, `effective_date`, `abnormal_flag`), each an
    `ExtractedField`.
  - `intake_form` — chief complaint, current meds (free text), reported
    allergies, social history flags, family history flags, pain scale.

  Validation tests verify (a) every field is `ExtractedField`-wrapped, (b) the
  `value_xor_abstain` invariant holds (a non-null value implies a non-null
  citation, and a null value implies a non-null `abstain_reason`),
  (c) round-tripping a known fixture
  through the schema preserves all fields. These tests are in
  `tests/Tests/Isolated/Common/CoPilot/Schemas/` so they run host-side without
  Docker.

  Verification of extracted facts (post-VLM, pre-display):

  1. **Schema validation.** Already covered above. *Shipped.*
  2. **Citation existence.** The cited bounding region exists on the cited
     page; we run a cheap OCR pass (Tesseract via the Python sidecar — yes,
     this is new infra, but it's batch-only, never on the hot path) and
     confirm the citation's `raw_text` is approximately present in that
     region. Mismatches → reject the field, do not display. *Deferred —
     MR W2-05; the strict + degraded path of §8.2 is the binding contract
     for that MR. The current ship has no Tesseract pass.*
  3. **Domain plausibility.** Lab values run through the same v3 §5 rules
     engine (value-sanity, unit checks, range checks). Implausible values
     are flagged, not silently dropped. *Inherited from v1; not extended
     for `intake_form` allergies/meds yet.*
  4. **Confidence floor.** Per-field confidence below 0.7 → abstain with
     `LOW_CONFIDENCE`, do not surface a guess. *Shipped — currently the
     only abstain gate on extracted fields.*

  Abstention rendering. The enum extends v3 §5's four-state enum; UX
  rendering for the four Week 2 additions is below. Canonical enum
  membership lives in Appendix A.1; this table is presentation only.

  ┌─────────────────────┬──────────────────────────────────────────────────────┐
  │ State               │ How it renders to the clinician                      │
  ├─────────────────────┼──────────────────────────────────────────────────────┤
  │ LOW_CONFIDENCE      │ "Could not read reliably — please verify in source"  │
  │                     │ + click-to-source link                               │
  ├─────────────────────┼──────────────────────────────────────────────────────┤
  │ OUT_OF_SCHEMA       │ Not surfaced to clinician at all; logged to          │
  │                     │ telemetry as a hallucination signal                  │
  ├─────────────────────┼──────────────────────────────────────────────────────┤
  │ CITATION_INVALID    │ "Extraction did not match source — please verify"    │
  │                     │ + click-to-source link                               │
  ├─────────────────────┼──────────────────────────────────────────────────────┤
  │ TOOL_FAILURE        │ Same as v3 §5: "Could not retrieve — retry?"         │
  └─────────────────────┴──────────────────────────────────────────────────────┘

  ---
  7. Hybrid RAG Over Guideline Corpus

  > **Status (2026-05-06).** The corpus is built and indexed; the
  > retriever is BM25-only. Today's corpus is **11 Markdown sources**
  > (~58 chunks) under `agent-service/corpus/sources/{uspstf,cdc,nih,
  > aha}/`, each a *synthetic excerpt adapted from public guidance* per
  > `corpus/sources/LICENSES.md` — not the canonical text and not for
  > clinical use. Dense embedding + cross-encoder rerank are deferred
  > (MR W2-06 proper); the public surface
  > `clinical_copilot.corpus.retriever.retrieve(query, k)` is shipped
  > and degrades cleanly to BM25-only when the dense artifacts aren't
  > present. The retriever is not yet wired into the chat synthesis
  > path — today it is reachable only via the
  > `clinical_copilot.scripts.retrieve_evidence` CLI.

  Corpus. A small (~200-document) curated set of ambulatory-medicine
  guideline excerpts. **Source content is restricted to material we have a
  documented permission basis for.** Stored as Markdown with YAML frontmatter
  (`source`, `version`, `url`, `license`, `retrieved_at`, `topic_tags`).
  Single corpus, single tenant — no per-clinic customization.

  Permitted-source list (initial):

  ┌─────────────────────────────┬────────────────────┬────────────────────────┐
  │ Source                      │ Usage basis        │ Notes                  │
  ├─────────────────────────────┼────────────────────┼────────────────────────┤
  │ USPSTF recommendations      │ Public domain      │ U.S. federal work      │
  │                             │ (17 USC §105)      │                        │
  ├─────────────────────────────┼────────────────────┼────────────────────────┤
  │ CDC clinical guidance       │ Public domain      │ U.S. federal work      │
  │                             │ (17 USC §105)      │                        │
  ├─────────────────────────────┼────────────────────┼────────────────────────┤
  │ NIH / NHLBI / NIDDK         │ Public domain      │ U.S. federal work;     │
  │ patient and clinician       │ (17 USC §105)      │ check per-document for │
  │ guides                      │                    │ third-party material   │
  ├─────────────────────────────┼────────────────────┼────────────────────────┤
  │ MedlinePlus (NLM)           │ Public domain      │ Verify per-page;       │
  │                             │ majority           │ image rights vary      │
  └─────────────────────────────┴────────────────────┴────────────────────────┘

  Explicitly excluded for MVP: AHA / ACC, ADA, IDSA, UpToDate, NEJM, society
  guidelines that are copyrighted, and any specialty-society guideline whose
  license is not explicitly compatible with redistribution-as-corpus. Those
  may appear later under a per-source license but are out of scope until the
  permission basis is documented in `corpus/LICENSES.md`.

  Versioning policy. Each corpus document is captured with its source URL,
  retrieval date, and source-published version (when available). Re-ingest is
  manual; the corpus does not auto-update. A document is removed if its
  upstream is retracted or its license terms change.

  Retrieval pipeline:

  1. **Query rewrite.** Patient-record-aware expansion. The supervisor passes
     active record context (problem list, current meds) to the
     evidence-retriever; the retriever rewrites "what does the guideline say"
     into a query that names the relevant condition or class. Logged.
  2. **Hybrid first stage.** Parallel BM25 (Tantivy or rank-bm25) + dense
     embedding search (text-embedding-3-small or equivalent), top 20 each,
     dedup by chunk id.
  3. **Cross-encoder rerank.** A small cross-encoder (e.g. ms-marco-MiniLM)
     reranks the union to top 5.
  4. **Snippet emission.** Each result carries `corpus_id`, `chunk_id`,
     `source_url`, `version`, `score`. The supervisor must cite by
     `corpus_id` + `chunk_id` for any prose grounded in retrieved text.

  Chunking. Sentence-window chunks (window 3 sentences, stride 1) at index
  time, with parent-document context preserved as metadata. Standard playbook;
  not novel.

  Why hybrid. BM25 captures medical-terminology-rare words (drug names,
  ICD codes) that dense retrieval can miss; dense captures paraphrase. The
  rerank fuses them. Each layer is independently testable.

  Patient-record vs. guideline citation separation. **This is a hard rule.**
  A claim of fact about *this patient* must cite a `source_id` that resolves
  to a record (Observation, MedicationStatement, document field). A claim of
  fact about *what is generally recommended* must cite a `corpus_id`. The
  critic agent rejects any sentence whose claim type and citation type
  disagree.

  ---
  8. Eval Gate (local pre-push hook)

  > **Status (2026-05-06).** The 50-case suite, the budget pre-flight,
  > the 3-of-3 unanimous judge, the quarantine ceiling, and the
  > pre-push `make copilot-eval` gate are all deferred (MR W2-11). What
  > exists today: 10 boolean-rubric cases (5 lab + 5 intake) under
  > `agent-service/tests/eval/w2_cases/`, runnable via
  > `python -m tests.eval.extraction_runner` (deterministic pass/fail
  > to stdout, optional `--csv-out`). Boolean rubrics only — same
  > spirit as §8 below, smaller scope. The pre-push hook today is
  > `agent-service-pytest` (unit + integration tests, no eval). The
  > existing `make eval` target runs the v1 Q&A suite + the extraction
  > runner against a deployed agent; it is not the 50-case golden set
  > of this section.

  This is the rubric's hard gate. *A working demo that cannot block
  regressions has not met the Week 2 standard.* Treated as P0.

  Dataset. 50 synthetic / demo cases, organized by use case:

  ┌────────────────┬───────┬───────────────────────────────────────────────────┐
  │ Bucket         │ Count │ What it tests                                     │
  ├────────────────┼───────┼───────────────────────────────────────────────────┤
  │ Extraction —   │ 12    │ Per-field correctness against ground-truth        │
  │ lab PDF        │       │ extractions; citation validity.                   │
  ├────────────────┼───────┼───────────────────────────────────────────────────┤
  │ Extraction —   │ 10    │ Same, intake forms.                               │
  │ intake form    │       │                                                   │
  ├────────────────┼───────┼───────────────────────────────────────────────────┤
  │ Reconciliation │ 8     │ Cross-source discrepancy detection (extracted     │
  │                │       │ vs. structured chart). Did the agent flag the     │
  │                │       │ seeded conflict?                                  │
  ├────────────────┼───────┼───────────────────────────────────────────────────┤
  │ Evidence       │ 8     │ Did the right guideline chunk surface? Did the    │
  │ retrieval      │       │ answer cite by `corpus_id` + `chunk_id`?          │
  ├────────────────┼───────┼───────────────────────────────────────────────────┤
  │ Citation       │ 6     │ Are patient-fact and guideline citations          │
  │ separation     │       │ correctly distinguished? (critic-gated)           │
  ├────────────────┼───────┼───────────────────────────────────────────────────┤
  │ RBAC /         │ 4     │ Can the agent see a document outside the          │
  │ document scope │       │ session-bound patient? (Must be 100% pass.)       │
  ├────────────────┼───────┼───────────────────────────────────────────────────┤
  │ Abstention     │ 2     │ Does the agent abstain when confidence is low or  │
  │                │       │ data is missing?                                  │
  └────────────────┴───────┴───────────────────────────────────────────────────┘

  Rubric design. **Boolean rubrics only.** Every rubric is a yes/no question
  whose evaluator can be deterministic (regex, schema match, citation lookup)
  or LLM-judged with a binary output. No 1–5 scales, no "quality scores."
  Failures must be actionable: a failing case names the rubric that failed.

  Examples of rubrics:

  - `extraction.field_present` — for each field in the ground-truth set, did
    the extraction emit a value (not abstain) and match within tolerance?
  - `extraction.citation_resolves` — does each extracted-field citation point
    to a region whose OCR text contains the cited `raw_text`?
  - `reconciliation.flag_raised` — for the seeded discrepancy, did the
    discrepancy engine emit a flag with the correct fact-pair?
  - `retrieval.guideline_in_top_k` — is the gold-labeled guideline chunk
    among the top 5 reranked results?
  - `citation.type_correct` — for each prose sentence, does the citation
    type (record vs. corpus) match the claim type?
  - `rbac.cross_patient_blocked` — does a query for a document under a
    different `patient_id` return UNAUTHORIZED + audit log entry?

  Eval gate. **No remote CI service for this repo.** Deploys are manual,
  consistent with the existing workflow. The rubric accepts "Git Hook or
  equivalent that runs the eval suite and blocks regressions" — we
  implement it as a local **pre-push git hook** (managed by `prek` /
  `pre-commit`) that calls `make copilot-eval` against the
  `agent-service/` package. The hook:

  - Pulls cached extraction / retrieval responses where deterministic.
  - Calls live Anthropic for VLM extraction. We do **not** claim
    determinism on multimodal calls — see §8.1 for the flake policy that
    governs how non-deterministic case outcomes are handled.
  - Caps live-model spend at $5 per run, enforced by a token-budget
    pre-flight gate (run is aborted with `BUDGET_EXCEEDED` if the projected
    spend exceeds the cap).
  - Emits a JSON results file + a Markdown summary written to
    `agent-service/src/clinical_copilot/evals/w2/results/<run_id>.md`.
    The summary is committed alongside the change for review on the MR
    by hand; there is no auto-comment bot.
  - **Exits non-zero (fail-fast) on rubric outcomes per Appendix A.2.**
    A non-zero exit blocks the `git push`. The contract is fail-fast on
    rubrics; the only retry mechanic applies to transient infra failures
    within a case, defined in §8.1.
  - Stores results in the agent's local Postgres for trend analysis.

  Bypass policy. The pre-push hook is bypassable via `git push --no-verify`
  but doing so for a Week 2 MR is a self-graded MR-template checkbox: any
  push made with `--no-verify` must be called out in the MR description
  with a reason. **For any bypassed push, reviewers must request the eval
  gate run artifact** (Markdown summary + JSON results from
  `agent-service/src/clinical_copilot/evals/w2/results/`) produced from a
  manual `make copilot-eval` run on the same commit before approving;
  if the developer cannot supply it, the bypass is refused. The reviewer
  (or the grader, in this case) can refuse a bypassed push for any reason.

  No baseline drift permitted on RBAC. Same rule as v3 §13: any RBAC
  failure is stop-ship, not a "mostly pass" category.

  ### 8.1 Live-VLM flake policy

  Multimodal calls are non-deterministic enough that a strict per-case
  pass/fail will produce false-negative MR blocks. Resolution:

  - **Per-case retry, not gate retry.** A case that returns
    `TOOL_FAILURE` (Anthropic 5xx, timeout, content-filter) is retried up
    to **2 additional times** with exponential backoff (1s, 4s) before
    being recorded as a rubric failure. Retries are visible in the eval
    log; they do not silently fix flakes.
  - **Tolerance bands per rubric class.** Boolean rubrics with
    deterministic evaluators (citation lookup, schema match, RBAC outcome)
    have *zero* tolerance — a fail is a fail. Rubrics whose evaluator
    invokes a judge model use a **3-of-3 unanimous** rule: the case is
    marked passing only if three independent judge calls agree. Disagreement
    among judges marks the case as `JUDGE_INCONCLUSIVE` and the case is
    quarantined (does not count toward pass/fail) until the next baseline
    refresh.
  - **Quarantine ceiling.** No more than 5% of cases (3 of 50) may be in
    `JUDGE_INCONCLUSIVE` state at the end of a run. Exceeding the ceiling
    fails the run with `QUARANTINE_OVERFLOW`.
  - **No two-strike merge gating.** A case that genuinely fails its
    rubric on the first run blocks the MR. We do not require "fail twice
    before block"; that policy was rejected as it gives one free
    regression per developer.

  ### 8.2 Citation-validity rule (formal)

  The §6 OCR citation check is formalized here so the eval `extraction.
  citation_resolves` rubric is reproducible. The rule has two paths: a
  **strict path** (high-confidence text comparison) and a **degraded
  path** (when OCR cannot speak for itself). Both produce one of three
  outcomes: `valid`, `LOW_CONFIDENCE` (degraded but plausible), or
  `CITATION_INVALID` (rejected).

  Strict path (default):

  1. Render the cited page region (`bbox`, normalized → pixel) at 300 DPI.
  2. Run Tesseract over the cropped region, with a margin of 5% of the
     bbox dimension on each side (forgiving small bbox errors).
  3. Compute `rapidfuzz.token_set_ratio` between the OCR output and the
     VLM-claimed `raw_text`, both lowercased and whitespace-normalized.
  4. **Threshold: 0.85.** ≥0.85 → `valid`; <0.85 falls through to the
     degraded path (does not auto-reject).

  Degraded path (when the strict path can't render a verdict):

  Triggered when (a) OCR returns empty output, (b) OCR returns fewer
  than 3 tokens, or (c) OCR confidence (Tesseract's per-word mean) is
  below 0.4. These conditions are common on scanned forms with
  handwriting, low-contrast prints, or low-resolution faxes — exactly
  the inputs we promise to handle.

  5. **Bbox plausibility check.** The cited bbox must (i) lie within the
     cited page bounds, (ii) have area ≥ 0.5% of the page (no zero-area
     citations), and (iii) be at most 60% of the page (an extracted
     field claiming the entire page as its source is structurally
     suspect).
  6. If the bbox plausibility check passes, mark the field
     `LOW_CONFIDENCE` rather than `CITATION_INVALID`. Render with the
     LOW_CONFIDENCE UX from §6 + click-to-source so the clinician can
     verify visually. If it fails, mark `CITATION_INVALID`.

  Eval-side tracking:

  - `citation.false_reject_rate` rubric — measured against a curated
    set of 10 hand-validated correct extractions (5 strict-path-clean,
    5 degraded-path candidates). False-reject rate target: ≤ 5%.
    Exceeding 5% triggers a documented threshold-revision decision per
    Appendix A.4.
  - `citation.degraded_path_rate` (informational, not gating) —
    fraction of fields that took the degraded path. A spike here
    typically means the document corpus shifted toward harder scans;
    informs whether to invest in pre-processing.

  The thresholds (0.85 token-set, 0.4 OCR confidence, 0.5%/60% area
  bounds) are starting points; changes require a documented decision in
  W2_ARCHITECTURE.md and a baseline-refresh run. The rule is
  centralized in `agent-service/src/clinical_copilot/verification/
  citation_check.py` so eval and runtime use one implementation.

  ---
  9. Observability & PHI Safety

  > **Status (2026-05-06).** The deny-by-default span filter, regex
  > PHI-signal detector, and `phi.span_redaction` rubric are deferred
  > (MR W2-12, test-first). The Week 2 demo runs with
  > `LANGSMITH_TRACING=false` so no extracted text reaches LangSmith;
  > re-enabling tracing requires that MR. v1's pseudonym-on-patient-id
  > and PHI-allowlist behaviour for chat-side spans is unchanged.

  Same LangSmith pipeline as v3, with two Week 2 reinforcements driven by
  the rubric's "Common Pitfalls" warning:

  - **Never log raw document text or screenshots to LangSmith.** Spans
    contain hashes (SHA-256 of the cited region), document_id, page, bbox.
    The actual extracted text and the document binary stay inside our
    own infrastructure. A redaction layer wraps every LangSmith call.
  - **Never log patient identifiers to LangSmith.** Replace with stable
    pseudonyms (HMAC of patient_id with an env-supplied salt). Reversible
    only inside our agent service for trace correlation; opaque to the
    SaaS observer.

  This is a fail-closed test in the eval suite: any case where a span
  contains a regex-detectable PHI signal (SSN-shape, MRN-shape, raw chart
  text) fails the run.

  ---
  10. Tech Stack — additions to v3 §8

  ┌────────────────────────┬──────────────────────────┬─────────────────────────┐
  │ Component              │ Choice                   │ Why                     │
  ├────────────────────────┼──────────────────────────┼─────────────────────────┤
  │ Orchestration          │ LangGraph                │ Rubric §4 names         │
  │ framework              │ (langgraph>=0.2,<0.3)    │ LangGraph as a          │
  │                        │                          │ permitted choice;       │
  │                        │                          │ StateGraph + conditional│
  │                        │                          │ edges fit the topology  │
  │                        │                          │ exactly. Used minimally │
  │                        │                          │ — graph + nodes only,   │
  │                        │                          │ no LangChain agents.    │
  ├────────────────────────┼──────────────────────────┼─────────────────────────┤
  │ VLM                    │ Anthropic Claude         │ Vision-capable, same    │
  │                        │ (Sonnet-tier candidate)  │ provider as v3, BAA-    │
  │                        │                          │ eligible. Same SDK call │
  │                        │                          │ shape — image content   │
  │                        │                          │ block. No new vendor.   │
  ├────────────────────────┼──────────────────────────┼─────────────────────────┤
  │ Embedding model        │ text-embedding-3-small   │ Cheap, good enough for  │
  │                        │ (or open-source          │ a 200-doc corpus.       │
  │                        │ alternative TBD)         │                         │
  ├────────────────────────┼──────────────────────────┼─────────────────────────┤
  │ Reranker               │ ms-marco-MiniLM-L-6-v2   │ Local cross-encoder, no │
  │                        │ (cross-encoder)          │ extra API.              │
  ├────────────────────────┼──────────────────────────┼─────────────────────────┤
  │ BM25                   │ rank-bm25 (Python)       │ Pure-Python, no service │
  │                        │                          │ to run.                 │
  ├────────────────────────┼──────────────────────────┼─────────────────────────┤
  │ OCR (citation check    │ Tesseract via            │ Batch-only, never on    │
  │ only)                  │ pytesseract              │ hot path. Used to       │
  │                        │                          │ validate VLM citations, │
  │                        │                          │ not to drive primary    │
  │                        │                          │ extraction.             │
  ├────────────────────────┼──────────────────────────┼─────────────────────────┤
  │ PDF rendering          │ pypdfium2                │ Pure-Python, no system  │
  │                        │                          │ deps for the bbox-      │
  │                        │                          │ overlay UI preview.     │
  ├────────────────────────┼──────────────────────────┼─────────────────────────┤
  │ Job queue              │ Postgres-backed          │ No new infra. Reuses    │
  │ (extraction)           │ FOR UPDATE SKIP LOCKED   │ agent-db. Redis still   │
  │                        │ work table               │ deferred per v3.        │
  ├────────────────────────┼──────────────────────────┼─────────────────────────┤
  │ Eval harness           │ Same Python harness as   │ Buckets + boolean       │
  │                        │ v3, extended             │ rubrics + hook exit     │
  │                        │                          │ codes                   │
  └────────────────────────┴──────────────────────────┴─────────────────────────┘

  Out of stack: LangChain agents / ReAct loops (we use LangGraph
  *only* for the StateGraph orchestrator — no agent abstractions),
  Redis, S3, a dedicated vector database service. The corpus is small
  enough to live in pgvector on the existing agent-db.

  ### 10.1 Latency budgets per stage

  v3 §4 set the two end-to-end lane budgets (fast lane <5s, slow lane
  10–20s). Week 2 keeps those bounds and adds **per-stage budgets** for
  the new pipeline so reviewers can score each stage independently and
  so a regression has one obvious owner. Numbers are p50 / p95 unless
  noted; "p95" is the binding number for success-criteria scoring.

  Asynchronous extraction pipeline (off the user's hot path — measured
  from upload event to fact-row write):

  ┌─────────────────────────────────┬──────────┬──────────┬───────────────────┐
  │ Stage                           │ p50      │ p95      │ Notes             │
  ├─────────────────────────────────┼──────────┼──────────┼───────────────────┤
  │ Upload event → queue enqueued   │ 100 ms   │ 500 ms   │ PHP gateway hop   │
  ├─────────────────────────────────┼──────────┼──────────┼───────────────────┤
  │ Queue lag → worker pickup       │ 2 s      │ 30 s     │ Cold-pool worst   │
  │                                 │          │          │ case, single      │
  │                                 │          │          │ Railway worker    │
  ├─────────────────────────────────┼──────────┼──────────┼───────────────────┤
  │ FHIR Binary fetch + page render │ 3 s      │ 10 s     │ pypdfium2; 300DPI │
  ├─────────────────────────────────┼──────────┼──────────┼───────────────────┤
  │ VLM extraction call (per doc)   │ 15 s     │ 45 s     │ Anthropic vision  │
  ├─────────────────────────────────┼──────────┼──────────┼───────────────────┤
  │ Schema validate + citation OCR  │ 2 s      │ 8 s      │ Per-field bbox    │
  │ check (per doc)                 │          │          │ × N fields        │
  ├─────────────────────────────────┼──────────┼──────────┼───────────────────┤
  │ Domain-rule + persist           │ 200 ms   │ 1 s      │ Local rules + DB  │
  ├─────────────────────────────────┼──────────┼──────────┼───────────────────┤
  │ **End-to-end: upload → ready** │ **25 s** │ **90 s** │ Side panel state  │
  │ **in side panel**               │          │          │ flips to          │
  │                                 │          │          │ "extracted"       │
  └─────────────────────────────────┴──────────┴──────────┴───────────────────┘

  Synthesis pipeline (on the user's hot path — measured from query to
  rendered answer; bounded by v3 lane budgets):

  ┌─────────────────────────────────┬──────────┬──────────┬───────────────────┐
  │ Stage                           │ p50      │ p95      │ Notes             │
  ├─────────────────────────────────┼──────────┼──────────┼───────────────────┤
  │ Planner (§5.1) decompose call   │ 600 ms   │ 1.5 s    │ Single Haiku call │
  ├─────────────────────────────────┼──────────┼──────────┼───────────────────┤
  │ Doc-fact lookup (already        │ 30 ms    │ 100 ms   │ Postgres read,    │
  │ extracted; agent-db read)       │          │          │ no LLM            │
  ├─────────────────────────────────┼──────────┼──────────┼───────────────────┤
  │ Hybrid retrieval + rerank       │ 800 ms   │ 2 s      │ BM25 + dense +    │
  │                                 │          │          │ cross-encoder     │
  ├─────────────────────────────────┼──────────┼──────────┼───────────────────┤
  │ Synthesis call (lane-dependent) │ 2 s /    │ 4 s /    │ Haiku fast lane / │
  │                                 │ 8 s      │ 18 s     │ Sonnet slow lane  │
  ├─────────────────────────────────┼──────────┼──────────┼───────────────────┤
  │ Critic pass (§5.2)              │ 400 ms   │ 1.5 s    │ Hard cap; exceeds │
  │                                 │          │          │ → abstain         │
  ├─────────────────────────────────┼──────────┼──────────┼───────────────────┤
  │ Verification middleware         │ 100 ms   │ 500 ms   │ v3 §5 unchanged   │
  ├─────────────────────────────────┼──────────┼──────────┼───────────────────┤
  │ **End-to-end fast lane**        │ **2 s**  │ **5 s**  │ Matches v3 §4     │
  ├─────────────────────────────────┼──────────┼──────────┼───────────────────┤
  │ **End-to-end slow lane**        │ **10 s** │ **20 s** │ Matches v3 §4     │
  └─────────────────────────────────┴──────────┴──────────┴───────────────────┘

  Binding behavior on budget overrun:

  - **Hot-path stages.** A stage exceeding its p95 hard cap aborts the
    response with `TOOL_FAILURE` rather than rendering partial unverified
    content. Critic and verification middleware are the most common
    sources; both are bounded.
  - **Async pipeline stages.** A stage exceeding its p95 cap does not
    abort — extraction is asynchronous — but it does flip the document's
    side-panel state to "slow" with the offending stage logged. Three
    consecutive slow runs page on-call (post-MVP).
  - **Eval-rubric tie-in.** A `latency.stage_p95` rubric class checks
    each stage against its budget on the eval suite. Budget regressions
    are eval-gate failures the same as correctness regressions (per
    Appendix A.2 clause 3 — rubric class regression > 5pp).

  Budgets are starting estimates. The first eval-gate run produces real
  numbers; if any p95 exceeds budget, either the stage is optimized or
  the budget is renegotiated in W2_ARCHITECTURE.md (with a written
  justification — budgets do not silently slip).

  ---
  11. Failure Modes (Week 2 additions)

  ┌────────────────────────────────┬────────────────────────────────────────────┐
  │ Failure                        │ Behavior                                   │
  ├────────────────────────────────┼────────────────────────────────────────────┤
  │ VLM extraction request fails / │ Job stays in `failed`, surfaced in side-   │
  │ rate-limited                   │ panel state with retry button. Never       │
  │                                │ falls back to "best guess" extraction.     │
  ├────────────────────────────────┼────────────────────────────────────────────┤
  │ Document is corrupt /          │ Extractor returns a structured failure;    │
  │ unreadable                     │ side panel shows "could not read this      │
  │                                │ document" with abstain_reason.             │
  ├────────────────────────────────┼────────────────────────────────────────────┤
  │ Citation OCR check disagrees   │ Field is rejected (CITATION_INVALID). The  │
  │ with VLM-claimed raw_text      │ rest of the schema's fields stand or fall  │
  │                                │ on their own checks.                       │
  ├────────────────────────────────┼────────────────────────────────────────────┤
  │ Critic rejects every claim     │ Supervisor abstains the whole answer with  │
  │                                │ VERIFICATION_FAILED; logs reasons.         │
  ├────────────────────────────────┼────────────────────────────────────────────┤
  │ Guideline corpus retrieval     │ Surface "no matching guideline" rather     │
  │ returns nothing relevant       │ than drafting unsupported prose. NO_DATA.  │
  ├────────────────────────────────┼────────────────────────────────────────────┤
  │ Eval gate sees a transient     │ Per-case retry up to 2× per §8.1. After    │
  │ Anthropic 5xx / timeout        │ that, the case is recorded as failing — no │
  │                                │ MR-level "second chance" retry.            │
  ├────────────────────────────────┼────────────────────────────────────────────┤
  │ Eval judge calls disagree      │ Case marked `JUDGE_INCONCLUSIVE` and        │
  │                                │ quarantined per §8.1; quarantine ceiling   │
  │                                │ of 5% of cases applies.                    │
  ├────────────────────────────────┼────────────────────────────────────────────┤
  │ User uploads a doc to wrong    │ Extraction never runs; the category gate   │
  │ category                       │ is the boundary. No silent re-routing.     │
  ├────────────────────────────────┼────────────────────────────────────────────┤
  │ User uploads PHI for a         │ Documents subsystem ACL refuses; not our   │
  │ different patient by mistake   │ failure mode. We trust the existing        │
  │                                │ boundary — but eval bucket "RBAC / doc     │
  │                                │ scope" verifies it from our side.          │
  └────────────────────────────────┴────────────────────────────────────────────┘

  ---
  12. Non-Goals (Week 2)

  Carries forward all v3 non-goals (§11) except where Week 2 explicitly
  promotes them. Lifted from v3 non-goals:

  - "Document and imaging integration" — **lifted** for two document types
    (lab PDF, intake form). All other document types remain out of scope.
  - "Multi-agent / specialist routing" — **partially lifted** to four nodes
    (supervisor + two workers + critic). No further decomposition.

  Newly explicit Week 2 non-goals:

  - More than two document schemas in MVP. Referral fax, medication list,
    imaging are stretch only.
  - Per-document fine-tuning. The VLM is used as-is; no domain adaptation.
  - User-facing eval dashboard. Eval results are pre-push hook run
    artifacts (committed Markdown summaries + JSON results); viewer is
    Phase 2.
  - Streaming extraction (still). Whole-document, whole-response.
  - Cross-document reasoning ("compare these two labs"). One document at
    a time for extraction; reconciliation happens against structured chart
    data only.
  - Patient-uploaded documents flowing into chart. Front-desk-uploaded only.
  - Modifying the OpenEMR Documents schema. Reuse, do not migrate.

  ---
  13. Risks & Audit-dependent Assumptions

  These are assumptions now. Audit confirms or kills them.

  1. **OpenEMR FHIR DocumentReference / Binary endpoints return raw blobs
     for our custom category** with the same RBAC enforcement as the UI. If
     not (e.g. CCD-export-only), we fall back to a custom PHP gateway
     endpoint that reuses `DocumentService::getFile()`.
  2. **The post-upload Symfony event hook fires reliably** for documents
     uploaded via both UI and REST. If only one path emits the event, the
     other is wrapped explicitly.
  3. **Anthropic Claude vision quality on scanned PDFs is good enough.**
     If the eval baseline shows extraction <80% field accuracy, consider
     pre-processing (deskew, contrast normalization) or a fallback to
     OCR-first → text-only LLM extraction. Decision lives in W2_ARCHITECTURE.
  4. **A 200-document guideline corpus produces useful retrieval.** If
     evidence-retrieval rubrics fail to clear 70%, the corpus needs
     curation, not bigger; this is a content problem.
  5. **Pre-push eval-gate cost stays under $5 per run.** If a single run
     spends more, either reduce the live-VLM bucket size or move to a
     cheaper model for the eval-only path. Documented in COST.md update.
  6. **Tesseract citation-checking has acceptable false-positive rate.**
     If too many valid citations get rejected by OCR mismatch, raise the
     similarity threshold or replace with a coarser bbox-presence check.
  7. **The category-as-boundary model holds.** A document mistakenly
     filed under our category does not contain a different patient's PHI
     (because the Documents subsystem ACL gates that upstream). Audit
     confirms.

  Each becomes a sentence in W2_ARCHITECTURE.md and a section in any audit
  follow-up.

  Conflict with v3. v3 §11 listed multi-agent and document integration as
  non-goals. Week 2 promotes both, scoped narrowly. The v3 reasoning ("not
  needed yet, adds verification complexity") is acknowledged and addressed:
  the four-node graph keeps every handoff logged and the critic agent
  preserves the verification trust story by gating citations.

  ---
  14. Success Criteria

  Week 2 succeeds if:

  - The deployed app accepts a lab PDF and an intake form via OpenEMR's
    existing Documents UI and renders extracted facts with click-to-source
    in the Documents-view side panel within the §10.1 async pipeline
    budget (p95 ≤ 90s end-to-end from upload event).
  - Every prose synthesis claim either carries a `source_id` (chart /
    extracted doc) or `corpus_id` (guideline), or is abstained per the
    extended taxonomy. The planner (§5.1) assigns claim type before
    routing; the critic (§5.2) rejects any sentence whose citation type
    disagrees with the planner-assigned claim type.
  - **Latency budgets per §10.1 are met** at p95 on the eval suite:
    extraction pipeline end-to-end ≤ 90s, fast-lane synthesis ≤ 5s,
    slow-lane synthesis ≤ 20s, critic ≤ 1.5s. Stage-level p95 budgets
    are tracked by the `latency.stage_p95` rubric class; budget
    regressions block pushes (Appendix A.2 clause 3).
  - **Tool-vs-RAG boundary holds** (§5.3): no patient data is indexed
    in the RAG corpus; no chart-fact claim cites a `corpus_id`. Eval
    bucket "citation-separation" enforces this.
  - The 50-case eval suite runs as a local pre-push git hook (rubric:
    "Git Hook or equivalent") and blocks pushes that fail. Committed
    eval results live alongside the change. Pass rate ≥90% overall,
    100% on RBAC, no rubric regressed >5pp from main-branch baseline.
    Fail-fast on rubric outcomes (see Appendix A.2).
  - LangSmith traces show every planner decompose, supervisor → worker
    handoff, and critic rejection with `parent_run_id` linkage; no PHI
    in spans (verified by the redaction eval bucket).
  - Cost report (updated COST.md): dev spend documented, projected
    production spend per active patient per day, p50/p95 latency per
    §10.1 stage with measured numbers, identified bottleneck.
  - W2_ARCHITECTURE.md exists and explains the worker graph, planner,
    critic contract, RAG design, eval gate, latency budgets, and
    tradeoffs without "we'll figure that out later."
  - 3–5 minute demo video shows: upload → extraction → side-panel
    citation → guideline-grounded answer → pre-push eval-gate run →
    trace.

  ---
  15. Submission Mapping

  Mapping the rubric deliverables to artifacts in this repo. All paths are
  relative to repo root and reflect the actual `agent-service/src/
  clinical_copilot/` layout already in this codebase.

  ┌──────────────────────────────┬──────────────────────────────────────────────┐
  │ Deliverable                  │ Repo artifact                                │
  ├──────────────────────────────┼──────────────────────────────────────────────┤
  │ Repository (Week 1 fork +    │ This repo, Week 2 branch. Hosted on GitLab.  │
  │ Week 2 changes)              │                                              │
  ├──────────────────────────────┼──────────────────────────────────────────────┤
  │ W2 Architecture Doc          │ ./W2_ARCHITECTURE.md (to be written from §5– │
  │                              │ §11 of this PRD).                            │
  ├──────────────────────────────┼──────────────────────────────────────────────┤
  │ Schemas (Pydantic + tests)   │ Runtime abstention enum (shared with v1):    │
  │                              │ agent-service/src/clinical_copilot/schemas/  │
  │                              │ abstain.py.                                  │
  │                              │ Document-specific schemas (W2):              │
  │                              │ agent-service/src/clinical_copilot/          │
  │                              │ documents/schemas/{lab_pdf.py,               │
  │                              │ intake_form.py, citation.py}.                │
  │                              │ Tests: agent-service/tests/unit/schemas/     │
  │                              │ + agent-service/tests/unit/documents/        │
  ├──────────────────────────────┼──────────────────────────────────────────────┤
  │ Eval Dataset (50 cases +     │ agent-service/src/clinical_copilot/evals/    │
  │ rubrics + judge config)      │ {harness.py, rubrics.py, judge.py,           │
  │                              │ budget.py, results.py} +                     │
  │                              │ agent-service/src/clinical_copilot/evals/w2/ │
  │                              │ {cases.jsonl, judge.yaml, fixtures/,         │
  │                              │ corpus_freeze/, results/} +                  │
  │                              │ agent-service/tests/eval/w2/                 │
  ├──────────────────────────────┼──────────────────────────────────────────────┤
  │ CI Evidence                  │ .pre-commit-config.yaml (prek-managed        │
  │ (rubric: "Git Hook or        │ pre-push hook running `make copilot-eval`)   │
  │ equivalent")                 │ + agent-service/Makefile target +            │
  │                              │ committed eval results in                    │
  │                              │ agent-service/src/clinical_copilot/evals/    │
  │                              │ w2/results/. No remote CI service.           │
  ├──────────────────────────────┼──────────────────────────────────────────────┤
  │ Demo Video                   │ Linked from README.md                        │
  ├──────────────────────────────┼──────────────────────────────────────────────┤
  │ Cost & Latency Report        │ COST.md (Week 2 section appended)            │
  ├──────────────────────────────┼──────────────────────────────────────────────┤
  │ Deployed Application         │ Railway URL in README.md                     │
  └──────────────────────────────┴──────────────────────────────────────────────┘

  TASKS.md is updated with a Week 2 block; each MR in that block lists the
  test files it must include (per CLAUDE.md test policy).

  ### 15.1 Test matrix per MR

  > **Status (2026-05-06).** The shipped W2 demo covers MRs W2-01
  > (schemas + abstain enum), W2-03 (lab_pdf VLM extractor) and W2-04
  > (intake_form extractor). MR W2-06 ships the corpus + BM25 retriever
  > and source set, but not the dense / rerank pipeline. The remaining
  > MRs (W2-02 Documents bridge, W2-05 OCR check, W2-07 LangGraph,
  > W2-08 reconciliation extension, W2-09 RBAC tests over documents,
  > W2-10 abstention rendering, W2-11 eval gate, W2-12 PHI redaction)
  > are deferred — see TASKS2.md for the per-MR landing status.

  Each Week 2 MR must include the test classes below. The matrix is the
  source of truth that TASKS.md draws from per-MR. *Eval-suite cases* are
  the rubric-level golden set; *integration tests* exercise the worker
  graph end-to-end with cassetted LLM responses.

  ┌──────────────────────────┬─────────┬─────────────┬──────┬─────────────────┐
  │ MR                       │ Unit    │ Integration │ Eval │ Notes           │
  ├──────────────────────────┼─────────┼─────────────┼──────┼─────────────────┤
  │ W2-01 schemas + abstain  │ ✓       │ —           │ —    │ Pydantic        │
  │ enum                     │         │             │      │ contract tests  │
  ├──────────────────────────┼─────────┼─────────────┼──────┼─────────────────┤
  │ W2-02 OpenEMR Documents  │ ✓       │ ✓           │ —    │ Event hook      │
  │ category + event hook    │         │             │      │ fires on UI +   │
  │                          │         │             │      │ REST upload     │
  ├──────────────────────────┼─────────┼─────────────┼──────┼─────────────────┤
  │ W2-03 extraction worker  │ ✓       │ ✓           │ +12  │ Eval bucket:    │
  │ + lab_pdf VLM call       │         │             │      │ extraction-lab  │
  ├──────────────────────────┼─────────┼─────────────┼──────┼─────────────────┤
  │ W2-04 intake_form        │ ✓       │ ✓           │ +10  │ Eval bucket:    │
  │ extraction               │         │             │      │ extraction-     │
  │                          │         │             │      │ intake          │
  ├──────────────────────────┼─────────┼─────────────┼──────┼─────────────────┤
  │ W2-05 citation OCR check │ ✓       │ —           │ —    │ §8.2 contract   │
  ├──────────────────────────┼─────────┼─────────────┼──────┼─────────────────┤
  │ W2-06 evidence retriever │ ✓       │ ✓           │ +8   │ Eval bucket:    │
  │ (BM25 + dense + rerank)  │         │             │      │ retrieval       │
  ├──────────────────────────┼─────────┼─────────────┼──────┼─────────────────┤
  │ W2-07 supervisor + critic│ ✓       │ ✓           │ +6   │ Eval bucket:    │
  │ + handoff logging        │         │             │      │ citation-       │
  │                          │         │             │      │ separation      │
  ├──────────────────────────┼─────────┼─────────────┼──────┼─────────────────┤
  │ W2-08 reconciliation +   │ ✓       │ ✓           │ +8   │ Eval bucket:    │
  │ discrepancy extension    │         │             │      │ reconciliation  │
  ├──────────────────────────┼─────────┼─────────────┼──────┼─────────────────┤
  │ W2-09 RBAC scope test    │ ✓       │ ✓           │ +4   │ Must be 100%    │
  │ for documents            │         │             │      │ pass            │
  ├──────────────────────────┼─────────┼─────────────┼──────┼─────────────────┤
  │ W2-10 abstention paths   │ ✓       │ ✓           │ +2   │ Eval bucket:    │
  │                          │         │             │      │ abstention      │
  ├──────────────────────────┼─────────┼─────────────┼──────┼─────────────────┤
  │ W2-11 pre-push eval hook │ —       │ ✓           │ all  │ §8 + §8.1 +     │
  │ + Makefile target        │         │             │      │ Appendix A.2    │
  ├──────────────────────────┼─────────┼─────────────┼──────┼─────────────────┤
  │ W2-12 PHI redaction in   │ ✓       │ ✓           │ —    │ Span fail-      │
  │ LangSmith spans          │         │             │      │ closed test     │
  └──────────────────────────┴─────────┴─────────────┴──────┴─────────────────┘

  Total eval cases: 50 (matches §8 dataset breakdown).

  ---
  16. Open Questions

  Don't block writing W2_ARCHITECTURE.md. Should be answered during build:

  1. **Guideline corpus content.** Which specific public-domain documents?
     Need a curated list before retrieval can be tuned. (USPSTF screening
     guidelines + a small set of CDC vaccine schedules is the candidate
     starter set.)
  2. **VLM model tier.** Sonnet-vision for cost, Opus-vision for quality.
     Pick after the first eval-gate run.
  3. **VLM-fail full-document fallback.** When the VLM extraction call
     itself fails (not a low-confidence field but a hard failure on the
     whole document), do we retry once and otherwise mark the document
     `failed` for re-upload, or attempt an OCR-first → text-LLM
     fallback? Per-field degraded handling is decided in §8.2; this
     question is only about the whole-document fail path.
  4. **Eval ground-truth labor.** Each of the 50 cases needs a hand-built
     ground truth. Estimate: ~6 hours total. Confirm before committing
     dataset size.
  5. **Demo data sufficiency for documents.** Synthea doesn't ship scanned
     PDFs. Plan: hand-craft 10 lab PDFs and 10 intake forms (varying
     scan quality) seeded into the demo patient panel.

  *Closed during reviewer pass:* the side-panel attachment surface
  (Documents view canonical, chart panel summary-only — §2/§3); eval
  runner platform (local pre-push hook only, no remote service — §8 /
  Appendix A.3); abstention-enum membership and runtime-vs-eval split
  (§6 / Appendix A.1); citation-validity threshold and degraded-OCR
  fallback (§8.2 / Appendix A.4); eval-gate fail-fast vs. flake
  tolerance (§8.1 / Appendix A.2); critic retry cap and rejection
  granularity (§5.2 / Appendix A.6); planner / query decomposition
  (§5.1); tool-vs-RAG boundary (§5.3 / Appendix A.5); per-stage
  latency budgets (§10.1). Exact DOM placement inside the Documents
  view is a visual-design call captured in W2_ARCHITECTURE.md, not an
  open product question.

  ---
  17. Document Map

  - PRD.md (v3) — Week 1 source of truth (still authoritative for the v1
    scope).
  - PRD2.md (this document) — Week 2 source of truth.
  - W2_ARCHITECTURE.md — to be written; pulls from §5–§11 of this PRD.
  - TASKS.md — Week 2 MR block to be appended (test matrix in §15.1).
  - COST.md — Week 2 cost section to be appended.
  - USERS.md — unchanged; primary user persona carries through.

  ---
  Appendix A — Normative Decisions

  Where the body of this PRD describes intent, this appendix is the binding
  contract. Conflicts resolve to A. Implementations and tests should cite
  the relevant Appendix-A clause by number.

  ### A.1 Canonical abstention enum (runtime vs. eval)

  Two **conceptually distinct** enums, even if they collapse into one
  Python type at the implementation level. The split is mandatory in the
  type system: a `RuntimeAbstainReason` value must never reach the eval
  harness state machine, and an `EvalCaseState` value must never reach
  the UI rendering layer. Defined once in `agent-service/src/
  clinical_copilot/schemas/abstain.py` (runtime) and
  `agent-service/src/clinical_copilot/evals/case_state.py` (eval); the
  modules do not cross-import.

  **A.1.a `RuntimeAbstainReason` — UI-surfaceable** (seven members).

  These can be rendered to the clinician. Each maps to UX text per v3 §5
  + §6 of this PRD.

  ┌─────────────────────┬──────────────────────────────────────────────────┐
  │ Member              │ Meaning (canonical)                              │
  ├─────────────────────┼──────────────────────────────────────────────────┤
  │ NO_DATA             │ Source genuinely has no value for this field /   │
  │                     │ record genuinely empty                           │
  ├─────────────────────┼──────────────────────────────────────────────────┤
  │ VERIFICATION_FAILED │ Synthesis claim drafted but failed verification  │
  │                     │ middleware                                       │
  ├─────────────────────┼──────────────────────────────────────────────────┤
  │ TOOL_FAILURE        │ Transient infrastructure failure                 │
  ├─────────────────────┼──────────────────────────────────────────────────┤
  │ UNAUTHORIZED        │ RBAC denied access                               │
  ├─────────────────────┼──────────────────────────────────────────────────┤
  │ LOW_CONFIDENCE      │ VLM confidence below 0.7 floor (Week 2)          │
  ├─────────────────────┼──────────────────────────────────────────────────┤
  │ OUT_OF_SCHEMA       │ VLM emitted a field not described by the schema  │
  │                     │ (Week 2; rendered as silent omission, see §6)    │
  ├─────────────────────┼──────────────────────────────────────────────────┤
  │ CITATION_INVALID    │ OCR check disagrees with VLM-claimed raw_text    │
  │                     │ (Week 2; threshold per §8.2)                     │
  └─────────────────────┴──────────────────────────────────────────────────┘

  **A.1.b `EvalCaseState` — eval harness only** (one member today,
  reserved for growth).

  These are states the eval harness assigns to *cases*, not to fields or
  responses. They never travel through the agent runtime, never reach
  the UI, and are never persisted on a fact row.

  ┌─────────────────────┬──────────────────────────────────────────────────┐
  │ Member              │ Meaning                                          │
  ├─────────────────────┼──────────────────────────────────────────────────┤
  │ JUDGE_INCONCLUSIVE  │ Eval judges disagree (§8.1). Case is             │
  │                     │ quarantined; not a runtime concept. Never        │
  │                     │ surfaced to clinicians.                          │
  └─────────────────────┴──────────────────────────────────────────────────┘

  Enforcement. The UI rendering layer's abstention switch must accept
  `RuntimeAbstainReason` only; passing an `EvalCaseState` is a
  type-check error. Symmetrically, the eval harness's case-state writer
  rejects `RuntimeAbstainReason` values. PHPStan-equivalent (mypy at
  strict) enforces this at the pre-push eval-gate run.

  Validators that traffic in either enum must match-on it without a
  `default` arm so that adding a member fails type-check until handled.
  This is the same exhaustiveness discipline as v3 §5 / CLAUDE.md
  ("Exhaustive Matching"), applied to both A.1.a and A.1.b.

  ### A.2 Eval-gate fail conditions

  Source of truth: the local pre-push git hook (managed by `prek` /
  `pre-commit`) invoking `make copilot-eval` in `agent-service/`. There
  is no remote CI service. The hook exits non-zero (blocking
  `git push`) on any of:

  1. **Any case in the `rbac.*` rubric class fails** — single-case fail =
     pipeline fail. No tolerance, no quarantine, no retry beyond the §8.1
     transient-infra retry.
  2. **Overall pass rate < 90.0%**, where pass rate is
     `passing / (total - quarantined)` and `total = 50`.
  3. **Any rubric class regresses > 5.0 percentage points** versus the
     `main` branch baseline. Baseline is the most recent successful eval
     run on `main`, stored in agent-db `eval_baseline` table.
  4. **Quarantine ceiling exceeded** — more than 5% of cases (3 of 50) in
     `JUDGE_INCONCLUSIVE` state.
  5. **Live-model spend exceeds $5** — the pre-flight token-budget gate
     aborts with `BUDGET_EXCEEDED` before the live calls run.
  6. **PHI leak detected in any LangSmith span** — see §9; this is a
     fail-closed test in the eval suite.

  Explicitly **not** in the contract:

  - "Two consecutive failures required to block." Rejected. A genuine
    rubric failure on the first run blocks the MR.
  - Per-case retry beyond the §8.1 mechanic (2 retries on transient
    infra failure only — never on rubric-evaluator outcome).

  ### A.3 Eval runner

  The eval gate runs **locally**, on the developer's machine, as a
  pre-push git hook. There is no remote CI service for this repo —
  deploys are manual and so is the gate-runner. The single source of
  truth is `make copilot-eval` in `agent-service/`; the hook calls it,
  and a developer can also call it by hand for a non-push run.

  Two-tier hook split (for cycle time):

  - **pre-commit (fast, deterministic only).** Runs the deterministic
    rubric classes — schema validity, citation OCR check, RBAC. Cached
    LLM responses; no live calls; offline; <30s expected.
  - **pre-push (full eval).** Runs the complete 50-case suite with live
    Anthropic calls per Appendix A.2. Slower and costs money; runs only
    on push, not on every commit.

  If the project later adopts a remote runner, it must call the same
  `make copilot-eval` target so the contract stays single-sourced. Until
  then, "CI" in this PRD means the local pre-push hook.

  ### A.4 Citation validity threshold

  Per §8.2: `rapidfuzz.token_set_ratio` ≥ 0.85 between OCR-of-cropped-
  region and VLM-claimed `raw_text`, both lowercased and whitespace-
  normalized. Empty Tesseract output on a non-empty bbox → invalid.

  The threshold is reviewable; changes require a documented decision in
  W2_ARCHITECTURE.md and a baseline-refresh run.

  ### A.5 Worker isolation and tool-vs-RAG boundary

  Worker isolation:

  - The supervisor is the only orchestrator of worker calls. Workers
    cannot call other workers.
  - The planner step (§5.1) runs inside the supervisor and is the only
    place where claim type is assigned to a sub-query.
  - The extraction worker is the only caller of the Anthropic vision API.
  - The verification middleware is the only writer of facts to agent-db.
  - The PHP gateway is the only writer to OpenEMR's `documents` table /
    document blob storage.

  Tool-vs-RAG boundary (per §5.3, restated as binding):

  - Patient-fact retrieval is **tool-mediated only** — chart-tools layer
    (FHIR/REST with RBAC at the tool layer) plus the intake-extractor's
    stored output. No vector index over patient data, no embeddings of
    chart notes, no semantic search across patient records.
  - Corpus retrieval is **RAG-only** — evidence-retriever's hybrid index
    over the §7 permitted-source corpus. The corpus contains zero
    patient identifiers; curation rejects any document containing
    identifying patient detail.
  - No worker reads from both stores. Cross-store reasoning happens at
    the supervisor, after each store has emitted its citation, and never
    by retrieval.

  Enforcement. These constraints are enforced by an import-graph rule
  (e.g. `import-linter` contract) where feasible: the
  `evidence_retriever` package may not import from `chart_tools` or
  `intake_extractor`'s read paths, and vice versa. Where static
  enforcement is impractical, an MR-template checklist line names the
  constraint and the reviewer checks it explicitly.

  ### A.6 Critic retry cap

  Per §5.2:

  - **Maximum one retry per sub-query.** The supervisor may resubmit a
    rejected sub-query exactly once with the rejection rationale
    appended to the worker prompt. A second rejection of the same
    sub-query forces abstain on that sub-query.
  - **No turn-level retries.** Retries are sub-query-scoped. The
    supervisor does not re-plan or re-decompose on critic rejection.
  - **Sentence-level rejection (slow lane only)** — surviving sentences
    render; rejected sentences render inline as
    `VERIFICATION_FAILED`. Whole-answer abstain is forced on any of:
    (1) ≥50% of sentences rejected, (2) any sentence triggers the
    action-suggestion blacklist (`start`, `stop`, `increase`,
    `decrease`, `switch to`, `discontinue`, `recommend [verb-ing]`),
    (3) any sub-query exhausts its retry, (4) planner-claim-type and
    citation-type disagree.
  - **Whole-answer abstain (fast lane)** — any critic rejection causes
    abstain. v3 §5 carries.
  - **Critic latency cap: 1.5s p95.** Critic timeout → abstain with
    `VERIFICATION_FAILED`, never render unchecked content.
  - **Rejection-reason taxonomy is part of the contract.** Every
    rejection carries one of: `NO_CITATION`, `CITATION_TYPE_MISMATCH`,
    `ACTION_BLACKLIST`, `CONFIDENCE_FLOOR`. Eval rubrics for
    adversarial cases assert the *expected* rejection reason, so
    silently changing the taxonomy is a rubric break.
