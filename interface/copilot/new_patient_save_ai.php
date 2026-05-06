<?php

/**
 * Clinical Co-Pilot — confirm-and-create handler for the new-patient
 * intake flow (PR W2-02).
 *
 * Creates a fresh OpenEMR patient row from the clinician-confirmed
 * intake review and seeds the lists table with active problems,
 * medications, and allergies in one transaction. Mirrors the canonical
 * ``interface/new/new_patient_save.php`` patient-allocation pattern
 * (table-lock → MAX(pid)+1 → unlock → newPatientData) so the AI path
 * never diverges from the stock-form path on the demographics write.
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

use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Session\SessionWrapperFactory;
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

// Allocate a new pid using the same table-lock pattern as
// interface/new/new_patient_save.php so we don't race against a
// concurrent stock-form patient creation.
QueryUtils::sqlStatementThrowException('LOCK TABLES patient_data READ');
$row = QueryUtils::querySingleRow('SELECT MAX(pid)+1 AS pid FROM patient_data');
QueryUtils::sqlStatementThrowException('UNLOCK TABLES');
$pidRaw = (is_array($row) ? ($row['pid'] ?? null) : null);
$newpid = is_int($pidRaw) ? $pidRaw : (is_numeric($pidRaw) ? (int) $pidRaw : 0);
if ($newpid <= 0) {
    $newpid = 1;
}

setpid($newpid);

$pubpid = $externalMrn !== '' ? $externalMrn : (string) $newpid;
$registeredOn = date('Y-m-d');

QueryUtils::sqlStatementThrowException('START TRANSACTION');
try {
    newPatientData(
        '',                  // db_id
        '',                  // title
        $fname,
        $lname,
        '',                  // mname
        $sex,
        $dob,
        '',                  // street
        '',                  // postal_code
        '',                  // city
        '',                  // state
        '',                  // country_code
        '',                  // ss
        '',                  // occupation
        $phone,              // phone_home
        '',                  // phone_biz
        '',                  // phone_contact
        '',                  // status
        '',                  // contact_relationship
        '',                  // referrer
        '',                  // referrerID
        $email,
        '',                  // language
        '',                  // ethnoracial
        '',                  // interpreter
        '',                  // migrantseasonal
        '',                  // family_size
        '',                  // monthly_income
        '',                  // homeless
        '',                  // financial_review
        $pubpid,
        (string) $newpid,
        '',                  // providerID
        '',                  // genericname1
        '',                  // genericval1
        '',                  // genericname2
        '',                  // genericval2
        '',                  // billing_note
        '',                  // phone_cell
        '',                  // hipaa_mail
        '',                  // hipaa_voice
        0,                   // squad
        0,                   // pharmacy_id
        '',                  // drivers_license
        '',                  // hipaa_notice
        '',                  // hipaa_message
        $registeredOn,
    );

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

    // Chief complaint is captured for the encounter — for tonight we
    // append it to the patient's history_data.usertext1 since creating
    // an encounter is a separate sub-flow. The clinician can convert
    // it into an actual encounter from the chart.
    if ($chiefComplaint !== '') {
        QueryUtils::sqlStatementThrowException(
            'UPDATE history_data SET usertext1 = ? WHERE pid = ? ORDER BY id DESC LIMIT 1',
            ['CHIEF COMPLAINT (Co-Pilot intake): ' . $chiefComplaint, $newpid],
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

$webrootRaw = $globalsBag->get('webroot', '');
$webroot = is_string($webrootRaw) ? $webrootRaw : '';

// Take the user to the new patient's chart. Use the standard
// patient-summary URL.
header('Location: ' . $webroot . '/interface/patient_file/summary/demographics.php?set_pid=' . $newpid);
exit;
