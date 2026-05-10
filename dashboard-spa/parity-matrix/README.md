# Parity Matrix

Side-by-side rendering check between the legacy OpenEMR dashboard
(PHP/Twig) and the SPA port (React/TS). The matrix is the evidence
backing the claims in §6 of `PATIENT_DASHBOARD_MIGRATION.md`.

## Capture methodology

Captured against the same Synthea patient on the dev stack:

- **Patient:** Agustín529 Olmos892 (MRN 90006)
- **OpenEMR base:** `https://localhost:9300/`
- **SPA base:** `http://localhost:5173/`

Both views load the same FHIR data; the SPA hits OpenEMR's REST/FHIR
API directly, the legacy view renders server-side from the same DB.

For each section, two screenshots are captured at the same browser
zoom level (100%) and the same viewport (≥1280 px wide so the legacy
3-up grid is visible without horizontal scroll):

| Filename | Source |
|---|---|
| `header-legacy.png` | Legacy patient summary header |
| `header-port.png` | SPA `/patients/{uuid}` patient header |
| `allergies-legacy.png` | Legacy "Allergies" card |
| `allergies-port.png` | SPA Allergies card |
| `problems-legacy.png` | Legacy "Medical Problems" card |
| `problems-port.png` | SPA Problem List card |
| `medications-legacy.png` | Legacy "Medications" card |
| `medications-port.png` | SPA Medications card |
| `prescriptions-legacy.png` | Legacy "Prescriptions" card |
| `prescriptions-port.png` | SPA Prescriptions card |
| `care-team-legacy.png` | Legacy "Care Team" card |
| `care-team-port.png` | SPA Care Team card |
| `labs-legacy.png` | Legacy lab section |
| `labs-port.png` | SPA Lab Results card |

## Notes

The legacy dashboard's chrome (top nav, breadcrumbs, app menu) is out
of frame intentionally — see §0 / §3 of the migration doc. The matrix
checks per-section content parity, not overall page chrome.
