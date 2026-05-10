# Patient Dashboard Migration — Tasks

Source plan: [`plans/patient-dashboard-port.md`](plans/patient-dashboard-port.md). Open this file
alongside that one — this is the PR-level execution tracker; the plan is the design.

**Hard rule (every PR):** zero edits to OpenEMR core. Everything new lives under
`dashboard-spa/`, plus two top-level docs (`PATIENT_DASHBOARD_MIGRATION.md`,
this file). A grep of any PR diff outside `dashboard-spa/` and root `*.md` files
should return nothing.

**Scope statement (preempts the broad "feature parity" reading):** Parity is
measured against the dashboard surface the assignment enumerates —
authentication, the persistent patient header, the five required clinical
cards (Allergies, Problem List, Medications, Prescriptions, Care Team), and
one additional section (Lab Results). The other ~15 cards in upstream's
`templates/patient/card/` (advance directives, appointments, billing,
eligibility, eRx, immunizations, insurance, recall, treatment plans, etc.) are
out of scope by the assignment's own enumeration. The port is positioned as a
**read-only clinical summary** — Care Team edit mode is deliberately out of
scope per this framing, *not* an API limitation. Defended in
`PATIENT_DASHBOARD_MIGRATION.md` §0 + §7.

---

## Target file structure

```
openemr/                              ← repo root (untouched OpenEMR)
├── dashboard-spa/                    ← NEW — all SPA code lives here
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   ├── index.html
│   ├── .env.example
│   ├── .gitignore
│   ├── README.md
│   ├── public/
│   ├── parity-matrix/                ← screenshots for the defense doc
│   └── src/
│       ├── main.tsx
│       ├── App.tsx                   ← router root, providers
│       ├── auth/
│       │   ├── pkce.ts
│       │   ├── oauth.ts
│       │   ├── AuthContext.tsx
│       │   └── routes.tsx            ← RequireAuth wrapper
│       ├── fhir/
│       │   ├── client.ts             ← fhirRead, fhirSearch
│       │   └── types.ts
│       ├── components/
│       │   ├── PatientHeader.tsx
│       │   ├── CardBase.tsx
│       │   ├── EmptyState.tsx
│       │   ├── Loading.tsx
│       │   └── ErrorBoundary.tsx
│       ├── cards/
│       │   ├── AllergiesCard.tsx
│       │   ├── ProblemsCard.tsx
│       │   ├── MedicationsCard.tsx
│       │   ├── PrescriptionsCard.tsx
│       │   ├── CareTeamCard.tsx
│       │   └── LabResultsCard.tsx
│       ├── pages/
│       │   ├── Login.tsx
│       │   ├── Callback.tsx
│       │   ├── PatientPicker.tsx     ← /patients
│       │   └── Dashboard.tsx         ← /patients/:id
│       ├── styles/
│       │   └── bootstrap.scss
│       └── prefs/
│           └── collapseStore.ts
└── PATIENT_DASHBOARD_MIGRATION.md    ← NEW — defense doc, repo root
```

---

## PR 1 — Scaffold `dashboard-spa/` and tooling

**Goal:** an empty SPA that boots at `http://localhost:5173/` with a hello page,
typechecks clean, and has Bootstrap 4.6 CSS loaded.

- [ ] Create `dashboard-spa/` via `npm create vite@latest -- --template react-ts`
- [ ] Pin Bootstrap 4.6.x in `package.json` (`bootstrap@^4.6`, `sass`)
- [ ] Add `@types/fhir`, `react-router-dom@6` (used in PR 3, scaffold the dep here)
- [ ] Configure `tsconfig.json` with `strict: true`, `noUncheckedIndexedAccess: true`
- [ ] Configure `vite.config.ts` with dev port `5173` and HTTPS proxy passthrough notes
- [ ] Import Bootstrap SCSS from `src/styles/bootstrap.scss` in `main.tsx`
- [ ] Hello-world `App.tsx` rendering `<h1>OpenEMR Dashboard SPA</h1>`
- [ ] `.env.example` with placeholder for `VITE_OPENEMR_BASE_URL` and `VITE_OAUTH_CLIENT_ID`
- [ ] `.gitignore` for `node_modules/` and `dist/`
- [ ] `README.md` with run instructions (`npm i`, `npm run dev`, the OpenEMR URL it expects)
- [ ] Add `npm run typecheck` script (`tsc --noEmit`)
- [ ] Add test infrastructure (per repo policy: tests ship with the code they cover):
  - [ ] Pin `vitest`, `@vitest/ui`, `@testing-library/react`, `@testing-library/jest-dom`,
        `@testing-library/user-event`, `jsdom` in `package.json`
  - [ ] Configure Vitest in `vite.config.ts`
        (`test: { environment: 'jsdom', setupFiles: ['./src/test/setup.ts'] }`)
  - [ ] `src/test/setup.ts` — `import '@testing-library/jest-dom/vitest'`
  - [ ] `npm run test` (watch) and `npm run test:ci` (`vitest run`) scripts
  - [ ] Smoke test `src/App.test.tsx` — renders the hello heading; confirms the harness works
- [ ] **Stub `PATIENT_DASHBOARD_MIGRATION.md` at repo root.** Write the
      framework-defense sections that don't need implementation evidence:
  - [ ] §0 Scope (full content — assignment-enumerated surface + read-only-summary framing)
  - [ ] §1 Why port at all (full content — costs of legacy stack, 2,072-line orchestrator)
  - [ ] §2 Why React + Vite (full content — Next.js / SvelteKit / Remix steel-manned)
  - [ ] §3 Why pure SPA, no BFF (full content — boundary constraint, foot-guns avoided)
  - [ ] §4 What was gained (full content — typed components, parallel fetches, no Twig/Smarty/jQuery)
  - [ ] §5 What was given up — `TBD` placeholder; PR 10 fills with measured numbers
  - [ ] §6 Parity matrix — `TBD` placeholder; PR 10 fills with screenshots
  - [ ] §7 Known parity gaps — pre-known list, marked `to be confirmed in PR 10`
  - [ ] §8 What's reusable (full content — `<CardBase>`, `fhirSearch`, auth flow)
- [ ] **Verify:** `npm run dev` serves the hello page; `npm run typecheck` and
      `npm run test:ci` both clean; `PATIENT_DASHBOARD_MIGRATION.md` renders cleanly
      in markdown preview

**New files:** all of `dashboard-spa/` (package.json, tsconfig.json, vite.config.ts,
index.html, .env.example, .gitignore, README.md, src/main.tsx, src/App.tsx,
src/styles/bootstrap.scss, src/test/setup.ts, src/App.test.tsx, public/), plus
`PATIENT_DASHBOARD_MIGRATION.md` at repo root.
**Edited files:** none outside `dashboard-spa/` and the root migration doc.

---

## PR 2 — OAuth2 PKCE login + AuthContext

**Goal:** click Login → land on OpenEMR consent → return with tokens in
memory; refresh-on-401 works single-flight; logout clears state.

- [ ] **One-time setup (manual, document in README):** register a public client in
      OpenEMR Admin → System → API Clients. `redirect_uri = http://localhost:5173/callback`.
      Capture `client_id` into `.env.local`. No client secret.
- [ ] Implement `auth/pkce.ts` — `generateCodeVerifier()`, `generateCodeChallenge(verifier)`
      using `crypto.subtle.digest('SHA-256', …)` + base64url encoding
- [ ] Implement `auth/oauth.ts`:
  - [ ] `discoverConfig()` — fetch & cache `/.well-known/openid-configuration`
  - [ ] `redirectToAuthorize()` — generate verifier+state, stash in `sessionStorage`, redirect
  - [ ] `exchangeCode(code, state)` — POST to token endpoint with verifier
  - [ ] `refreshTokens(refreshToken)` — single-flight wrapper (Promise dedup)
  - [ ] `logout(idTokenHint)` — clear state, redirect to logout endpoint
- [ ] Implement `auth/AuthContext.tsx` — provider with `{accessToken, refreshToken, expiresAt, patient, sub}`
- [ ] Implement `auth/routes.tsx` — `<RequireAuth>` wrapper that redirects to /login if no token
- [ ] Implement `pages/Login.tsx` — calls `redirectToAuthorize()`
- [ ] Implement `pages/Callback.tsx` — reads `?code` + `?state`, exchanges, stores in context, navigates to /patients
- [ ] **Tests (test-first per repo policy — auth is the canonical "silent failure
      bypasses authz" surface):**
  - [ ] `src/auth/pkce.test.ts` — verifier length within RFC 7636 bounds (43–128
        chars, URL-safe charset); `generateCodeChallenge` is base64url with no
        padding; deterministic for a fixed verifier (fixture vector)
  - [ ] `src/auth/oauth.test.ts` — `state` mismatch in `exchangeCode` throws and
        does not POST; concurrent `refreshTokens()` calls share one in-flight
        promise (single-flight); failed refresh clears tokens and rejects all waiters
  - [ ] `src/auth/AuthContext.test.tsx` — initial state unauthenticated;
        `setTokens()` populates `accessToken`/`expiresAt`; `logout()` clears state
- [ ] **Verify:** full round-trip against `https://localhost:9300/`. In React DevTools,
      confirm `AuthContext` has `access_token` and `patient`. Manually expire the
      token (set `expiresAt` to past) → next FHIR call refreshes silently.

**New files:** `src/auth/pkce.ts`, `src/auth/oauth.ts`, `src/auth/AuthContext.tsx`,
`src/auth/routes.tsx`, `src/pages/Login.tsx`, `src/pages/Callback.tsx`,
`src/auth/pkce.test.ts`, `src/auth/oauth.test.ts`, `src/auth/AuthContext.test.tsx`.
**Edited files:** `src/App.tsx` (wrap in `<AuthProvider>`, add `/login` + `/callback` routes),
`.env.example` (add OAuth vars), `README.md` (document client-registration step).

---

## PR 3 — FHIR client + patient picker

**Goal:** authenticated user lands on `/patients`, can search by name or MRN,
clicks a result, navigates to `/patients/:id` (empty dashboard shell).

- [ ] Implement `fhir/client.ts`:
  - [ ] `fhirRead<T>(resourceType, id)` — `GET /apis/default/fhir/{resourceType}/{id}`
  - [ ] `fhirSearch<T>(resourceType, params)` — returns flattened array (not Bundle)
  - [ ] 401 → single-flight refresh → one retry
  - [ ] 403 → log warning, return `[]` (out-of-scope ≠ error)
  - [ ] Bundle.type assertion: skip (OpenEMR returns `'collection'` not `'searchset'`)
  - [ ] `_count=200` default; document the cap
- [ ] Implement `fhir/types.ts` — narrow `@types/fhir` where it's too loose
- [ ] Implement `pages/PatientPicker.tsx`:
  - [ ] Search input → `Patient?name=...` or `Patient?identifier=...`
  - [ ] Results list with name, DOB, MRN
  - [ ] Click → navigate to `/patients/{id}`
- [ ] Implement `pages/Dashboard.tsx` — placeholder shell with "Patient {id}" heading
- [ ] **Tests:**
  - [ ] `src/fhir/client.test.ts` (mock `fetch`):
    - [ ] 200 Bundle → entries flattened to `T[]`
    - [ ] 401 → triggers single refresh → retries once → returns data
    - [ ] 401 after retry → rejects (no infinite loop)
    - [ ] 403 → resolves to `[]`, logs warning, does not throw
    - [ ] `_count=200` is the default when not specified
  - [ ] `src/pages/PatientPicker.test.tsx` — typing a name issues
        `Patient?name=…`; typing digits/MRN issues `Patient?identifier=…`;
        clicking a result calls `navigate('/patients/{id}')`
- [ ] **Verify:** logged-in user sees patient list; clicking navigates to `/patients/:id`;
      no console errors; network tab shows Bearer-authenticated FHIR calls

**New files:** `src/fhir/client.ts`, `src/fhir/types.ts`, `src/pages/PatientPicker.tsx`,
`src/pages/Dashboard.tsx`, `src/fhir/client.test.ts`, `src/pages/PatientPicker.test.tsx`.
**Edited files:** `src/App.tsx` (add `/patients` and `/patients/:id` routes, both
`<RequireAuth>`-wrapped).

---

## PR 4 — Patient header + `CardBase` primitive

**Goal:** dashboard renders the persistent identity bar plus 6 empty
collapsible card placeholders matching the upstream Twig layout.

- [ ] Implement `components/PatientHeader.tsx`:
  - [ ] `Patient.read(id)` on mount
  - [ ] Fields: `name[0].text`, `birthDate`, `gender`, `active`, MRN
  - [ ] MRN: filter `identifier` by `type.coding[].code === 'PT'`; "—" if absent
  - [ ] Lift HTML from rendered upstream `dashboard_header.php` output
- [ ] Implement `components/CardBase.tsx` — collapsible wrapper mirroring
      `templates/patient/card/card_base.html.twig` (header, chevron, body)
- [ ] Implement `components/EmptyState.tsx` — variants: `"No Known Allergies"`,
      `"Nothing Recorded"`, etc.
- [ ] Implement `components/Loading.tsx` — small spinner mirroring `loader.html.twig`
- [ ] Implement `components/ErrorBoundary.tsx` — per-card error fallback so one
      card failing doesn't blank the dashboard
- [ ] Wire dashboard layout: `<PatientHeader>` + 6 `<CardBase title="…">` placeholders
- [ ] **Tests:**
  - [ ] `src/components/PatientHeader.test.tsx` — fixture `Patient` with MRN
        (`identifier.type.coding[].code === 'PT'`) renders MRN; fixture without
        MRN renders `—`; missing `name[0].text` falls back gracefully
  - [ ] `src/components/CardBase.test.tsx` — initial state expanded; click
        header toggles collapse; chevron icon flips
  - [ ] `src/components/ErrorBoundary.test.tsx` — child that throws renders
        fallback UI; sibling rendered outside the boundary still mounts
- [ ] **Verify:** open `/patients/:id` for a Synthea patient. Header populates.
      6 empty cards render and collapse/expand. Side-by-side screenshot vs upstream.

**New files:** `src/components/PatientHeader.tsx`, `src/components/CardBase.tsx`,
`src/components/EmptyState.tsx`, `src/components/Loading.tsx`, `src/components/ErrorBoundary.tsx`,
`src/components/PatientHeader.test.tsx`, `src/components/CardBase.test.tsx`,
`src/components/ErrorBoundary.test.tsx`.
**Edited files:** `src/pages/Dashboard.tsx` (render header + 6 placeholders).
**Reference (read-only):** `interface/patient_file/summary/dashboard_header.php`,
`templates/patient/card/card_base.html.twig`, `templates/patient/card/loader.html.twig`.

---

## PR 5 — Allergies + Problems cards

**Goal:** first two clinical cards live, with field-for-field parity vs upstream.

- [ ] Implement `cards/AllergiesCard.tsx`:
  - [ ] `AllergyIntolerance?patient={id}`
  - [ ] **Client-side** filter `clinicalStatus.coding[0].code === 'active'`
  - [ ] Fields: `code.text`, `criticality` (severity badge), `reaction[].manifestation[].text`
  - [ ] Empty-state distinction: "No Known Allergies" (NKDA-coded record) vs "Nothing Recorded" (no record)
- [ ] Implement `cards/ProblemsCard.tsx`:
  - [ ] `Condition?patient={id}&category=problem-list-item`
  - [ ] Fields: `code.text` only
- [ ] Lift HTML from rendered upstream cards (Synthea patient with rich data)
- [ ] **Tests** (introduce shared fixture dir `src/__fixtures__/fhir/` for
      reusable FHIR bundles, used by every card test from here on):
  - [ ] `src/cards/AllergiesCard.test.tsx` — bundle with mixed
        `clinicalStatus.coding[0].code` values: only `active` rows render;
        NKDA-coded fixture → "No Known Allergies"; empty bundle → "Nothing
        Recorded"; criticality `high` renders the severity badge
  - [ ] `src/cards/ProblemsCard.test.tsx` — fixture with multiple Conditions
        renders each `code.text`; empty → "Nothing Recorded"
- [ ] **Verify:** parity matrix screenshots for one Synthea patient, both cards.
      Test empty states by picking/creating a patient with no data.

**New files:** `src/cards/AllergiesCard.tsx`, `src/cards/ProblemsCard.tsx`,
`src/cards/AllergiesCard.test.tsx`, `src/cards/ProblemsCard.test.tsx`,
`src/__fixtures__/fhir/` (shared FHIR bundle fixtures for card tests).
**Edited files:** `src/pages/Dashboard.tsx` (replace 2 placeholders with real cards).
**Reference (read-only):** `templates/patient/card/allergies.html.twig`,
`templates/patient/card/medical_problems.html.twig`.

---

## PR 6 — Medications + Prescriptions cards

**Goal:** both `MedicationRequest`-backed cards live, with the intent-filter
distinction handled correctly.

- [ ] Implement `cards/MedicationsCard.tsx`:
  - [ ] `MedicationRequest?patient={id}` then **client-side** filter `intent ∈ {plan, proposal}`
  - [ ] Fields: `medication{Reference,CodeableConcept}.display` (title), `dosageInstruction[0].text`
  - [ ] Do **not** filter `intent != order` — picks up `original-order`, `instance-order`
- [ ] Implement `cards/PrescriptionsCard.tsx`:
  - [ ] `MedicationRequest?patient={id}&intent=order`
  - [ ] Fields: drug, dose, frequency, route, refills (`dispenseRequest.numberOfRepeatsAllowed`),
        quantity (`dispenseRequest.quantity.value`)
  - [ ] Reconstruct row layout from upstream Smarty fragment — no direct Twig analog
- [ ] **Tests:**
  - [ ] `src/cards/MedicationsCard.test.tsx` — fixture with intents `plan`,
        `proposal`, `order`, `original-order`: only `plan` + `proposal` render;
        `dosageInstruction[0].text` displayed; falls back to
        `medicationCodeableConcept.text` when `medicationReference` absent
  - [ ] `src/cards/PrescriptionsCard.test.tsx` — fixture renders refills
        (`dispenseRequest.numberOfRepeatsAllowed`), quantity
        (`dispenseRequest.quantity.value`), route, frequency; row missing
        `dispenseRequest` doesn't crash
- [ ] **Verify:** parity matrix screenshots; confirm Meds card includes
      problem-list medications (intent=plan) that Prescriptions excludes

**New files:** `src/cards/MedicationsCard.tsx`, `src/cards/PrescriptionsCard.tsx`,
`src/cards/MedicationsCard.test.tsx`, `src/cards/PrescriptionsCard.test.tsx`.
**Edited files:** `src/pages/Dashboard.tsx` (replace 2 more placeholders).
**Reference (read-only):** `templates/patient/card/medication.html.twig`,
`templates/patient/card/rx.html.twig` (Smarty fragment — read rendered HTML).

---

## PR 7 — Care Team card (parallel Practitioner fetches)

**Goal:** Care Team renders with member names resolved via parallel
`Practitioner` reads, memoized per request.

- [ ] Implement `cards/CareTeamCard.tsx`:
  - [ ] `CareTeam?patient={id}`
  - [ ] Collect unique `participant.member.reference` UUIDs
  - [ ] Parallel `Practitioner.read(uuid)` for each, memoized within the request
        (don't fetch the same provider twice)
  - [ ] Fields: member name, role (`participant.role[].text`), facility, status,
        since-date (`participant.period.start`), note
  - [ ] Edit mode: **out of scope** per the read-only-clinical-summary framing
        (see "Scope statement" at top of this file). Defended in the migration
        doc as a deliberate scope decision, *not* an API limit.
- [ ] **Tests:**
  - [ ] `src/cards/CareTeamCard.test.tsx` (mock `fhirRead`) — fixture
        `CareTeam` with 4 participants referencing 3 unique Practitioner UUIDs
        (one duplicate): exactly 3 `Practitioner.read()` calls fire; resolved
        names render; `participant.role[].text` and `participant.period.start`
        display; missing role/period rows render without crashing
- [ ] **Verify:** patient with multi-member care team; network tab shows N parallel
      Practitioner reads with no dupes; total wait sub-100ms on localhost

**New files:** `src/cards/CareTeamCard.tsx`, `src/cards/CareTeamCard.test.tsx`.
**Edited files:** `src/pages/Dashboard.tsx` (replace placeholder).
**Reference (read-only):** `templates/patient/card/manage_care_team.html.twig`,
`src/Services/FHIR/FhirCareTeamService.php` (search-param surface).

---

## PR 8 — Lab Results card (additional section)

**Goal:** lab observations grouped by LOINC, sorted by date, with
high/low/critical badges from `interpretation`.

- [ ] Implement `cards/LabResultsCard.tsx`:
  - [ ] `Observation?patient={id}&category=laboratory&_count=200&_sort=-date`
  - [ ] Group by `code.coding[?].system === 'http://loinc.org' .code` ?? `code.text` ?? `'Unknown'`
  - [ ] Fields per row: value (`valueQuantity.{value, unit}` or `valueString`),
        ref range (`referenceRange[0].{low, high, text}`),
        badge (`interpretation[0].coding[0].code` → H/L/HH/LL/A)
  - [ ] Client-side tiebreaker sort by `effectiveDateTime`
  - [ ] Document the `_count=200` cap in the migration doc
- [ ] **Tests:**
  - [ ] `src/cards/LabResultsCard.test.tsx` — fixture mixed Observations
        group by LOINC `code.coding[?].system === 'http://loinc.org'`; missing
        LOINC falls back to `code.text` then `'Unknown'`; H/L/HH/LL/A
        interpretation codes each render the right badge color; rows within a
        group sort by `effectiveDateTime` newest-first; `valueString` rows
        render alongside `valueQuantity` rows
- [ ] **Verify:** patient with diverse labs (mix of normal/high/low/critical/abnormal);
      grouping correct, badges colored correctly, sort newest-first

**New files:** `src/cards/LabResultsCard.tsx`, `src/cards/LabResultsCard.test.tsx`.
**Edited files:** `src/pages/Dashboard.tsx` (replace last placeholder).
**Reference (read-only):** `interface/patient_file/summary/labdata_fragment.php` (legacy reference only),
`src/Services/Search/SearchQueryConfig.php` (Observation `_sort=-date` field).

---

## PR 9 — Polish: collapse persistence, error boundaries, parity matrix

**Goal:** every card has loading/error/empty handled, collapse state
persists across refresh, and the parity matrix is captured.

- [ ] Implement `prefs/collapseStore.ts` — `localStorage` wrapper keyed by
      `${user.sub}:${cardId}`
- [ ] Wire `CardBase` to read/write collapse state through `collapseStore`
- [ ] Per-card `<ErrorBoundary>` so one card failing doesn't blank the dashboard
- [ ] Tighten loading states (skeleton or spinner per card)
- [ ] Tighten empty states (allergies NKDA distinction, "Nothing Recorded" elsewhere)
- [ ] Boundary smoke check: `grep -rE 'mysql|mysqli|PDO|Doctrine|ADODB' dashboard-spa/` returns nothing
- [ ] Capture parity matrix: 3 Synthea patients × 6 sections, side-by-side screenshots
      saved to `dashboard-spa/parity-matrix/`
- [ ] **Tests:**
  - [ ] `src/prefs/collapseStore.test.ts` — keys are namespaced
        `${user.sub}:${cardId}`; setting user A's pref does not affect user
        B's read; round-trip get/set; corrupt/missing localStorage value
        returns the documented default
  - [ ] `src/pages/Dashboard.test.tsx` — render the dashboard with one card
        component swapped for a thrower; assert the other 5 cards still
        mount and only the failing card shows the boundary fallback
- [ ] **Verify:** force-throw inside one card → other 5 still render; refresh
      browser → collapse state preserved

**New files:** `src/prefs/collapseStore.ts`, `src/prefs/collapseStore.test.ts`,
`src/pages/Dashboard.test.tsx`, `dashboard-spa/parity-matrix/*.png`.
**Edited files:** `src/components/CardBase.tsx` (collapse persistence),
all 6 `src/cards/*.tsx` (loading/error/empty polish), possibly `src/pages/Dashboard.tsx`
(wrap each card in `<ErrorBoundary>`).

---

## PR 10 — Finalize `PATIENT_DASHBOARD_MIGRATION.md`

**Goal:** the doc was stubbed in PR 1 with the architectural defense. PR 10
fills in the parts that need implementation evidence: measured tradeoffs,
parity-matrix screenshots, concrete per-card gap data. Final graded
deliverable.

- [ ] **§5 What was given up** — replace `TBD` with measured numbers:
  - [ ] First paint timing (cold load: JS + token + network) — measured against
        upstream legacy timing for same patient
  - [ ] Re-auth timing on page refresh (~1s expected, confirm)
  - [ ] Bundle size after `vite build`
  - [ ] Architectural items already in scope: no SSR, page-refresh re-auths
- [ ] **§6 Parity matrix** — embed all screenshots from
      `dashboard-spa/parity-matrix/`. One row per visible section: header +
      Allergies + Problems + Medications + Prescriptions + Care Team + Lab
      Results = 7 rows. Legacy vs port side-by-side per row.
- [ ] **§7 Known parity gaps** — finalize with concrete data per item:
  - [ ] Collapse state per-browser via `localStorage` (legacy was per-user via AJAX)
        — direct consequence of no-BFF decision
  - [ ] `hide_dashboard_cards` global unsupported
  - [ ] **Care Team edit mode out of scope per §0 read-only-summary framing**
        — explicitly *not* an API limit; FHIR `CareTeam` writes are technically
        feasible against OpenEMR; this is a deliberate scope decision
  - [ ] AllergyIntolerance `clinical-status` filter is client-side (API doesn't
        support the search param)
  - [ ] **Fork-local Co-Pilot panel excluded** (this port targets upstream
        OpenEMR's surface, not this fork's local additions)
- [ ] Final review pass on §0–§4 + §8 stubbed in PR 1 — tighten any claims
      that read as stale after a week of implementation
- [ ] **Verify:** doc compiles cleanly in markdown preview; all parity-matrix
      images load; every claim about counts/timings has a real number behind it

**New files:** none (stub created in PR 1).
**Edited files:** `PATIENT_DASHBOARD_MIGRATION.md` (finalize stub).

---

## Pre-prod-deploy checklist

Run through these **before** the first prod deploy of the SPA. Each PR
above only registers / wires for **dev** (localhost). Prod is a separate
environment with its own OpenEMR DB and its own OAuth client registry —
nothing from dev carries over.

- [ ] **Decide where the prod SPA is hosted.** Options: same Railway service
      as OpenEMR (path-mounted), separate Railway static service, Cloudflare
      Pages, Vercel, etc. The redirect_uri for the prod OAuth client depends
      on this decision — register it once you know the URL.
- [ ] **Register a separate public OAuth client in prod OpenEMR admin**
      (https://openemr-production-6c31.up.railway.app/ — Admin → System →
      API Clients). Same form as the dev registration in PR 2:
  - [ ] Application Type: **Public**
  - [ ] Redirect URI: `https://<prod-spa-url>/callback` (HTTPS only — prod
        OAuth refuses HTTP redirects outside localhost)
  - [ ] Logout URI: `https://<prod-spa-url>/`
  - [ ] Same scope set as dev
  - [ ] If created with `is_enabled=0`, flip to enabled
  - [ ] Copy the prod `client_id` (different from dev — never share)
- [ ] **Set Railway env vars** on the SPA service:
  - [ ] `VITE_OPENEMR_BASE_URL=https://openemr-production-6c31.up.railway.app`
  - [ ] `VITE_OAUTH_CLIENT_ID=<prod-client-id-from-step-above>`
  - [ ] These are **build-time** vars for Vite — must be set before `npm run
        build` runs in CI/CD, not just at SPA runtime
- [ ] **Confirm CORS / cert** on prod OpenEMR allows the SPA origin to call
      `/oauth2/default/*` and `/apis/default/fhir/*` cross-origin. The dev
      stack relaxes this; prod may need an explicit allowlist entry.
- [ ] **Smoke test the prod auth flow** end-to-end with a non-admin clinician
      account before announcing.
- [ ] **Verify the boundary check on prod build artifact:** `dist/` contains
      no references to localhost or dev URLs (grep `dist/` for `localhost`,
      `9300`, `5173`, dev `client_id` — all should be absent). Catches the
      case where someone shipped a build with `.env.local` baked in.

---

## Cross-PR conventions

- **Branch naming:** `dashboard-port/pr-NN-short-slug` (e.g. `dashboard-port/pr-02-oauth-pkce`)
- **Commit prefix:** `feat(dashboard-spa): …` for new features, `fix(dashboard-spa): …`,
      `docs(dashboard-spa): …`. PR 10 uses `docs(dashboard): add migration defense`.
- **Never reference PR numbers in commit messages** (per repo convention) — let the MR
      title do that work.
- **Each PR ends in a working state.** No half-shipped PRs that depend on the next one
      to compile.
- **Tests ship with the code they cover** (per repo `CLAUDE.md` policy). `npm run test:ci`
      must be green before a PR is marked ready. PR 2 (auth) follows test-first; PRs 3–9
      are test-alongside.
- **Boundary check on every PR:** the diff outside `dashboard-spa/` and root `*.md` files
      should be empty. If it isn't, something's wrong.
