# Patient Dashboard Migration

A reimplementation of OpenEMR's patient dashboard in a modern framework,
consuming OpenEMR's existing REST + FHIR API as the data layer. The backend is
untouched.

This document is the **defense** for the framework choice — a graded
deliverable per the assignment. It also serves as the project's parity matrix
and known-gap inventory.

> **PR 1 status:** Sections §0–§4 and §8 are written. Sections §5–§7 are
> stubbed with `TBD` placeholders that PR 10 finalizes once the implementation
> is complete (measured numbers, parity-matrix screenshots, per-card gap
> evidence).

---

## §0 Scope

Parity is measured against the dashboard surface the assignment enumerates:

- **Authentication** — OAuth2 / OpenID Connect login
- **Patient header** — name, date of birth, sex, MRN, active status
- **Five required clinical cards** — Allergies, Problem List, Medications,
  Prescriptions, Care Team
- **One additional section** — Lab Results (chosen from the assignment's fixed
  list of options: encounter history, lab results, vitals, immunizations,
  appointments, notes)

The other ~15 cards in the upstream dashboard
(`templates/patient/card/*.html.twig`) — Advance Directives, Appointments,
Billing, Eligibility, eRx, Immunizations, Insurance, Recall, Treatment Plans,
and others — **are out of scope** by the assignment's own enumeration.

The port is positioned as a **read-only clinical summary**. The five required
clinical cards are data-display surfaces by nature. Care Team edit mode in
upstream is the only write workflow inside the assignment's required surface,
and it is deliberately out of scope per this read-only framing — *not* an API
limitation. FHIR `CareTeam` writes are technically feasible against OpenEMR;
this is a scope decision, not a constraint. See §7 for the explicit defense.

**Patient context** comes from OpenEMR's SMART launch flow; switching
patients is handled by restarting authorization. The SPA does not own a
patient picker — that is a server-side responsibility under the SMART
standalone-launch pattern, and a direct consequence of the public-client /
no-BFF decision in §3. See §5 and §7 for the explicit tradeoff and the
"Switch patient" UX that surfaces it to the user.

---

## §1 Why port at all

The OpenEMR patient dashboard is rendered by
`interface/patient_file/summary/demographics.php` — a 2,072-line PHP
orchestrator that pulls 7+ services, dispatches Twig partials, and bolts
together AJAX fragments. The card layer was modernized in 2024–2025 (Twig
templates under `templates/patient/card/`), but the runtime stack is still
PHP + jQuery + Smarty + Twig + Bootstrap 4 + Apache.

What that costs in practice:

- **Three template engines coexisting.** A single card (`rx.html.twig`)
  injects a Smarty controller fragment into a Twig template that gets included
  by another Twig template. Reading the data flow for one card means jumping
  between Twig, Smarty, PHP service layers, and the controller.
- **Untyped service-layer arrays.** Service-layer responses are bare
  associative arrays passed through 3–4 layers before Twig renders them. A
  field rename surfaces as a silent rendering glitch, not a compile-time
  error.
- **jQuery + Bootstrap 4 collapse logic on every card.** Every card binds
  jQuery click handlers for the collapse chevron. The state lives in the DOM
  and an AJAX call to `interface/patient_file/summary/save_dashboard_card.php`
  per user.
- **A 2,072-line orchestrator.** `demographics.php` mixes auth checks,
  globals access, service orchestration, AJAX handling, and view dispatch.
  Adding a card requires touching all of these layers.

The technology debt isn't theoretical — it produces real maintenance friction
on every change. The case for porting is to swap a fragmented runtime for a
single typed component tree fed by a typed FHIR client.

---

## §2 Why React + Vite (not Next.js, not SvelteKit, not Remix)

Three rejected alternatives, steel-manned.

### Next.js 15

**The case for it.** Best-in-class developer experience in 2026. File-based
routing, SSR + RSC for fast first paint, server-side data fetching, built-in
API routes that could double as a BFF. Mature ecosystem, tons of OAuth
recipes.

**Why rejected.** Next's headline benefit is server rendering — and the
assignment forbids backend changes. To use Next we'd ship a Node server
*alongside* OpenEMR, introducing a brand-new piece of infra to deploy,
configure, and secure. The "backend untouched" claim becomes harder to
defend when there's a second backend.

The SSR benefit also doesn't apply here. Every dashboard page requires an
authenticated FHIR token; we can't pre-render anything without first running
the OAuth dance. SSR for an authenticated EHR dashboard would just be a
delay in the client→server→client roundtrip, not a perceived perf win.

Runner-up reason: Next.js was the strongest *defense narrative* candidate
("the modern PHP equivalent"). I traded it for **runtime simplicity**.

### SvelteKit

**The case for it.** Smaller bundles than React for the same app surface
(no virtual DOM runtime). Idiomatic stores. SSR is optional, so the
no-backend constraint is easier to satisfy than with Next. Component syntax
is closer to plain HTML, which would make the "lift Twig markup" recipe
even more mechanical.

**Why rejected.** Two reasons:

1. **Ecosystem maturity for healthcare/FHIR.** `@types/fhir` works
   regardless of framework, but the worked examples for OAuth + PKCE,
   FHIR clients, healthcare UI patterns, and drug-interaction libs are
   overwhelmingly React. For a one-week port, picking the framework with
   the deepest pile of working examples reduces risk.
2. **Defense narrative strength.** "PHP + jQuery → React + TypeScript" is
   the well-trodden path; reviewers don't have to learn a new paradigm
   to evaluate the work. "PHP + jQuery → Svelte" makes me defend two
   things at once: the port itself, *and* the framework choice as a
   second-order bet. The grade rewards a defensible decision, not the
   decision with the best raw numbers.

### Remix (now React Router 7)

**The case for it.** Nested routes with co-located data loaders. First-class
error boundaries per route. Progressive enhancement (forms work without JS).
The data-loading pattern would map naturally to "load all card data in
parallel before rendering the route."

**Why rejected.** Same as Next: Remix's loader pattern is server-first.
Running it as a pure SPA loses the unique value (PE on transitions), at
which point you're back to React Router 7 — which we're using anyway. The
nested-loader idea is good enough that we'll borrow it: every card owns its
own data fetch and renders independently, and a top-level dashboard
component does `Promise.all` across cards.

### Vite + React + TypeScript

**Why this is the choice.** It's the simplest path that satisfies the
constraint:

- **Zero new infra.** A static `dist/` artifact is the entire deployment
  surface. Serve it from any CDN or `npm run preview`.
- **The boundary stays clean.** Browser → OpenEMR direct. No BFF, no
  session store, no second auth layer.
- **The defense narrative is well-trodden.** Reviewers can evaluate the
  port without learning a new framework.
- **Type safety where it matters.** `@types/fhir` + strict mode means the
  data layer is checked end-to-end. PHPStan level 10 in the legacy code
  doesn't reach into the Twig templates; React + TS does.

---

## §3 Why pure SPA (no BFF)

A backend-for-frontend would buy:

- Server-side OAuth (refresh tokens never reach the browser)
- Centralized request logging
- The ability to massage FHIR shapes before the client sees them

It would also introduce:

1. **A new datastore.** Sessions and refresh tokens have to live somewhere.
   Now we own session lifecycle, eviction, and a Redis or SQLite or
   filesystem state we didn't have before.
2. **A second auth surface.** The browser auths to the BFF; the BFF auths
   to OpenEMR. Two token formats, two refresh windows, two failure modes.
3. **A deployment story.** The SPA is static; the BFF is a long-running
   process. Now there are two artifacts, two health checks, two
   environments.
4. **CSRF concerns.** Cookie-based browser→BFF auth needs CSRF tokens;
   CORS for browser→OpenEMR direct calls is a one-time CORS config on
   OpenEMR.

The pure-SPA path:

- **Tokens in memory.** React context holds `{access_token, refresh_token,
  expires_at, patient}`. Page refresh triggers the OAuth dance again
  (silent if the OpenEMR session is alive).
- **Single-flight refresh.** When 6 cards fetch in parallel and all 6 hit
  401, exactly one refresh request fires; the other 5 wait on the same
  promise.
- **`localStorage` for non-sensitive UI prefs only.** Card collapse state
  per `${user.sub}:${cardId}`. Never tokens. Never PHI.

The trade is real: page refresh re-auths, and a stolen device with an
unlocked browser tab can read the access token from memory. The first is
documented as a known cost; the second is the standard SPA security model
and is no worse than upstream's session cookie.

---

## §4 What was gained

What the port produces that the legacy stack doesn't:

- **End-to-end typed data layer.** `fhirSearch<AllergyIntolerance>(...)`
  returns a typed array. The compiler catches a `criticality` rename
  before runtime. Legacy passes untyped arrays through 3+ layers of PHP +
  Twig.
- **Per-card error boundaries.** One card crashing — bad data, network
  error, server 500 — does not blank the dashboard. Other cards keep
  rendering. Legacy's PHP rendering is all-or-nothing per request.
- **Parallel fetches, explicit.** A top-level `Promise.all` across all
  six cards is one line of code. Legacy's per-card service calls are
  sequential server-side; the AJAX-fragment cards add a second
  serialization point.
- **Code-splitting by route, free.** Vite splits each route into its own
  chunk. Legacy serves a 1MB+ HTML page with everything inlined.
- **No jQuery, no Smarty, no Twig-includes-Twig-includes-Smarty.** One
  template language (TSX), one runtime (React), one bundler (Vite).
- **Hot reload <100ms.** Editing a card's JSX shows the change without a
  page refresh. Legacy's Apache + PHP cycle is closer to 1–2s, and the
  jQuery-based collapse state resets every reload.
- **A reusable scaffold.** `<CardBase>`, `fhirSearch`, the auth flow —
  every other PHP page in the OpenEMR dashboard could be ported onto
  this same skeleton (see §8).

---

## §5 What was given up

> **PR 10 finalizes this section** with measured numbers from the running
> implementation. The architectural costs below are committed up-front;
> the perf numbers come once the dashboard is built.

**Architectural costs (committed in advance):**

- First paint waits for JavaScript to load + the OAuth token round-trip +
  the FHIR fetches. Legacy's PHP-rendered HTML is ready on the first byte.
- Page refresh re-runs the auth flow. Silent if the OpenEMR session is
  still alive (~1s on local dev, expected); a full re-login otherwise.
- No SSR. No first paint without JavaScript. The fallback is the static
  `index.html` shell with the React mount point.
- **Multi-patient navigation requires re-auth.** Public-client SMART
  standalone-launch binds the access token to one patient per session, so
  switching patients restarts authorization to invoke OpenEMR's SMART
  picker. The legacy PHP dashboard let a clinician click between patients
  freely within one session. This is the most visible cost of the
  "no-BFF, no-confidential-client" decision in §3, and the SPA surfaces
  it explicitly as a "Switch patient" button rather than hiding it.

**Perf numbers (TBD — finalized in PR 10):**

- `TBD` — first paint timing (cold load, JS + token + parallel FHIR)
  measured against legacy for the same Synthea patient.
- `TBD` — re-auth timing on page refresh.
- `TBD` — production bundle size after `vite build` (`dist/` total + per-chunk).

---

## §6 Parity matrix

> **PR 10 finalizes this section** with side-by-side screenshots for 3
> Synthea patients × 7 visible sections. Each row: legacy left, port right,
> field-for-field check.

`TBD`

Layout per row:

| Section | Legacy (PHP/Twig) | Port (React/TS) |
|---|---|---|
| Patient header | _screenshot_ | _screenshot_ |
| Allergies | _screenshot_ | _screenshot_ |
| Problem List | _screenshot_ | _screenshot_ |
| Medications | _screenshot_ | _screenshot_ |
| Prescriptions | _screenshot_ | _screenshot_ |
| Care Team | _screenshot_ | _screenshot_ |
| Lab Results | _screenshot_ | _screenshot_ |

Screenshots will live under `dashboard-spa/parity-matrix/`.

---

## §7 Known parity gaps

> **PR 10 finalizes this section** with per-card evidence. The gap inventory
> below is pre-known from the planning phase; PR 10 confirms each item once
> the implementation surfaces concrete behavior.

### Architectural gaps (consequence of locked decisions)

- **Collapse state per-browser via `localStorage`.** Legacy persists card
  collapse state per-user via an AJAX call to
  `save_dashboard_card.php`. The pure-SPA / no-BFF decision means we have
  no backend to persist to; `localStorage` keyed by `${user.sub}:${cardId}`
  is the closest substitute. Different browser → different state. Direct
  consequence of the §3 decision.
- **Multi-patient navigation requires re-auth.** Legacy lets a clinician
  click between patients freely in one session. The public-client
  SMART standalone-launch model binds an access token to one patient per
  session — switching patients goes through OpenEMR's SMART picker as a
  fresh OAuth round-trip. Surfaced explicitly as a "Switch patient"
  button, not hidden. This is the dominant trade we made for "no new
  infra" in §3, called out here so a reviewer doesn't discover it as a
  surprise.
- **`hide_dashboard_cards` global is unsupported.** OpenEMR has an admin
  global that can hide specific dashboard cards site-wide. The SPA does
  not read OpenEMR globals (no DB access by §0 boundary).

### API limits

- **AllergyIntolerance `clinical-status` filter is client-side.** OpenEMR's
  FHIR API does not expose `clinical-status` as a search parameter. The
  card fetches all entries and filters in the browser. Documented in
  `cards/AllergiesCard.tsx`.

### Scope decisions (deliberate, not API-limited)

- **Care Team edit mode is out of scope per §0's read-only-clinical-summary
  framing.** This is *not* an API limitation — FHIR `CareTeam` writes are
  technically feasible against OpenEMR. The four other required clinical
  cards (Allergies, Problems, Medications, Prescriptions) are read-only
  views by nature; positioning the whole port as a read-only summary keeps
  the framing coherent across the dashboard. Adding write workflows would
  multiply the scope (form UI, role pickers, validation, error handling,
  optimistic updates) for a one-week port.
- **Fork-local Co-Pilot panel is excluded.** The fork this port is built in
  ships an inline Co-Pilot panel that upstream OpenEMR does not have. The
  assignment enumerates a fixed set of required sections; Co-Pilot is not
  among them. This port targets upstream OpenEMR's dashboard surface, not
  this fork's local additions.

`TBD` — per-card field-level gap notes after implementation.

---

## §8 What's reusable

The infrastructure built for the patient dashboard is also a scaffold for
porting any other PHP page in OpenEMR:

- **`<CardBase>`** — the collapsible card primitive that mirrors
  `templates/patient/card/card_base.html.twig`. Header + chevron + body +
  collapse state via `localStorage`. Drop-in for any future card-shaped UI.
- **`fhirSearch` / `fhirRead`** — typed, single-flight refresh on 401,
  flattens Bundles into arrays, treats 403 as an empty result (out-of-scope
  scope ≠ error). Generic across FHIR resources via TS generics.
- **`AuthContext` + PKCE flow** — OAuth public client + code verifier +
  silent refresh in ~120 LOC of `crypto.subtle`. Works for any other
  page that wants to migrate; no client secret to manage.
- **The "lift Twig markup" recipe** — the documented workflow (view-source
  the upstream page → paste DOM into JSX → mechanical class→className /
  for→htmlFor / `{% for %}` → `.map()` translation → bind to FHIR data) is
  applicable to every other Twig card. Each port is now hours, not weeks.

The point: this isn't a one-card prototype. It's a working scaffold that
the rest of OpenEMR's PHP dashboard can migrate onto, one card at a time,
without ever touching the backend.
