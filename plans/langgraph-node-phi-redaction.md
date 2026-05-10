# LangGraph node PHI redaction

## Background

A LangSmith trace screenshot (2026-05-10) showed the `synthesizer` node's
`final_response.synthesized_text` containing a patient first name
("Olivia"), a clinical condition ("Hashimoto's thyroiditis"), and a FHIR
resource ID. This violates the demo claim that LangSmith traces do not
contain raw PHI.

Root cause: `observability/tracing.py` wraps three call sites with
allowlist redactors — `traceable_orchestrator_run`, `traceable_llm_complete`,
`traceable_tool_dispatch`. The LangGraph supervisor (`orchestrator/supervisor_langgraph.py`)
adds a fourth surface that bypasses all of them: each node body is
auto-instrumented by LangGraph and its full input/return state dict is
emitted to LangSmith without redaction. `TurnState` carries `user_query`,
`drafts`, `final_response.synthesized_text`, and other PHI-bearing fields
verbatim.

## Hotfix (separate, env-only — applied by user on Railway)

Set `LANGSMITH_HIDE_INPUTS=true` and `LANGSMITH_HIDE_OUTPUTS=true` in the
deployed agent-service env. This blanks inputs/outputs uniformly for all
spans. Stops the bleed but loses the redacted-input visibility on the
explicit `traceable_*` wrappers as collateral. Acceptable during the demo
window; this plan replaces it with a granular fix.

## Code fix (this plan)

### Test files (test-first per CLAUDE.md PHI redaction policy)

1. **`tests/unit/observability/test_supervisor_node_redaction.py`** (new)
   - Build a fixture `TurnState` with known PHI: patient name in
     `user_query`, `synthesized_text` containing the name + condition,
     a draft with prose, a sub_query with patient context.
   - Assert `redact_supervisor_node_inputs` and
     `redact_supervisor_node_outputs` return dicts that contain *none*
     of the known PHI strings (recursive value walk).
   - Assert allowlisted fields survive: `request_id`, counts of
     `drafts`/`sub_queries`/`verdicts`, `usage_totals` shape,
     `rerank_backend`, `abstention_state` derived from `final_response`.
   - Assert the regex backstop catches PHI baked into otherwise-allowed
     fields (e.g. an MRN appearing inside `request_id` would be
     replaced with `[REDACTED:MRN]`).

2. **`tests/unit/observability/test_langsmith_safe.py`** (extend)
   - Add a case that captures a real LangGraph run end-to-end with the
     LangSmith client patched, then asserts the synthesizer/planner/critic
     spans' emitted payloads do not contain the known-PHI fixture strings.
   - This is the load-bearing regression test — it validates wiring,
     not just unit-level redaction.

### Implementation files

3. **`src/clinical_copilot/observability/redaction.py`** (extend)
   - Add `redact_supervisor_node_inputs(inputs: dict[str, Any]) -> dict[str, Any]`.
     Allowlist: `request_id` from `session`, counts of list-valued
     state keys, `rerank_backend`, `usage_totals` shape (input/output
     token totals only), `retry_counts` (sub_query_id → int — IDs are
     not PHI). Drop everything else.
   - Add `redact_supervisor_node_outputs(output: object) -> dict[str, Any]`.
     Allowlist: `final_response.abstention_reason`, counts of
     `drafts`/`verdicts` produced by this node, `usage_totals` delta.
     Drop `final_response.synthesized_text`, all draft/verdict prose.
   - Both run through `_scrub_payload` for the regex backstop.

4. **`src/clinical_copilot/observability/tracing.py`** (extend)
   - Add `traceable_supervisor_node(name: str)` factory that returns a
     decorator equivalent to `@traceable(run_type="chain", name=name,
     process_inputs=redact_supervisor_node_inputs,
     process_outputs=redact_supervisor_node_outputs)`.
   - Keep the existing three wrappers untouched.

5. **`src/clinical_copilot/orchestrator/supervisor_langgraph.py`** (modify)
   - Wrap each node body with `traceable_supervisor_node("<name>")`:
     `planner`, `intake_extractor`, `evidence_retriever`, `synthesizer`,
     `critic`, `verification`. Routers (`_planner_router`,
     `_critic_router`) are conditional edges — they don't emit
     significant payloads, but wrap them too for symmetry.
   - **Open question (verify before merging):** does LangGraph's
     auto-instrumentation still emit a parallel auto-span when a node
     body is wrapped in `@traceable`? If yes, we get double-tracing
     and the auto-span still leaks. Two known mitigations:
     - (a) Pass `RunnableConfig({"callbacks": []})` on graph compile to
       disable LangChain callbacks for the LangGraph layer specifically.
     - (b) Configure the graph with `compiled.with_config(tags=["redacted"])`
       and rely on LangSmith filtering — weaker, only cosmetic.
   - Plan: prototype the synthesizer wrap, run a single live turn with
     `LANGSMITH_TRACING=true` against a dev project, inspect whether
     the trace shows one node span (good) or two (need (a)).

### Out of scope

- Migrating the supervisor away from LangGraph. The auto-tracing
  behavior is a known LangChain/LangSmith integration cost — fixing it
  in our redactor layer is correct.
- Re-architecting the redactor to read from typed schemas. The dict-shape
  redactor pattern is already established in this module; consistency
  beats novelty here.

## Acceptance

- All new + existing observability tests pass.
- Live smoke against a dev LangSmith project: synthesizer span shows
  `synthesized_text_length: <int>` and `abstention_reason` in output,
  no patient name / condition / FHIR id in the payload.
- Hotfix env vars (`LANGSMITH_HIDE_INPUTS` / `LANGSMITH_HIDE_OUTPUTS`)
  can be removed from Railway after the code fix lands and is verified
  in prod.
