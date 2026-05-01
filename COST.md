# Clinical Co-Pilot — AI Cost Analysis

This document is the full version of PRD.md §15 and the financial counterpart to
ARCHITECTURE.md §9 (which covers what *changes* at each tier; this document covers what
each tier *costs* and why the curve is sub-linear). Architectural changes per tier live
in ARCHITECTURE.md §9 and are referenced — not duplicated — here.

The case study sentence *"this is not simply cost-per-token × n users"* is the thesis of
this document. Linear extrapolation from a single per-request token count produces a
number that is wrong by an order of magnitude in either direction depending on cache hit
rate, prompt-cache reuse, model-routing posture, and discrepancy-pass batching. Each tier
section names the levers that bend the curve.

---

## 1. Scope and Methodology

**What's included.** LLM inference (Anthropic API), inference-adjacent infra
(`agent-service` compute, agent-db Postgres, cache memory), observability (LangSmith),
and per-request audit-log storage. Excluded: OpenEMR-side compute (already paid for in
the EHR baseline), engineer time, and one-time migration costs at tier boundaries — those
are called out qualitatively where they matter.

**Pricing source of truth.** Anthropic Console pricing at the date of the Final
submission. Specific rates are pinned in §3 with a "checked on" date so the document is
auditable rather than aspirational. **All $ figures below marked `TBD` are populated from
two sources:** (a) the LangSmith dashboard for actual per-request token counts measured
against the eval suite (PR 22–23), and (b) the Anthropic Console for the corresponding
rate card. Until the eval has run end-to-end, every $ figure is a derivation, not a
measurement.

**Assumptions.** Tracked in §7 with each one tagged as *measured* (from the eval) vs.
*estimated* (defended-but-not-yet-measured). A reader should be able to swap any
estimated assumption for a measured value and recompute the table.

---

## 2. Per-Request Token Math, by Lane

The agent has three distinct LLM-touching request shapes. Each has a different token
profile and cost driver. Combining them into a single "per-query" number is the bug the
case study sentence warns against.

### 2.1 Fast lane — between-room query

ARCHITECTURE.md §2 fast lane. Budget: ≤5s p50, ≤8s p95. Model tier: Haiku-class.
Verification depth: light (leans on slow-lane pre-warmed flags).

| Component | Tokens | Notes |
|---|---|---|
| System prompt (fast) | ~800 | Cacheable; reused across every fast-lane request for the session |
| Tool schemas (7 tools) | ~2,500 | Cacheable alongside system prompt |
| Retrieved context (cards + flags, 1–2 records) | ~600–1,200 | Per-patient; smaller than slow-lane because the warmer already extracted relevant flags |
| User query | <50 | Clinician between-room queries are terse |
| **Total input (cold)** | **~4,500** | First request in a session — full price |
| **Total input (warm)** | **~1,200 fresh + ~3,300 cached** | System prompt + tool schemas served from prompt cache |
| Output | ~300–500 | Structured response; bounded by Pydantic schema |

**Per-request unit cost (Haiku 4.5):**
- Cold: 4,500 × $0.80/M + 400 × $4.00/M ≈ **$0.0052**
- Warm (cache active): 1,200 × $0.80/M + 3,300 × $0.08/M + 400 × $4.00/M ≈ **$0.0028**

Cache write (first request that establishes prefix): one-time +$0.0033, amortized
across the session.

### 2.2 Slow lane — Daily Brief / synthesis

ARCHITECTURE.md §2 slow lane. Budget: 10–20s per query. Model tier: Sonnet-class.
Verification depth: full (citation + field check on every claim).

The slow lane runs the orchestrator's tool-using loop end-to-end: turn 1 emits
`tool_use` blocks, turn 2 consumes the tool results and emits a final JSON draft,
turn 3 fires only when the schema retry path triggers (`agent.py:124`). Each turn
is a separate billable LLM call.

| Component | Tokens | Notes |
|---|---|---|
| System prompt (slow) | ~800 | `system.md` ≈ 3,300 chars; cacheable |
| Tool schemas (7 tools) | ~2,500 | Cacheable alongside system prompt |
| Retrieved context (full chart slice) | ~3,000 | 6 tool results: problems + meds + allergies + recent labs + visits + last 1 note |
| User query | 30–200 | Briefing prompts at the lower end |
| Assistant turn 1 output (tool_use) | ~150 | Tool decisions, one round |
| Assistant turn 2 output (final JSON) | ~600–800 | Cards + cited prose |
| **Per-query input total** | **~10,000** | Sum across the 2 turns; 3,300 of these are cacheable |
| **Per-query output total** | **~900** | |
| Schema-retry round (post-3f1249b6b: ≪10% of briefing queries) | +7,000 in / +700 out | Validation error + retry draft |

**Per-request unit cost (Sonnet 4.x):**
- Cold (no cache): 10,000 × $3/M + 900 × $15/M ≈ **$0.045**
- Warm (cache active): 6,700 × $3/M + 3,300 × $0.30/M + 900 × $15/M ≈ **$0.034**
- With one schema retry, cold: ≈ **$0.077**
- With one schema retry, warm: ≈ **$0.060**

Sonnet ≈ 4× the per-token rate of Haiku (see §3). The retrieved context is also
~3× larger than fast lane. **Slow-lane unit cost is the dominant single-request
driver** and the reason the slow lane runs as a server-triggered pre-warm rather
than per-click.

### 2.3 Discrepancy worker — background pass

ARCHITECTURE.md §3 layer 2 / §6.5 rules engine. **No LLM cost** for the rules engine
itself — it's deterministic Python over the FHIR/REST output. The cost it does incur is
the slow-lane synthesis runs that the worker triggers (use case 4 — "what conflicts or
missing data should I verify"), one per patient on the day's panel. Already counted in
§2.2; called out here so it isn't double-counted.

---

## 3. Pinned Rate Card

**Checked on:** 2026-05-01 (working figures; re-verify at Final submission).
**Source:** [Anthropic Console pricing](https://www.anthropic.com/pricing) — record screenshot in `docs/cost-evidence/` at Final.

| Model tier | Input ($/M tokens) | Cached input ($/M) | Cache write ($/M) | Output ($/M) |
|---|---|---|---|---|
| Haiku 4.5 (fast lane) | $0.80 | $0.08 | $1.00 | $4.00 |
| Sonnet 4.x (slow lane) | $3.00 | $0.30 | $3.75 | $15.00 |

Cache reads on Anthropic's prompt cache are priced at ~10% of the base input rate;
the system-prompt + tool-schema portion of every request after the first in a session
hits this rate. Cache writes (the first request that establishes the cached prefix) are
billed at 1.25× the base input rate. The prompt-cache hit rate is the **single largest
knob** on per-request cost — see §6.

---

## 4. Per-User-Day Request Volume

Anchored to USERS.md §1 — the cross-coverage primary-care physician on a ~20-patient day.
Volume is *per user-day*, not per user; physicians are bursty (clinic days vs.
admin/charting days), and request rate on a clinic day is the only operationally
meaningful figure.

| Request type | Per patient | Per user-day | Lane | Trigger |
|---|---|---|---|---|
| Slow-lane pre-warm | 1 | ~20 | Slow | Server (schedule load / login / cron) |
| Daily Brief render | 1 (if user opens) | 0–1 | Slow (cached) | User opens UI |
| Between-room query | 2–4 (estimated) | ~60 | Fast | User clicks side panel / asks |
| Discrepancy synthesis | 1 (when flags present) | ~5–10 | Slow | Server (during pre-warm) |
| **Total slow-lane LLM calls** | | **~25–30** | | |
| **Total fast-lane LLM calls** | | **~60** | | |

The 60-fast / 25-slow ratio matters: fast-lane volume dominates, but slow-lane unit cost
dominates. **At MVP the cost split is roughly even**, so the headline "cost per
user-day" is sensitive to *both* model-tier choice on the slow lane *and* cache hit rate
on the fast lane. Either lever moved 2× changes the daily total ~50%.

---

## 5. Tier Cost Table

Architectural changes per tier live in ARCHITECTURE.md §9. This table adds the dollar
figures and the per-component breakdown the case study explicitly asks for.

### Assumptions for the table

- 22 clinical user-days per month per active user.
- Cache hit rate on prompt cache: **TBD** (target 70%+; see §6).
- Discrepancy cache hit rate (fast lane reads slow-lane flags): **TBD** (target 90%+
  during clinic hours).
- LangSmith tier: free / Plus / Enterprise switches at ~1K and ~10K users.
- Audit log retention: 30 days minimum (HIPAA floor; PRD §13).
- Railway pricing at MVP; AWS/GCP+BAA at 10K+ (HIPAA-eligible).

### Per-user-day building block

Computed from §2.1 and §2.2 unit costs × §4 volume, at the **expected** cache hit
rate (70% prompt, 90% discrepancy). All figures assume PR 9 prompt caching is
shipped; pre-PR-9 numbers are ~25% higher across the board.

| Lane | Calls / user-day | Unit cost (warm) | Per user-day |
|---|---|---|---|
| Slow (Sonnet 4.x) | ~27 | ~$0.034 | ~$0.92 |
| Fast (Haiku 4.5) | ~60 | ~$0.0028 | ~$0.17 |
| **Total LLM cost / user-day** | | | **~$1.10** |

22 user-days/month → **~$24 / user / month** at the 1K+ tier (where caching is
working at target hit rate). MVP (100-user) tier runs hotter — see the table.

### Table

All LLM-cost cells derive from the per-user-day building block above × 22 ×
user count, adjusted down by tier-specific cache and routing improvements.
Infrastructure cells are order-of-magnitude estimates from Railway / AWS public
pricing as of 2026-05; verify before quoting in any external context.

| Tier | LLM (Anthropic) | Compute (`agent-service`) | Agent DB (Postgres) | Cache | Observability | Audit log storage | **Monthly total (estimated)** |
|---|---|---|---|---|---|---|---|
| **100 users** | ~$2,800 | ~$30 (Railway, single replica) | ~$15 (Railway managed) | $0 (in-process) | $0 (LangSmith free) | <$5 | **~$2,850** |
| **1K users** | ~$24,000 | ~$200 (Railway, multi-replica) | ~$80 (Railway upgrade) | ~$50 (Redis introduced) | ~$300 (LangSmith Plus) | ~$30 | **~$24,700** |
| **10K users** | ~$190,000 | ~$1,500 (AWS+BAA, autoscale) | ~$800 (RDS + read replicas) | ~$300 (ElastiCache) | ~$2,000 (Enterprise + retention) | ~$300 | **~$195,000** |
| **100K users** | ~$1,500,000 | ~$10,000 (AWS regional, dedicated) | ~$5,000 (Aurora multi-region) | ~$2,000 (Redis cluster) | ~$10,000 (vendor-flex; possibly self-hosted) | ~$3,000 | **~$1,530,000** |

The 100-user row is **higher per user** ($28.50) than the 1K row ($24.70) because
session reuse is too sparse to keep the prompt cache warm across users — the
expected cache hit rate degrades to ~50% at MVP scale. The unit cost crosses
back below at ~250 active users when the cache stays hot through the clinic
day. Above the 10K tier, **Anthropic enterprise pricing kicks in** with
typical 20–35% discounts at sustained volume; the 100K row above does *not*
yet bake that in (treat it as a ceiling).

**Crossover events** (called out here because they are *not* line items above):

- **100 → 1K:** Redis is added not because in-process cache fails but because the
  multi-replica agent service breaks per-process cache locality. This is an
  *architectural* cost, not a volumetric one.
- **1K → 10K:** **HIPAA-eligible cloud migration.** Acquiring a BAA, re-doing infra-as-code
  on AWS or GCP, threat-modeling the new perimeter — a one-time cost amortized over the
  user base. Estimated at **TBD engineer-weeks**, *not* a recurring line item.
- **10K → 100K:** Regional deployments and dedicated model capacity. Anthropic's
  enterprise pricing has volume discounts and provisioned-throughput options that change
  the unit economics — the slope of the LLM cost curve flattens here.

---

## 6. Why It Isn't Linear — the Levers

Five levers bend the cost curve away from `cost-per-request × volume`. Each is named, the
direction it bends, and the tier it activates.

| Lever | Direction | Mechanism | Activates at |
|---|---|---|---|
| **Prompt cache hit rate** | ↓ unit cost | System prompt + tool schemas served at ~10% of base input rate after first call in a session | Every tier; benefit is largest at MVP because session reuse dominates |
| **Discrepancy cache hit rate** | ↓ slow-lane volume | Fast-lane queries read pre-warmed flags from cache instead of re-running synthesis. The slow lane is the warmer; the fast lane is the consumer (ARCHITECTURE.md §2.3) | 100 users onward; **the design's central cost lever**, not just a latency lever |
| **Model-tier routing** | ↓ unit cost | A "is this a synthesis question or a retrieval question?" classifier sends retrieval-shaped queries to Haiku-class even on the slow surface. Currently rejected at MVP (one model tier per lane) — eligible for 1K+ tier when eval data shows it doesn't degrade synthesis quality | 1K users |
| **Provisioned throughput / volume contract** | ↓ unit cost | Anthropic enterprise pricing trades commit for discounted rate. Only economic past a sustained volume threshold | 10K users |
| **Fast-lane fine-tune or distillation** | ↓ unit cost (large) | A fine-tuned small model for the bounded fast-lane shape (between-room retrieval over a stable schema) substitutes Haiku-class API calls with cheaper inference. Only justified when fast-lane volume × Haiku unit cost exceeds fine-tune project cost | 100K users |

**The discrepancy cache lever is the one to internalize.** It's the design's central
cost story (slow-lane *is* the warmer for the fast lane), and it converts a high-cost
slow-lane synthesis call into a free flag read at the fast-lane budget. A 90%
discrepancy-cache hit rate during clinic hours doesn't reduce slow-lane volume —
slow-lane runs once per patient regardless — but it prevents fast-lane queries from
falling back to on-demand recomputation, which would shift fast-lane unit cost ~5×
upward. The cache hit rate is therefore directly tied to the fast-lane cost line and is
on the eval dashboard for that reason, not just for latency.

**What we explicitly are not betting on for the MVP cost story.** Streaming (already
rejected for trust reasons; would not change cost), local/open-source inference (would
require operating a model serving stack we don't have the headcount for), and ad-hoc
batching of unrelated user requests (savings small, latency penalty large).

---

## 7. Assumptions Ledger

Every dollar figure in §5 derives from the assumptions below. *Measured* assumptions are
backed by the eval run; *estimated* assumptions are defended-but-not-yet-measured and
are the highest-leverage targets for sharper numbers post-Final.

| # | Assumption | Status | Source / how to validate |
|---|---|---|---|
| A1 | 22 clinical user-days per active user per month | Estimated | Industry baseline; sensitivity in §8 |
| A2 | ~20 patients per clinic day | Estimated | USERS.md §1 |
| A3 | 2–4 fast-lane queries per patient | Estimated | To be measured during demo + observed clinical pilots |
| A4 | Prompt cache hit rate ≥70% | Target | Measured by LangSmith from PR 20 onward |
| A5 | Discrepancy cache hit rate ≥90% during clinic hours | Target | Measured by `agent-service` cache instrumentation (PR 14) |
| A6 | Slow-lane retrieved context ≈ 3–5× fast-lane retrieved context | Estimated | Measurable from a single eval run |
| A7 | Output tokens bounded by Pydantic schema | Confirmed | ARCHITECTURE.md §3 — structured output is enforced |
| A8 | Audit log row size ~500 bytes | Estimated | PR 2 schema; check after first deploy |
| A9 | Anthropic prompt cache pricing at ~10% of base input | Pinned | Anthropic Console; rechecked at Final |
| A10 | No LangChain in stack → no per-request framework overhead | Confirmed | ARCHITECTURE.md §8 — `@traceable` on raw SDK |

---

## 8. Sensitivity / Bounds

Worst / expected / best per **user-day** at the MVP tier (100 users). Everything else
scales from this row using the tier table levers.

| Scenario | Cache hit rate (prompt) | Discrepancy cache hit | Notes | Per user-day cost |
|---|---|---|---|---|
| **Worst** | 0% | 0% | Cold deploy, every request goes to base rates, fast lane recomputes flags via Sonnet | ~$3.10 |
| **Expected** | 70% | 90% | Steady-state operation after a normal clinic morning | ~$1.10 |
| **Best** | 90% | 95% | Mature deployment, well-warmed cache, repeat user | ~$0.85 |

Headline: a 3.6× spread between worst and best — almost entirely explained by the two
cache hit rates. Quoting the expected number alone hides the operational risk of a
cold deploy (every Monday morning if the cache TTL is short) costing ~3× the steady-
state.

The **range itself is the answer to a hospital CTO question**, not a point estimate.
Quoting the expected number without the worst-case bound conceals the most important
thing about LLM cost economics.

---

## 9. Actual Dev Spend

This section is populated **during** the project, not at the end. Two sources:

1. **Anthropic Console usage dashboard** — daily spend during development. Screenshot at
   each submission checkpoint and check into `docs/cost-evidence/`.
2. **LangSmith spend dashboard** (PR 20 onward) — token + cost per traced request,
   broken down by suite (eval / dev / demo).

| Phase | Spend (USD) | Source |
|---|---|---|
| Architecture defense (Mon–Tue) | TBD | Anthropic Console |
| MVP build (Tue–Thu) | TBD | Anthropic Console |
| Early submission eval runs | TBD | LangSmith |
| Final submission eval runs + demo recording | TBD | LangSmith |
| **Total dev spend** | **TBD** | |

The dev-spend total is the **floor** on the per-100-user monthly cost — if I can't run
the eval suite for less than the projected per-user-month cost, the projection is wrong.

---

## 10. Open Questions

1. Anthropic prompt-cache TTL behavior across multi-replica `agent-service` — does
   each replica maintain its own cache, or is it tied to API key + content hash? Affects
   §5 1K-user row.
2. Whether use-case-4 (discrepancy synthesis) is best served by Sonnet-class or by a
   verifier-model second pass on a Haiku-class draft. Eval (PR 22–23) is the input.
3. LangSmith pricing tier crossover — confirm the 1K and 10K user breakpoints against
   current LangSmith pricing.
4. Audit-log retention beyond 30 days. HIPAA's *minimum* is 6 years; the operator-side
   cost of compliant long-term retention is not in this document and changes the 10K-tier
   numbers materially.
