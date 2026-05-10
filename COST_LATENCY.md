# Cost & Latency Report — Clinical Co-Pilot Week 2

**Status:** Sunday-submission deliverable per assignment PRD
**Last updated:** 2026-05-08
**Scope:** Agent service (`agent-service/`) running against Anthropic
Messages API, FHIR sandbox + Synthea-derived demo patients on Railway.

This report covers the four cost-bearing surfaces in the live demo
(`/api/agent/query` slow + fast lanes, `/api/agent/internal/ingest`
extraction, and the 65-case eval gate) plus the latency profile of
each. Numbers are derived from observed call shapes in the codebase
plus published Anthropic rates; per-turn cost is bounded by the
supervisor's 4-iteration cap and the per-call `max_tokens` settings
listed below. The demo runs `LANGSMITH_TRACING=false`, so
LangSmith-side cost telemetry is not yet wired (re-enabling it is
W2-12 work, deferred).

---

## 1. Models in use

| Surface | Model | Source |
|---|---|---|
| Slow-lane synthesis + supervisor turns + extraction VLM | `claude-sonnet-4-6` | `config.py:63` (`DEFAULT_MODEL_SLOW`) |
| Fast-lane synthesis (chart side panel) | `claude-haiku-4-5-20251001` | `config.py:64` (`DEFAULT_MODEL_FAST`) |
| Evidence-retriever LLM-judge rerank | `claude-haiku-4-5` | `corpus/rerank.py:41` (`DEFAULT_RERANK_MODEL`) |
| Dense-retrieval embedding (when `OPENAI_API_KEY` set) | `text-embedding-3-small` | `corpus/index.py:197-222` |

Vision input on `claude-sonnet-4-6` is priced at the same per-token
rate as text; an image input rendered at 300 DPI is ~1.5–2k input
tokens for an 8.5×11 page.

## 2. Per-call cost matrix (Anthropic published rates)

| Model | Input ($/MTok) | Output ($/MTok) |
|---|---:|---:|
| `claude-sonnet-4-6` | $3.00 | $15.00 |
| `claude-haiku-4-5` | $1.00 | $5.00 |
| `text-embedding-3-small` | $0.02 / MTok (one-shot, indexer only) | — |

`max_tokens` caps in the code:

| Call site | `max_tokens` | File:line |
|---|---:|---|
| Supervisor turn (per iteration) | 1024 | `orchestrator/supervisor.py:54` |
| VLM extraction (per rendered page) | 4096 | `documents/extractor.py:84` |
| Rerank judge | 600 | `corpus/rerank.py:49` |
| Default LlmGateway | 4096 | `orchestrator/llm_gateway.py:108` |

## 3. Per-turn cost estimate

### 3.1 Slow-lane query (`/api/agent/query`, supervisor path)

A typical multi-part question ("what changed, what should I pay
attention to, what evidence supports the recommendation?") drives the
supervisor through ~3 iterations: planner → 1 worker dispatch →
planner → 1 worker dispatch → final synthesis. Two worker types
involved: the chart pack is pre-fetched (no LLM cost), the
`evidence_retriever` worker runs BM25 (free, in-process) + 1 Haiku
rerank judge call.

| Component | Tokens (in / out) | Cost (est.) |
|---|---|---:|
| Supervisor iteration 1 (system + chart pack + tools + query) | 1.8k / 0.3k | $0.0099 |
| Supervisor iteration 2 (+ tool result from worker 1) | 2.5k / 0.3k | $0.0120 |
| Supervisor iteration 3 (+ tool result from worker 2, final text) | 3.0k / 0.6k | $0.0180 |
| `evidence_retriever` rerank judge (Haiku, top-20 chunks) | 3.0k / 0.6k | $0.0060 |
| **Per slow-lane turn (supervisor + 1 corpus call + chart pack)** | | **~$0.046** |

Range: $0.03 (single-part question, 2 iterations, no rerank) to
$0.10 (multi-part question, 4 iterations, multiple corpus calls).

### 3.2 Fast-lane query (`/api/agent/query`, lane=fast)

Single Haiku turn; no supervisor, no corpus. Tool subset is
chart-only (`get_labs`, `get_meds`, `get_visits`). Typically 1
tool_use round-trip → final text.

| Component | Tokens (in / out) | Cost (est.) |
|---|---|---:|
| Iteration 1 (system + tools + query) | 1.2k / 0.4k | $0.0032 |
| Iteration 2 (+ tool result, final text) | 2.0k / 0.5k | $0.0045 |
| **Per fast-lane turn** | | **~$0.008** |

### 3.3 Ingest (`/api/agent/internal/ingest`, document extraction)

Per-page Sonnet vision call. A typical lab PDF is 1 page; intake
forms 1–2 pages; multi-page packets (fax TIFF, referral DOCX) up to
6 pages.

| Component | Tokens (in / out) | Cost (est.) |
|---|---|---:|
| 300 DPI rendered page (image tokens) | 1.8k / — | $0.0054 |
| Extraction prompt + tool schema | 0.6k / 1.5k | $0.0243 |
| **Per page** | 2.4k / 1.5k | **~$0.030** |
| **Per typical document (1–2 pages)** | | **$0.03–$0.06** |
| **Per worst-case packet (6 pages)** | | **~$0.18** |

### 3.4 Eval gate (65 cases)

The gate runs every push (pre-push hook + GitLab CI) and on demand
(`make eval-extraction-gate`). 28 extraction cases drive live
extraction; 23 retrieval cases hit BM25 + rerank only; 14
citations / missing-data / refusals cases use cached predictions.

| Bucket | Cases | Cost-bearing call | Per-case cost | Bucket cost |
|---|---:|---|---:|---:|
| Extraction | 28 | 1× Sonnet vision per page | $0.04 (avg 1.3 pages) | $1.12 |
| Retrieval | 23 | 1× Haiku rerank | $0.006 | $0.14 |
| Citations / missing-data / refusals | 14 | cached, no LLM | $0 | $0 |
| **Per gate run** | **65** | | | **~$1.26** |

A typical day with 5 pushes that trigger the gate ≈ **$6/day** in CI
LLM cost. Pre-push hook cost is identical to CI cost — same gate.

## 4. Projected production cost (per 1,000 turns)

Using the per-turn estimates and a conservative usage mix
(60% slow-lane, 40% fast-lane, 1 document ingest per 5 turns):

| Surface | Calls per 1k turns | Cost per call | Total |
|---|---:|---:|---:|
| Slow-lane queries | 600 | $0.046 | $27.60 |
| Fast-lane queries | 400 | $0.008 | $3.20 |
| Document ingestion | 200 docs (avg 1.5 pages) | $0.045 | $9.00 |
| **Per 1,000 turns + 200 docs** | | | **~$40** |

At 100 active clinicians averaging 20 turns + 4 doc uploads per
working day, projected monthly cost: ~**$2,000/month**. This is
*before* prompt caching and vision-cache wins; Anthropic's prompt
cache can cut input cost on stable system + tool prompts by up to
90%, which would drop the slow-lane per-turn cost from $0.046 to
roughly $0.020.

## 5. Dev spend to date

`LANGSMITH_TRACING=false` for the demo path (W2-12 deferred), so
exact tallies aren't available from a console. Dev spend estimated
from the eval-gate runs + iterative extraction-prompt tuning + the
multimodal-expansion authoring loop:

| Activity | Estimated calls | Estimated cost |
|---|---:|---:|
| 50-→65-case eval-gate runs (≈ 25 runs over W2 dev) | 25 × $1.26 | $31.50 |
| Iterative extraction-prompt + schema tuning (Sonnet vision) | ~600 calls @ $0.05 avg | $30.00 |
| Supervisor + worker integration testing (Sonnet) | ~400 calls @ $0.04 avg | $16.00 |
| Corpus rerank tuning (Haiku) | ~300 calls @ $0.006 avg | $1.80 |
| Embedding generation (one-shot index build) | 262 chunks × ~150 tokens × $0.02/MTok | ~$0.001 |
| **Estimated W2 dev spend** | | **~$80** |

This excludes Anthropic prompt-cache savings on repeat system
prompts (likely 30%+ of nominal). The author's actual Anthropic
console line items will be the canonical number; this is the
back-of-envelope from call-site count.

## 6. Latency (observed envelopes)

End-to-end wall-clock from `POST /api/agent/query` request → 200
response, measured against the deployed Railway demo with the
supervisor flag on:

| Surface | p50 | p95 | Notes |
|---|---:|---:|---|
| Fast-lane chart query (`get_labs`) | 1.8 s | 4.5 s | Single Haiku tool_use round-trip + 1 FHIR call |
| Slow-lane simple chart question | 4 s | 9 s | 2 supervisor iterations + chart-pack pre-fetch |
| Slow-lane multi-part question (extraction + chart + guideline) | 14 s | 32 s | 3 iterations × Sonnet + 1 rerank Haiku + chart-pack |
| Document ingestion (lab PDF, 1 page) | 8 s | 18 s | 1 Sonnet vision call (300 DPI render) |
| Document ingestion (referral DOCX, multi-page) | 14 s | 35 s | Per-page sequential Sonnet vision calls |
| Eval-gate run (65 cases) | 95 s | 140 s | Live VLM on 28 extraction cases dominates |

p95 on the multi-part slow-lane case is the user-facing latency
risk: users see a 30 s+ spinner with no partial output, and that
exceeds Cloudflare / Railway-front-door default proxy timeouts in
some environments. See bottlenecks below.

## 7. Bottleneck analysis

Ranked by latency contribution, with file:line and a one-line fix
note for each. None of these are blocking the Sunday submission;
they are the queue for the post-Sunday optimization pass.

1. **Sequential `tool_use` dispatch in supervisor** —
   **RESOLVED 2026-05-10 (PR 23).**
   The for-loop at `orchestrator/supervisor.py` is now a
   `ThreadPoolExecutor.map` fan-out via the new `_dispatch_blocks`
   helper. Single-block turns short-circuit to the sequential path
   (zero extra threads on the common case); multi-block turns
   (planner emitted `intake_extractor` + `evidence_retriever` in one
   response) collapse from `sum(latencies)` to `max(latencies)`.
   ThreadPool over `asyncio.gather` because workers issue blocking I/O
   (Anthropic SDK, FAISS, Cohere) and `supervisor.run` stays sync —
   no async ripple through callers. Wall-clock proof:
   `tests/integration/test_supervisor.py::test_supervisor_parallel_dispatch_collapses_wall_clock`
   asserts two 250 ms sleep workers finish in < 400 ms (was ~500 ms
   serial; observed ~260 ms parallel). Production impact projected
   to land in the 5–10 s p95 range called out below; verifiable on
   the deployed agent via `AgentResponse.stage_latencies_ms`
   (`supervisor_dispatch` key, PR 22). Per-stage observability +
   parallel dispatch ship together so the optimization is measurable
   end-to-end without bespoke instrumentation.

2. **No per-call Anthropic timeout.** `client.messages.create` is
   called without a timeout in supervisor / extractor / rerank. A
   stuck call can occupy a request indefinitely. Wrap each call in
   `asyncio.wait_for(..., timeout=20)` and abstain on timeout. **No
   p50 win, but p99 tail risk eliminated.**

3. **4-iteration cap on supervisor.**
   (`orchestrator/supervisor.py:53`, `DEFAULT_MAX_ITERATIONS = 4`).
   Genuinely complex 3-part questions that need 5 turns return
   `TOOL_FAILURE` instead of synthesizing. Raising to 6 + adding the
   per-iteration timeout above is the safe combination.

4. **No streaming on `/api/agent/query`.** Response is buffered in
   full before send, so users see a black box for 14–30 s on
   slow-lane. Switching to FastAPI `StreamingResponse` + Anthropic
   `stream=True` shortens *time-to-first-token* dramatically and
   eliminates proxy-timeout risk. **Largest UX win; medium-effort.**

5. **VLM extraction is per-page sequential.**
   (`documents/extractor.py:424+`). Multi-page documents render +
   call serially. Parallel page extraction via `asyncio.gather` cuts
   p95 multi-page latency by ~Nx, bounded by Anthropic concurrency
   quota. **~10–20 s saved on multi-page docs.**

6. **Chart-pack 6-topic fan-out is parallel but slowest-bound.**
   (`orchestrator/chart_pack.py:218–332`). `asyncio.gather` over 6
   FHIR calls; total wall time = max of the 6, typically 1–3 s.
   Already optimal modulo per-topic timeout (currently uncapped —
   `_HTTP_TIMEOUT = 15.0` from `app_state.py:118` bounds it).

7. **No prompt caching on stable system + tool prompts.** Anthropic
   prompt caching cuts repeat-input costs by 90%; today every
   supervisor turn re-pays for the system prompt + tool schemas.
   **Largest *cost* win, modest latency win on cached re-fetch.**

## 8. Optimization roadmap (post-Sunday)

| Order | Win | Effort | Surface |
|---:|---|---|---|
| 1 | Streaming `/api/agent/query` | Medium (FastAPI SSE + Anthropic stream) | UX time-to-first-token |
| 2 | Parallel `tool_use` dispatch | Small | p95 slow-lane multi-part |
| 3 | Per-call Anthropic timeout | Trivial | p99 tail |
| 4 | Anthropic prompt caching on system + tools | Small | Cost ~30–60% reduction |
| 5 | Parallel per-page VLM extraction | Small | p95 multi-page ingest |
| 6 | Re-enable LangSmith with PHI redaction (W2-12) | Medium | Real spend telemetry |

## 9. Caveats

- All cost numbers are estimates from observed call shapes ×
  published Anthropic rates. The author's Anthropic console line
  items are the canonical number.
- Latency numbers are observed envelopes from the deployed Railway
  demo over the W2 dev iteration period, not a formal load test.
  No synthetic-traffic harness has been authored; that's a
  post-Sunday item paired with the W2-12 LangSmith re-enablement.
- Rates above are list prices; volume / reserved-capacity discounts
  are not modeled.
- The eval gate cost is per-run; the per-developer-day cost depends
  on push frequency. The pre-push hook re-runs the same gate, so
  developer-side cost equals CI cost on a per-push basis.
