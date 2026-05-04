<?php

/**
 * Seed two patients with discrepancy data so the daily-brief shows
 * Co-Pilot flags during a demo.
 *
 * Picks the first two pids assigned to the named clinician (default
 * ``admin``) and writes:
 *
 *   - Patient 1: an active metoprolol row in ``lists`` plus a recent
 *     pnote that says "discontinued metoprolol" → triggers
 *     ``med_vs_note_conflict`` (consistency rule pack).
 *   - Patient 2: a recent pnote that says "patient reports sulfa
 *     allergy" with no row in ``lists.type='allergy'`` →
 *     triggers ``narrative_only_allergy`` (consistency).
 *
 * Both shapes mirror the M5 fixture scenarios in
 * ``tests/Tests/Fixtures/discrepancy-scenarios.php`` — the discrepancy
 * engine reads them through FHIR (MedicationRequest + DocumentReference)
 * via the same projection the agent uses for its own tools.
 *
 * Idempotent: re-running skips per-patient seeding when the marker
 * note title is already present. Drop the rows by hand to re-seed.
 *
 * Usage::
 *
 *   php scripts/copilot/seed_demo_flags.php [--clinician=admin] \
 *       [--pid-med-conflict=N] [--pid-narrative-allergy=M]
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

if (PHP_SAPI !== 'cli') {
    fwrite(STDERR, "This script must be run from the CLI.\n");
    exit(1);
}

// @phpstan-ignore openemr.forbiddenRequestGlobals (CLI bootstrap)
$_GET['site'] = 'default';
$ignoreAuth = true;
require_once __DIR__ . '/../../interface/globals.php';
// ClinicalNotesService::createClinicalNotesParentForm calls the global
// ``addForm()`` which lives in library/forms.inc.php. interface/globals
// does not auto-include it for CLI bootstraps, so wire it in here.
require_once __DIR__ . '/../../library/forms.inc.php';

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Services\ClinicalNotesService;

$opts = getopt('', ['clinician::', 'pid-med-conflict::', 'pid-narrative-allergy::']);
if ($opts === false) {
    fwrite(STDERR, "Failed to parse arguments.\n");
    exit(2);
}

$pidFromOpt = static function (string $name) use ($opts): ?int {
    if (!isset($opts[$name])) {
        return null;
    }
    $raw = $opts[$name];
    if (!is_string($raw) || !ctype_digit($raw)) {
        fwrite(STDERR, "--$name must be a positive integer.\n");
        exit(2);
    }
    return (int) $raw;
};

$asInt = static function (mixed $value): ?int {
    if (is_int($value)) {
        return $value;
    }
    if (is_string($value) && ctype_digit($value)) {
        return (int) $value;
    }
    return null;
};

$todayMinusDays = (static fn(int $days): string => (new DateTimeImmutable('today'))
    ->sub(new DateInterval("P{$days}D"))
    ->format('Y-m-d'));

$clinicianUsername = isset($opts['clinician']) && is_string($opts['clinician']) && $opts['clinician'] !== ''
    ? $opts['clinician']
    : 'admin';

$clinicianRow = QueryUtils::querySingleRow(
    'SELECT id, username FROM users WHERE username = ? AND active = 1 LIMIT 1',
    [$clinicianUsername],
);
if (!is_array($clinicianRow) || !isset($clinicianRow['id']) || !is_numeric($clinicianRow['id'])) {
    fwrite(STDERR, "Clinician '$clinicianUsername' not found or inactive.\n");
    exit(1);
}
$clinicianId = (int) $clinicianRow['id'];

// Resolve the two pids: explicit flag wins, otherwise pick the first
// two assigned patients ordered by pid (same order the daily-brief
// renders, so the flags land on the top-left two cards).
$pidMed = $pidFromOpt('pid-med-conflict');
$pidAllergy = $pidFromOpt('pid-narrative-allergy');
if ($pidMed === null || $pidAllergy === null) {
    $defaults = QueryUtils::fetchTableColumn(
        'SELECT pid FROM patient_data WHERE providerID = ? ORDER BY pid LIMIT 2',
        'pid',
        [$clinicianId],
    );
    if (count($defaults) < 2) {
        fwrite(STDERR, "Clinician '$clinicianUsername' needs at least 2 assigned patients before seeding flags.\n");
        exit(1);
    }
    $pidMed ??= $asInt($defaults[0]);
    $pidAllergy ??= $asInt($defaults[1]);
}
if ($pidMed === null || $pidAllergy === null) {
    fwrite(STDERR, "Could not resolve patient ids.\n");
    exit(1);
}
if ($pidMed === $pidAllergy) {
    fwrite(STDERR, "--pid-med-conflict and --pid-narrative-allergy must be different patients.\n");
    exit(2);
}

// Marker titles — used both as audit trail in the chart and as the
// idempotency check. Dropping the corresponding rows by hand will let
// the script re-seed cleanly.
$markerMedNote = 'DEMO: Office visit (discontinue metoprolol)';
$markerMedRow  = 'DEMO Metoprolol Tartrate 50mg';
$markerAllergyNote = 'DEMO: Intake note (sulfa allergy reported)';

// Seed a clinical note via OpenEMR's ``ClinicalNotesService`` so it lands
// in ``form_clinical_notes`` (the table the FHIR DocumentReference search
// reads from). Inserting straight into ``pnotes`` does NOT surface
// through FHIR — only ``form_clinical_notes`` and uploaded documents do.
$seedClinicalNote = static function (int $pid, string $title, string $body): bool {
    $existingForm = QueryUtils::fetchSingleValue(
        "SELECT 1 AS hit FROM form_clinical_notes WHERE pid = ? AND description LIKE ? LIMIT 1",
        'hit',
        [$pid, '%' . $title . '%'],
    );
    if ($existingForm !== null) {
        return false;
    }
    $encounterRaw = QueryUtils::fetchSingleValue(
        'SELECT encounter FROM form_encounter WHERE pid = ? ORDER BY date DESC LIMIT 1',
        'encounter',
        [$pid],
    );
    if (!is_numeric($encounterRaw)) {
        fwrite(STDERR, "pid=$pid: no encounter on record — cannot attach a clinical note.\n");
        exit(1);
    }
    $encounter = (int) $encounterRaw;
    $service = new ClinicalNotesService();
    // ``saveArray`` requires ``form_id`` populated up front — its
    // auto-create branch sits behind a guard that rejects empty
    // ``form_id``. Mint the parent form here and pass the id down.
    $formId = $service->createClinicalNotesParentForm($pid, $encounter, 1);
    $service->saveArray([
        'form_id' => $formId,
        'pid' => $pid,
        'encounter' => $encounter,
        'authorized' => 1,
        'activity' => 1,
        'date' => (new DateTimeImmutable('today'))->format('Y-m-d'),
        // Encode the marker title in the description prefix so the
        // idempotency check above can find it. The discrepancy engine
        // reads the full description text — keyword matches still fire.
        'description' => "[$title]\n" . $body,
        'clinical_notes_type' => 'progress_note',
    ]);
    return true;
};

$seedMedConflict = static function (int $pid) use ($markerMedRow, $markerMedNote, $todayMinusDays, $seedClinicalNote): void {
    $alreadyMed = QueryUtils::fetchSingleValue(
        "SELECT 1 AS hit FROM lists WHERE pid = ? AND title = ? AND type = 'medication' LIMIT 1",
        'hit',
        [$pid, $markerMedRow],
    );
    if ($alreadyMed === null) {
        // ``lists.uuid`` is nullable in the schema, but the FHIR
        // MedicationRequest projection rejects rows whose ``id`` is
        // empty (pydantic ``string_type``). Mint the uuid up front so
        // the discrepancy engine can read the row through FHIR.
        $uuid = (new UuidRegistry(['table_name' => 'lists']))->createUuid();
        QueryUtils::sqlStatementThrowException(
            'INSERT INTO lists (type, title, diagnosis, begdate, enddate, activity, comments, pid, date, uuid) '
            . "VALUES ('medication', ?, 'RXCUI:866924', ?, NULL, 1, 'For hypertension management', ?, NOW(), ?)",
            [$markerMedRow, $todayMinusDays(60), $pid, $uuid],
        );
    }
    $wrote = $seedClinicalNote(
        $pid,
        $markerMedNote,
        'Patient developed bradycardia. Discontinued metoprolol effective today; '
        . 'follow up in two weeks for BP recheck.',
    );
    if ($wrote) {
        echo "pid=$pid: seeded med_vs_note_conflict (active metoprolol + 'discontinued' note).\n";
    } else {
        echo "pid=$pid: med_vs_note_conflict already seeded — skipping.\n";
    }
};

$seedNarrativeAllergy = static function (int $pid) use ($markerAllergyNote, $seedClinicalNote): void {
    // The rule fires only when the structured allergy list lacks a
    // matching entry. Verify before inserting the note so a manually
    // backfilled allergy doesn't silently mask the demo.
    $hasAllergy = QueryUtils::fetchSingleValue(
        "SELECT 1 AS hit FROM lists WHERE pid = ? AND type = 'allergy' "
        . "AND LOWER(title) LIKE '%sulfa%' LIMIT 1",
        'hit',
        [$pid],
    );
    if ($hasAllergy !== null) {
        echo "pid=$pid: structured sulfa allergy already exists — skipping (rule will not fire).\n";
        return;
    }
    $wrote = $seedClinicalNote(
        $pid,
        $markerAllergyNote,
        'Patient reports a sulfa allergy — developed rash on Bactrim as a teen. '
        . 'No allergy listed in chart prior to this visit.',
    );
    if ($wrote) {
        echo "pid=$pid: seeded narrative_only_allergy (sulfa-allergy note, no structured row).\n";
    } else {
        echo "pid=$pid: narrative_only_allergy already seeded — skipping.\n";
    }
};

$seedMedConflict($pidMed);
$seedNarrativeAllergy($pidAllergy);

echo "Done. Open the daily brief as '$clinicianUsername' to see the flags.\n";
exit(0);
