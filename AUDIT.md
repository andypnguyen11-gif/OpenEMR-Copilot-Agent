# AUDIT.md — OpenEMR Audit for Clinical Co-Pilot

**Status:** Draft for MVP submission
**Last updated:** 2026-04-27
**Scope:** Audit of the OpenEMR fork at `./openemr/` running locally, conducted to inform the AI agent integration plan in `./ARCHITECTURE.md`.
**Method:** Codebase walk (entry points, REST/FHIR layer, ACL, audit log, schema, demo data) + running local instance for behavioral confirmation. File:line references throughout point to the fork at `/Users/andynguyen/Desktop/OpenEMR/openemr/`.

---

## Executive Summary

OpenEMR is a security-mature, architecture-mixed, data-thin codebase. The audit produced findings in all five required dimensions; five are load-bearing for the agent integration and are summarized here.

**1. FHIR API authorization is consistent — PRD assumption #2 confirmed.** Every FHIR R4 route in `apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php` (~80 endpoints) re-checks ACL/scope on every request via either `RestConfig::request_authorization_check()` or patient-context filtering against `$request->getPatientUUIDString()`. OAuth2 token claims are not trusted alone: token validity is checked at kernel.request stage by `BearerTokenAuthorizationStrategy`, and ACL is then re-checked from the database. **This is the most consequential finding of the audit.** It means the agent service can call OpenEMR's FHIR endpoints directly with a properly-scoped OAuth2 client, and the existing ACL is the source of truth — exactly the model PRD §6 and ARCHITECTURE.md §4 assume. If this had failed, every PHI access would have had to route through a custom PHP gateway re-implementing ACL.

**2. Demo data is insufficient for the differentiating feature (use case 3).** `sql/example_patient_data.sql` contains ~14 patient demographics with **no encounters, no problems, no medications, no allergies, no notes** seeded. Discrepancy detection cannot be demonstrated against this data — there is nothing to detect. The audit recommends seeding a small adversarial fixture (3 patients with hand-crafted med-vs-note, allergy-vs-intake, and active-vs-resolved-problem conflicts). This kills PRD §14 open question #3 (resolved: yes, seeding required) and adds it to the MVP critical path.

**3. Clinical notes are a partial FHIR gap.** Most agent tools (`get_meds`, `get_allergies`, `get_labs`, `get_problems`, `get_visits`, `get_immunizations`) map cleanly to existing FHIR R4 controllers in `src/RestControllers/FHIR/`. Clinical narrative notes do not — `FhirDocumentReferenceRestController` returns metadata for scanned/uploaded documents, not encounter SOAP notes. Encounter notes live in `pnotes.body` (LONGTEXT) and `form_encounter` form data with no FHIR Composition surface. The agent will need a custom PHP gateway endpoint that hydrates note text via the `EncounterService` layer. This confirms the PRD's "FHIR primary, custom gateway for gaps" architecture.

**4. Existing audit infrastructure is good but not sufficient for BAA traceability.** OpenEMR has a mature audit system: `EventAuditLogger` writes to `log` and `api_log` tables, including SQL-level interception via `library/ADODB_mysqli_log.php`. SELECT logging is **disabled by default** (a HIPAA gap on its own), but more importantly the schema doesn't carry the fields needed to demonstrate "PHI sent to LLM under BAA" — `tool_name`, `fields_requested` (schema, not values), `prompt_hash`. This **confirms the PRD §8 decision to keep the agent's audit log in a separate Postgres table** rather than extending OpenEMR's `api_log`.

**5. Performance and data quality both narrow the fast lane.** The `lists` table (problems/allergies/meds in one table by `type`) lacks a composite `(pid, type)` index; `pnotes.body` is unindexed LONGTEXT; there is no application-level cache (no Redis/APCu integration in `src/` or `library/`). For the fast-lane (<5s) budget this means: per-request DB hits, sequential scans on note text, and an architectural need for the agent service's own cache layer (PRD §8 already calls for in-memory + TTL at MVP). Schema quality is mixed: `lists` allows nullable `begdate`/`enddate`/`diagnosis`/`activity`; no FK constraints anywhere; coding (ICD-10, RxNorm) is free-text with optional coded references. Discrepancy detection must normalize text and tolerate orphans.

**Net effect on the integration plan:** the architecture in PRD/ARCHITECTURE.md is feasible as designed. Two adjustments are needed: (a) add a seeded discrepancy fixture to the MVP critical path, and (b) build a custom `/api/agent/patient/:pid/notes` endpoint to fill the FHIR notes gap. Everything else — OAuth2 client-credentials auth, FHIR-first data access, separate Postgres for agent audit, in-process cache — is supported by what's already in OpenEMR.

---

## 1. Security Audit

### 1.1 Authentication

OpenEMR uses bcrypt by default (`PASSWORD_DEFAULT` in PHP 7.2+) with a documented Argon2 upgrade path. No hardcoded admin credentials — `setup.php:522` requires admin-supplied password during installation; the installer generates a random username if not specified.

| # | Severity | Finding | Location |
|---|---|---|---|
| S-01 | LOW | Password hashing is bcrypt with Argon2 fallback support — no MD5/SHA1 paths found. | `src/Common/Auth/AuthHash.php:53–126` |
| S-02 | LOW | No default credentials shipped; setup.php forces admin to provide password. | `setup.php:522, ~900` |

### 1.2 Session management

| # | Severity | Finding | Location |
|---|---|---|---|
| S-03 | HIGH | `session.cookie_secure` defaults to `false` for core UI sessions. Session cookies can travel over HTTP if HTTPS is not enforced at the web server. **Mitigated for the agent** because the agent uses OAuth2 bearer tokens, not session cookies. | `src/Common/Session/SessionConfigurationBuilder.php:26` |
| S-04 | HIGH | `session_regenerate_id()` is not explicitly called at login completion. Pre-login session IDs may persist into authenticated sessions (session fixation risk). | `library/auth.inc.php:29–74` |
| S-05 | MEDIUM | Default `cookie_samesite=Strict` on core sessions, but the OAuth2 portal session overrides to `None`. Acceptable when Secure flag is on; risky on misconfigured HTTPS. | `src/Common/Session/SessionConfigurationBuilder.php:25, 99` |
| S-06 | HIGH | No application-level idle timeout enforcement. Timeout is delegated to PHP `session.gc_maxlifetime`. HIPAA expects ~15-minute idle timeout enforced at the app. | (no app-level enforcement found) |

### 1.3 Authorization — ACL/RBAC

OpenEMR's ACL is implemented via the `gacl` library and `src/Gacl/`, surfaced through `AclMain::aclCheck*()`. The ACL pattern is **pre-fetch authorization**: ACL is checked before data is returned, not after.

| # | Severity | Finding | Location |
|---|---|---|---|
| S-07 | LOW (POSITIVE) | All ~80 FHIR R4 routes either filter by `$request->getPatientUUIDString()` for patient-scoped requests or call `RestConfig::request_authorization_check()` for user/system-scoped requests. **No unguarded FHIR endpoints found.** | `apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php:73–1000+` |
| S-08 | MEDIUM | OAuth2 token scopes are **frozen at issuance** — `BearerTokenAuthorizationStrategy` does not re-fetch the user's current ACL from `gacl` tables; only token claims and `RestConfig::scope_check()` are consulted. If a clinician's permissions are revoked, outstanding tokens still carry the original scopes until expiry. | `src/RestControllers/Authorization/BearerTokenAuthorizationStrategy.php:136–180` vs. `src/RestControllers/Config/RestConfig.php:196–240` |
| S-09 | MEDIUM | OAuth2 client registration is admin-only (no machine-readable provisioning). The agent's OAuth2 client must be pre-registered with minimal scopes via the OpenEMR admin UI before sidecar startup. | `API_README.md:23–31`; `oauth2/authorize.php` |

### 1.4 Injection / input handling

| # | Severity | Finding | Location |
|---|---|---|---|
| S-10 | LOW (POSITIVE) | Modern REST controllers use `QueryUtils::fetchRecords($sql, $binds)` with parameterized binds; table/column names are whitelisted. | `src/Common/Database/QueryUtils.php:30–69` |
| S-11 | MEDIUM | Legacy procedural code in `library/` may construct SQL via concatenation. The agent does not call into legacy code paths and is not directly affected, but any custom PHP gateway endpoint must use modern QueryUtils, not legacy `sqlStatement()` patterns. | `library/api.inc.php` (and similar) |

### 1.5 CSRF

CSRF tokens use HMAC-SHA256 with `hash_equals()` for constant-time comparison. CSRF is enforced for session-based forms but **not** for REST/FHIR endpoints — those rely on OAuth2 instead. This is the correct posture.

| # | Severity | Finding | Location |
|---|---|---|---|
| S-12 | LOW (POSITIVE) | CSRF protection on session forms; OAuth2 used for API surfaces. | `src/Common/Csrf/CsrfUtils.php` |

### 1.6 PHI leakage surfaces

| # | Severity | Finding | Location |
|---|---|---|---|
| S-13 | MEDIUM | `display_errors=0` in bootstrap (good), but exception handlers can log full stack traces to PSR-3 logger. If exception data carries patient values, those are written to error logs unencrypted. | `bootstrap.php`; `src/Core/ErrorHandler.php:161` |
| S-14 | MEDIUM | Patient IDs appear in GET parameters across legacy `interface/patient_file/` pages. Mitigated by HTTPS in transit, but PHI identifiers can land in webserver access logs. | `interface/patient_file/reminder/clinical_reminders.php`, `pat_ledger.php` |

### 1.7 Implications for the agent

**Two-token model (clarified post-audit).** The agent integration uses *two* tokens at *two* boundaries — they do not duplicate each other (full table in `ARCHITECTURE.md` §4 "Two trust layers, two tokens"):

- **HMAC JWT (HS256)** — internal PHP gateway → Python agent service. 5-min, carries per-request `{user_id, role, patient_id, nonce}`.
- **OAuth2 bearer** — agent service → OpenEMR FHIR/REST. Pre-registered client, scope-frozen at issuance, bears minimum patient-read scopes.

The HMAC JWT does not travel to OpenEMR's FHIR endpoints; the OAuth2 token does not encode per-request user/patient context. They compose, they don't substitute.

1. **Authenticate the agent service to OpenEMR exclusively via OAuth2 bearer tokens** (no session cookie sharing). PRD §6 already specifies this; the audit confirms the OAuth2 flow is the only safe authentication boundary at this layer.
2. **Pre-register the agent's OAuth2 client with minimum scopes** (e.g., `patient/Patient.read patient/Observation.read patient/Condition.read patient/AllergyIntolerance.read patient/MedicationRequest.read patient/Encounter.read`). Do not request `user/*` or `system/*` scopes.
3. **Plan for token refresh when permissions change**, since outstanding tokens carry frozen scopes (finding S-08). For MVP this is acceptable given short-lived (5-min) HMAC tokens between PHP gateway and agent service.
4. **Ensure HTTPS is enforced at the deployment layer** (Railway terminates TLS; this is satisfied) — finding S-03 is mitigated by infrastructure, not application code.

---

## 2. Architecture Audit

### 2.1 Codebase shape

OpenEMR is a hybrid: a modern PSR-4 namespaced layer in `src/` (Laminas MVC + Symfony components, Doctrine DBAL, Twig 3.x) sits atop a legacy procedural codebase in `interface/` and `library/`. The two halves coexist: most pages still go through per-page entry scripts in `interface/`, while REST/FHIR APIs flow through `apis/dispatch.php` to controllers in `src/RestControllers/`.

| Concern | Where it lives |
|---|---|
| Browser entry point | `public/index.php` → `FallbackRouter` (legacy routing to per-page scripts) |
| REST/FHIR entry point | `apis/dispatch.php` → controllers in `src/RestControllers/` |
| Modern templating | `templates/` (Twig 3.x) |
| Legacy templating | inline PHP in `interface/`, some Smarty |
| Service layer | `src/Services/` and `src/Services/FHIR/` (use these, never raw SQL) |
| ORM | Doctrine DBAL via `QueryUtils`; minimal Doctrine entities |
| Audit log | `src/Common/Logging/` + `library/ADODB_mysqli_log.php` |
| Module system | `interface/modules/custom_modules/` with `moduleConfig.php` |

JS assets (jQuery 3.7, Bootstrap 4.6, KnockoutJS) are loaded globally via `config/config.yaml`. There is no SPA framework on the main interface, which is good for the PRD §7 decision to defer React.

### 2.2 Insertion points for agent surfaces

The PRD calls for two surfaces: a Daily Brief (slow lane, pre-clinic) and an in-chart Side Panel (fast lane, between rooms). The audit identified concrete, non-forking attachment points for both.

| Surface | Path / mechanism | Forkability |
|---|---|---|
| **Daily Brief — new top-nav tab** | Add menu entry; opens new frame via `interface/main/tabs/js/include_opener.js` pattern | Non-forking |
| **In-chart Side Panel — primary insertion point** | Listen to `patientSummaryCard.render` event fired in `interface/patient_file/summary/demographics.php`; append content via `RenderEvent::addAppendedData(RenderInterface)` | **Non-forking — preferred.** Event-driven injection is exactly the hook the PRD wanted. |
| **In-chart Side Panel — fallback (encounter view)** | Twig template extension or Symfony listener on encounter pages | Moderate; legacy paths |
| **Custom PHP gateway endpoint (REST)** | Add route to `apis/routes/_rest_routes_standard.inc.php` as `"GET /api/agent/..." => function(HttpRestRequest $request) { ... }` | Non-forking |
| **Custom PHP gateway endpoint (FHIR-style)** | Mirror `apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php` pattern; create controller extending `RestControllerHelper` | Non-forking |
| **Custom module (full lifecycle)** | New directory under `interface/modules/custom_modules/` with `moduleConfig.php`, `openemr.bootstrap.php`, `src/`, `sql/` | Non-forking; standard module API |

**Decision:** the agent will live as a **custom module** for clean install/uninstall, with the in-chart panel attaching via the `patientSummaryCard.render` event. The custom REST endpoints (PHP gateway) live in the module's bootstrap. PRD §14 open question 1 (Daily Brief nav location) and 2 (side panel attachment) are **answered by this audit**: top-nav for the brief, `patientSummaryCard.render` listener for the panel.

### 2.3 FHIR coverage table — per agent tool

This is the load-bearing audit output for ARCHITECTURE.md §5 (data access).

| Agent tool | FHIR resource | Coverage | Source |
|---|---|---|---|
| `get_meds` | MedicationRequest, Medication | **Covered** | `src/RestControllers/FHIR/FhirMedicationRequestRestController.php`, `FhirMedicationRestController.php` |
| `get_allergies` | AllergyIntolerance | **Covered** | `src/RestControllers/FHIR/FhirAllergyIntoleranceRestController.php` |
| `get_labs` | Observation (laboratory category) | **Covered** | `src/RestControllers/FHIR/FhirObservationRestController.php`, `src/Services/FHIR/FhirObservationService.php` |
| `get_vitals` | Observation (vital-signs category) | **Covered** | same Observation service with category filter |
| `get_problems` | Condition | **Covered** | `src/RestControllers/FHIR/FhirConditionRestController.php` |
| `get_visits` | Encounter | **Covered** | `src/RestControllers/FHIR/FhirEncounterRestController.php` |
| `get_immunizations` | Immunization | **Covered** | `src/RestControllers/FHIR/FhirImmunizationRestController.php` |
| `get_notes` (clinical narrative) | DocumentReference | **Partial** — returns scanned-document metadata, not SOAP/H&P narrative | `src/RestControllers/FHIR/FhirDocumentReferenceRestController.php` |
| `get_notes` (encounter SOAP) | (no FHIR Composition resource in fork) | **Gap** — text lives in `pnotes.body` (LONGTEXT) and `form_encounter` form data | none — needs custom PHP gateway endpoint |

**Action:** build one custom module endpoint, e.g. `GET /apis/default/api/agent/patient/:pid/notes`, that hydrates note text via `src/Services/EncounterService` (or equivalent) rather than reading legacy tables directly. This isolates the agent from schema churn.

### 2.4 Architectural risks

| # | Risk | Mitigation |
|---|---|---|
| A-01 | Patient context in legacy pages depends on `$_SESSION['pid']` and `$_GET['pid']` globals (`OEGlobalsBag` singleton). Silent patient ID mismatch is possible. | The agent always passes `patient_id` explicitly in tool calls; the agent service never trusts session globals. PRD §6 patient-context binding handles this. |
| A-02 | `patientSummaryCard.render` only fires on the demographics tab. The side panel is invisible elsewhere. | For MVP, scope the side panel to the demographics tab. Listening on additional events is post-MVP work. |
| A-03 | jQuery/Bootstrap/KnockoutJS load globally. A modern UI framework would coexist but not integrate. | Embed the agent UI in an iframe or shadow DOM; use distinct selectors (`data-agent-*`); do not modify core form elements. PRD §7 already chose vanilla/Alpine over React. |
| A-04 | Notes are stored in proprietary tables (`pnotes`, `form_encounter.data` serialized form fields). Direct SQL access will break on schema migrations. | Always go through `src/Services/` layer, never raw SQL. Custom note endpoint will wrap `EncounterService`. |
| A-05 | No application cache (Redis/APCu) means every FHIR call hits the DB. | The agent service maintains its own in-memory + TTL cache per PRD §8. The discrepancy engine pre-warms the fast lane via the slow-lane background pass. |

---

## 3. Data Quality Audit

This section is closely tied to use case 3 (discrepancy detection) — the differentiating feature depends on data quality being interesting enough to detect against, and clean enough to detect reliably.

### 3.1 Schema-level concerns

| # | Severity | Finding | Affects use case | Location |
|---|---|---|---|---|
| D-01 | HIGH | `lists` table (problems/allergies/meds in one table, discriminated by `type`) allows NULL on `begdate`, `enddate`, `diagnosis`, `activity`. A problem with no start date or no `activity` flag is ambiguous (active? resolved? archived?). | 1, 3 | `sql/database.sql:7674–7700` |
| D-02 | HIGH | Coding is free-text with **optional** coded references. `lists.title` is free text + optional `lists.list_option_id`. `prescriptions.drug` is free text + optional `prescriptions.rxnorm_drugcode`. "Metformin", "metformin", "metformin 500mg" can coexist as three rows. | 1, 2, 3 | `sql/database.sql:7677–7688, 8709–8711` |
| D-03 | MEDIUM | **No foreign key constraints** in the schema. Deleted patient rows orphan all `lists`, `prescriptions`, `pnotes` rows. The discrepancy engine must tolerate orphan PHI. | 3 | `sql/database.sql` (no `FOREIGN KEY` matches) |
| D-04 | MEDIUM | Notes are unstructured LONGTEXT. No SOAP/section markers in the schema. "Was the medication mentioned in the assessment vs. the plan?" requires regex/NLP, not structured queries. | 1, 3 | `sql/database.sql:8670–8689` (`pnotes.body`) |
| D-05 | LOW | No uniqueness constraint on common-sense duplicate-prone shapes (same `pid`, `type`, `title` in `lists`). | 3 | `sql/database.sql:7674–7700` |

### 3.2 Demo data sufficiency

This is the biggest data-quality finding of the audit:

> **`sql/example_patient_data.sql` contains ~14 patient demographics with zero clinical content** — no encounters, no problems, no medications, no allergies, no notes. **Discrepancy detection cannot be demonstrated against this data.**

This kills PRD §14 open question #3 (sample-data sufficiency). The answer is **no, not sufficient**, and the MVP critical path now includes a seeded fixture.

**Minimum adversarial fixture for use case 3 demo + eval:**

| Conflict type | Setup | Detection rule it exercises |
|---|---|---|
| **Med list vs. note disagreement** | `lists` row: medication "Metoprolol 50mg active" with no `enddate`. `pnotes.body` for the most recent encounter contains "patient discontinued metoprolol on 2026-03-19 due to fatigue." | Record-consistency: med-active-in-list vs. discontinued-in-note |
| **Allergy on intake form not in allergy table** | Encounter intake form mentions "patient reports new sulfa allergy." `lists` has no allergy entry of type `allergy` for sulfa. | Record-consistency: narrative-only allergy not coded |
| **Active-but-resolved problem** | `lists` row: problem "Hypertension, active=1, no enddate". Recent note says "BP well-controlled for 6 months, consider tapering." | Data-quality: stale-active flag |
| **Allergen / med conflict (safety flag)** | `lists` allergy row: "Penicillin." Active medication: "Amoxicillin." | Safety flag: active med matches recorded allergen |
| **Stale lab** | `procedure_result` row: HbA1c last drawn 14 months ago for a patient with `lists` problem "Type 2 Diabetes". | Data-quality: chronic-condition lab not within expected window |

**File to create:** `sql/example_discrepancy_data.sql` with ~3 patients, the inserts above, and a script that loads it after the standard demo data. This file should also live in the agent module's `sql/` directory for reproducibility.

### 3.3 Implications for the agent

1. **Discrepancy engine must normalize free-text codes.** Lowercase + trim + dose-stripping, plus optional cross-reference against `list_option_id` / `rxnorm_drugcode` when present. Without this, false negatives dominate.
2. **The seeded fixture is part of the MVP.** Without it, the differentiating feature has no demo and no eval.
3. **Note structure is regex/NLP territory, not structured queries.** Discrepancy rules that compare against note content must use either keyword presence checks (cheap, brittle) or LLM-extracted facts (expensive, requires its own verification). For MVP, scope to keyword checks against the most recent note(s) only.
4. **Tolerate orphans.** No FK constraints means the engine may encounter PHI rows whose patient row is deleted. Filter at query time.

---

## 4. Performance Audit

The fast lane (<5s) is the binding constraint. The slow lane has slack. Findings below are ranked by their effect on the fast lane.

### 4.1 Index coverage

| # | Severity | Finding | Impact | Location |
|---|---|---|---|---|
| P-01 | HIGH | `lists(pid, type)` is missing as a composite index. Separate `KEY pid` and `KEY type` indexes exist, but queries filtering on both — which is what the agent does for every meds/allergies/problems lookup — fall back to index merge or scan. | 2–3 seconds added per patient on a non-trivial list. | `sql/database.sql:7709–7711` |
| P-02 | MEDIUM | `pnotes.body` (LONGTEXT) has no FULLTEXT index. "Is this medication mentioned in the last note?" requires sequential scan of the text column. | 1–2 seconds per query for a patient with many notes. | `sql/database.sql:8670–8689` |
| P-03 | LOW | Large LEFT JOINs in `FhirPatientService` resolve without pagination. Survives slow lane; a risk on the fast lane only for patients with very high encounter counts. | Variable. | `src/Services/FHIR/FhirPatientService.php:1–200` |

### 4.2 Caching

| # | Severity | Finding | Impact | Location |
|---|---|---|---|---|
| P-04 | MEDIUM | **No application-level cache.** Grep for `redis`, `memcache`, `apcu` in `src/` and `library/` returns zero production cache integrations. Every FHIR call generates fresh DB queries. | Without the agent service's own cache, fast lane is impossible at any concurrency. | (none) |

### 4.3 N+1 risk

| # | Severity | Finding | Impact | Location |
|---|---|---|---|---|
| P-05 | LOW–MEDIUM | Legacy `interface/patient_file/summary/` uses procedural `sqlQuery`/`sqlStatement` calls inside per-row formatters. The agent does not call into these paths, so this is contextual rather than directly impacting. | None for the agent. | `interface/patient_file/summary/*` |

### 4.4 Implications for the agent

1. **The agent service's in-memory + TTL cache is non-optional.** The audit confirms PRD §8: without our own cache, fast lane (<5s) is not achievable. Cache scope: per-patient FHIR responses (meds, allergies, labs, problems, last-N visits) with TTL that the slow-lane discrepancy pass invalidates.
2. **The slow-lane discrepancy pass is the fast-lane warmer.** Pre-computing flags during the 7:35–8:55 AM window is what makes the in-room <5s budget achievable. This is the architecture's core latency move and the audit confirms there is no alternative.
3. **Composite index `lists(pid, type)`** is a one-line schema addition the agent module's `sql/` migration should include. Pure win, no risk.
4. **No FULLTEXT on `pnotes.body` is acceptable for MVP** — the agent only reads the most recent 1–3 notes per patient, where sequential scan cost is bounded. If the eval shows note search is a fast-lane bottleneck, FULLTEXT is the fix.

---

## 5. Compliance & Regulatory Audit

The case study calls out: "audit logging requirements, data retention policies, breach notification obligations, and BAA implications of sending PHI to an LLM provider."

### 5.1 Existing audit log infrastructure

OpenEMR has a mature audit system. The relevant pieces:

| Component | Purpose | Location |
|---|---|---|
| `log` table | Login, form actions, security events. Columns: `id, date, event, category, user, groupname, comments, patient_id, success, crt_user, log_from, menu_item_id, ccda_doc_id` | `sql/database.sql` |
| `log_comment_encrypt` | Tamper-detection metadata: SHA3-512 checksum, encryption flag | `sql/database.sql` |
| `api_log` | HTTP request/response audit. Columns: `log_id, user_id, patient_id, ip_address, method, request, request_url, request_body, response, created_time` | `sql/database.sql` |
| `EventAuditLogger` | Audit orchestrator; `newEvent()` is the call site | `src/Common/Logging/EventAuditLogger.php` |
| `LogTablesSink` | Writes to the audit tables | `src/Common/Logging/Audit/LogTablesSink.php:650–651` |
| `ADODB_mysqli_log` | SQL-level interception — every `Execute()` call can be logged | `library/ADODB_mysqli_log.php:50` |
| `logHttpRequest()` | HTTP-level interception | called from `interface/globals.php:20–21` |

This is good infrastructure. It's not, however, sufficient for BAA-grade traceability of PHI sent to an LLM (see §5.3).

### 5.2 HIPAA gaps in the existing system

| # | Severity | Finding | Location |
|---|---|---|---|
| C-01 | CRITICAL | **SELECT logging disabled by default.** `EventAuditLogger.php:425` shows `queryEvents: false` as default. PHI **reads** may not be logged unless this is explicitly enabled. HIPAA's audit log expectation is read-and-write. | `src/Common/Logging/EventAuditLogger.php:425` |
| C-02 | CRITICAL | **`AuditConfig` allows audit logging to be disabled entirely** at runtime. There's no enforcement that audit must be on. | `src/Common/Logging/AuditConfig.php:22–28` |
| C-03 | HIGH | **`setup.php` is not auto-locked post-install.** `$allow_multisite_setup` and `$allow_cloning_setup` default to false in code, but the file remains web-accessible. An attacker with FS write can re-run setup and reset the database. | `setup.php:49–57` |
| C-04 | HIGH | **No application-level idle session timeout.** Inherited from S-06. HIPAA expects ~15 min. | (no enforcement) |
| C-05 | HIGH | **Patient IDs in GET parameters** across legacy `interface/patient_file/` pages. Mitigated by HTTPS in transit but written to web server access logs. | `interface/patient_file/reminder/clinical_reminders.php`, `pat_ledger.php` |
| C-06 | HIGH | **PHI may appear in error logs.** `ErrorHandler.php:161` writes full exception data to the PSR-3 logger. If exception messages carry patient values, those land in plaintext logs. | `src/Core/ErrorHandler.php:161` |
| C-07 | MEDIUM | **API `request_body` and `response` stored unencrypted by default.** Encrypted only when `enable_auditlog_encryption = true`. Otherwise full PHI payloads are stored plaintext in `api_log`. | `src/Common/Logging/Audit/LogTablesSink.php:650–651` |
| C-08 | MEDIUM | **No HSTS or HTTP→HTTPS redirect at the application layer.** Delegated to web server config. | (no app-level enforcement) |
| C-09 | MEDIUM | **Soft-delete is the exception, not the rule.** Most tables use hard DELETE. No retention or breach-investigation trail for deleted records. | `sql/database.sql` (most tables) |

### 5.3 What the agent integration must add

The existing audit log was built for OpenEMR's own UI/API access patterns. It does not carry the fields required to demonstrate "PHI was sent to Anthropic under BAA, for a legitimate clinical purpose, scoped to the minimum necessary fields." Agent audit must capture:

| Field | Why |
|---|---|
| `user_id` | Already in `api_log`; required for "who" |
| `patient_id` | Already in `api_log`; required for "whose PHI" |
| `tool_name` | Which agent tool fired (e.g., `get_meds`, `get_flags`). Required for "what was the clinical intent" |
| `fields_requested` | Schema of what was sent (e.g., `["medication_name", "start_date"]`) — **not the values**. Required to demonstrate minimum-necessary. |
| `prompt_hash` | SHA-256 of the prompt sent to Anthropic. Auditor can verify a legitimate clinical query was made without storing full prompts (which carry adjacent PHI). |
| `model` | Which LLM was called (Sonnet/Haiku tier). Required for cost + provider attribution. |
| `created_at` | Already in `api_log` |
| `baa_vendor` | Hardcoded to `Anthropic` for this integration; would change per vendor. |

### 5.4 Decision: separate Postgres `agent_audit_log` table vs. extending `api_log`

The PRD §8 calls for the agent's audit log to live in a separate Postgres table. The audit confirms this is the correct call:

| Reason | Why it points to a separate table |
|---|---|
| **Schema independence** | OpenEMR's `api_log` schema is owned upstream. Adding `tool_name`, `fields_requested`, `prompt_hash` would require either forking OpenEMR's schema (rebases will fight us) or storing them in the existing `comments` column as serialized blobs (which defeats indexing for compliance queries). |
| **Retention separation** | Agent-LLM audit may need different retention than general API audit (e.g., 7 years for HIPAA-relevant audit, vs. shorter for general API logs). Mixing retention policies in one table is operationally fragile. |
| **Containment posture** | If the OpenEMR DB is breached, agent audit on a segregated Postgres instance survives independently. HIPAA's incident-containment posture rewards this separation. |
| **Tamper-evidence is independent** | OpenEMR's audit has its own tamper-evidence mechanism (`log_comment_encrypt` SHA3-512). The agent's audit will have its own (signed-row chain or append-only constraint). Mixing both into one table conflates two independent integrity stories. |
| **Indexing for compliance queries** | "Which clinicians sent what fields to which model for which patients in 2026-Q2" is a query the agent audit log must answer. That requires `tool_name` and `model` as first-class indexed columns — easier in a fresh table than retrofitting `api_log`. |

Decision: **separate Postgres `agent_audit_log`, write-through on every tool call that touches PHI, append-only schema with a row-chain hash for tamper-evidence.** This matches PRD §8 and §10's "agent metadata DB unreachable → critical audit-log writes block the request" failure-mode rule.

### 5.5 BAA / LLM provider considerations

To satisfy 45 CFR §164.504(e) (BAA) for sending PHI to Anthropic, the audit trail must demonstrate, on demand:

1. **Who** — `user_id` + `groupname`
2. **Whose PHI** — `patient_id`
3. **What** — `fields_requested` (schema, not values)
4. **Why** — `tool_name` + `prompt_hash`
5. **Minimum-necessary** — proof from `fields_requested` that only required fields were sent
6. **Encryption-in-transit** — TLS 1.3 to Anthropic (enforced by Anthropic's endpoint; record success/failure in audit row)
7. **Retention + disposal** — defined retention window per deployment; Anthropic's BAA commits they don't train on inputs

`prompt_hash` is the key clever piece: an auditor can verify the LLM was used legitimately without the system needing to store full prompts (which would contain PHI of the patient, plus potentially adjacent context). If a breach is suspected, the hash chain can be reconstructed against a separately-secured copy of actual prompts in a vault.

### 5.6 Compliance changes required pre-production (out of MVP)

Captured here so they are documented, not ignored:

- Move from Railway (no BAA) to a HIPAA-eligible operator (AWS+BAA, GCP+BAA, Aptible, Datica). The architecture ports cleanly; the operator changes. PRD §9 already flags this.
- Enforce SELECT logging on production OpenEMR (`queryEvents: true`).
- Enforce idle session timeout at the application layer.
- Lock or delete `setup.php` post-install.
- Move PHI-bearing GET parameters to POST or URL path (long-term project).
- Sanitize PHI from exception logs.
- Enable `enable_auditlog_encryption`.
- Enforce HSTS at the application or proxy layer.

---

## 6. What This Audit Changed in the Architecture Plan

This section closes the case-study interview question: *"How did the audit change your AI integration plan?"*

| PRD assumption | Audit verdict | Architectural impact |
|---|---|---|
| **#1** — OpenEMR FHIR API covers use cases 1–4 | Confirmed for meds, allergies, labs, problems, visits, vitals, immunizations. **Partial gap** for clinical narrative notes (DocumentReference is metadata-only). | Add one custom PHP gateway endpoint for narrative notes via the `EncounterService` layer. |
| **#2** — FHIR/REST handlers enforce ACL consistently | **Confirmed.** All FHIR R4 routes re-check ACL or filter by patient context. | The agent service can call FHIR endpoints directly with a properly-scoped OAuth2 client. No need to route every call through a custom gateway. |
| **#3** — OpenEMR session can be safely mapped to short-lived signed claims | **Mostly confirmed**, with one finding: session regeneration at login is not explicit (S-04). | Use OAuth2 client-credentials for the agent's identity (not session-derived tokens). The HMAC-signed JWT in PRD §6 is for the PHP-gateway → Python-agent boundary; the OpenEMR session itself is not the trust source for the agent. |
| **#4** — Discrepancy invalidation hooks reachable in write paths | The Symfony event system is in place (`patientSummaryCard.render` proves the hook pattern works). Specific write-path events for invalidation are TBD; fall-back is TTL. | TTL + event-listener invalidation hybrid as PRD §5 already specifies. |
| **#5** — Sample data is rich enough for use case 3 | **Killed.** Demo data has no clinical content. | **Add a seeded discrepancy fixture (`sql/example_discrepancy_data.sql`) to the MVP critical path.** This is now the most concrete non-architecture build task. |
| **#6** — Smarty injection points allow side-panel partials without forking core | **Better than expected** — the `patientSummaryCard.render` Symfony event is purpose-built for this. | Side panel is a Symfony event listener in the custom module, not a template fork. Cleaner than originally planned. |
| **#7** — Railway cold starts don't blow fast-lane budget | Not yet measured (out of audit scope). | Plan unchanged — keep agent service on always-on tier or warming heartbeat. |
| **#8** — OpenEMR data model carries the safety flags we need | **Partial** — coding is free-text + optional coded references. The discrepancy engine must normalize text and tolerate the gap. | Discrepancy rules narrow to record-consistency + value-sanity + allergy-vs-med matching where coded references exist. Specialty-specific guideline checking remains out of scope. |

**Net delta to the build plan:** add one custom note endpoint, add one seeded fixture file, add one composite index. Architecture is otherwise unchanged.

---

## 7. Open Verification Items (Pre-Defense)

Items the audit identified as plausible but not exhaustively verified line-by-line. Listed here so they get verified before the architecture defense:

1. **SELECT logging default** — confirm `queryEvents: false` is the actual default by reading `EventAuditLogger.php:425` directly. (C-01)
2. **OAuth2 client registration UX** — walk through the admin UI flow once and document the steps in the build runbook. (S-09)
3. **Demo data row count** — verify `example_patient_data.sql` line count and confirm it loads zero clinical rows. (use case 3 hard gate)
4. **`patientSummaryCard.render` event firing** — load the demographics tab in the running instance and confirm a no-op Symfony listener gets invoked. (A-02 mitigation)
5. **FHIR coverage smoke test** — hit `/fhir/Patient`, `/fhir/Condition?patient=...`, `/fhir/MedicationRequest?patient=...` against the running instance and confirm 200s with expected payload shapes.
6. **Pre-warm trigger events** — confirm whether OpenEMR fires reachable Symfony events for (a) login completion and (b) schedule/calendar load, so the slow-lane discrepancy pass can fire on these triggers per ARCH §2 trigger/consumption split (the architectural answer to "what if the clinician has zero prep time?"). If neither event is reachable, fall back to a pre-clinic cron job only — no architectural change required, but the trigger story narrows.

These are not blockers; they're the items where the audit produced a high-confidence inference but the running instance can give a one-line confirmation.

---

## 8. Document Provenance

| Field | Value |
|---|---|
| Method | Codebase walk + running local instance + four parallel exploration passes (security, architecture, compliance, performance/data-quality) |
| Scope | OpenEMR fork at `./openemr/`. Agent integration not yet built; this audit informs the build plan in `./ARCHITECTURE.md`. |
| Inputs | `./PRD.md` (v3) for assumptions to test; `./USERS.md` for user-shape constraints; case-study spec for required dimensions |
| Drives | `./ARCHITECTURE.md` (assumptions confirmed/killed); MVP critical path (seeded discrepancy fixture, custom note endpoint, composite index migration) |
| Last updated | 2026-04-27 |
