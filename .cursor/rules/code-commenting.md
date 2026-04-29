# Code Commenting Standards

## Core Principle

Comments exist to explain **why**, not **what**. The code already shows what it does — comments reveal the intent, trade-offs, constraints, and domain knowledge that the code cannot express on its own.

---

## When to Add a Comment

### Always Comment

| Situation | Example |
|-----------|---------|
| Non-obvious business logic or clinical rule | `// CMS requires a 30-day gap between wellness visits` |
| Security-sensitive sections | `// Input is sanitized here before SQL interpolation — never bypass` |
| Workarounds for external system quirks | `// HL7 sender omits the trailing segment delimiter; add it manually` |
| Performance trade-offs | `// Skip eager-loading: most callers only need the summary row` |
| Subtle ordering dependencies | `// Encounter must be saved before diagnoses — FK constraint` |
| Intentionally skipped cases | `// Deceased patients are excluded by the query; no runtime check needed` |
| Complex regex or bit-field operations | explain what the pattern captures or what each bit represents |
| Public API surface (classes, methods, constants) | PHPDoc / JSDoc with `@param`, `@return`, `@throws` |

### Never Comment

- Lines that literally restate the code (`$count++; // increment count`)
- Scaffolding narration (`// Define the function`, `// Import module`)
- Commented-out dead code — delete it; version control preserves history
- TODO/FIXME without a ticket reference — use `// TODO(#1234): ...` instead

---

## Comment Format by Language

### PHP

**File-level docblock** — required on every new or modified file:

```php
/**
 * Brief one-line description of the file's purpose.
 *
 * @package OpenEMR
 * @link    https://www.open-emr.org
 * @author  Name <email@example.com>
 * @copyright Copyright (c) YEAR Name or Organization
 * @license https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */
```

**Class docblock** — required for all classes in `/src/`:

```php
/**
 * Manages scheduling rules for recurring clinical appointments.
 *
 * Encapsulates the CMS gap-checking logic so it is not scattered
 * across controllers and templates.
 */
final class AppointmentRuleEngine { ... }
```

**Method docblock** — required for public/protected methods; optional for private methods when the signature is self-explanatory:

```php
/**
 * Returns the next eligible appointment date for a patient.
 *
 * Enforces the 30-day CMS gap rule and skips facility-closed days.
 * Returns null when no eligible slot exists within the look-ahead window.
 *
 * @param PatientId            $patientId   The patient to schedule.
 * @param \DateTimeImmutable   $after       Earliest acceptable date (exclusive).
 * @param int                  $windowDays  How many days forward to search.
 * @return \DateTimeImmutable|null
 * @throws SchedulingException  When the facility calendar cannot be loaded.
 */
public function nextEligibleDate(PatientId $patientId, \DateTimeImmutable $after, int $windowDays): ?\DateTimeImmutable
```

**Inline comments** — use `//` for single lines, `/* */` only when spanning multiple lines is genuinely clearer:

```php
// OpenEMR stores vitals in metric internally; convert only at the boundary.
$weightKg = $weightLbs * 0.453592;
```

### Python (agent-service)

**Module docstring** — at the top of every module:

```python
"""
Health-check endpoint for the clinical copilot agent service.

Exposes a /health route that returns service status and build metadata.
"""
```

**Class and function docstrings** — Google-style for consistency with the existing service:

```python
def next_eligible_date(patient_id: int, after: date, window_days: int) -> date | None:
    """Return the next CMS-compliant appointment date.

    Enforces a 30-day gap between wellness visits and skips facility-closed
    days. Returns None when no eligible slot exists within the look-ahead window.

    Args:
        patient_id: Internal OpenEMR patient identifier.
        after: Earliest acceptable date (exclusive).
        window_days: How many calendar days forward to search.

    Returns:
        The next eligible date, or None if none found.

    Raises:
        SchedulingError: When the facility calendar cannot be loaded.
    """
```

**Inline comments** — same rule as PHP: explain why, not what:

```python
# The HL7 v2 sender drops the trailing carriage-return; re-add it
# before forwarding to the downstream parser or it rejects the message.
segment += "\r"
```

### JavaScript / TypeScript

**File-level comment** — one line at the top for non-trivial modules:

```js
/** Scheduling grid component — renders availability slots using FullCalendar. */
```

**JSDoc for exported functions and classes:**

```js
/**
 * Calculates the earliest slot that satisfies the CMS 30-day gap rule.
 *
 * @param {number} patientId - OpenEMR patient identifier.
 * @param {Date}   after     - Exclusive lower bound for the search.
 * @returns {Date | null} First eligible date, or null if none found.
 */
export function nextEligibleDate(patientId, after) { ... }
```

**Inline comments** — follow the same why-not-what rule:

```js
// Angular digest is already running here; $apply would throw "already in progress".
$timeout(() => { ... }, 0);
```

### SQL (migrations and schema files)

Comment every non-trivial constraint, index, or trigger:

```sql
-- Partial index: the vast majority of queries filter on active patients only.
-- Including inactive rows would double index size for no read benefit.
CREATE INDEX idx_patient_active ON patient_data (pid) WHERE deleted = 0;
```

### Twig / HTML templates

Use Twig comments (`{# ... #}`) for template-level notes; they are not rendered in output:

```twig
{# Renders only when the user holds the "manage_schedule" ACL.
   The surrounding <div> must stay even when hidden — CSS transitions depend on it. #}
```

---

## Documenting Important Functionalities

When implementing a feature or subsystem that carries significant clinical, security, or architectural weight, add a **section-level comment block** immediately above the relevant class or function group:

```php
// ─── CLINICAL RULE: CMS Chronic Care Management ──────────────────────────
// Patients qualify when they have ≥2 chronic conditions documented in the
// last 12 months AND have consented to CCM enrollment.
// Reference: CMS MLN Fact Sheet ICN 909188 (Rev. Nov 2023).
// Changing qualification logic here requires a parallel update to the
// billing code mapping in BillingCodeMapper::mapCcmCodes().
// ─────────────────────────────────────────────────────────────────────────
```

Use this pattern for:
- **Authorization checkpoints** — explain what is being protected and why the check is placed here
- **Data-integrity invariants** — explain what constraint is enforced and where it is documented
- **Integration boundaries** — explain the external system, protocol version, and any known quirks
- **Performance-critical paths** — explain what was profiled, what was tried, and what the measured gain is
- **Regulatory or compliance requirements** — cite the specific rule, standard, or ticket

---

## PHPDoc Typing

PHPDoc types supplement native PHP types when native types cannot express the full shape. Follow the array-typing progression from most to least precise:

```php
// Preferred: array shape for small, stable structures
/** @param array{pid: int, fname: string, lname: string} $patient */

// For lists of a single type
/** @return list<EncounterDto> */

// For typed key-value maps
/** @return array<string, int> */

// Bare array — only as a last resort on legacy code
/** @return array */
```

---

## Checklist Before Committing

- [ ] Every public class and public/protected method has a docblock
- [ ] Non-obvious logic blocks have an inline `//` comment explaining **why**
- [ ] Workarounds cite the root cause (upstream bug, external system quirk, etc.)
- [ ] Regulatory or clinical rules cite the source document or ticket number
- [ ] No comments that merely restate the code
- [ ] No orphaned commented-out code blocks
- [ ] TODOs include a ticket reference: `// TODO(#1234): ...`
