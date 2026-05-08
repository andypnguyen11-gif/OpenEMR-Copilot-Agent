# Clinical Co-Pilot — Week 2 Submission Index

**Submission date:** 2026-05-10 (Sunday afternoon)
**Repo:** `ssh://git@labs.gauntletai.com:22022/andynguyen/clinicalcopilot.git`
**Deployed application:** *<TODO: paste Railway URL — agent-service +
OpenEMR overlay; both behind the same Railway project>*
**Author:** Andy Nguyen

This is the cover sheet for the cohort-5 Week 2 grader. Every claim
points at a concrete artifact. The full design sits in `PRD2.md`,
`W2_ARCHITECTURE.md`, and `TASKS2.md`; this document is the
two-page version that lets you verify each rubric criterion in
five minutes.

---

## 1. Deliverables map

The assignment lists eight deliverables. For each, the artifact
that fulfills it:

| # | Deliverable | Artifact | Status |
|---:|---|---|---|
| 1 | GitLab repo (Week-1 fork + Week-2 changes, setup guide, deployed link, env-var docs) | This repo. Setup: `agent-service/README.md § Quick start`; env vars in `.env.example` + `agent-service/README.md § Production env vars` | ✅ |
| 2 | W2 Architecture Doc | `./W2_ARCHITECTURE.md` (1,258 lines; topology, ingestion flow, multi-agent graph, hybrid RAG, eval, observability) | ✅ |
| 3 | Pydantic schemas for `lab_pdf` + `intake_form` w/ citation fields + validation tests | `agent-service/src/clinical_copilot/documents/schemas/{lab_pdf.py,intake_form.py,citation.py}`; XOR validator at `citation.py:55–89`; tests under `agent-service/tests/unit/documents/schemas/` | ✅ |
| 4 | 50-case golden eval set, boolean rubrics, judge config, results | **65 cases** at `agent-service/evals/extraction/cases.jsonl`; rubrics in `agent-service/src/clinical_copilot/evals/extraction/rubrics.py`; thresholds + last-pass-rates in `agent-service/evals/extraction/baseline.json`; results in `agent-service/evals/extraction/results/<run_id>.json` (gitignored; CI uploads as artifact) | ✅ (exceeded — 65 vs. 50 required) |
| 5 | CI evidence (git hook or equivalent that blocks regressions) | Pre-push hook at `agent-service/scripts/pre-push.sh` (commit `4a81eca23`) + GitLab pipeline at `.gitlab-ci.yml` (commit `24ae138b9`); both invoke `make eval-extraction-gate` which exits non-zero on threshold breach or > 5 pp regression | ✅ |
| 6 | Demo video (3-5 min) | *<TODO: paste video URL — required Sunday deliverable, see TASKS2.md "Open recovery items" §[write up] for the 8-beat shot list>* | 🟡 OPEN — record before submit |
| 7 | Cost & latency report (dev spend, projected prod cost, p50/p95, bottlenecks) | `./COST_LATENCY.md` | ✅ |
| 8 | Deployed application | Railway: agent-service + OpenEMR PHP overlay (same project). URL above. | ✅ |

## 2. The four hard problems → how the system addresses each

The assignment names four hard problems. Where each is solved in
this codebase:

### 2.1 Vision extraction without invention

Each extracted field is wrapped in `ExtractedField[T]`
(`documents/schemas/lab_pdf.py`, `intake_form.py`). The Pydantic
validator at `documents/schemas/citation.py:55–89` enforces an XOR
rule: every leaf carries either a `SourceCitation` (page + bbox +
raw text) **or** an `abstain_reason` from `RuntimeAbstainReason`,
never neither, never both. The eval gate's
`citation_present ≥ 0.95` threshold (`baseline.json`) makes this a
CI invariant on every push. VLM confidence below 0.7 produces
`LOW_CONFIDENCE` abstention rather than a low-quality field.

The clinician sees the citation in the review UI:
`interface/copilot/lab_review.php:160–188` (Citation column per
observation row); `intake_review.php:145+` (`.citation` div under
every named field plus Citation columns in problems / medications /
allergies tables); `document_review.php:305,448` (yellow abstain
badges).

### 2.2 Evidence grounding (chart vs guideline)

Every claim in a synthesized answer must cite either a chart
`source_id` (FHIR `ResourceType/{id}` from the chart-pack
pre-fetch) or a corpus `chunk_id`. The supervisor's system prompt
(`orchestrator/supervisor.py:102–144`) enforces "never invent a
source_id; if you cannot ground, abstain — do not pad." The chart
pack in `orchestrator/chart_pack.py:191–215` formats every record
with verbatim `source_id=<ResourceType>/<id>` so the model copies
it into the synthesis. The corpus is read-only and patient-free
(LICENSES.md restricts to USPSTF / CDC / NIH / AHA public-domain
or fair-use excerpts).

### 2.3 Multi-agent architecture (handoffs logged + explainable)

The supervisor + 2 workers (`intake_extractor`,
`evidence_retriever`) live in `orchestrator/supervisor.py` and
`orchestrator/workers/`. Each `tool_use` dispatch records a
`Handoff` with `worker`, `tool_use_id`, `arguments`, `output`,
`error`, `latency_ms` (`supervisor.py:60–76`). Handoffs are
surfaced via `GET /api/agent/supervisor/audit/{resident_user_id}`
and emitted as structlog events on every dispatch. Production
wiring is at `main.py:541–632`, gated on
`resolved_settings.use_supervisor` (default on) for slow-lane
traffic with a v1-orchestrator fallback on supervisor exception.

End-to-end coverage:
`agent-service/tests/integration/test_supervisor.py` (215 lines —
asserts both workers dispatched, both handoffs logged, synthesis
cites returned source_ids) and
`tests/integration/test_query_route_supervisor.py` (508 lines —
end-to-end through `/api/agent/query`).

### 2.4 Eval-driven CI as the gate (non-negotiable)

Pre-push hook + GitLab CI pipeline both invoke
`make eval-extraction-gate`, which loads the 65-case manifest,
runs the rubrics, and exits non-zero if **any** of the five
boolean rubrics drops below its threshold (`schema_valid = 1.0`,
`citation_present ≥ 0.95`, `factually_consistent ≥ 0.90`,
`safe_refusal = 1.0`, `no_phi_in_logs = 1.0`) or regresses by more
than 5 pp from `baseline.json`. The runner is at
`agent-service/src/clinical_copilot/evals/extraction/runner.py:195–202`.

Boolean rubrics only — no LLM-as-judge with fuzzy criteria. Per
the assignment's common-pitfall list ("Using llm-as-a-judge
without clear rubric. Use boolean rubrics so failures are
actionable") this was a deliberate choice.

## 3. What is honestly deferred

Calling these out so the grader doesn't have to dig for the gap.

| Deferred surface | Why | Where it's tracked |
|---|---|---|
| W2-07 LangGraph rewrite (planner + critic + conditional edges + LangSmith per-node spans) | **Sunday-blocking target.** Current supervisor is plain Python via Anthropic `tool_use`, working and wired live (`main.py:541–632`) — satisfies the Core "one supervisor and two workers" requirement today. The LangGraph upgrade lands behind a new `use_langgraph` flag with the current supervisor as v1 fallback; the dedicated critic node closes the Extension-tier "critic agent" rubric line. | TASKS2.md "Open recovery items" → `[build] W2-07 LangGraph pivot` |
| Critic agent (Extension-tier rubric line) | **Intent met by 3-layer enforcement today** — XOR validator at `documents/schemas/citation.py:55–89` (schema-time), supervisor system prompt at `orchestrator/supervisor.py:102–144` ("if you cannot ground a claim, abstain — do not pad"), and the CI gate's `citation_present ≥ 0.95` + `safe_refusal = 1.0` thresholds. **Dedicated critic LLM node lands as part of the W2-07 LangGraph pivot above.** If W2-07 slips, this row is the honest framing — the three layers already prevent uncited claims and unsafe action suggestions, but the Extension rubric line is only fully closed when the dedicated node ships. | `documents/schemas/citation.py:55–89`; `orchestrator/supervisor.py:102–144`; `evals/extraction/baseline.json` |
| W2-05 OCR strict + degraded path for citations | Tesseract not yet bundled. Today's gate is VLM-confidence < 0.7 only. `CitationKind` is in the enum but unreachable. **Scoped post-Sunday MR.** | TASKS2.md MR W2-05; W2_ARCHITECTURE §7.1 |
| W2-RR cross-encoder rerank | LLM-judge in `corpus/rerank.py` is the substitute. **Scoped post-Sunday MR** — Cohere `rerank-v3.5` API preferred; LLM-judge stays as fallback when `COHERE_API_KEY` absent. | TASKS2.md "W2-RR — Cross-encoder rerank backend" |
| W2-12 LangSmith deny-by-default redaction | Demo runs with `LANGSMITH_TRACING=false`. Re-enabling needs the redaction layer. **Scoped post-Sunday MR** — deny-by-default per-span allowlist + regex backstop. | TASKS2.md MR W2-12; W2_ARCHITECTURE §8 |
| `extracted_facts` Postgres table durability | Facts persist as `data/extracted/<id>.json` on the agent-service local disk — non-durable across container restarts. The chart-write path lands accepted facts into OpenEMR durably, so the JSON is effectively a temp buffer. | TASKS2.md "Open recovery items" → `[decide] Extracted-facts durability` |
| Eval buckets beyond extraction + retrieval | 65 cases live across 5 buckets. The originally-planned `reconciliation` / `citation-separation` / `rbac` / `abstention` buckets remain at zero. | TASKS2.md "Eval-suite bucket inventory"; W2_ARCHITECTURE §9 |
| Dense embedding artifact on Railway | Demo runs BM25-only (deployed env doesn't have `OPENAI_API_KEY` for the rebuild). Falls back cleanly per `corpus/retriever.py`. | W2_ARCHITECTURE §6; PRD2 §7 |

**Chart-write idempotency.** The current implementation does not
dedupe writes against existing chart rows. This is intentional for
the clinician-confirmed flow: the review surface is the right place
for the clinician to decide whether a duplicate medication or allergy
represents real new information vs. a re-import of an existing
record (see the rationale at `ChartWriteService.php:17–21`). However,
accidental double-submit (clinician double-clicks Save, or a network
blip causes the form to resubmit) *will* duplicate rows under the
current code — `tests/Tests/Services/Copilot/ChartWrite/ChartWriteServiceTest::testWriteAllergiesDoesNotDedupeOnRepeatCall`
locks this behaviour so any future fix is a deliberate change, not a
regression. Production hardening would add a per-`document_id`
idempotency marker on the `documents` row (or a dedicated
`chart_write_audit` table) so a repeat POST with the same
`document_id` becomes a no-op. Tracked as a post-Sunday item; out of
scope for the submission MR.

FHIR Bundle export of the just-written facts is also out of scope for
Sunday submission. The chart-write path lands the structured rows
into `lists` / `dated_reminders` / `procedure_*` directly; OpenEMR's
existing FHIR API exposes those rows on read, so the round-trip works
end-to-end. A dedicated "POST FHIR Bundle on save" pathway would
duplicate the persistence layer — it adds value only when the bundle
is the durable record of truth, which it is not in this deployment.

## 4. Five-minute reading tour

Open these files in order to walk the request path:

1. `agent-service/src/clinical_copilot/main.py:520–632` — the
   `POST /api/agent/query` route. Slow-lane traffic flows through
   the supervisor branch (lines 541–632); fast-lane through v1
   orchestrator (line 634).
2. `agent-service/src/clinical_copilot/orchestrator/supervisor.py`
   — the 2-worker supervisor (the `dispatch_*` tool_use blocks
   are at `:150–225`; the run loop is at `:248–333`; the dispatch
   helper is at `:336–406`).
3. `agent-service/src/clinical_copilot/orchestrator/workers/evidence_retriever.py`
   — the corpus worker. BM25 + dense + RRF fusion lives in
   `corpus/retriever.py:62–146`; LLM-judge rerank in
   `corpus/rerank.py:83–141`.
4. `agent-service/src/clinical_copilot/documents/extractor.py`
   — VLM extraction. `VLM_MAX_TOKENS = 4096` per page. Per-format
   dispatch table at `:309–390`.
5. `agent-service/src/clinical_copilot/evals/extraction/runner.py:112–202`
   — the eval gate runner. Threshold + regression check is
   `_check_thresholds_and_regression` near `:195`.

For the chart-side (PHP) read path:

6. `interface/copilot/upload_document.php` — universal upload
   entry point.
7. `interface/copilot/document_review.php` — review UI with
   citations + abstention badges.
8. `interface/copilot/api/save_document.php` →
   `src/Services/Copilot/ChartWrite/ChartWriteOrchestrator.php` →
   `src/Services/Copilot/ChartWrite/ChartWriteService.php` — the
   chart-write path that lands accepted facts into OpenEMR's
   `lists` / `dated_reminders` / `procedure_*` tables. The
   orchestrator dispatches one writer per ticked review-page section;
   the service holds the SQL contracts. Both layers have tests:
   `tests/Tests/Isolated/Services/Copilot/ChartWrite/ChartWriteOrchestratorTest.php`
   for the dispatch logic, `tests/Tests/Services/Copilot/ChartWrite/ChartWriteServiceTest.php`
   for the SQL contracts.

## 5. Common-pitfalls cross-reference

The assignment lists five common pitfalls. Where this submission
stands on each:

| Pitfall | This submission |
|---|---|
| Trying to support five doc types before two work reliably | Lab PDF + intake form are the two ratified core types; the multimodal expansion (DOCX / TIFF / XLSX / HL7-ORU / HL7-ADT) is on top, not in place of, and each has its own eval bucket. |
| Using a VLM answer directly without schema validation or source metadata | The `ExtractedField[T]` XOR validator (`citation.py:55–89`) makes schema-without-citation impossible to construct. `citation_present ≥ 0.95` is a CI invariant. |
| Letting the supervisor become a black box | Every dispatch records a `Handoff`, surfaced both as a structlog event and via the audit endpoint. Tests assert handoffs are logged. |
| LLM-as-judge without clear rubric | All five rubrics are boolean (`rubrics.py`). No fuzzy criteria. |
| Logging raw doc text, patient identifiers, or screenshots to SaaS observability | Demo runs with `LANGSMITH_TRACING=false`. Re-enabling requires W2-12's deny-by-default redaction layer (deferred, scope honestly declared in §3 above). |

## 6. The originating user question

The assignment scenario question is:

> *"What changed, what should I pay attention to, and what evidence
> supports the recommendation?"*

For a demo patient (e.g. p06 Marcus Johnson — HFrEF + CKD 3a + ACE-I
angioedema reconciliation discrepancy), the live path:

1. Slow-lane query enters `/api/agent/query`.
2. Supervisor pre-fetches the chart pack (6 FHIR topics, parallel).
3. Supervisor dispatches `dispatch_evidence_retriever("HFrEF GDMT")`
   → BM25 hits `aha/heart_failure_reduced_ef.md` rank 1, Haiku
   rerank confirms.
4. Supervisor synthesizes: "What changed: lisinopril remains
   active despite ACE-I angioedema (cite chart `MedicationStatement/<id>`).
   What to pay attention to: substitute losartan; SGLT2 + MRA
   already onboard. Evidence: AHA HFrEF GDMT (cite
   `aha/heart_failure_reduced_ef#<chunk>`) recommends ARB after
   ACE-I angioedema."
5. Audit row at `GET /api/agent/supervisor/audit/<user>` shows
   the supervisor → evidence_retriever handoff with source chunks.

The demo video walks this end-to-end (deliverable §6).
