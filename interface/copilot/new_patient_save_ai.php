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

require_once(__DIR__ . "/../globals.php");

use OpenEMR\BC\ServiceContainer;
use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Database\SqlQueryException;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Core\OEGlobalsBag;

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
