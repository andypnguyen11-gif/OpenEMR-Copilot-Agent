<?php

/**
 * Clinical Co-Pilot — confirm-and-create handler for the new-patient
 * intake flow (PR W2-02).
 *
 * Creates a fresh OpenEMR patient row from the clinician-confirmed
 * intake review and seeds the lists table with active problems,
 * medications, and allergies in one transaction.
 *
 * Family-history write-back is documented in the plan as cuttable for
 * tonight; the family-history rows submitted from the review form are
 * intentionally not persisted yet (a follow-up MR adds the
 * ``history_data.family_*`` write).
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

require_once(__DIR__ . "/_site_recovery.php");
require_once(__DIR__ . "/../globals.php");

use OpenEMR\BC\ServiceContainer;
use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Database\SqlQueryException;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Services\Copilot\PatientMatch\PatientMatchScorer;
use OpenEMR\Services\Copilot\PatientMatch\PatientMatchService;

$globalsBag = OEGlobalsBag::getInstance();
$srcdirRaw = $globalsBag->get('srcdir', '');
$srcdir = is_string($srcdirRaw) ? $srcdirRaw : '';
require_once($srcdir . "/pid.inc.php");
require_once($srcdir . "/patient.inc.php");
require_once($srcdir . "/lists.inc.php");

if (filter_input(INPUT_SERVER, 'REQUEST_METHOD') !== 'POST') {
    http_response_code(405);
    exit('method not allowed');
}

if (!AclMain::aclCheckCore('admin', 'super')) {
    http_response_code(403);
    exit('forbidden');
}

CsrfUtils::checkCsrfInput(
    INPUT_POST,
    session: SessionWrapperFactory::getInstance()->getActiveSession(),
    dieOnFail: true,
);

$readPost = static fn(string $key): string => (string) (filter_input(INPUT_POST, $key) ?? '');

/**
 * @return list<string>
 */
$readPostList = static function (string $key): array {
    $raw = filter_input(INPUT_POST, $key, FILTER_DEFAULT, FILTER_REQUIRE_ARRAY);
    if (!is_array($raw)) {
        return [];
    }
    $out = [];
    foreach ($raw as $value) {
        $out[] = is_string($value) ? $value : '';
    }
    return $out;
};

$documentId = $readPost('document_id');

$fname = ucwords(trim($readPost('fname')));
$lname = ucwords(trim($readPost('lname')));
$dob = trim($readPost('dob'));
$sex = trim($readPost('sex'));
$phone = trim($readPost('phone'));
$email = trim($readPost('email'));
$externalMrn = trim($readPost('external_mrn'));
$chiefComplaint = trim($readPost('chief_complaint'));
$tobaccoStatus = trim($readPost('tobacco_status'));
$tobaccoPackYears = trim($readPost('tobacco_pack_years'));

if ($fname === '' || $lname === '' || $dob === '' || $sex === '') {
    http_response_code(400);
    exit('first name, last name, date of birth, and sex are required');
}

// Duplicate-patient guard. Same logic as ``api/save_document.php``:
// if the (fname, lname, DOB) we're about to INSERT crosses the
// matcher's PRESELECT_THRESHOLD against an existing chart, refuse to
// create unless the clinician has confirmed via
// ``force_create_duplicate``. Re-emits every other POST field as a
// hidden input so the clinician's edits to problems / meds / etc.
// aren't lost when they confirm — POSTing back to this same handler
// with the override flag set runs the full insert path.
$forceCreateDupRaw = filter_input(INPUT_POST, 'force_create_duplicate');
$forceCreateDup = is_string($forceCreateDupRaw) && $forceCreateDupRaw === '1';
if (!$forceCreateDup) {
    $matchService = new PatientMatchService();
    $existingMatches = $matchService->match(
        $fname,
        $lname,
        $dob,
        $externalMrn !== '' ? $externalMrn : null,
    );
    $blockingMatches = array_values(array_filter(
        $existingMatches,
        static fn ($candidate): bool => PatientMatchScorer::shouldPreselect($candidate->score),
    ));
    if ($blockingMatches !== []) {
        $webrootRaw = $globalsBag->get('webroot', '');
        $webroot = is_string($webrootRaw) ? $webrootRaw : '';

        /**
         * Recursively walk the POST array and emit one ``<input type="hidden">``
         * per leaf, preserving the bracket-name structure (so ``problem_save[3]``
         * round-trips correctly on resubmit).
         *
         * @param array<int|string, mixed> $data
         */
        $emitHiddenInputs = static function (array $data, string $prefix = '') use (&$emitHiddenInputs): string {
            $out = '';
            foreach ($data as $key => $value) {
                $name = $prefix === ''
                    ? (string) $key
                    : $prefix . '[' . $key . ']';
                if ($name === 'force_create_duplicate') {
                    // Don't echo the override back — we're about to inject
                    // it explicitly on the force-create form below.
                    continue;
                }
                if (is_array($value)) {
                    $out .= $emitHiddenInputs($value, $name);
                    continue;
                }
                // Scalar leaves only — anything else (object, resource) can't
                // safely round-trip through a form input. The intake-review
                // form posts only string scalars, so nothing legitimate is
                // dropped here.
                if (!is_scalar($value)) {
                    continue;
                }
                $out .= sprintf(
                    '<input type="hidden" name="%s" value="%s">',
                    htmlspecialchars($name, ENT_QUOTES, 'UTF-8'),
                    htmlspecialchars((string) $value, ENT_QUOTES, 'UTF-8'),
                );
            }
            return $out;
        };

        $rawPost = filter_input_array(INPUT_POST);
        $hiddenPostInputs = is_array($rawPost) ? $emitHiddenInputs($rawPost) : '';

        ?><!DOCTYPE html>
<html>
<head>
    <title>Possible duplicate patient</title>
    <style>
        body { font-family: system-ui, sans-serif; padding: 2rem; max-width: 760px; }
        h1 { margin-top: 0; }
        .alert-warn {
            background: #fff8e6; border: 1px solid #e6c46a; padding: 1rem 1.25rem;
            border-radius: 4px; margin: 1rem 0; color: #5a4400;
        }
        .alert-warn strong { font-size: 1.05em; }
        ul.match-list { list-style: none; padding: 0; margin: 0.6rem 0; }
        ul.match-list li {
            padding: 0.55rem 0.75rem; border: 1px solid #e0e0e0;
            border-radius: 3px; background: #fafafa; margin-bottom: 0.4rem;
        }
        ul.match-list strong { color: #154f9c; }
        ul.match-list code { background: #f4ecd0; padding: 0.05em 0.3em; border-radius: 2px; }
        ul.match-list .actions { margin-top: 0.4rem; }
        ul.match-list .actions a {
            display: inline-block; padding: 0.35rem 0.8rem; background: #2057a8;
            color: white; text-decoration: none; border-radius: 3px;
            margin-right: 0.4rem; font-size: 0.9em;
        }
        .force-create {
            margin-top: 1rem; padding-top: 1rem; border-top: 1px solid #eee;
            font-size: 0.95em;
        }
        .force-create button {
            padding: 0.45rem 1rem; background: #e0e0e0; color: #333;
            border: 1px solid #999; border-radius: 3px; cursor: pointer;
        }
        .back-link { margin-top: 1.5rem; font-size: 0.9em; color: #555; }
    </style>
</head>
<body>
<div class="alert-warn">
    <strong>This patient may already exist.</strong>
    <p style="margin: 0.5rem 0;">
        <strong><?php echo htmlspecialchars($fname . ' ' . $lname, ENT_QUOTES, 'UTF-8'); ?></strong>,
        DOB <?php echo htmlspecialchars($dob, ENT_QUOTES, 'UTF-8'); ?> matches
        <?php echo count($blockingMatches) === 1 ? 'an existing chart' : count($blockingMatches) . ' existing charts'; ?>
        at high confidence. Creating a new chart now would duplicate the patient.
    </p>
</div>

<h2>Existing charts that match</h2>
<ul class="match-list">
        <?php foreach ($blockingMatches as $candidate): ?>
        <li>
            <strong><?php echo htmlspecialchars($candidate->firstName . ' ' . $candidate->lastName, ENT_QUOTES, 'UTF-8'); ?></strong>
            — pid <code><?php echo $candidate->pid; ?></code>
            | DOB <?php echo htmlspecialchars($candidate->dob, ENT_QUOTES, 'UTF-8'); ?>
            <?php if ($candidate->mrn !== null && $candidate->mrn !== ''): ?>
                | MRN <code><?php echo htmlspecialchars((string) $candidate->mrn, ENT_QUOTES, 'UTF-8'); ?></code>
            <?php endif; ?>
            <span style="color:#666; font-size:0.85em;">
                — <?php echo number_format($candidate->score * 100, 0); ?>%
                (<?php echo htmlspecialchars($candidate->matchReason, ENT_QUOTES, 'UTF-8'); ?>)
            </span>
            <div class="actions">
                <a href="<?php echo htmlspecialchars(
                    $webroot . '/interface/patient_file/summary/demographics.php?'
                        . http_build_query(['set_pid' => $candidate->pid]),
                    ENT_QUOTES,
                    'UTF-8',
                         ); ?>">Open existing chart</a>
            </div>
        </li>
    <?php endforeach; ?>
</ul>

<div class="force-create">
    <p>
        Different person who happens to have the same name and date of birth
        (twins, etc.)? Force-create a separate chart with the demographics
        you just entered:
    </p>
    <form method="post"
          action="<?php echo htmlspecialchars(
              $webroot . '/interface/copilot/new_patient_save_ai.php',
              ENT_QUOTES,
              'UTF-8',
                  ); ?>">
        <?php echo $hiddenPostInputs; ?>
        <input type="hidden" name="force_create_duplicate" value="1">
        <button type="submit">Create as a separate chart anyway</button>
    </form>
</div>

<p class="back-link">
    Or use your browser&rsquo;s Back button to return to the intake review and
    edit the demographics if the agent extracted them incorrectly.
</p>
</body>
</html>
        <?php
        exit;
    }
}

// We INSERT directly rather than calling library/patient.inc.php's
// newPatientData(): for a fresh pid it queries the (not-yet-existing)
// row for fitness/referral_source, gets null back, and tries to write
// fitness = NULL — which trips STRICT_TRANS_TABLES (fitness is NOT NULL
// in schema). Inserting only the columns we actually populate lets
// MySQL fill the rest with their schema defaults.

$session = SessionWrapperFactory::getInstance()->getActiveSession();
$authUserIdRaw = $session->get('authUserID');
$authUserId = is_int($authUserIdRaw)
    ? $authUserIdRaw
    : (is_numeric($authUserIdRaw) ? (int) $authUserIdRaw : 0);

$nowDateTime = date('Y-m-d H:i:s');
$nowDate = date('Y-m-d');

$pidRow = QueryUtils::querySingleRow('SELECT MAX(pid) + 1 AS pid FROM patient_data');
$pidVal = is_array($pidRow) ? ($pidRow['pid'] ?? null) : null;
$newpid = is_int($pidVal) ? $pidVal : (is_numeric($pidVal) ? (int) $pidVal : 1);
if ($newpid <= 0) {
    $newpid = 1;
}

$pubpidValue = $externalMrn !== '' ? $externalMrn : (string) $newpid;
$patientUuid = (new UuidRegistry(['table_name' => 'patient_data']))->createUuid();

QueryUtils::sqlStatementThrowException('START TRANSACTION');
try {
    QueryUtils::sqlInsert(
        <<<'SQL'
        INSERT INTO patient_data SET
            pid = ?, uuid = ?, fname = ?, lname = ?, sex = ?, DOB = ?,
            phone_home = ?, email = ?, pubpid = ?,
            regdate = ?, date = ?, created_by = ?, updated_by = ?
        SQL,
        [
            $newpid, $patientUuid, $fname, $lname, $sex, $dob,
            $phone, $email, $pubpidValue,
            $nowDate, $nowDateTime, $authUserId, $authUserId,
        ],
    );

    setpid($newpid);

    // OpenEMR expects a stub employer_data + history_data row to exist
    // for every patient — the stock new-patient flow does the same thing
    // immediately after newPatientData.
    if (function_exists('newEmployerData')) {
        newEmployerData((string) $newpid);
    }
    if (function_exists('newHistoryData')) {
        newHistoryData((string) $newpid);
    }

    $now = date('Y-m-d H:i:s');

    // Active problems → lists.type='medical_problem'.
    // Step 0.5 spike pinned the type strings; the column shape comes
    // from inspecting an existing seeded row (title, comments,
    // diagnosis, occurrence, classification, begdate).
    $problemSave = $readPostList('problem_save');
    $problemConditions = $readPostList('problem_condition');
    $problemIcds = $readPostList('problem_icd10');
    $problemSnomeds = $readPostList('problem_snomed');
    $problemOnsets = $readPostList('problem_onset');
    foreach ($problemSave as $idx => $flag) {
        if ($flag !== '1') {
            continue;
        }
        $condition = trim($problemConditions[$idx] ?? '');
        if ($condition === '') {
            continue;
        }
        $icd = trim($problemIcds[$idx] ?? '');
        $snomed = trim($problemSnomeds[$idx] ?? '');
        $onset = trim($problemOnsets[$idx] ?? '');
        $diagnosisCol = '';
        if ($icd !== '') {
            $diagnosisCol = 'ICD10:' . $icd;
        }
        if ($snomed !== '') {
            $diagnosisCol = ($diagnosisCol === '')
                ? 'SNOMED-CT:' . $snomed
                : $diagnosisCol . ';SNOMED-CT:' . $snomed;
        }
        $begdate = ctype_digit($onset) ? ($onset . '-01-01 00:00:00') : $now;
        QueryUtils::sqlInsert(
            <<<'SQL'
            INSERT INTO lists (date, type, title, pid, diagnosis, occurrence, classification, begdate, activity)
            VALUES (NOW(), 'medical_problem', ?, ?, ?, 0, 0, ?, 1)
            SQL,
            [$condition, $newpid, $diagnosisCol, $begdate],
        );
    }

    // Medications → lists.type='medication'.
    $medSave = $readPostList('med_save');
    $medNames = $readPostList('med_name');
    $medDoses = $readPostList('med_dose');
    $medFreqs = $readPostList('med_freq');
    $medRxnorms = $readPostList('med_rxnorm');
    $medIndications = $readPostList('med_indication');
    $medStarteds = $readPostList('med_started');
    foreach ($medSave as $idx => $flag) {
        if ($flag !== '1') {
            continue;
        }
        $name = trim($medNames[$idx] ?? '');
        if ($name === '') {
            continue;
        }
        $dose = trim($medDoses[$idx] ?? '');
        $freq = trim($medFreqs[$idx] ?? '');
        $rxnorm = trim($medRxnorms[$idx] ?? '');
        $indication = trim($medIndications[$idx] ?? '');
        $started = trim($medStarteds[$idx] ?? '');
        $title = trim($name . ' ' . $dose . ($freq !== '' ? (' ' . $freq) : ''));
        $diagnosisCol = $rxnorm !== '' ? ('RXCUI:' . $rxnorm) : '';
        $begdate = ctype_digit($started) ? ($started . '-01-01 00:00:00') : $now;
        QueryUtils::sqlInsert(
            <<<'SQL'
            INSERT INTO lists (date, type, title, pid, comments, diagnosis, occurrence, classification, begdate, activity)
            VALUES (NOW(), 'medication', ?, ?, ?, ?, 0, 0, ?, 1)
            SQL,
            [$title, $newpid, $indication, $diagnosisCol, $begdate],
        );
    }

    // Allergies → lists.type='allergy'.
    $allergySave = $readPostList('allergy_save');
    $allergySubstances = $readPostList('allergy_substance');
    $allergyReactions = $readPostList('allergy_reaction');
    $allergySeverities = $readPostList('allergy_severity');
    foreach ($allergySave as $idx => $flag) {
        if ($flag !== '1') {
            continue;
        }
        $substance = trim($allergySubstances[$idx] ?? '');
        if ($substance === '') {
            continue;
        }
        $reaction = trim($allergyReactions[$idx] ?? '');
        $severity = trim($allergySeverities[$idx] ?? '');
        $comments = $reaction;
        if ($severity !== '') {
            $comments = $reaction !== '' ? ($reaction . ' (' . $severity . ')') : ('(' . $severity . ')');
        }
        QueryUtils::sqlInsert(
            <<<'SQL'
            INSERT INTO lists (date, type, title, pid, comments, occurrence, classification, begdate, activity)
            VALUES (NOW(), 'allergy', ?, ?, ?, 0, 0, ?, 1)
            SQL,
            [$substance, $newpid, $comments, $now],
        );
    }

    // Tobacco status → lists.type='medical_problem' tagged with a
    // SNOMED tobacco-use code. The stock OpenEMR social-history surface
    // uses history_data.tobacco; we mirror that as a problem so it's
    // visible in the chart's problems panel where clinicians look first.
    if ($tobaccoStatus !== '' && $tobaccoStatus !== 'never') {
        $packs = $tobaccoPackYears !== '' ? (' (' . $tobaccoPackYears . ' pack-years)') : '';
        $title = ucfirst($tobaccoStatus) . ' tobacco user' . $packs;
        QueryUtils::sqlInsert(
            <<<'SQL'
            INSERT INTO lists (date, type, title, pid, occurrence, classification, begdate, activity)
            VALUES (NOW(), 'medical_problem', ?, ?, 0, 0, ?, 1)
            SQL,
            [$title, $newpid, $now],
        );
    }

    // Link the source intake document back to the new patient pid.
    if ($documentId !== '' && str_starts_with($documentId, 'openemr:doc:')) {
        $docNumeric = (int) substr($documentId, strlen('openemr:doc:'));
        if ($docNumeric > 0) {
            QueryUtils::sqlStatementThrowException(
                'UPDATE documents SET foreign_id = ? WHERE id = ?',
                [$newpid, $docNumeric],
            );
        }
    }

    QueryUtils::sqlStatementThrowException('COMMIT');
} catch (\Throwable $exc) {
    QueryUtils::sqlStatementThrowException('ROLLBACK');
    throw $exc;
}

// Chief complaint is captured for the encounter, not the patient
// record — the stock new-patient flow has the clinician add it on the
// first encounter form. We stash it best-effort in
// history_data.additional_history so it isn't lost between intake
// upload and the first encounter; failure here must not undo a
// successfully created patient, so it lives outside the transaction.
if ($chiefComplaint !== '') {
    try {
        QueryUtils::sqlStatementThrowException(
            'UPDATE history_data SET additional_history = ? WHERE pid = ? ORDER BY id DESC LIMIT 1',
            ['CHIEF COMPLAINT (Co-Pilot intake): ' . $chiefComplaint, $newpid],
        );
    } catch (SqlQueryException $exc) {
        ServiceContainer::getLogger()->warning(
            'Co-Pilot intake: chief-complaint stash failed',
            ['pid' => $newpid, 'exception' => $exc],
        );
    }
}

$webrootRaw = $globalsBag->get('webroot', '');
$webroot = is_string($webrootRaw) ? $webrootRaw : '';

// Take the user to the new patient's chart. Use the standard
// patient-summary URL.
header('Location: ' . $webroot . '/interface/patient_file/summary/demographics.php?set_pid=' . $newpid);
exit;
