# Plan — Port the OpenEMR Patient Dashboard to a Modern Framework

## Context

The OpenEMR patient dashboard is rendered server-side in PHP (`/interface/patient_file/summary/demographics.php` — a 2,072-line orchestrator that pulls 7+ services, dispatches Twig partials, and bolts together AJAX fragments). The UX was modernized in 2024–2025 (cards under `templates/patient/card/*.twig`), but the runtime stack is still PHP + jQuery + Smarty + Twig + Bootstrap 4 + Apache.

The assignment is a one-week port: keep the backend untouched, consume the existing FHIR R4 API, reproduce feature parity for the persistent patient header + the five required clinical cards (Allergies, Problem List, Medications, Prescriptions, Care Team) + one additional section, and **defend the framework choice** in `PATIENT_DASHBOARD_MIGRATION.md`.

The grade is split between (a) a working reimplementation with parity and (b) the written defense.

## Non-goals

- No backend changes. Zero PHP, MySQL, or service-layer edits.
- No UX redesign. Field-for-field parity with the existing Twig cards.
- No new clinical features (writes, e-prescribing, lab orders).
- Not a SMART-on-FHIR app store certification effort.
- **No Co-Pilot panel port.** The fork-local Co-Pilot panel is not part of upstream OpenEMR and not on the assignment's required-section list. Out of scope; documented as a known gap in the defense doc.

## Hard architectural boundary

**The new dashboard talks to OpenEMR only via the REST and FHIR APIs.** No direct MySQL access. No reading from OpenEMR tables. No joining against the OpenEMR schema for fields FHIR doesn't expose. If a parity gap surfaces, we accept it, document it in the migration doc, or contribute upstream — we do not back-channel the database.

A separate datastore for the dashboard's *own* state would be allowed (sessions, UI prefs, recently-viewed patient IDs) but the chosen architecture **does not require one** — see Locked decisions.

---

## Locked decisions

1. **Framework: Vite + React + TypeScript, pure SPA, no BFF.** OAuth public client + PKCE; access/refresh tokens kept in memory; UI prefs in `localStorage`. Zero new datastore. Page refresh re-runs the OAuth dance silently via the OpenEMR session.
2. **Repo placement: sibling dir `/dashboard-spa/` inside this monorepo.** Keeps the "backend untouched" claim provable in a single repo and a single commit history; lets the migration doc link directly to the PHP/Twig files it replaces.
3. **Additional section: Lab results.** `Observation?patient=&category=laboratory`, grouped by LOINC code with reference-range badges (high / low / critical via `interpretation`).
4. **HTML strategy: lift the existing Twig markup nearly verbatim.** View-source the existing dashboard for a populated patient, paste into JSX, swap `class`→`className`, port `{% for %}` to `.map()`, replace jQuery interactions with React state. Bootstrap 4.6 CSS is loaded as-is. Visual parity becomes nearly free; the framework is the value-add.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│ Browser SPA (Vite + React + TS)                                │
│                                                                │
│  /login         → OAuth2 PKCE redirect to OpenEMR              │
│  /callback      → exchange code → tokens kept in memory        │
│  /patients      → patient picker (FHIR Patient search)         │
│  /patients/:id  → dashboard shell                              │
│       ├── PatientHeader     (Patient/{id})                     │
│       ├── AllergiesCard     (AllergyIntolerance?patient=)      │
│       ├── ProblemsCard      (Condition?patient=&category=...)  │
│       ├── MedicationsCard   (MedicationRequest?patient=,       │
│       │                      filter intent ∈ {plan,proposal})  │
│       ├── PrescriptionsCard (MedicationRequest?patient=        │
│       │                      &intent=order)                    │
│       ├── CareTeamCard      (CareTeam + parallel Practitioners)│
│       └── LabResultsCard    (Observation?category=laboratory)  │
│                                                                │
│  Token refresh: silent via refresh_token grant on 401          │
│  UI prefs (collapse state): localStorage                       │
└────────────────────────────────────────────────────────────────┘
                          │ HTTPS + Bearer token
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ OpenEMR (untouched)                                            │
│   /oauth2/default/*    /apis/default/fhir/*                    │
└────────────────────────────────────────────────────────────────┘
```

All cards fetch in parallel on dashboard mount via a top-level `Promise.all`. Each card owns its loading / error / empty states (matches the Twig contract). One card failing does not blank the dashboard — per-card error UI mirrors the existing empty-state patterns.

---

## OAuth2 / PKCE flow

Discovery first, then standard public-client PKCE:

1. **One-time setup:** register a public client in the OpenEMR admin UI (Admin → System → API Clients). Dynamic registration via `POST /oauth2/default/registration` works but creates clients with `is_enabled=0` that need admin approval anyway, so just register through the UI directly. Capture the `client_id`. No client secret.
2. **Discovery:** `GET https://localhost:9300/oauth2/default/.well-known/openid-configuration` → cached in memory at app start.
3. **Login:**
   - Generate `code_verifier` (43–128 char URL-safe random) + `code_challenge = base64url(sha256(code_verifier))`.
   - Generate `state` (random) + stash `{state, code_verifier}` in `sessionStorage` keyed by `state`.
   - Redirect to `/oauth2/default/authorize?response_type=code&client_id=...&redirect_uri=http://localhost:5173/callback&scope=openid+offline_access+launch/patient+patient/*.read&code_challenge=...&code_challenge_method=S256&state=...&aud=https://localhost:9300/apis/default/fhir/`.
4. **Callback:** read `?code` and `?state`, look up the verifier, `POST /oauth2/default/token` with `grant_type=authorization_code&code=...&redirect_uri=...&code_verifier=...&client_id=...`. Store `{access_token, refresh_token, expires_at, patient}` in a React context. **The `patient` field comes back at the top level of the token response (per SMART App Launch), not in the id_token.**
5. **Refresh:** the `fetchFhir` wrapper retries once on 401 by `POST /oauth2/default/token` with `grant_type=refresh_token`. OpenEMR rotates refresh tokens — persist the new one over the old. Wrap the refresh call in a single-flight promise so 6 parallel 401s don't fire 6 refreshes.
6. **Logout:** clear in-memory tokens, redirect to `/oauth2/default/logout?id_token_hint=...&post_logout_redirect_uri=http://localhost:5173/`.

Scopes (granular, patient-context):
```
openid offline_access launch/patient
patient/Patient.read
patient/AllergyIntolerance.read
patient/Condition.read
patient/MedicationRequest.read
patient/CareTeam.read
patient/Practitioner.read
patient/Observation.read
```

Library: `oidc-client-ts` for the dance, or hand-roll ~120 LOC against `crypto.subtle` for SHA-256 — both fine. Recommendation: hand-roll. Fewer deps, full visibility, the migration doc gets to point at clean code.

---

## FHIR client design

Hand-rolled fetch wrapper. No Medplum SDK, no fhirclient.js.

```ts
// dashboard-spa/src/fhir/client.ts
import type { Bundle, Resource } from 'fhir/r4';

export async function fhirRead<T extends Resource>(
  resourceType: T['resourceType'],
  id: string,
): Promise<T> { ... }

export async function fhirSearch<T extends Resource>(
  resourceType: T['resourceType'],
  params: Record<string, string>,
): Promise<T[]> {
  // GET /apis/default/fhir/{resourceType}?...
  // Bundle has no `next` link in OpenEMR — request _count=200 and accept the wall.
  // Bundle.type is 'collection' not 'searchset' — don't assert on it.
  // 401 → single-flight refresh → one retry.
  // 403 → log warning, return [] (out-of-scope ≠ error).
}
```

Types from `@types/fhir`. The wrapper returns flattened arrays (not Bundles) to keep card code clean.

---

## File / directory layout

```
dashboard-spa/
├── package.json
├── tsconfig.json
├── vite.config.ts
├── index.html
├── public/
├── src/
│   ├── main.tsx
│   ├── App.tsx                   ← router
│   ├── auth/
│   │   ├── pkce.ts               ← code_verifier / code_challenge helpers
│   │   ├── oauth.ts              ← discovery, authorize redirect, token exchange, refresh
│   │   ├── AuthContext.tsx       ← React context holding tokens + patient context
│   │   └── routes.tsx            ← /login, /callback, /logout, RequireAuth wrapper
│   ├── fhir/
│   │   ├── client.ts             ← fetchFhir, fhirRead, fhirSearch
│   │   └── types.ts              ← narrowed types where @types/fhir is too loose
│   ├── components/
│   │   ├── PatientHeader.tsx
│   │   ├── CardBase.tsx          ← collapsible wrapper, mirrors card_base.html.twig
│   │   ├── EmptyState.tsx        ← "No Known Allergies" vs "Nothing Recorded"
│   │   └── Loading.tsx
│   ├── cards/
│   │   ├── AllergiesCard.tsx
│   │   ├── ProblemsCard.tsx
│   │   ├── MedicationsCard.tsx
│   │   ├── PrescriptionsCard.tsx
│   │   ├── CareTeamCard.tsx
│   │   └── LabResultsCard.tsx
│   ├── pages/
│   │   ├── Login.tsx
│   │   ├── Callback.tsx
│   │   ├── PatientPicker.tsx     ← /patients
│   │   └── Dashboard.tsx         ← /patients/:id
│   ├── styles/
│   │   └── bootstrap.scss        ← imports bootstrap@4.6.x
│   └── prefs/
│       └── collapseStore.ts      ← localStorage wrapper, keyed by user.sub + cardId
├── .env.example
└── README.md

PATIENT_DASHBOARD_MIGRATION.md    ← at repo root, the defense doc
```

---

## HTML lift strategy

For each card, the workflow is:

1. Open the existing OpenEMR dashboard at `https://localhost:9300/interface/patient_file/summary/demographics.php?set_pid=<id>` for a Synthea patient with rich data on the card.
2. Inspect the rendered HTML for that card. Copy the DOM snippet.
3. Paste into the corresponding `cards/*.tsx`. Mechanical translation:
   - `class=` → `className=`
   - `for=` → `htmlFor=`
   - Self-close `<input>`, `<img>`, `<br>`, etc.
   - Inline event handlers → React handlers
4. Cross-reference the source Twig (`templates/patient/card/<card>.html.twig`) to recover conditionals, loop variables, and the empty-state semantics. Translate `{% if %}` to ternaries / `&&`, `{% for %}` to `.map()`, `{{ var }}` to `{var}`.
5. Bind to FHIR data via the card's hook (`useQuery`-style or hand-rolled `useEffect` — TBD, lean toward hand-rolled to keep deps minimal).

Three things that do NOT lift verbatim and need a real port:

- `templates/patient/card/rx.html.twig` injects a **Smarty** controller fragment for prescription rows. Read the rendered HTML, rebuild the row in JSX (~30 LOC).
- jQuery-driven Bootstrap collapse → 10-line `<CardBase>` with `useState` (or `react-bootstrap@1.x`).
- Care Team's inline edit mode is out of scope. The port is positioned as a **read-only clinical summary** — the other required cards (Allergies, Problems, Medications, Prescriptions) are read-only views by nature, so the read-only framing is coherent across the dashboard, not a one-card concession.

---

## Card-by-card mapping (with parity notes folded in)

### Patient header
- **Source:** `dashboard_header.php` + `patient/dashboard_header.html.twig` + `OemrUI::pageHeading()`
- **FHIR:** `GET /Patient/{id}`
- **Fields:** `name[0].text`, `birthDate`, `gender`, `active`, MRN.
- **MRN gotcha:** filter `identifier` by `type.coding[].code === 'PT'` (not by OID system URL). Field is only present when `pubpid` is set in OpenEMR — for patients without a pubpid, show "—".

### Allergies
- **Source:** `templates/patient/card/allergies.html.twig`
- **FHIR:** `GET /AllergyIntolerance?patient={id}`
- **Fields:** `code.text` (title), `criticality` (severity badge — `severe`/`life_threatening`/`fatal` highlighted), `reaction[].manifestation[].text`.
- **Filter:** OpenEMR does NOT support `clinical-status` as a search param. Filter to `clinicalStatus.coding[0].code === 'active'` client-side.
- **Empty states:** "No Known Allergies" (touched / NKDA recorded) vs "Nothing Recorded" (untouched). Distinguish via the presence of any AllergyIntolerance record (incl. NKDA-coded).

### Problem List
- **Source:** `templates/patient/card/medical_problems.html.twig`
- **FHIR:** `GET /Condition?patient={id}&category=problem-list-item`
- **Fields:** `code.text` only.

### Medications
- **Source:** `templates/patient/card/medication.html.twig`
- **FHIR:** `GET /MedicationRequest?patient={id}` then **client-side** filter `intent ∈ {plan, proposal}`. Do **not** filter `intent != order` — that picks up `original-order`, `instance-order`, etc.
- **Fields:** `medication{Reference,CodeableConcept}.display` (title), `dosageInstruction[0].text`.
- **Why:** PHP card pulls from `lists.type='medication'`, which OpenEMR's `FhirMedicationRequestService` emits with `intent='plan'` (default). Filtering by `intent=order` server-side would silently drop every problem-list medication.

### Prescriptions
- **Source:** `templates/patient/card/rx.html.twig` (Smarty fragment)
- **FHIR:** `GET /MedicationRequest?patient={id}&intent=order`
- **Fields:** drug, dose, frequency, route, refills (`dispenseRequest.numberOfRepeatsAllowed`), quantity (`dispenseRequest.quantity.value`).
- **Note:** Smarty fragment columns need to be reconstructed from rendered HTML — no direct Twig analog.

### Care Team
- **Source:** `templates/patient/card/manage_care_team.html.twig`
- **FHIR:** `GET /CareTeam?patient={id}` THEN parallel `GET /Practitioner/{uuid}` for each unique `participant.member.reference`.
- **Why parallel fetches:** OpenEMR's `FhirCareTeamService` declares only `patient`, `status`, `_id`, `_lastUpdated` as search params — `_include=CareTeam:participant` is silently dropped. Parallel fetches are 1–4 extra requests on localhost FHIR, sub-100ms total. Memoize per-request by reference so the same provider isn't fetched twice.
- **Fields:** member name, role (`participant.role[].text`), facility, status, since-date (`participant.period.start`), note.
- **Edit mode:** out of scope per the read-only-clinical-summary framing (see HTML lift strategy). Defended in `PATIENT_DASHBOARD_MIGRATION.md` §0/§7 as a deliberate scope decision, not an API constraint.

### Lab Results (additional section)
- **Source:** new section; existing dashboard has `labdata_fragment.php` for reference
- **FHIR:** `GET /Observation?patient={id}&category=laboratory&_count=200&_sort=-date`
- **Fields:** `code` (LOINC), `valueQuantity.{value, unit}` or `valueString`, `referenceRange[0].{low, high, text}`, `interpretation[0].coding[0].code` (H/L/HH/LL/A → high/low/critical-high/critical-low/abnormal badge), `effectiveDateTime`.
- **Grouping key:**
  ```ts
  const groupKey =
    obs.code.coding?.find(c => c.system === 'http://loinc.org')?.code
    ?? obs.code.text
    ?? 'Unknown';
  ```
  Synthea data is fully LOINC-coded; OpenEMR-native lab entries may have `code.text` only — fallback handles both.
- **Sort:** server-side `_sort=-date` works (`SearchQueryConfig.php:54`), but the underlying field is `report_date`. Re-sort client-side by `effectiveDateTime` as tiebreaker.
- **No paging:** Bundle has no `next` link. `_count=200` covers any plausible single-patient lab history; document the cap in the migration doc.

---

## PATIENT_DASHBOARD_MIGRATION.md outline

The defense doc is part of the grade. **Stub the doc with §0–§4 + §8 in PR 1** —
the framework decision is locked and the headline argument can be written before
any code. PR 10 finalizes §5–§7 with measured numbers and parity-matrix
screenshots. Sections §5–§7 land as `TBD` placeholders in the PR 1 stub so the
doc's structure exists from day one.

0. **Scope** — Parity is measured against the dashboard surface the assignment
   enumerates: authentication, the persistent patient header, the five required
   clinical cards (Allergies, Problem List, Medications, Prescriptions, Care
   Team), and one additional section (Lab Results, chosen from the assignment's
   fixed list). Other surfaces in the upstream dashboard (advance directives,
   appointments, billing, eligibility, eRx, immunizations, insurance, recall,
   treatment plans — ~15 cards in `templates/patient/card/` total) are out of
   scope by the assignment's own enumeration. The port is positioned as a
   **read-only clinical summary** — the required cards are data-display views
   by nature; write workflows (Care Team edit) are deliberately out of scope,
   not an API limitation.
1. **Why port at all** — what the legacy stack costs (untyped service-layer arrays flowing into Twig, jQuery + Smarty + Twig coexisting in one card, 2,072-line orchestrator).
2. **Why React + Vite (not Next.js, not SvelteKit, not Remix)** — three rejected alternatives steel-manned, with the actual reasons each was the runner-up. Honest about the *defense narrative* trade vs the *runtime simplicity* win.
3. **Why pure SPA (no BFF)** — the boundary constraint, the foot-gun list from the Plan-agent critique that BFF would have introduced, and how the SPA path leaves zero new infra.
4. **What was gained** — typed components, parallel fetches, per-card error boundaries, code-splitting by route, no jQuery, no Smarty, no Twig-includes-Twig-includes-Smarty, hot reload <100ms.
5. **What was given up** — first paint waits for JS + token + network (measured number, not vibes); page refresh re-auths (~1s on local — measured); no SSR; one-card-per-card parity gaps documented below. Care-team edit is *not* listed here — it's covered in §0 as a deliberate scope decision.
6. **Parity matrix** — for each of the 7 visible sections (header + 5 required cards + Lab Results): side-by-side screenshots of legacy vs port for the same Synthea patient. Field-for-field check.
7. **Known parity gaps** — collapse-state persistence is per-browser via `localStorage` (legacy was per-user via AJAX) — direct consequence of the no-BFF decision. `hide_dashboard_cards` global is unsupported. **Care-team edit mode is out of scope per §0's read-only-summary framing, not an API limitation** (FHIR `CareTeam` writes are technically possible against OpenEMR — this is a deliberate scope decision). AllergyIntolerance `clinical-status` filtering is client-side because the API doesn't support the search param. Fork-local Co-Pilot panel excluded (this port targets upstream OpenEMR's dashboard surface, not this fork's local additions).
8. **What's reusable** — `<CardBase>`, `fhirSearch` wrapper, the auth flow — this scaffold is what other PHP pages would migrate onto.

---

## Verification plan

- **Local stack:** dev OpenEMR at `https://localhost:9300/` (existing Docker setup), Synthea-seeded patients (already loaded for Co-Pilot work), Vite dev server at `http://localhost:5173/`.
- **OAuth round-trip:** click Login → land on OpenEMR auth screen → consent → callback → tokens in memory (verify in React DevTools that `AuthContext` has `access_token` and `patient`). Force-expire the token (set `expires_at` to the past) → next FHIR call refreshes silently → no UI hiccup.
- **Card-by-card parity matrix:** pick 3 Synthea patients with rich data on every card. For each (patient × card), screenshot legacy and port side-by-side. Field-for-field diff. Empty-state patient: pick or create a patient with no allergies → confirm "No Known Allergies" vs "Nothing Recorded" semantics.
- **Lab results:** pick a patient with diverse lab results (different LOINCs, mix of normal / high / low / critical / abnormal). Verify grouping, sort, badge colors.
- **Boundary smoke check:** `grep -r 'mysql\|mysqli\|PDO\|Doctrine\|ADODB' dashboard-spa/` returns nothing.
- **Type check:** `npm run typecheck` clean, no `any`s in card or fetch code.
- **Defense doc:** written, committed, screenshots embedded.

---

## Week sequencing (rough)

Day 1 — bootstrap Vite + TS + Bootstrap CSS, register OAuth client in OpenEMR admin, implement PKCE flow, login → callback → tokens-in-context, `/patients` picker.

Day 2 — `<CardBase>`, patient header + Allergies + Problems. Lift HTML verbatim, bind FHIR, screenshot parity check.

Day 3 — Medications + Prescriptions + Care Team (the parallel-Practitioner fetch is the trickiest piece).

Day 4 — Lab results section (grouping + reference-range badges).

Day 5 — Polish: empty states, error boundaries, refresh-token single-flight, `localStorage` collapse persistence, screenshots for the parity matrix.

Buffer (Day 6–7) — write `PATIENT_DASHBOARD_MIGRATION.md`, fix gaps, demo prep. Defense doc is written *while* features are fresh, not after.

---

## Risks & open questions

- **OAuth client registration UI** — confirm `Admin → System → API Clients` is the right path on this OpenEMR build before Day 1; if dynamic registration is the only option, the runbook adds an admin-approval step.
- **`launch/patient` scope** — for a standalone (non-EHR-launched) app, OpenEMR may want a patient picker before issuing patient-context tokens. Verify behavior in the first auth round-trip; fallback is to use `user/*.read` scopes and treat the picker as our own.
- **Synthea data on Care Team** — confirm at least one Synthea patient has a populated CareTeam; if not, we may need to seed manually.
- **Bootstrap 4.6 visual fidelity** — small differences vs whatever exact Bootstrap version OpenEMR ships. If a card's HTML uses a class introduced in 4.5+ or removed by 4.6, the lift may need a 1-line patch.
- **Self-signed dev cert** — browser will warn on `https://localhost:9300/` redirects. Document in README; not a code issue.
