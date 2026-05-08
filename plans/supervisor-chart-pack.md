# Pre-fetch chart pack so the supervisor can answer slow-lane chart questions

## Context

Slow-lane chat (`interface/copilot/chat.php` → `Lane.SLOW`) has been
silently abstaining on every chart question since commit `649cf9dd8`
(W2-07, 2026-05-07 17:54Z) routed `/api/agent/query` through the
supervisor. Direct repro:

```
POST /api/agent/query  query="what are this patient's labs?"  patient_id=90109 → NO_DATA
POST /api/agent/query  query="what are this patient's labs?"  patient_id=90096 → NO_DATA
```

The 2026-05-07 user noticed it on Olivia (pid 90109, UI-added today)
but Synthea fixtures (e.g. Randall, pid 90096) reproduce identically —
not a per-patient data bug. Daily-brief panel still renders Olivia's
labs because that surface reads `procedure_result` directly via SQL
(`interface/copilot/daily_brief.php:192`), not via the agent.

**Why the supervisor abstains.** Locked design constraints from
`plans/week2-early-submission.md:7-21` and
`plans/early-submission-supervisor-wiring.md:24-32`:

> "Supervisor + 2 workers" — *"intake_extractor"* (multimodal) +
> *"evidence_retriever"* (corpus). *"No `chart_tools` worker for early
> submission."* *"Anthropic Messages call with two `tool_use` tools."*
> *"supervisor has no `chart_tools` worker by design (locked decision),
> so chart-data questions only stay correct on the fast lane."*

The original plan accepted that slow-lane chart questions would NO_DATA;
the side panel (fast lane = v1 orchestrator with chart tools) was the
chart-Q surface. The user has reversed that decision: the demo needs
chart questions to work *on the slow lane* (the supervisor surface)
**without violating the 2-worker / 2-tool lock**.

**User-confirmed approach:** pre-fetch a bounded chart pack for the
bound patient before the supervisor runs, expose it as cited context,
let the supervisor cite chart records from the pack and still dispatch
`evidence_retriever` for guidelines. Workers stay at 2. The
supervisor's tool schema stays at 2. The chart pack is request-time
input, not a worker.

Document Q&A is already covered: `interface/copilot/api/save_document.php`
runs `ChartWriteService::writeAllergies/Medications/ActiveProblems/
Reminders/LabObservations` (lines 215–248) when a clinician confirms
upload — extracted facts land in OpenEMR's chart tables and become
visible via FHIR. So `chart_pack` reading FHIR pulls in the
attached-document content automatically.

## Recommended approach

Three pieces. No new worker. No new tool_use schema. Supervisor's
`SYSTEM_PROMPT` learns about the chart pack so it cites against it.

### 1. `chart_pack.py` — new module

`agent-service/src/clinical_copilot/orchestrator/chart_pack.py` (new)

Pure Python, no Anthropic. Function:

```python
@dataclass(frozen=True, slots=True)
class ChartPackRecord:
    source_id: str            # "Observation/12345"
    resource_type: str        # "Observation"
    summary: str              # one-line human-readable
    fields: dict[str, Any]    # the projected record dict the tool returned

@dataclass(frozen=True, slots=True)
class ChartPack:
    patient_id: str
    records: tuple[ChartPackRecord, ...]
    fetched_topics: tuple[str, ...]    # which topics returned data
    failed_topics: tuple[str, ...]     # topics whose fetch raised

    def source_ids(self) -> frozenset[str]: ...
    def to_prompt_block(self) -> str: ...   # markdown <patient_chart>...</patient_chart>

async def build_chart_pack(
    *,
    scoped_registry: PatientScopedToolRegistry,
    claims: ClinicianClaims,
    request_id: str,
    per_topic_cap: int = 5,
    topics: Sequence[ChartTopic] = ("labs", "meds", "problems",
                                    "allergies", "visits", "notes"),
) -> ChartPack: ...
```

Behaviour:

* `asyncio.gather` of 6 dispatches via `scoped_registry.dispatch(name=
  tool_name, claims=claims, request_id=request_id)`. Each dispatch
  inherits patient cross-check (`tools/registry.py:296-300`) and
  audit-log writes (`tools/base.py:167-173`) — no new isolation or
  audit code.
* Per-topic exception swallowed into `failed_topics` (one missing tool
  doesn't tank the whole request). Logged via `structlog`.
* `UnauthorizedToolCallError` is re-raised — never recover from a
  patient-mismatch wiring bug.
* Truncate to `per_topic_cap` (default 5) most-recent records per
  topic, ordered by the same date field each tool already sorts on.
* `to_prompt_block()` renders:

  ```text
  <patient_chart>
  ## Recent labs (3 records)
  - source_id=Observation/12345 | TSH 6.73 mIU/L | observed_on=2026-04-05
  - source_id=Observation/12346 | Free T4 0.92 ng/dL | observed_on=2026-04-05
  ...
  ## Active medications (2 records)
  - source_id=MedicationRequest/8801 | levothyroxine 50 mcg PO daily | started=2026-04-10
  ...
  </patient_chart>
  ```

  Plain markdown, no JSON the LLM has to re-parse. Each line ends with
  the `source_id` it came from so the model can copy it into a
  `CitedClaim`.

### 2. Supervisor wiring — modify `orchestrator/supervisor.py`

* `run()` signature: add `chart_pack: ChartPack | None = None` (default
  `None` keeps every existing test/caller passing).
* When `chart_pack is not None and chart_pack.records`, prepend the
  pack's prompt block to the user message:

  ```python
  user_msg = f"{chart_pack.to_prompt_block()}\n\n{query}" if chart_pack else query
  messages: list[MessageParam] = [{"role": "user", "content": user_msg}]
  ```
* Extend `SYSTEM_PROMPT` (line 101) with a "Chart pack" rule and
  reaffirm the locked PRD2 rules:

  ```
  Patient chart records (when present): the user message starts with a
  <patient_chart> block listing this patient's recent chart records,
  each with a source_id. Cite chart records BY THAT source_id when
  answering chart questions. Do NOT invent source_ids; if the chart
  pack does not cover the question, abstain.

  Patient scope is fixed: never speak about any patient other than the
  one whose records are listed. Never write "no X recorded" — if the
  pack lacks something, abstain.
  ```

* No change to `_tool_schemas()`, no change to `_dispatch()`, no
  change to `WorkerName`. Workers stay at 2; tool schema stays at 2.

### 3. Adapter widening — modify `_supervisor_to_agent_response` in `main.py`

Today `_first_citation_source_id(sup.handoffs)` only checks worker
handoff outputs (`main.py:309`). Extend it (or replace its call site)
so the anchor search also includes `chart_pack.source_ids()`:

```python
anchor = _first_citation_source_id(sup.handoffs, chart_pack=chart_pack)
```

If the supervisor's synthesized text references a `source_id` present
in `chart_pack`, that's a valid anchor even when no worker fired.
Otherwise the existing NO_DATA branch (line 311-321) still runs.

To make the rule robust, parse `source_id`s the LLM emits (they appear
in the synthesized text by convention) and confirm each appears in
either a handoff citation or `chart_pack.source_ids()`. Drop claims
whose source_id resolves to neither into `dropped_claims`
(`schemas.py:97-132`) — that's the existing slow-lane sidecar, no new
plumbing.

### 4. Route handler wiring — modify `main.py` supervisor branch

Around line 396-430, before `supervisor_run(...)`:

```python
# Cross-patient guard: name-level reject before the supervisor or
# any FHIR fetch runs. Currently only on the v1 path
# (orchestrator/agent.py:284-300); lifting onto the supervisor branch
# closes the gap.
guard_reason = cross_patient_check(
    query=body.query, bound_patient_name=bound_patient_name,
)
if guard_reason is not None:
    return _abstain(NO_DATA, reason=guard_reason, session_id=canonical_id)

# Build the per-request scoped registry once. The same handle drives
# the chart-pack fetch AND any supervisor-internal tool dispatches.
scoped_registry = resolved_state.tool_registry.scoped_for(claims.patient_id)

# Pre-fetch chart pack. Fan-out is parallel; each leg writes its own
# audit row via tools/base.py Tool.execute().
chart_pack = await build_chart_pack(
    scoped_registry=scoped_registry,
    claims=claims,
    request_id=request_id,
)

# Existing supervisor invocation, plus chart_pack.
sup = supervisor_run(
    client=...,
    model=...,
    query=body.query,
    intake_extractor=resolved_state.supervisor_intake_extractor,
    evidence_retriever=resolved_state.supervisor_evidence_retriever,
    chart_pack=chart_pack,
    request_id=request_id,
)
return _supervisor_to_agent_response(sup, chart_pack=chart_pack,
                                     session_id=canonical_id)
```

### 5. Tests (test-first per CLAUDE.md for RBAC paths)

* `tests/unit/test_chart_pack.py` — `build_chart_pack` fans out to all
  6 topics, truncates per-topic, surfaces `failed_topics` on partial
  errors, `to_prompt_block()` shape stable.
* `tests/integration/test_chart_pack_isolation.py` — registry scoped
  to pid A, claims carry pid B → `UnauthorizedToolCallError` raised
  *before* any FHIR call (defense-in-depth check at registry level).
* `tests/integration/test_supervisor_chart_pack.py` — supervisor with
  scripted Anthropic mock (no worker calls) emits text containing
  `source_id=Observation/12345` matching the pack; adapter returns
  `CitedClaim` anchored at that source_id. Pattern: copy
  `tests/integration/test_query_route_supervisor.py` test #3 (corpus-
  only) and adjust.
* `tests/integration/test_supervisor_chart_plus_corpus.py` — mixed
  query ("what are her labs and what guidelines apply?"); supervisor
  dispatches `evidence_retriever` once; final response carries one
  chart-pack-anchored claim and one corpus-anchored claim.
* `tests/integration/test_supervisor_cross_patient_guard.py` — bound
  pid 90109 / "Olivia Nguyen"; query *"what about Randall's labs?"* →
  guard short-circuits before `build_chart_pack` runs (Anthropic mock
  asserts not called).

### Critical files

**New:**
* `agent-service/src/clinical_copilot/orchestrator/chart_pack.py`
* `agent-service/tests/unit/test_chart_pack.py`
* `agent-service/tests/integration/test_chart_pack_isolation.py`
* `agent-service/tests/integration/test_supervisor_chart_pack.py`
* `agent-service/tests/integration/test_supervisor_chart_plus_corpus.py`
* `agent-service/tests/integration/test_supervisor_cross_patient_guard.py`

**Modified:**
* `agent-service/src/clinical_copilot/orchestrator/supervisor.py` —
  add `chart_pack` parameter, prepend pack to user message, extend
  `SYSTEM_PROMPT`. No worker / tool-schema changes.
* `agent-service/src/clinical_copilot/main.py` — apply
  `cross_patient_check` on supervisor branch; build
  `scoped_registry` + `chart_pack`; thread `chart_pack` through to
  `supervisor_run` and `_supervisor_to_agent_response`. Update the
  adapter's anchor lookup to consult `chart_pack.source_ids()`.

### Reused existing pieces (do not reinvent)

* `tools/registry.py` `PatientScopedToolRegistry.dispatch()` — patient
  cross-check + `Tool.execute()`, the "tool registry factory" the user
  named.
* `tools/base.py` `Tool.execute()` — RBAC + audit-log writes (closes
  the W2-07 PHI-audit gap automatically; no new audit code).
* `tools/fhir_base.py` `reference_id()` — `source_id` formatter shared
  with v1.
* `orchestrator/cross_patient_guard.py` `cross_patient_check()` —
  query-level name match (currently only on v1 branch).
* `schemas.py` `dropped_claims` (slow-lane sidecar, lines 97-132) —
  reuse for any LLM-emitted `source_id` that doesn't resolve.
* `tools/{labs,meds,problems,allergies,visits,notes}.py` — six
  existing FhirBackedTool classes, used as-is via the registry.

## Verification

1. **Restart agent-service** (uvicorn must restart to read code; the
   `--reload` watcher only sees `.py` changes, which our edits make).

2. **Direct API smoke** — replay `/tmp/copilot_query.py` for pid 90109
   ("what are this patient's labs?"):
   * Response: 200 with `abstention=null` and `prose[0].text` mentions
     a TSH/Free T4/Ferritin value, `prose[0].source_id` starts with
     `Observation/`.
   * `/tmp/agent-service.log`: a `supervisor.turn iteration=1` event,
     plus six `tool.SUCCESS` rows (one per topic) from the pack fetch.
   * Audit DB: six rows for `request_id=...` with action `SUCCESS`.

3. **Cross-patient probe** — bound to 90109, query *"what about
   Randall's labs?"* → `state: NO_DATA`; logs show no
   `supervisor.turn` for the request, no `tool.SUCCESS` rows
   (build_chart_pack never runs).

4. **Mixed query** — *"what are her labs and what guidelines apply?"*
   → response contains both a chart-pack-anchored CitedClaim
   (Observation/...) and a corpus-anchored CitedClaim (chunk_id from
   evidence_retriever). The handoff log shows one
   `dispatch_evidence_retriever` call; chart-pack contributions show
   only as the `tool.SUCCESS` rows.

5. **Browser smoke** — open `interface/copilot/chat.php`, pick Olivia,
   ask *"what are her labs?"* — values render in cited prose
   (matches the daily-brief card: TSH 6.73/7.38/8.4, Free T4 0.92,
   Ferritin 18).

6. **Document-content smoke** — pick a patient with a recently
   confirmed-and-attached upload (`save_document.php` writeback is
   already verified to land facts in chart tables). Ask about the new
   content — the chart pack pulls it via the same FHIR endpoints.

7. **Run new + adjacent tests**:

   ```
   cd agent-service
   uv run pytest \
     tests/unit/test_chart_pack.py \
     tests/integration/test_chart_pack_isolation.py \
     tests/integration/test_supervisor_chart_pack.py \
     tests/integration/test_supervisor_chart_plus_corpus.py \
     tests/integration/test_supervisor_cross_patient_guard.py \
     tests/integration/test_query_route_supervisor.py \
     tests/integration/test_supervisor.py \
     tests/unit/test_query_route.py -v
   make check
   ```

8. **Existing W2-07 tests** (`tests/integration/test_query_route_
   supervisor.py`, the five locked tests) keep passing — the new
   supervisor parameter is optional, and these tests construct
   `AppState` without `chart_pack` so they go through the
   `chart_pack=None` path which is identical to the pre-change shape.

## Out of scope

* Per-patient document index / standalone document_lookup worker —
  obviated by `save_document.php` chart writeback.
* Caching of chart packs — the demo doesn't need it; a 30s LRU keyed
  on `patient_id` is a future optimization if the round-trips become
  visible.
* Fast-lane chart_pack — fast lane already has v1 chart tools via
  tool_use; supervisor isn't on that path.
* Adding chart-data tests to the eval rubric — rubric work is its
  own PR.
