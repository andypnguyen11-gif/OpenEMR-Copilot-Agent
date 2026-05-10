# Dashboard SPA

Modern framework port of the OpenEMR patient dashboard, consuming OpenEMR's
existing REST + FHIR API as the data layer. See
[`../PATIENT_DASHBOARD_MIGRATION.md`](../PATIENT_DASHBOARD_MIGRATION.md) for
the framework defense and parity matrix.

## Deployed demo

- **URL:** https://dashboard-spa-production.up.railway.app
- **Login:** `admin` / `ChangeMe_StrongAdminPass_456` (same as the deployed
  OpenEMR — the SPA OAuths against it via SMART standalone launch)
- **Patient to pick:** `Sofia Reyes` on the SMART patient-selection screen
  (the rest of the list is Synthea fixture data without curated content)

Grading window only — credentials and host will be invalidated post-review.

## Stack

- Vite 8 + React 19 + TypeScript 6 (strict, `noUncheckedIndexedAccess`)
- Bootstrap 4.6 CSS — JS components reimplemented in React; we deliberately
  avoid jQuery to make the migration argument
- React Router v7
- `@types/fhir` for FHIR R4 types
- OAuth2 PKCE public client, no client secret (PR 2)

## Prerequisites

- Node 20+ (developed against 22.22.1)
- A running OpenEMR instance with FHIR + OAuth2 enabled. Default expected at
  `https://localhost:9300/`.

## One-time setup

1. Register a public OAuth client in OpenEMR:
   - Sign in as admin
   - Admin → System → API Clients → Register New Client
   - Application Type: **Public**
   - Redirect URI: `http://localhost:5173/callback`
   - Scopes: `openid`, `offline_access`, `launch/patient`, `patient/*.read`
   - After saving, copy the `client_id`
2. Copy `.env.example` to `.env.local` and fill `VITE_OAUTH_CLIENT_ID`

## Run

```bash
npm install            # first time only
npm run dev            # serves at http://localhost:5173/
npm run typecheck      # type-only check, no emit
npm run build          # production build to dist/
npm run preview        # serve the dist/ build locally
```

The first time you load the app it will redirect to OpenEMR's authorize
endpoint at `https://localhost:9300/`. You'll need to accept OpenEMR's
self-signed cert in your browser once.

## Why this directory exists

This SPA is the "modern framework port" deliverable for the OpenEMR patient
dashboard take-home. Constraints:

- **Backend untouched.** Zero PHP / MySQL / service-layer edits in the parent
  repo. Diffs outside `dashboard-spa/` and the root migration doc should be
  empty.
- **FHIR / REST API only.** No direct DB access, no joins against OpenEMR's
  schema for fields FHIR doesn't expose.
- **Read-only clinical summary.** Care Team edit mode and other write
  workflows are out of scope per the assignment's data-display framing.

See [`../plans/patient-dashboard-port.md`](../plans/patient-dashboard-port.md)
for the implementation plan and
[`../Patient-Dashboard-Migrations-Tasks.md`](../Patient-Dashboard-Migrations-Tasks.md)
for the PR-by-PR breakdown.
