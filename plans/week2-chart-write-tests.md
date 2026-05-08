# Plan — Week 2 Chart-Write Tests (W2-CW Sunday-blocking)

## Context

`TASKS2.md` Open recovery item #3 ("Tests for the chart-write path")
is Sunday-blocking per CLAUDE.md test policy: tests ship in the same MR
as the code they cover. The chart-write code already landed
(`save_document.php:199–238` → `ChartWriteService` → `lists` /
`dated_reminders` / `procedure_*`). What's missing:

1. Service-level tests proving the SQL contracts hold.
2. Orchestration tests proving the section-checkbox dispatch is correct.
3. A written idempotency position in `SUBMISSIONW2.md`.

Parallel-safe: a separate Claude session is on the LangGraph (W2-07)
work in `agent-service/src/clinical_copilot/orchestrator/`. This plan
touches PHP-side files only (`src/Services/Copilot/ChartWrite/`,
`interface/copilot/api/save_document.php`, `tests/Tests/`,
`SUBMISSIONW2.md`). Zero file collision.

## Locked decisions

1. **`ChartWriteService` tests live at services-tier, not isolated.**
   The service calls `QueryUtils::sqlInsert()` directly — there is no
   DB abstraction injected, and the repo has no static-method mocking
   infrastructure. The load-bearing contract is *what rows land in
   which tables with which columns*, which can only be verified
   against a real MariaDB. New file:
   `tests/Tests/Services/Copilot/ChartWrite/ChartWriteServiceTest.php`.
2. **`ChartWriteSummary` gets an isolated test.** Pure value object,
   no DB. Quick win; gives the new
   `tests/Tests/Isolated/Services/Copilot/ChartWrite/` directory
   something native. New file:
   `tests/Tests/Isolated/Services/Copilot/ChartWrite/ChartWriteSummaryTest.php`.
3. **Orchestration test via mechanical closure extraction.** The
   `$runChartWrites` closure at `save_document.php:206–250` becomes a
   one-method `ChartWriteOrchestrator` class. Isolated test stubs
   `ChartWriteService` and verifies which writers get called for which
   `$checkedSections`. New files:
   `src/Services/Copilot/ChartWrite/ChartWriteOrchestrator.php` +
   `tests/Tests/Isolated/Services/Copilot/ChartWrite/ChartWriteOrchestratorTest.php`.
4. **Refactor boundary in `save_document.php`:** delete the closure
   declaration (lines 206–250), replace the two call sites
   (`$runChartWrites($targetPid, ...)` and the create-new branch's
   equivalent) with `(new ChartWriteOrchestrator(new ChartWriteService($authUserId)))->run(...)`.
   No other edits to `save_document.php`. Line-for-line equivalent
   behavior; PHPStan-level-10 + the new orchestrator test catch drift.
5. **Idempotency position in `SUBMISSIONW2.md`:** state current
   no-dedupe behavior, give the clinician-judgment rationale (lifted
   from `ChartWriteService.php:17-21`), and acknowledge accidental
   double-submit as a production-hardening gap (per-document-id
   idempotency markers).
6. **No FHIR Bundle export note** in this MR — that's a separate
   open item (#2 from the parallel-work list); keep this MR scoped to
   the test gate.

## Files

### NEW
- `src/Services/Copilot/ChartWrite/ChartWriteOrchestrator.php`
  — single-method dispatcher lifted from `save_document.php` closure.
- `tests/Tests/Services/Copilot/ChartWrite/ChartWriteServiceTest.php`
  — Docker/MariaDB-required service tests.
- `tests/Tests/Isolated/Services/Copilot/ChartWrite/ChartWriteSummaryTest.php`
  — host-runnable, value-object coverage.
- `tests/Tests/Isolated/Services/Copilot/ChartWrite/ChartWriteOrchestratorTest.php`
  — host-runnable, dispatch-logic coverage with stubbed service.

### EDIT
- `interface/copilot/api/save_document.php` — closure → orchestrator
  call. ~45 lines deleted, ~3 lines added at the two call sites.
- `SUBMISSIONW2.md` — add idempotency-position paragraph in the
  chart-write section.

## Test plan

### `ChartWriteServiceTest` (services-tier, requires Docker)

Each test inserts then asserts on raw rows; `tearDown` deletes by `pid`.

- `writeAllergies` — inserts one `lists` row per entry, type='allergy',
  comments format `<reaction> (<severity>)`, skips empty-substance entries.
- `writeMedications` — title format `<name> <dose> <freq>`, diagnosis
  column = `RXCUI:<n>` when rxnorm present (empty string when not),
  begdate respects `started_year` when given.
- `writeActiveProblems` — diagnosis column = `ICD10:<code>`,
  `;SNOMED-CT:<code>` appended when both, empty when neither.
- `writeReminders` — message ≤160 chars (truncates with ellipsis at 159),
  OVERDUE → priority 1, anything else → priority 2, default due_date =
  today when missing, `dr_from_ID` = author user id.
- `writeLabObservations` — full chain: 1 `procedure_order` row, 1
  `procedure_order_code` (LOINC fallback to `COPILOT-IMPORT`), 1
  `procedure_report`, N `procedure_result` rows; abnormal flag mapping
  (H/HH→high, L/LL→low, A→abnormal, default→empty).
- `writeAllergies` with `pid <= 0` → returns 0, no rows inserted.
- **Idempotency lock-in:** `writeAllergies` called twice with the same
  input → 2× rows in `lists`. Test name makes the intent explicit:
  `testWriteAllergiesDoesNotDedupeOnRepeatCall`. The `SUBMISSIONW2.md`
  paragraph references this test as the documented behavior.

### `ChartWriteSummaryTest` (isolated, no DB)

- `testRecordAccumulatesPerSection` — `record('allergies', 2)` then
  `record('allergies', 1)` → counts['allergies'] === 3.
- `testIsEmptyTrueForNoRecords`.
- `testIsEmptyFalseAfterRecord`.
- `testTotalRowsWrittenSumsAllSections`.
- `testSkipAppendsToSkippedList`.

### `ChartWriteOrchestratorTest` (isolated, stubs ChartWriteService)

Strategy: subclass `ChartWriteService` in the test file with a public
spy that captures `(method, pid, rowCount)` per call and returns a fixed
count. Verify dispatch:

- `testRunDispatchesOnlyCheckedSections` — checked = `['allergies', 'medications']`
  → spy saw `writeAllergies` + `writeMedications`, did *not* see
  `writeActiveProblems` / `writeReminders` / `writeLabObservations`.
- `testRunWithEmptyChecklistWritesNothing` — summary.isEmpty() true.
- `testRunPassesPidThroughToEachWriter`.
- `testRunRecordsCountsIntoSummary` — summary.counts() reflects what
  the spy returned.
- `testRunDispatchesLabObservationsWithPanelMetadata` — verifies the
  `FactsExtractor::labObservations` payload threading
  (panel_name/loinc/report_date/observations).

## Idempotency writeup (SUBMISSIONW2.md)

Add a short paragraph in the chart-write section, ~5 sentences:

> **Chart-write idempotency.** The current implementation does not
> dedupe writes against existing chart rows. This is intentional for
> the clinician-confirmed flow: the review surface is the right place
> for the clinician to decide whether a duplicate medication or allergy
> represents real new information vs. a re-import of an existing
> record. However, accidental double-submit (clinician double-clicks
> Save, or a network blip causes the form to resubmit) *will* duplicate
> rows under the current code — see
> `tests/Tests/Services/Copilot/ChartWrite/ChartWriteServiceTest::testWriteAllergiesDoesNotDedupeOnRepeatCall`
> which locks this behavior. Production hardening would add a
> per-`document_id` idempotency marker on the
> `documents` row (or a dedicated `chart_write_audit` table) so a
> repeat POST with the same `document_id` becomes a no-op. This is
> tracked as a post-Sunday item; out of scope for the submission MR.

## Execution order

1. Land plan file (this file). ← review gate
2. Write `ChartWriteSummaryTest.php` — fastest, host-runnable, validates
   the new test directory layout.
3. Write `ChartWriteOrchestrator.php` (extraction).
4. Edit `save_document.php` — closure → orchestrator call.
5. Write `ChartWriteOrchestratorTest.php`.
6. Run host-side checks: `composer phpunit-isolated`, `composer phpstan`
   on changed files.
7. Confirm with user before bringing up Docker stack.
8. Write `ChartWriteServiceTest.php`.
9. Run `docker compose exec openemr /root/devtools services-test` —
   filter to the new test class.
10. `SUBMISSIONW2.md` paragraph.
11. `composer code-quality` end-to-end before commit.

## Which tests require Docker

| Test file | Tier | Docker? |
|---|---|---|
| `ChartWriteSummaryTest` | isolated | No |
| `ChartWriteOrchestratorTest` | isolated | No |
| `ChartWriteServiceTest` | services | **Yes** (MariaDB via `development-easy`) |

User approval required before `docker compose up` (per memory:
"don't surprise-start Docker").

## Risk flags

- **`save_document.php` is on the chart-write submission path.** The
  refactor is mechanical (closure → class with same method body), but
  the file has CSRF + ACL + agent-HTTP plumbing around it. Mitigation:
  delete-and-replace only the closure block; do not touch surrounding
  code. PHPStan + new orchestrator unit tests catch drift.
- **Services-tier tests pollute the test DB.** `tearDown` deletes by
  `pid` from `lists`, `dated_reminders`, `procedure_order`,
  `procedure_order_code`, `procedure_report`, `procedure_result`.
  Use a synthetic `pid = 999999` (well above any fixture) to avoid
  collision.
- **No collision with the LangGraph thread.** They are in
  `agent-service/src/clinical_copilot/orchestrator/`; this plan is
  PHP-side only.

## Out of scope

- FHIR Bundle export position (open item #2 — separate small docs MR).
- Extracted-facts durability decision (open item #4 — separate writeup).
- Demo script polish (open item #5 — separate plan).
- Refactoring `ChartWriteService` to inject a DB abstraction (would
  unlock pure-isolated tests but is a much bigger change and out of
  scope for the test-gate MR).
- Idempotency *implementation* (per-document-id marker) — documented
  as production-hardening gap, deferred to post-Sunday MR queue.
