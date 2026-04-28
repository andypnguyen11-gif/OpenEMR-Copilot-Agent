# USERS.md — Clinical Co-Pilot

**Status:** Draft for case-study submission
**Last updated:** 2026-04-27
**Role:** Source of truth for agent scope. ARCHITECTURE.md traces back to this document; every agent capability must map to a use case below.

---

## Executive Summary

The Clinical Co-Pilot is built for one specific user: a **primary-care / internal-medicine physician serving in cross-coverage on a colleague's ambulatory patient panel.** Not a generic "physician." Not a hospitalist. Not an ED resident. A clinician who walks into clinic on a Tuesday morning, opens a schedule of 20-some unfamiliar follow-up patients, and has roughly thirty minutes before the first room and roughly ninety seconds between subsequent rooms to figure out who each patient is, what's changed, and what actually matters today.

This user is chosen deliberately. The case study lists "a primary care physician with a 20-patient day, an ED resident on overnight intake, a hospitalist rounding on twelve admissions" as **examples, not a fixed menu.** A cross-coverage primary-care physician is a sharp specialization of the first example with a stronger agent-shape defense than any of the three: hospitalist and ED resident both fight OpenEMR's ambulatory data model; a PCP working their own known panel can usually be served by a "what's new since last visit" dashboard and doesn't need an agent at all. Cross-coverage is the workflow where the user has *no chart memory* of the patient, has *no time* to acquire it the long way, and needs a system that can interrogate an unfamiliar history on demand.

The agent enters the user's day at two distinct moments with very different latency-quality tradeoffs:

1. **Pre-clinic, ~30 minutes before doors open.** The user is sitting at a desk with coffee, going through today's panel one patient at a time, building enough context to walk into rooms. The agent is allowed to take 10–20 seconds per patient. Verification depth is full. The output is read carefully.
2. **Between rooms, ~90 seconds.** The user has just left room 4, is walking to room 5, and needs a refresher in time to put their hand on the door. The agent has under 5 seconds. Verification depth is lighter (it leans on flags pre-computed in the slow lane). The output is glanced at.

Four use cases cover the day. Three of them — *what's changed since last visit*, *active problems / meds / allergies / abnormal labs*, and *what should I know before walking in* — are concrete chart-reading tasks that today take dense minutes and produce error under pressure. The fourth — *what conflicts or missing data should I verify* — is the **differentiating feature**: a synthesis the user simply cannot get from a dashboard, because discrepancies aren't a list view, they're a cross-source comparison the chart UI doesn't make. This fourth use case is also the surface where the agent honors the case study's domain-constraint requirement, by flagging what the chart already says rather than generating new clinical recommendations.

Each use case in this document includes an explicit defense of why a conversational agent — and not a dashboard, sorted list, or better chart view — is the right shape for it. That defense is the bar the case study sets, and it is the bar this user makes meetable: a clinician with no chart memory, under time pressure, asking open-ended questions whose follow-ups are not knowable in advance is exactly the user-shape that makes a conversational agent genuinely chosen rather than imposed.

A secondary RBAC scenario — *resident under attending supervision* — is included as an authorization story (same workflow, supervisor read-access to resident activity) to demonstrate multi-user enforcement. It is not a second user persona with its own workflow.

This document was derived from the case study, OpenEMR's actual data model, and safety-design heuristics — not from primary interviews with real cross-coverage clinicians. That caveat is honored explicitly in §1.3.

---

## 1. Target User

### 1.1 Profile

| Attribute | Value |
|---|---|
| Role | Primary care / internal medicine physician |
| Setting | Outpatient ambulatory clinic |
| Today's mode | Cross-coverage for a colleague's panel |
| Panel for the day | ~20 follow-up patients, mostly unknown to the user |
| Time before clinic opens | ~30 minutes for prep |
| Time between rooms | ~60–120 seconds in practice, called "90 seconds" as a planning number |
| Visit type today | Established follow-up (not new-patient intake) |
| Technical fluency | Comfortable with EHR; not interested in agent UX as a tool to learn |
| Tolerance for wrong answers | Effectively zero on RBAC and factual claims; moderate on summary phrasing |
| Tolerance for "I don't know" | High, when the agent abstains explicitly and points to the chart |

The defining feature of this user is **the asymmetry between time pressure and chart unfamiliarity.** A clinician with their own panel knows their patients and uses the chart to refresh specifics. A cross-coverage clinician knows nothing and has to acquire the entire baseline through the chart in compressed windows. The chart UI is built for the first user, not the second.

### 1.2 Why this user (and not the case study's other examples)

The case study lists three example users; each was considered and rejected for specific reasons.

| Candidate user | Why rejected |
|---|---|
| **Hospitalist rounding on inpatient admissions** | OpenEMR is an ambulatory EHR. Inpatient workflows (rounds, sign-out, order management, discharge planning) fight the data model. Building a hospitalist agent on OpenEMR means working around what OpenEMR isn't designed to track. |
| **ED resident on overnight intake** | Same codebase fit problem. ED workflow (triage, undifferentiated complaint, disposition decision) is not what OpenEMR is structured around. Also: ED is a higher-stakes verification surface than the case study's MVP scope can responsibly cover. |
| **Primary care physician with their own panel** | Weak agent-shape defense. A PCP with a known panel has chart memory; "what's new since last visit" is often well-served by a dashboard with a date filter. The bar from the case study is "the agent is the thing the user would actually choose," and a known-panel PCP would often choose the chart they already know. |
| **Cross-coverage primary-care physician** *(chosen)* | Strong agent-shape defense. The user has no chart memory of the patients, can't acquire it the long way under time pressure, and asks open-ended interrogation questions whose follow-ups can't be pre-rendered. This is the workflow where a conversational agent isn't a UI improvement — it's the only viable shape. |

A reasonable critique: cross-coverage isn't every-day work; most days a PCP works their own panel. That's true and accepted. The argument is not that this user is a high-volume persona — it's that this user is the **highest agent-fit surface** in the case study's allowed space, and an agent that earns its keep here generalizes more cleanly than one that has to manufacture justification on a known-panel day.

### 1.3 Honesty disclosure

This user profile is derived from:

- The case study's example list and constraints
- OpenEMR's actual data model and feature set (verified during audit)
- General safety-design and verification-architecture heuristics
- Public knowledge of ambulatory primary-care workflows

It is **not** derived from primary interviews with practicing cross-coverage clinicians. That affects which claims in this document are confident vs. reasoned:

- **Confident:** the user has no chart memory; the chart is dense; the time windows are tight; the existing EHR UI was not built for this workflow shape; verification failures are unacceptable.
- **Reasoned:** the specific 30-min / 90-sec budget framing; the ranking of which queries are most valuable; the assertion that discrepancy detection is the differentiating feature for this user.

Validation with a real cross-coverage clinician is post-MVP work and is called out as a known limitation.

### 1.4 Secondary RBAC user: resident under attending supervision

A second access pattern is included to demonstrate multi-user authorization, not to add a second workflow:

- **Resident** — same read scope as the covering clinician, but every agent action is logged for supervisor review.
- **Supervisor (attending)** — read access to the supervised resident's activity log.

The supervisor role expands **audit visibility, not data permissions.** A supervisor sees the same patient PHI a covering attending would see in their own panel; what's added is the ability to read a supervised resident's activity log. No role in MVP unlocks PHI beyond what its base clinical scope already grants.

This is intentionally light. It exists so the architecture's per-tool RBAC enforcement and audit-log durability have a concrete second use to demonstrate against. The agent does not gain new capabilities for the resident user; it gains a different audit posture.

### 1.5 Authorized users in MVP

Authorized clinical users in MVP are **attending physicians, covering physicians, and supervised residents operating within the same ambulatory primary-care / internal-medicine workflow.** Other clinical specialties (cardiology consultants, surgical specialties, behavioral health), non-clinical staff (billing, scheduling, medical assistants), and patient-facing users are out of scope and are not provisioned to invoke the agent. RBAC enforcement at the tool layer (PRD §6) is the technical mechanism; this subsection states the policy.

---

## 2. Workflow — A Day in the Life

This walks through one specific Tuesday for the target user. Times are illustrative; the structure of the day is not. The agent's entry points are called out explicitly.

### 7:30 AM — arrival

The user logs into OpenEMR, opens today's schedule, sees ~20 patients they don't know. The schedule is the entry surface for the day.

**No agent yet.**

### 7:35 AM — pre-clinic prep (Daily Brief, slow lane)

The user opens the **Daily Brief** — the agent surface designed for this moment. Today's panel is rendered with one card per patient, each showing a precomputed briefing: active problems, recent labs/imaging within an expected window, medication snapshot, and any flags surfaced by the discrepancy engine.

For a flagged patient (say, the agent surfaced "med list shows metoprolol; last note from 2026-03-19 says discontinued"), the user clicks in and the chat opens scoped to that patient. They ask:

- *"What does the most recent note say about the metoprolol?"*
- *"Has the patient had any visits since that note?"*
- *"Any cardiology contact in the last six months?"*

These are open-ended, threaded questions. The user reads the answer carefully because they have time. They write a note to themselves: *check metoprolol with patient.*

For a patient with no flags, the user reads the briefing card, decides whether to dive in, and moves on.

By 8:55 AM, every patient on today's panel has been touched. The user has a paper or mental list of three or four patients who need extra attention.

**Agent latency budget here: 10–20 seconds per query. Verification depth: full.**

### 8:55 AM — final glance at room 1

The user puts the laptop down, walks to the first exam room. Before opening the door, they pull the in-chart side panel and ask: *"What should I know before walking in?"*

A 30-second-read briefing comes back. They read it standing in the hallway. They open the door.

**Agent latency budget here: under 5 seconds. Verification depth: lighter; uses precomputed flags from the Daily Brief.**

### 9:00 AM – 11:30 AM — patient flow, between rooms (in-chart side panel, fast lane)

This is the bulk of the morning. After each patient, the user finishes their note, navigates to the next patient's chart, and opens the side panel. Common between-room queries:

- *"What's changed since their last visit?"*
- *"What are the active problems, meds, allergies, and abnormal labs?"*
- *"Any conflicts I should know about?"*
- *"What should I know before walking in?"*

These are short queries. The user expects compressed, prioritized output. They are reading while walking. If verification fails or a tool is unavailable, the user wants to know that explicitly so they can rely on the chart instead — a confidently-wrong answer is far worse than a "couldn't retrieve."

Some queries are follow-ups, not first-turn:

- The user just got a summary, then asks *"any prior reaction to ACE inhibitors?"*, then *"what about beta blockers?"*

That's multi-turn with carried context — within the same patient session. When the user closes the panel or switches patients, the chat is gone.

**Agent latency budget here: under 5 seconds, p50. Verification depth: lighter; abstain whole-response on any verification failure (this is the safer fast-lane behavior — nuance is unread between rooms).**

### 11:30 AM – 12:00 PM — buffer / finish the morning

The user catches up on notes, returns calls, eats. The agent is not in this loop — note-writing and patient communication are out of MVP scope.

### 1:00 PM — afternoon restart

Same shape as 7:35 AM but compressed. The user revisits the Daily Brief for any afternoon-only patients, or skims the same flagged patients as a refresher.

### 5:30 PM — wrap up

End of clinic. The agent exits the day. Conversation history is dropped on session end (no cross-session memory at MVP).

### Recap: the agent's role across the day

| Moment | Surface | Lane | What the user is doing |
|---|---|---|---|
| 7:35 – 8:55 AM | Daily Brief | Slow | Building baseline context across the panel |
| 8:55 AM – 5:30 PM | In-chart Side Panel | Fast | Refreshing context before each room; chasing follow-up threads |
| Note-writing, calls, orders, education | — | — | **Out of scope.** Agent does not write notes, place orders, or speak to patients. |

---

## 3. Use Cases

Each use case below answers the case study's bar: *why a conversational agent and not a dashboard / sorted list / better chart view.*

### Use Case 1 — "What's changed since the last visit?"

| Field | Value |
|---|---|
| **Trigger** | Slow-lane: clinician opens a patient card in the Daily Brief. Fast-lane: clinician opens side panel before entering the room. |
| **Lane** | Slow (primary); Fast (refresh) |
| **Latency budget** | ≤20s p95 (slow); ≤5s p50 (fast) |
| **Inputs** | patient_id (session-bound), implicit time range "since last visit" |
| **Tools called** | `get_visits`, `get_problems`, `get_meds`, `get_labs`, `get_notes` |
| **Output shape** | Structured cards (problem deltas, med deltas, new labs) + cited prose summary |
| **Verification surface** | Citation-required structured output; programmatic field-level check; per-claim marking on slow lane |
| **Why an agent (not a dashboard)** | The query is open-ended: "changed" is a soft predicate. A dashboard can show diffs in structured fields (problems added, meds discontinued), but the meaningful changes — *the abnormal TSH from two weeks ago, the note saying "patient reports new fatigue"* — sit in narrative records that don't diff cleanly in a UI. The clinician also follows threads ("what about that abnormal TSH?", "any prior thyroid workup?") whose followups aren't pre-renderable. Multi-turn over heterogeneous fields is the agent shape; a dashboard would either show too little or too much. |
| **Success criterion** | All structured deltas accurate; narrative summary 100% cited; abstains explicitly when grounding is weak. |
| **Known failure modes** | Unstructured note text without clear deltas; missing prior-visit data (handled via `NO_DATA` taxonomy). |

### Use Case 2 — "What are the active problems, meds, allergies, and abnormal labs?"

| Field | Value |
|---|---|
| **Trigger** | First query a clinician asks on a new-to-them patient. Both lanes. |
| **Lane** | Both |
| **Latency budget** | ≤20s p95 (slow); ≤5s p50 (fast, often cached from slow-lane pass) |
| **Inputs** | patient_id |
| **Tools called** | `get_problems`, `get_meds`, `get_allergies`, `get_labs`, optionally `get_flags` |
| **Output shape** | Structured cards rendered directly from records (no LLM-generated prose for hard facts) + a short cited synthesis paragraph |
| **Verification surface** | Retrieval-first rendering of cards (eliminates an entire hallucination class); citations on the synthesis paragraph; field-level check. |
| **Why an agent (not a dashboard)** | Two reasons. First, OpenEMR already shows these fields in different parts of the chart UI — pulling them together for a clinician with no chart memory under time pressure is a synthesis act, not a retrieval one. Second, the *cited synthesis paragraph* is what makes this useful: it ranks and highlights ("BP elevated, on ACEi, last A1c 9.4 — this is the patient's main issue today"). A dashboard can render the cards; it can't tell the user what's important. The agent's answer compresses 4 panels of chart data into a 30-second read. That compression is the value. |
| **Success criterion** | Cards match the chart 1:1 (this is non-negotiable); synthesis paragraph cited 100%; no fabricated entries. |
| **Known failure modes** | Stale labs (handled by discrepancy engine flagging staleness); abbreviation/coding drift in problem list (handled by retrieval-first rendering — the agent doesn't paraphrase, it shows the record). |

### Use Case 3 — "What conflicts or missing data should I verify?" *(differentiating feature)*

| Field | Value |
|---|---|
| **Trigger** | Pre-clinic Daily Brief surfaces flags per patient. In-chart side panel exposes a "show me conflicts" query. |
| **Lane** | Slow (precomputed); Fast (cached read) |
| **Latency budget** | Cache hit on fast lane: ≤5s. Background discrepancy pass runs on schedule load and on relevant chart writes. |
| **Inputs** | patient_id |
| **Tools called** | `get_flags` (cache read) → `get_meds`/`get_notes`/`get_allergies`/etc. for drill-in |
| **Output shape** | Flag list with categorized issue type (record-consistency, data-quality, safety-flag, value-sanity), each with source records cited and a one-line rationale. |
| **Verification surface** | Discrepancy engine rules are deterministic, not generated; the agent uses the rules engine output as a tool, it doesn't author rules at runtime. The synthesized explanation is cited and field-checked. |
| **Why an agent (not a dashboard / sorted list / chart view)** | Discrepancies aren't a list view. The flags are a *cross-source reasoning result*: "med list says metoprolol active; last note from 2026-03-19 says discontinued." That comparison sits across two different parts of the chart and isn't surfaced anywhere in the existing OpenEMR UI. A dashboard could surface a static rules result, but the explanation — *which records, why they conflict, what to look at* — is what the clinician actually needs and can chase up with follow-up questions ("which note? what was the indication for stopping?"). The agent is the only shape that connects detection to explanation to drill-in. |
| **Domain-constraint scope (what this use case enforces)** | **In scope:** record-consistency rules (med vs. note disagreement, allergy on intake form not in allergy table); data-quality rules (missing fields, stale labs); basic safety flags read from existing chart data (allergy conflict when an active med matches a recorded allergen, interaction flags when the chart already encodes a flagged combination); value-sanity (lab values outside plausible ranges). **Out of scope:** treatment recommendation, dosage suggestion, novel interaction detection beyond what's already in the chart, specialty-specific guideline checking. **Principle:** the agent flags what the chart already says; it does not generate new clinical recommendations. |
| **Success criterion** | Adversarial test suite of seeded discrepancies — agent surfaces ≥90% with correct categorization and source attribution; zero fabricated discrepancies. |
| **Known failure modes** | Rule set is finite — discrepancies outside the encoded rules are missed. The agent abstains on those (does not invent rules); they show up in the eval gap and feed rule additions. |

### Use Case 4 — "What should I know before walking in?"

| Field | Value |
|---|---|
| **Trigger** | Side panel query immediately before entering a room. |
| **Lane** | Fast |
| **Latency budget** | ≤5s p50 |
| **Inputs** | patient_id, today's visit type/reason if available |
| **Tools called** | `get_flags` (cache), `get_problems`, `get_meds`, `get_visits` (last 1–2) |
| **Output shape** | A compressed, prioritized "30-second briefing" — 3–5 lines, with citations. Often references precomputed flags rather than re-deriving them. |
| **Verification surface** | Same as use cases 1–2; whole-response abstain on any verification failure (fast lane policy). |
| **Why an agent (not a dashboard / template)** | Different patients need different briefings. A diabetic on insulin walking in for a chronic-care follow-up needs a different one-paragraph summary than a younger patient with a recent abnormal Pap. Static templates ("show problems / meds / last labs") don't prioritize, and dashboards can't compress. The clinician wants *what matters today for this patient*, not *all the structured data*. That's a synthesis-and-prioritization task, which is where an LLM-driven agent earns its keep. The agent is also the only shape that can absorb a follow-up ("anything from cardiology recently?") without forcing the user to navigate elsewhere. |
| **Success criterion** | 100% of factual claims cited; prioritization picks issues a chart-experienced clinician would also flag, validated against a hand-graded subset; abstains explicitly when grounding is weak. |
| **Known failure modes** | Eval ground truth is harder for prioritization ("what should the briefing emphasize?") than for retrieval. Likely needs a human-graded eval subset. Tracked as PRD §14 open question 4. |

---

## 4. Why a Conversational Agent (overarching defense)

The case study sets a high bar: *be ready to defend why a conversational agent is the right shape — not a dashboard, not a sorted list, not a better chart view.* The per-use-case defense above is local. This section is the structural defense.

For the cross-coverage clinician, four properties of the workflow rule out simpler shapes.

| Workflow property | Why simpler shapes fail |
|---|---|
| **No chart memory** | The user doesn't know what to look for. A dashboard surfaces fields; it can't surface *what's important for this patient*. The user can't write the right query into a search bar because they don't know enough yet. |
| **Open-ended follow-ups** | The user asks one question, reads the answer, then asks a sharper one. ("Metoprolol — discontinued? when? for what reason?") A dashboard offers no follow-up surface. A search bar requires re-querying from scratch each time. Multi-turn is the natural shape; carrying context across the turns is the value. |
| **Time pressure across heterogeneous data** | The chart already has all the data, in different parts of the UI. The user's bottleneck isn't *finding* anything; it's *synthesizing* across structured (meds, labs) and narrative (notes) records under time pressure. Synthesis is a reasoning task. Dashboards don't reason; they render. |
| **Cross-source consistency questions** | Discrepancy detection (use case 3) is intrinsically cross-source. A list view can show flagged issues, but the explanation — *which records, why they conflict, what to look at next* — is conversational. The flag is the start of the interaction, not the end. |

A reasonable counter-argument is that a **better chart view** could solve uses 1, 2, and 4: a redesigned patient summary screen with smart defaults could compress the relevant context. The problem is that "smart defaults" is the work — *what's relevant for this patient today* — and the agent is what does it. A static reorganization of the chart still presents the same data the same way to every clinician for every patient; the agent re-prioritizes per query. The sharper distinction: **a summary page can prioritize one default view; the agent supports iterative interrogation when the next question depends on the previous answer.** That dependency — second question shaped by first answer — is the part no static UI can serve. Use case 3 is decisively not solvable by chart-view improvements.

A weaker counter-argument is that a **rule-based system** could surface conflicts. It can, and the system uses one — the discrepancy engine in PRD §5 layer 5 is exactly this. What it can't do alone is *explain* the conflict in context, *answer follow-ups* about it, or *compose* it with the rest of the briefing. The rules engine produces the flags; the agent produces the use that the clinician can act on.

The agent earns its place on every use case in this document. It is not the right shape for tasks the user does at other times of day (note-writing, order entry, patient communication) — those are explicitly out of scope.

---

## 5. Capability Trace Map

Every architecture capability serves a use case here. This table is the contract.

| Architecture capability (ARCHITECTURE.md / PRD ref) | Serves use case | Why it's needed |
|---|---|---|
| Daily Brief surface (PRD §7) | 1, 2, 3, 4 | Pre-clinic prep moment; precomputes flags that the fast lane reads |
| In-chart Side Panel surface (PRD §7) | 1, 2, 3, 4 | Between-room moment; bound to current patient |
| Slow lane (10–20s budget) (PRD §4) | 1, 2, 3 | Pre-clinic prep tolerates depth; full verification |
| Fast lane (<5s budget) (PRD §4) | 2, 4 | Between-room is read while walking; lighter verification + cache |
| Retrieval-first rendering (PRD §5 layer 2) | 1, 2, 4 | Hard facts (meds/allergies/labs) — can't be hallucinated if not generated |
| Citation-required structured output (PRD §5 layer 3) | 1, 2, 3, 4 | Every prose claim attributable to a record |
| Field-level verification check (PRD §5 layer 4) | 1, 2, 3, 4 | Catches dominant fabrication mode programmatically |
| Discrepancy / domain-constraint engine (PRD §5 layer 5) | 3 (primary); 4 (consumes flags) | Differentiating feature; honors case-study domain-constraint requirement |
| Abstention taxonomy (NO_DATA / VERIFICATION_FAILED / TOOL_FAILURE / UNAUTHORIZED) (PRD §5) | 1, 2, 3, 4 | "I don't know" is a good answer; the four states have different UX |
| Per-tool RBAC enforcement (PRD §6) | All; secondary user explicitly | Auth is verification; resident/supervisor demonstration |
| Patient context binding (session-immutable) (PRD §6) | All | Structural prevention of wrong-patient drift |
| Multi-turn within session (PRD §3) | 1 (primary); 3 (drill-in); 2, 4 (occasional) | Threaded follow-ups are the user's natural pattern |
| HMAC-signed JWT (HS256) trust handoff (PRD §6, ARCH §1) | All | Trust boundary between PHP gateway and Python sidecar. HS256 (shared-secret HMAC) is used for service-to-service trust within a controlled internal boundary, not for third-party identity federation — RS256 is unnecessary overhead at this trust scope. |
| FHIR/REST data access (PRD §7) | All | OpenEMR's existing ACL is the source of truth; no direct DB access |
| Two-model strategy (Sonnet/Haiku candidates) (PRD §4, §8) | 1, 3 → slow; 2, 4 → fast | Latency budgets demand model-tier split |
| Non-streaming responses (PRD §5) | All | Whole-response verification depends on buffer-then-display |
| Audit log durability (PRD §6, §10) | All; secondary user | HIPAA-relevant; supervisor view depends on it |
| LangSmith observability (PRD §8) | All | Per-request trace, token cost, latency — case-study required; annotation queue feeds use-case-4 human grading |

Every capability in ARCHITECTURE.md should map to at least one row in this table. Capabilities with no row are scope creep and should be removed.

---

## 6. Anti-Use-Cases

Things this agent will explicitly **not** do for this user, even though they may sound reasonable. The anti-list is part of the user definition.

| Anti-use-case | Why it's excluded |
|---|---|
| Suggest a treatment, dose, or change in plan | Out of MVP scope. Triggers regulatory surface (clinical decision support) the architecture isn't designed to defend. The agent surfaces what the chart says; it doesn't recommend. |
| Detect novel drug interactions or guideline violations beyond what the chart already encodes | Verification is intractable for novel reasoning. The discrepancy engine flags conflicts already present in the chart and a small ruleset of widely-known conflicts; nothing further. |
| Write or modify clinical notes | Out of scope. Note-writing is a different surface with different verification requirements. |
| Place orders (labs, imaging, prescriptions) | Out of scope. Autonomous chart writes are explicitly excluded. |
| Speak to patients | Patient-facing surfaces are out of MVP. |
| Persist conversation history across sessions | Out of MVP. Sessions are short-lived; conversation drops on session end. |
| Provide a generic medical chatbot for non-patient questions | The agent is bound to a patient session; non-patient queries are not supported. |
| Cover specialty-specific workflows | The user is primary care / internal medicine. Specialty chart shapes are a Phase 2 expansion. |
| Offer break-glass emergency access | Out of MVP. Standard RBAC only. |
| Generate predictive risk scores | Out of MVP. The agent retrieves and synthesizes; it does not model. |

The anti-list narrows the verification surface. Every excluded capability is one less class of failure mode the architecture has to defend.

---

## 7. Open Questions Tied to the User

These are user-side open questions; the broader architecture/build open-question list lives in PRD §14.

1. **Where does the Daily Brief live in OpenEMR's nav?** New top-nav entry vs. embedded in the calendar/schedule view. Affects how naturally the user finds it at 7:35 AM.
2. **Where does the in-chart side panel attach?** Right sidebar, bottom drawer, modal — affects how much OpenEMR's existing chrome gets in the way of a between-room read.
3. **Sample data sufficiency for use case 3.** The differentiating feature depends on conflicting/incomplete records being present in the demo data. If the sample data is too clean, the audit will need to call out a "seeded discrepancy" fixture.
4. **Real-clinician validation.** This document is reasoned, not interviewed. Post-MVP work should include reviewing the use cases and workflow with a practicing cross-coverage primary-care physician.
5. **Eval ground truth for prioritization (use case 4).** "What should the briefing emphasize?" is not a retrieval task — it's a judgment task. Likely needs a human-graded eval subset rather than purely synthetic ground truth.
6. **Resident/supervisor flow validation.** The secondary RBAC scenario is included for architecture demonstration; the actual supervision workflow (when does an attending review? at what cadence?) is not validated against real teaching-clinic practice.

---

## 8. Document Provenance

| Field | Value |
|---|---|
| Derived from | PRD.md (v3) §2, §3; case-study Stage 4 requirements |
| Drives | ARCHITECTURE.md (every capability traces here); AUDIT.md scoping (which OpenEMR areas matter) |
| Validation status | Reasoned from case study + OpenEMR data model; not validated with primary clinician interviews (see §1.3) |
| Last updated | 2026-04-27 |
