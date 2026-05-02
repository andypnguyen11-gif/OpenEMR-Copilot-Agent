---
  Clinical Co-Pilot — Product Requirements Document (v3)

  Status: Working PRD — feeds USERS.md, AUDIT.md, ARCHITECTURE.md
  Last updated: 2026-04-27
  Owner: [you]

  Changes from v2:
  - User wording softened to align with case study's "primary care physician" example while
  preserving the cross-coverage angle (§2)
  - New subsection "Domain constraints scope" added to Verification Architecture, explicitly
  mapping the case study's "clinical rules, dosage thresholds, interaction flags" language to
  what's in scope vs. out of scope (§5)
  - Use case 3 ("conflicts/missing data") given a one-line tie-in to domain constraints (§3)

  ---
  1. Product Overview

  Problem. A clinician walking into an unfamiliar patient's room has 90 seconds to recall who
  they're seeing, what's changed, and what matters today. Today that means scanning dense EHR
  notes, flipping through labs, cross-referencing meds — under time pressure, with a patient
  already waiting. Errors here can directly harm patients.

  Product. A Clinical Co-Pilot embedded in OpenEMR that gives a covering clinician fast,
  source-grounded context on patients they don't know yet — with verification strong enough that
  every claim traces to the chart.

  MVP Goal. A deployed agent serving four use cases for one user, with verification and
  observability wired in, evaluated against an adversarial test suite.

  Non-Goal. A general-purpose clinical chatbot. A diagnostic or treatment-recommendation system.
  A patient-facing tool.

  ---
  2. Target User

  Primary User: Primary care / internal medicine physician serving in cross-coverage for a
  colleague's ambulatory patient panel. Faces a panel of unfamiliar follow-up patients with
  established histories. Two distinct moments in the day:

  - Pre-clinic (variable, 0–30 min before clinic starts; the architecture does not assume
  this window exists — pre-warming is server-triggered): reviewing today's panel, prepping
  for unknown patients when time allows
  - Between rooms (≈90 sec): refreshing context on the next patient before walking in

  Why this user (and not the case study's other examples):

  - The case study lists "a primary care physician with a 20-patient day, an ED resident on
  overnight intake, a hospitalist rounding on twelve admissions" as examples, not a fixed menu. A
   cross-coverage primary-care physician is a specialization of the first example with a stronger
   agent-shape defense.
  - Hospitalist / ED resident → wrong codebase fit. OpenEMR is ambulatory; inpatient/ED workflows
   fight the data model.
  - PCP with their own panel of known patients → weak agent defense. Often a dashboard is
  sufficient ("what's new since last visit").
  - Cross-coverage primary-care → strong agent defense. Stranger-patient workflow needs
  interrogation of unfamiliar history, not enumeration of known facts.

  Honesty disclosure. This user profile is derived from the case study, OpenEMR's actual data
  model, and safety-design heuristics — not from primary clinician interviews. This affects which
   "right shape" claims are confident vs. reasoned. Validation with a real cross-coverage
  physician is post-MVP work.

  Secondary RBAC Wrinkle (light scope): Resident under attending supervision. Same workflow as
  covering clinician + supervisor read-access to resident activity. Used as an authorization
  story to demonstrate multi-user enforcement, not as a separate workflow.

  ---
  3. Use Cases

  Every use case must answer: why a conversational agent and not a dashboard?

  #: 1
  Use case: "What's changed since the last visit?"
  Lane: Slow
  Why an agent?: Open-ended scope; clinician follows threads ("what about that abnormal TSH?").
    Multi-turn.
  ────────────────────────────────────────
  #: 2
  Use case: "What are the active problems, meds, allergies, and abnormal labs?"
  Lane: Both
  Why an agent?: Synthesis across structured + narrative records. Agent renders facts as cards +
    cited prose summary.
  ────────────────────────────────────────
  #: 3
  Use case: "What conflicts or missing data should I verify?" (differentiating feature)
  Lane: Slow (precomputed)
  Why an agent?: Discrepancies aren't a list view. Cross-source reasoning ("med list says
    metoprolol, last note says discontinued"). No dashboard surfaces this naturally. Also the
    surface for in-scope domain constraints (allergy conflicts, value-sanity flags) — see §5.
  ────────────────────────────────────────
  #: 4
  Use case: "What should I know before walking in?"
  Lane: Fast
  Why an agent?: Compressed, prioritized briefing. Different patients need different summaries;
    static templates fail.

  Differentiating-feature thesis. Use case 3 is what makes this agent worth building. Discrepancy
  detection is uniquely agent-shaped because it requires cross-source reasoning, can't be
  pre-rendered as a dashboard, and produces output (flags) that is genuinely useful to a covering
   clinician with no chart memory. It is also the surface where the agent honors the case study's
   domain-constraint requirement — flagging existing safety issues in the chart, not generating
  new clinical recommendations.

  Multi-turn / session model. Within a chat session, conversation history is held in-memory and
  used as context for follow-up questions ("any prior reaction to ACE inhibitors?", then "what
  about beta blockers?"). When the session ends (panel closed, patient switched, timeout),
  conversation history is dropped. Persistent chat history is explicitly out of scope for MVP
  (see §11).

  ---
  4. Two Latency Budgets

  A core design decision: speed-vs-completeness is bifurcated, not blended.

  ┌─────────────────┬─────────────┬────────┬──────────────────────────────┬────────────────┐
  │      Lane       │   Budget    │  Use   │      Verification depth      │   Candidate    │
  │                 │             │ cases  │                              │     model      │
  ├─────────────────┼─────────────┼────────┼──────────────────────────────┼────────────────┤
  │ Slow lane       │ 10–20s      │ 1, 2,  │ Full: citation + field-check │ Claude Sonnet  │
  │ (pre-clinic)    │ acceptable  │ 3      │  + discrepancy + (post-MVP)  │ (candidate)    │
  │                 │             │        │ verifier model               │                │
  ├─────────────────┼─────────────┼────────┼──────────────────────────────┼────────────────┤
  │                 │             │        │ Lighter: citation +          │                │
  │ Fast lane       │ <5s target  │ 2, 4   │ field-check, lean on         │ Claude Haiku   │
  │ (between rooms) │             │        │ precomputed flags from slow  │ (candidate)    │
  │                 │             │        │ lane                         │                │
  └─────────────────┴─────────────┴────────┴──────────────────────────────┴────────────────┘

  Implication: the slow lane pre-warms the fast lane. Discrepancy detection and patient summaries
   computed pre-clinic are cached; between-room queries hit cache + light synthesis instead of
  cold-fetch + verify. Critically, the slow-lane pre-warm runs as a server-side background pass
  (triggered by schedule load, EMR login, or pre-clinic cron) — *not* by the clinician opening
  the Daily Brief. The Daily Brief is one consumption surface; the cache warms whether or not it
  is used. This decouples the architecture from any assumption about clinician prep time, which
  varies in practice from 0 to ~30 minutes.

  Model selection note. Anthropic Claude is committed as the LLM provider (clinical reasoning
  quality, structured-output support, tool use, BAA availability). Specific model tier per lane
  is candidate-only until eval and cost data are gathered — production model selection happens
  after the first eval run. Two-model strategy itself is locked; the specific tier names may
  shift.

  ---
  5. Verification Architecture

  Principle: Deterministic where possible. Probabilistic only where necessary.

  Layered approach (in order of necessity):

  1. RBAC at the tool layer. Authorization is verification. Every tool call: verify token → RBAC
  check → fetch scoped data. Never fetch then check.
  2. Retrieval-first rendering for hard facts. Meds, allergies, labs, problems → render directly
  from records as structured cards. Not generated text. Eliminates an entire class of
  hallucination.
  3. Citation-required structured output for synthesis. Prose claims emit {claim, source_id,
  source_field}. Schema enforced.
  4. Programmatic field-level check. Middleware looks up each source_id, asserts the cited field
  supports the claim. Failure → reject/abstain.
  5. Discrepancy / data-quality / domain-constraint rules engine. Cross-source consistency,
  staleness, value-sanity, in-scope clinical safety flags. Powers use case 3, runs as background
  pass, also acts as verification telemetry.
  6. Verifier model on synthesis claims. Slow lane only. Post-MVP. Deferred because (4) catches
  the dominant failure mode at lower cost.
  7. Abstain-and-cite behavior. When grounding is weak, agent says "I don't know" rather than
  guessing.

  Domain constraints scope

  The case study explicitly calls for awareness of "clinical rules, dosage thresholds,
  interaction flags." For MVP these are scoped narrowly and live inside the rules engine (layer
  5):

  In scope (MVP):
  - Record-consistency rules — med list vs. last note disagreement; allergy on intake form not in
   allergy table; problem listed but no associated note.
  - Data-quality rules — missing required field for a chronic-condition follow-up; lab not drawn
  within expected window; duplicate or contradictory entries.
  - Basic clinical safety flags read from existing chart data — allergy conflict surfaced when an
   active med matches a recorded allergen; medication interaction flags surfaced when OpenEMR's
  own data already indicates a flagged combination.
  - Value-sanity rules — lab values outside plausible ranges; vital signs that contradict
  surrounding context.

  Out of scope (MVP):
  - Treatment recommendation logic.
  - Dosage suggestion or adjustment.
  - Novel interaction detection beyond what's already encoded in chart data or in a small ruleset
   of widely-known conflicts.
  - Clinical decision support beyond surfacing what the chart already says.
  - Specialty-specific guideline checking.

  The principle: the agent flags domain-rule violations already present in the chart; it does not
   generate new clinical recommendations. This keeps the agent in retrieval/synthesis territory
  (where verification is tractable) and out of advice-giving territory (where verification is
  much harder and the regulatory surface expands).

  Failure granularity

  - Fast lane: whole-response abstain on any verification failure. Nuance is unread between
  rooms; safer to block.
  - Slow lane: per-claim marking. Failed claims rendered as "unverified, please check chart";
  verified claims render normally.

  Abstention taxonomy

  (An enum, not free text.)

  ┌─────────────────────┬────────────────────────┬───────────────────────────────────────────┐
  │        State        │        Meaning         │                    UX                     │
  ├─────────────────────┼────────────────────────┼───────────────────────────────────────────┤
  │ NO_DATA             │ Field is empty in      │ Render the negative as the answer ("No    │
  │                     │ record                 │ allergies on file")                       │
  ├─────────────────────┼────────────────────────┼───────────────────────────────────────────┤
  │ VERIFICATION_FAILED │ Claim drafted but not  │ "Unable to verify — please check chart    │
  │                     │ grounded               │ directly"                                 │
  ├─────────────────────┼────────────────────────┼───────────────────────────────────────────┤
  │ TOOL_FAILURE        │ Transient infra        │ "Could not retrieve — retry?"             │
  │                     │ failure                │                                           │
  ├─────────────────────┼────────────────────────┼───────────────────────────────────────────┤
  │ UNAUTHORIZED        │ RBAC denied access     │ "You don't have access to this record" +  │
  │                     │                        │ audit log entry                           │
  └─────────────────────┴────────────────────────┴───────────────────────────────────────────┘

  These are not interchangeable phrasings. Each one means something different to the clinician.

  Architecture for verification

  Agent draft (structured: claims + source_refs)
          ↓
  Verification Middleware (between agent and UI)
    ├─ citation existence check
    ├─ field-level value check
    ├─ discrepancy + domain-constraint flag enrichment
    └─ fail/abstain decision per granularity rule
          ↓
  UI (cards + verified prose + flags)

  Middleware is a service module, not prompt magic. Easier to test, model-independent, reusable.

  Streaming

  Decided: non-streaming for MVP. Streaming partial unverified tokens undermines the trust story
  (verification is whole-response). The flow is buffer → verify → display. Perceived-latency
  mitigation comes from pre-warming the fast lane via slow-lane caching, not from streaming.

  ---
  6. RBAC / Authorization Model

  Trust handoff: OpenEMR's PHP session is the source of truth. PHP gateway signs a short-lived
  (5-min) HMAC token with {user_id, role, patient_id, scopes, exp}. Python agent service verifies
   signature; token claims become the trust boundary for every tool call.

  Per-tool enforcement: Every tool re-checks RBAC against token claims before fetching. No tool
  fetches PHI then decides whether to return it.

  Patient context binding: When the in-chart side panel opens for patient X, the chat session is
  bound to patient_id=X. Switching patients = new session. The agent cannot drift to
  wrong-patient mistakes mid-conversation.

  Session lifecycle:

  - Session created when chat panel opens (in-chart) or Daily Brief query begins (pre-clinic)
  - Token renewed on each request from PHP gateway (always within 5-min validity)
  - In-memory conversation history accumulates within the session
  - Session ends on: panel close, patient switch, idle timeout, explicit logout
  - On session end: conversation history dropped (not persisted); agent audit log entries persist

  Roles for MVP:

  - physician — full read on assigned cross-coverage panel
  - resident — same read scope as physician, but every action logged for supervisor review
  - supervisor — read on supervised resident's activity log

  The supervisor role expands **audit visibility, not PHI permissions** — they see the
  same patient data a covering attending would see, plus read-access to their supervised
  resident's activity log. No role in MVP unlocks PHI beyond what its base clinical scope
  already grants.

  Out of scope for MVP: break-glass emergency access, role overrides, fine-grained scopes per
  data type.

  ---
  7. UI / Integration

  Two surfaces, one stack:

  ┌──────────────────┬──────┬────────────────────────────────────────────────────────────────┐
  │     Surface      │ Lane │                        Where in OpenEMR                        │
  ├──────────────────┼──────┼────────────────────────────────────────────────────────────────┤
  │ Daily Brief      │ Slow │ New page; renders today's panel with precomputed flags +       │
  │                  │      │ per-patient briefings                                          │
  ├──────────────────┼──────┼────────────────────────────────────────────────────────────────┤
  │ In-chart Side    │ Fast │ Patient summary / encounter view; chat scoped to current       │
  │ Panel            │      │ patient                                                        │
  └──────────────────┴──────┴────────────────────────────────────────────────────────────────┘

  Implementation approach: Smarty templates + lightweight embedded JS (vanilla, htmx, or Alpine).

  On framework choice: React is deferred for MVP to avoid fighting OpenEMR's host UI (legacy
  frame layout, Smarty templates, jQuery patterns). Embedded widgets ship faster and integrate
  cleanly with the existing chrome. A React-based reimplementation is a reasonable Phase 2
  evolution if the agent surface area grows.

  Backend split: PHP gateway in OpenEMR + Python agent sidecar. PHP handles session/auth and
  proxies signed requests; Python does orchestration, tool calls, verification.

  Data access: FHIR/REST APIs primary (RBAC enforced server-side); custom PHP gateway endpoints
  only for FHIR coverage gaps. No direct MySQL from Python.

  ---
  8. Tech Stack

  ┌────────────────┬────────────────────────────────┬────────────────────────────────────────┐
  │   Component    │             Choice             │                  Why                   │
  ├────────────────┼────────────────────────────────┼────────────────────────────────────────┤
  │ Frontend       │ Smarty + vanilla/Alpine JS in  │ Lowest friction with host system;      │
  │                │ OpenEMR                        │ React deferred                         │
  ├─────────────────┼───────────────────────────┼───────────────────────────────────────────┤
  │ Backend (PHP    │ Built into OpenEMR fork   │ Session-aware, signs tokens               │
  │ gateway)        │                           │                                           │
  ├─────────────────┼───────────────────────────┼───────────────────────────────────────────┤
  │ Agent service    │ Python + FastAPI          │ Async, Pydantic-native, AI ecosystem      │
  ├──────────────────┼───────────────────────────┼───────────────────────────────────────────┤
  │ Agent framework  │ Plain Python + Anthropic  │ LangGraph adds value when graphs branch;  │
  │                  │ SDK + Pydantic            │ we have one orchestrator                  │
  ├──────────────────┼───────────────────────────┼───────────────────────────────────────────┤
  │ LLM provider     │ Anthropic Claude          │ Clinical reasoning, structured output,    │
  │                  │ (committed)               │ tool use, BAA-eligible                    │
  ├───────────────────┼──────────────────────────┼──────────────────────────────────────────┤
  │                   │ Sonnet (slow lane),      │ Final tier selection after eval/cost     │
  │ LLM model tier    │ Haiku (fast lane) —      │ data                                     │
  │                   │ candidate                │                                          │
  ├───────────────────┼──────────────────────────┼──────────────────────────────────────────┤
  │ Cache             │ In-process Python TTL    │ Redis is operational debt without payoff │
  │                   │ cache for hot flags +    │  at MVP scale                            │
  │                   │ Postgres-backed durable  │                                          │
  │                   │ cache for precomputed    │                                          │
  │                   │ artifacts. No Redis MVP. │                                          │
  ├────────────────────────┼─────────────────────────┼───────────────────────────────────────┤
  │ Agent metadata DB      │ Railway managed         │ Eval results, traces, audit log —     │
  │ (deployed)             │ Postgres                │ persistence matters; Railway volumes  │
  │                        │                         │ have redeploy edge cases              │
  ├────────────────────────┼─────────────────────────┼───────────────────────────────────────┤
  │ Agent metadata DB      │ SQLite                  │ Zero-setup for development            │
  │ (local dev)            │                         │                                       │
  ├────────────────────────┼─────────────────────────┼───────────────────────────────────────┤
  │                        │ Same Postgres, separate │ HIPAA-relevant access logging —       │
  │ Agent audit log        │  table; retention TBD   │ durability is a requirement, not a    │
  │                        │                         │ nice-to-have                          │
  ├────────────────────────┼─────────────────────────┼───────────────────────────────────────┤
  │ Observability          │ LangSmith (cloud, free  │ Traces, token cost, latency + annota- │
  │                        │ tier)                   │ tion queue for human-graded eval (use │
  │                        │                         │ case 4); Claude Code MCP integration  │
  │                        │                         │ for iteration loop (post-MVP)         │
  ├────────────────────────┼─────────────────────────┼───────────────────────────────────────┤
  │ Eval framework         │ Custom Python harness,  │ Faster to iterate at MVP volume than  │
  │                        │ JSON test cases         │ Braintrust                            │
  ├────────────────────────┼─────────────────────────┼───────────────────────────────────────┤
  │ Discrepancy /          │ Python module, rules as │                                       │
  │ domain-constraint      │  YAML/JSON config       │ Grow rule set without redeployment    │
  │ rules                  │                         │                                       │
  └────────────────────────┴─────────────────────────┴───────────────────────────────────────┘

  LLM cost+latency strategy: Sonnet-class in slow lane (where 10–20s tolerates depth),
  Haiku-class in fast lane (where <5s demands speed). Two-model split is intentional; aligns
  directly with two latency budgets. Specific tier choice pending eval.

  ---
  9. Deployment

  MVP target: Railway. Three services + two managed databases:

  ┌──────────────────────────┬──────────────────────────────────────────────────────┐
  │ Railway service / plugin │                   Container / role                   │
  ├──────────────────────────┼──────────────────────────────────────────────────────┤
  │ openemr-web              │ OpenEMR fork (Apache + PHP)                          │
  ├──────────────────────────┼──────────────────────────────────────────────────────┤
  │ agent-service            │ Python/FastAPI sidecar                               │
  ├──────────────────────────┼──────────────────────────────────────────────────────┤
  │ openemr-db               │ Managed MariaDB plugin (OpenEMR system of record)    │
  ├──────────────────────────┼──────────────────────────────────────────────────────┤
  │ agent-db                 │ Managed Postgres plugin (agent metadata + audit log) │
  └──────────────────────────┴──────────────────────────────────────────────────────┘

  Why Railway over single VPS: native multi-service, GitHub auto-deploy, built-in observability,
  auto-provisioned demo URL. Maps the architecture's logical separation to deployment primitives.

  Known constraint — HIPAA: Railway is not HIPAA-eligible (no BAA). Acceptable for the case study
   (demo data only) but must be flagged in the architecture defense:

  ▎ "Demo deployment uses Railway, which is not HIPAA-eligible. Production deployment would
  ▎ target a HIPAA-eligible operator (AWS+BAA, GCP+BAA, Aptible, Datica). The architecture port
  ▎ is straightforward; the operator changes. Cost analysis at higher user tiers reflects this
  ▎ migration."

  Other Railway constraints to plan around:

  - Cold starts on agent service may break fast-lane budget → keep service warm (heartbeat or
  always-on tier).
  - File uploads (OpenEMR documents) need a committed volume; out of MVP scope but worth noting.
  - Use Railway's managed databases rather than self-hosted in compose — both for OpenEMR
  (MariaDB) and agent metadata (Postgres).

  ---
  10. Failure Modes

  ┌─────────────────────────────────┬────────────────────────────────────────────────────────┐
  │             Failure             │                        Behavior                        │
  ├─────────────────────────────────┼────────────────────────────────────────────────────────┤
  │ Tool times out                  │ Surface TOOL_FAILURE, offer retry; do not silently     │
  │                                 │ substitute                                             │
  ├─────────────────────────────────┼────────────────────────────────────────────────────────┤
  │ Tool returns partial data       │ Continue, mark missing fields with NO_DATA, do not     │
  │                                 │ fabricate                                              │
  ├─────────────────────────────────┼────────────────────────────────────────────────────────┤
  │ middleware rejects a   │ per-claim)                                                      │
  │ claim                  │                                                                 │
  ├────────────────────────┼─────────────────────────────────────────────────────────────────┤
  │ LLM unavailable /      │ Surface failure, fall back to retrieval-only fact cards if      │
  │ rate-limited           │ possible                                                        │
  ├────────────────────────┼─────────────────────────────────────────────────────────────────┤
  │ Cold start exceeds     │ Surface "warming up, please retry" rather than partial answer   │
  │ latency budget         │                                                                 │
  ├────────────────────────┼─────────────────────────────────────────────────────────────────┤
  │ Discrepancy cache      │ Use TTL + write-invalidation; on miss, recompute synchronously  │
  │ stale                  │                                                                 │
  ├────────────────────────┼─────────────────────────────────────────────────────────────────┤
  │ Authorization denied   │ Terminate session, log to audit trail, surface UNAUTHORIZED     │
  │ mid-session            │                                                                 │
  ├────────────────────────┼─────────────────────────────────────────────────────────────────┤
  │ Wrong-patient context  │ Prevented by session-bound patient_id; switching = new session  │
  │ risk                   │                                                                 │
  ├────────────────────────┼─────────────────────────────────────────────────────────────────┤
  │ Agent metadata DB      │ Trace/eval logging degrades to local file buffer; critical      │
  │ unreachable            │ audit-log writes block the request and surface error (audit log │
  │                        │  integrity > availability)                                      │
  └────────────────────────┴─────────────────────────────────────────────────────────────────┘

  ---
  11. Non-Goals (explicitly out of scope for MVP)

  - Persistent chat history across sessions
  - Supervisor audit-trail viewer UI (logs exist, viewer deferred)
  - Long-term memory / learned clinician preferences
  - Document and imaging integration
  - Patient-facing surfaces
  - Voice / mobile / wearable
  - Verifier-model second pass (deferred until eval shows it's needed)
  - Multi-agent / specialist routing
  - Streaming response
  - Diagnostic or treatment recommendations
  - Order entry
  - Specialty-specific workflows
  - Break-glass emergency access
  - Predictive risk models
  - Autonomous actions on the chart
  - Treatment-recommendation logic, dosage suggestion, novel interaction detection

  ---
  12. Risks & Audit-Dependent Assumptions

  These are assumptions now. The audit either confirms them or kills them. Architecture changes
  accordingly.

  1. OpenEMR FHIR API covers use cases 1–4. If gaps, custom PHP gateway endpoints expand.
  2. OpenEMR FHIR/REST handlers enforce ACL consistently with the PHP UI. If not, FHIR can't be
  trusted; everything routes through PHP gateway.
  3. OpenEMR session identity can be safely mapped to short-lived signed claims without privilege
   escalation. Specifically: (a) PHP gateway can read session role/scope without spoofing risk;
  (b) FHIR endpoints re-check resource-type scope; (c) tokens can't be replayed (short expiry +
  nonce or session-bound). Note: per-patient access (a clinician asking for a patient outside
  their panel) is **not** caught at the FHIR layer because the agent uses `system/*` SMART
  Backend Services scopes — that gate lives at the PHP gateway. See ARCHITECTURE.md §4.5.
  4. Discrepancy invalidation hooks are reachable in OpenEMR's write paths. If not, fall back to
  TTL-only freshness with longer windows.
  5. Sample data is rich enough for use case 3. If too sterile, hand-craft adversarial
  conflicting records for demo + eval.
  6. Smarty injection points allow adding side-panel partials without forking core templates.
  7. Railway cold starts don't blow fast-lane budget. If they do, switch to always-on service
  tier or warming heartbeat.
  8. OpenEMR's existing data model exposes the safety flags we need for in-scope domain
  constraints (allergy/med matching, encoded interaction flags). If the data model doesn't carry
  them, the rules engine narrows to record-consistency only and we re-scope use case 3.

  Each of these is a sentence in ARCHITECTURE.md and a section in AUDIT.md.

  ---
  13. Success Criteria

  MVP succeeds if:

  - All four use cases execute end-to-end on deployed app with demo data
  - Fast lane returns answer ≤5s p50 for warm cache; slow lane ≤20s p95
  - 100% of generated factual claims carry source citations OR are abstained per taxonomy
  - Authorization probes (e.g., "tell me about patient_id 999" when not assigned) are blocked at
  tool layer with audit log entry
  - Adversarial eval suite covers: missing data, ambiguous queries, RBAC bypass attempts,
  conflicting records, stale data, fabrication probes
  - Eval suite passes ≥90% overall, and 100% on RBAC / security cases. Security failures are not
  a "mostly pass" category — any RBAC bypass is a stop-ship.
  - Observability (LangSmith) shows per-request trace, latency, token cost, tool calls
  - Architecture defense (Tuesday) holds without "we'll figure that out later" answers

  ---
  14. Open Questions Still to Resolve

  These don't block writing ARCHITECTURE.md, but should be answered before/during build:

  1. Where in OpenEMR's nav does the Daily Brief live? New top-nav entry vs. embedded in calendar
   view.
  2. Where does the in-chart side panel attach? Right sidebar, bottom drawer, modal — affects how
   much chrome you fight.
  3. Sample data sufficiency — verify in audit; if too clean, plan a "seeded discrepancy" data
  fixture.
  4. Eval ground truth source — case study calls for measurable correctness; for use case 3,
  ground truth is "the discrepancy is in the data we seeded." For use case 4 ("what should I
  know"), ground truth is harder. Likely needs human-graded subset.
  5. Audit log retention policy — what's the minimum window the agent's access log must cover?
  HIPAA expectations vs. MVP storage cost.
  6. Domain-constraint rule sourcing — for the small set of "obvious" interaction/allergy flags,
  do we author them ourselves or pull from a public list (e.g., RxNorm / DrugBank subset)?
  Affects scope and licensing.

  ---
  15. Cost Analysis (preview — full version in COST.md)

  Full analysis lives in COST.md (per-request token math, per-user-day volume, tier
  table with $/month, sub-linear cost levers, dev spend). The preview below is the
  architectural shape only.

  Architecture inflection points to model:

  ┌─────────┬────────────────────────────────────────────────────────────────────────────────┐
  │  Tier   │                                  What changes                                  │
  ├─────────┼────────────────────────────────────────────────────────────────────────────────┤
  │ 100     │ Single Railway agent service, in-memory cache. Cost dominated by LLM tokens.   │
  │ users   │                                                                                │
  ├─────────┼────────────────────────────────────────────────────────────────────────────────┤
  │ 1K      │ Multi-process agent service, Redis cache, managed DB upgrade. Add background   │
  │ users   │ worker for discrepancy.                                                        │
  ├─────────┼────────────────────────────────────────────────────────────────────────────────┤
  │ 10K     │ Migrate to HIPAA-eligible cloud (AWS + BAA). Read replicas, queue-based        │
  │ users   │ discrepancy pipeline, model routing/caching.                                   │
  ├─────────┼────────────────────────────────────────────────────────────────────────────────┤
  │ 100K    │ Regional deploys, dedicated model capacity (or fine-tuned small models for     │
  │ users   │ fast lane), full observability stack, on-call rotation.                        │
  └─────────┴────────────────────────────────────────────────────────────────────────────────┘

  ---
  16. Document Map

  This PRD is the working source of truth. Graded artifacts pull from it:

  - USERS.md ← sections 2, 3
  - AUDIT.md ← driven by section 12, with audit findings filling the assumptions
  - ARCHITECTURE.md ← sections 4–10, with section 5 expanded into the verification chapter
