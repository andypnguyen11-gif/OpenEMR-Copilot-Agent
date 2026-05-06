<?php

/**
 * Clinical Co-Pilot — confirm-and-save handler for the lab flow (PR W2-02).
 *
 * Writes a clinician-confirmed extraction batch to OpenEMR's procedure
 * tables. The pattern mirrors the canonical OpenEMR import path:
 *
 *     procedure_order  (one row per upload — the "I uploaded a lab" event)
 *     procedure_order_code  (one row per panel — the LOINC of the panel)
 *     procedure_report  (one row, references the order)
 *     procedure_result  (N rows, one per confirmed observation)
 *
 * Pinned by the Step 0 schema spike (see /Users/andynguyen/.claude/plans/
 * ok-so-what-i-validated-boot.md). The chart-side "Lab Reports" view
 * joins these four tables, so a successful insert here renders on the
 * patient's chart without any further wiring.
 *
 * Only rows whose ``confirm[idx]`` checkbox is set (or remained checked
 * after the clinician's review) are written. Unchecked rows — typically
 * those the extractor abstained on — are dropped.
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

if (filter_input(INPUT_SERVER, 'REQUEST_METHOD') !== 'POST') {
    http_response_code(405);
    exit('method not allowed');
}

if (!AclMain::aclCheckCore('patients', 'med')) {
    http_response_code(403);
    exit('forbidden');
}

CsrfUtils::checkCsrfInput(
    INPUT_POST,
    session: SessionWrapperFactory::getInstance()->getActiveSession(),
    dieOnFail: true,
);

/**
 * @param string $key
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

$pidParam = filter_input(INPUT_POST, 'pid');
$pid = (is_string($pidParam) && ctype_digit($pidParam)) ? (int) $pidParam : 0;
if ($pid <= 0) {
    http_response_code(400);
    exit('missing pid');
}

$documentId = (string) (filter_input(INPUT_POST, 'document_id') ?? '');
if ($documentId === '') {
    http_response_code(400);
    exit('missing document_id');
}

$panelName = (string) (filter_input(INPUT_POST, 'panel_name') ?? 'Co-Pilot lab import');

$confirm = $readPostList('confirm');
$displays = $readPostList('display');
$codes = $readPostList('code');
$values = $readPostList('value');
$units = $readPostList('unit');
$references = $readPostList('reference');
$flagsIn = $readPostList('flag');

$session = SessionWrapperFactory::getInstance()->getActiveSession();
$authUserIdRaw = $session->get('authUserID');
$authUserId = is_int($authUserIdRaw) ? $authUserIdRaw
    : (is_numeric($authUserIdRaw) ? (int) $authUserIdRaw : 0);

$confirmedRows = [];
foreach ($confirm as $idx => $flag) {
    if ($flag !== '1') {
        continue;
    }
    $display = trim($displays[$idx] ?? '');
    $value = trim($values[$idx] ?? '');
    if ($display === '' || $value === '') {
        // Both required; silently skip an empty row rather than fail
        // the whole batch — clinicians may have cleared a row they
        // wanted to drop.
        continue;
    }
    $confirmedRows[] = [
        'display' => $display,
        'code' => trim($codes[$idx] ?? ''),
        'value' => $value,
        'unit' => trim($units[$idx] ?? ''),
        'reference' => trim($references[$idx] ?? ''),
        'flag' => trim($flagsIn[$idx] ?? ''),
    ];
}

if (count($confirmedRows) === 0) {
    http_response_code(400);
    exit('no observations confirmed');
}

// Map our flag glyphs (H/L/N/HH/LL) to procedure_result.abnormal values
// the chart rendering layer recognizes.
$abnormalMap = [
    '' => 'no',
    'N' => 'no',
    'H' => 'yes',
    'L' => 'yes',
    'HH' => 'yes',
    'LL' => 'yes',
];

// Step 0 spike pattern: order → code → report → results in one transaction.
QueryUtils::sqlStatementThrowException('START TRANSACTION');
try {
    $orderId = QueryUtils::sqlInsert(
        <<<'SQL'
        INSERT INTO procedure_order
            (provider_id, patient_id, encounter_id, date_collected, date_ordered,
             order_status, lab_id, specimen_type, procedure_order_type, order_intent)
        VALUES (?, ?, 0, NOW(), NOW(), 'complete', 0, 'serum', 'laboratory_test', 'order')
        SQL,
        [$authUserId, $pid],
    );

    QueryUtils::sqlInsert(
        <<<'SQL'
        INSERT INTO procedure_order_code
            (procedure_order_id, procedure_order_seq, procedure_code, procedure_name,
             procedure_source, procedure_order_title)
        VALUES (?, 1, '', ?, '1', ?)
        SQL,
        [$orderId, $panelName, $panelName],
    );

    $reportId = QueryUtils::sqlInsert(
        <<<'SQL'
        INSERT INTO procedure_report
            (procedure_order_id, procedure_order_seq, date_collected, date_report,
             source, specimen_num, report_status, review_status)
        VALUES (?, 1, NOW(), NOW(), ?, ?, 'complete', 'received')
        SQL,
        [$orderId, $authUserId, $documentId],
    );

    foreach ($confirmedRows as $row) {
        $abnormal = $abnormalMap[strtoupper($row['flag'])] ?? 'no';
        QueryUtils::sqlInsert(
            <<<'SQL'
            INSERT INTO procedure_result
                (procedure_report_id, result_data_type, result_code, result_text,
                 units, `result`, `range`, abnormal, result_status)
            VALUES (?, 'N', ?, ?, ?, ?, ?, ?, 'final')
            SQL,
            [
                $reportId,
                $row['code'],
                $row['display'],
                $row['unit'],
                $row['value'],
                $row['reference'],
                $abnormal,
            ],
        );
    }

    QueryUtils::sqlStatementThrowException('COMMIT');
} catch (\Throwable $exc) {
    QueryUtils::sqlStatementThrowException('ROLLBACK');
    throw $exc;
}

$globals = OEGlobalsBag::getInstance();
$webrootRaw = $globals->get('webroot', '');
$webroot = is_string($webrootRaw) ? $webrootRaw : '';

// Take the clinician straight to the just-saved order's results
// view. ``orders/list_reports.php`` is the canonical lab inbox but
// it short-circuits without ``form_refresh`` and never auto-scopes
// to a single patient via the URL — so it would render an empty
// grid even though the rows are in the DB. ``single_order_results``
// renders this specific order's procedure_report rows directly.
$dest = $webroot . '/interface/orders/single_order_results.php?orderid=' . $orderId;
header('Location: ' . $dest);
exit;
