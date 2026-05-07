<?php

/**
 * Clinical Co-Pilot — post-save confirmation page.
 *
 * Lands here after ``api/save_document.php`` finishes both branches of
 * the editable-confirm flow:
 *   - "Create new patient" → a fresh chart was inserted and the source
 *     document was attached to it.
 *   - "Pick existing patient" → ``documents.foreign_id`` was flipped
 *     from "00" to that pid; chart-write sections (allergies / meds /
 *     problems / care gaps / lab observations) ran against the chart.
 *
 * The page reads the per-section row counts the save handler put in the
 * URL (``count_<section>=N``) and renders a one-shot confirmation so
 * the clinician sees what landed before clicking through to the chart.
 *
 * For ``fax_tiff`` the underlying schema deliberately carries no
 * structured rows (only patient demographics + a list of per-page
 * summaries — see ``agent-service/.../documents/schemas/fax_tiff.py``),
 * so a "No chart sections were written" message would read as "nothing
 * happened" even though the fax IS attached and the per-page summaries
 * are useful for triage. For that document type we fetch the extracted
 * facts back from the agent service and render the page summaries
 * inline so the clinician can see what they just filed.
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

use GuzzleHttp\Client as GuzzleClient;
use GuzzleHttp\Psr7\HttpFactory;
use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Core\Header;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\AgentServiceException;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use OpenEMR\Services\Copilot\DocumentClassifier;

if (!AclMain::aclCheckCore('patients', 'demo')) {
    http_response_code(403);
    exit('forbidden');
}

$pidParam = filter_input(INPUT_GET, 'pid');
$pid = (is_string($pidParam) && ctype_digit($pidParam)) ? (int) $pidParam : 0;
if ($pid <= 0) {
    http_response_code(400);
    exit('missing pid');
}

$createdParam = (string) (filter_input(INPUT_GET, 'created') ?? '0');
$wasCreated = $createdParam === '1';

$documentType = (string) (filter_input(INPUT_GET, 'document_type') ?? '');
$documentId = (string) (filter_input(INPUT_GET, 'document_id') ?? '');

$globals = OEGlobalsBag::getInstance();
$webrootRaw = $globals->get('webroot', '');
$webroot = is_string($webrootRaw) ? $webrootRaw : '';

// Pull (fname, lname, pubpid) so the confirmation can name the patient
// the user just operated on. The API tier already authorized the user
// for ``patients/demo`` so reading this row is in scope.
$row = QueryUtils::querySingleRow(
    'SELECT fname, lname, pubpid FROM patient_data WHERE pid = ?',
    [$pid],
);
$fname = is_array($row) && is_string($row['fname'] ?? null) ? $row['fname'] : '';
$lname = is_array($row) && is_string($row['lname'] ?? null) ? $row['lname'] : '';
$pubpidRaw = is_array($row) ? ($row['pubpid'] ?? null) : null;
$pubpid = is_string($pubpidRaw)
    ? $pubpidRaw
    : (is_int($pubpidRaw) || is_float($pubpidRaw) ? (string) $pubpidRaw : '');

$displayName = trim($fname . ' ' . $lname);
if ($displayName === '') {
    $displayName = '(unknown name)';
}

// Pick the count_* params back up. Each one is a per-section count the
// save handler emitted via ``http_build_query``. Any other prefix is
// ignored so the URL remains tamper-tolerant.
$counts = [];
$sectionLabels = [
    'allergies' => 'allergies',
    'medications' => 'medications',
    'active_problems' => 'active problems',
    'care_gaps' => 'care gaps / reminders',
    'lab_observations' => 'lab observations',
];
// filter_input_array() returns array|false|null. Normalize to an array
// so the foreach below has something iterable on every code path.
$rawQuery = filter_input_array(INPUT_GET);
if (!is_array($rawQuery)) {
    $rawQuery = [];
}
foreach ($rawQuery as $key => $value) {
    if (!is_string($key) || !str_starts_with($key, 'count_')) {
        continue;
    }
    if (!is_string($value) || !ctype_digit($value)) {
        continue;
    }
    $section = substr($key, strlen('count_'));
    if (!isset($sectionLabels[$section])) {
        continue;
    }
    $counts[$section] = (int) $value;
}
$totalRowsWritten = array_sum($counts);

// For fax_tiff, fetch the page summaries from the agent service so we
// can render them inline. Failure here is non-fatal — the save already
// happened; missing summaries just degrade the confirmation copy.
$faxPages = [];
$faxPagesError = '';
if ($documentType === DocumentClassifier::TYPE_FAX_TIFF && $documentId !== '') {
    $config = new CopilotConfig($globals);
    $factory = new HttpFactory();
    $httpClient = new GuzzleClient([
        'timeout' => max($config->getAgentTimeoutSeconds(), 10),
        'http_errors' => false,
    ]);
    $agentClient = new AgentHttpClient($httpClient, $factory, $config);
    try {
        $response = $agentClient->getInternal(
            '/api/agent/internal/extracted/' . rawurlencode($documentId),
            $config->getInternalToken(),
        );
        if ($response->statusCode === 200 && is_array($response->body['pages'] ?? null)) {
            foreach ($response->body['pages'] as $page) {
                if (!is_array($page)) {
                    continue;
                }
                $pageNumberField = $page['page_number'] ?? null;
                $pageTypeField = $page['page_type'] ?? null;
                $summaryField = $page['summary'] ?? null;
                $pageNumber = is_array($pageNumberField) ? ($pageNumberField['value'] ?? null) : null;
                $pageType = is_array($pageTypeField) ? ($pageTypeField['value'] ?? null) : null;
                $summary = is_array($summaryField) ? ($summaryField['value'] ?? null) : null;
                $faxPages[] = [
                    'number' => is_int($pageNumber) ? $pageNumber : (is_numeric($pageNumber) ? (int) $pageNumber : 0),
                    'type' => is_string($pageType) ? $pageType : '',
                    'summary' => is_string($summary) ? $summary : '',
                ];
            }
            usort(
                $faxPages,
                static fn (array $a, array $b): int => $a['number'] <=> $b['number'],
            );
        } elseif ($response->statusCode !== 200) {
            $faxPagesError = 'extractor returned status ' . $response->statusCode;
        }
    } catch (AgentServiceException $e) {
        $faxPagesError = $e->getMessage();
    }
}

$chartUrl = $webroot . '/interface/patient_file/summary/demographics.php?'
    . http_build_query(['set_pid' => $pid]);
$documentsUrl = $webroot . '/controller.php?'
    . http_build_query([
        'document' => '',
        'list' => '',
        'patient_id' => $pid,
    ]);
$uploadAnotherUrl = $webroot . '/interface/copilot/upload_document.php';

Header::setupHeader();
?>
<!DOCTYPE html>
<html>
<head>
    <title><?php echo $wasCreated ? 'Patient created' : 'Document attached'; ?></title>
    <style>
        body { font-family: system-ui, sans-serif; padding: 2rem; max-width: 760px; }
        h1 { margin-top: 0; }
        h2 { margin-top: 1.75rem; padding-bottom: 0.3rem; border-bottom: 1px solid #ddd; }
        .alert-success {
            background: #e8f6ec; border: 1px solid #8acc9a; padding: 1rem 1.25rem;
            border-radius: 4px; margin: 1rem 0; color: #1d5a2e;
        }
        .alert-success strong { font-size: 1.05em; }
        .meta { color: #555; margin: 0.5rem 0 1rem; }
        .meta code { background: #f4f4f4; padding: 0.05rem 0.25rem; border-radius: 2px; }
        ul.write-summary { margin: 0.5rem 0 1rem; padding-left: 1.5rem; }
        ul.write-summary li { padding: 0.15rem 0; }
        ul.write-summary li.empty { color: #555; }
        ul.write-summary li.empty .lead { display: block; font-weight: 600; color: #1d5a2e; margin-bottom: 0.25rem; }
        ul.fax-pages { list-style: none; padding: 0; margin: 0.5rem 0 1rem; }
        ul.fax-pages li {
            padding: 0.5rem 0.75rem; border: 1px solid #e0e0e0; border-radius: 3px;
            margin-bottom: 0.4rem; background: #fafafa;
        }
        ul.fax-pages .page-head { font-weight: 600; color: #154f9c; margin-bottom: 0.15rem; }
        ul.fax-pages .page-type {
            display: inline-block; font-size: 0.75em; padding: 0.05rem 0.4rem;
            background: #e0e8f5; color: #154f9c; border-radius: 2px;
            margin-left: 0.4rem; text-transform: uppercase; letter-spacing: 0.04em;
        }
        ul.fax-pages .page-summary { color: #444; font-size: 0.95em; }
        .fax-pages-error { color: #888; font-size: 0.9em; font-style: italic; }
        .actions { margin: 1.5rem 0; }
        .actions a {
            display: inline-block; padding: 0.6rem 1.2rem; background: #2057a8;
            color: white; text-decoration: none; border-radius: 3px; margin-right: 0.5rem;
        }
        .actions a.secondary { background: #e0e0e0; color: #333; }
    </style>
</head>
<body>
<div class="alert-success">
    <strong>
        <?php if ($wasCreated): ?>
            Patient created.
        <?php else: ?>
            Document attached to patient.
        <?php endif; ?>
    </strong>
    <div class="meta">
        <?php echo htmlspecialchars($displayName, ENT_QUOTES, 'UTF-8'); ?>
        — pid <code><?php echo $pid; ?></code>
        <?php if ($pubpid !== '' && $pubpid !== (string) $pid): ?>
            | MRN <code><?php echo htmlspecialchars($pubpid, ENT_QUOTES, 'UTF-8'); ?></code>
        <?php endif; ?>
        <?php if ($documentType !== ''): ?>
            | source <code><?php echo htmlspecialchars($documentType, ENT_QUOTES, 'UTF-8'); ?></code>
        <?php endif; ?>
    </div>
</div>

<h2>What landed in the chart</h2>
<?php if ($totalRowsWritten === 0): ?>
    <ul class="write-summary">
        <li class="empty">
            <span class="lead">
                <?php if ($documentType === DocumentClassifier::TYPE_FAX_TIFF): ?>
                    Fax packet attached as a TIFF document.
                <?php else: ?>
                    Source document attached to the chart.
                <?php endif; ?>
            </span>
            <?php if ($documentType === DocumentClassifier::TYPE_FAX_TIFF): ?>
                The packet itself is now in the chart's Documents tab — the
                fax_tiff schema doesn't auto-extract structured rows
                (medications, problems, labs), so chart-side action on
                individual pages is up to the clinician.
            <?php else: ?>
                No structured rows landed in the chart's allergies / medications
                / problems / labs tables — either the document type doesn't
                carry those (e.g. an HL7 ADT demographics-only update) or every
                checkbox was unticked on the review screen.
            <?php endif; ?>
        </li>
    </ul>
<?php else: ?>
    <ul class="write-summary">
        <?php foreach ($counts as $section => $count): ?>
            <?php if ($count <= 0) {
                continue;
            } ?>
            <li>
                <strong><?php echo $count; ?></strong>
                <?php echo htmlspecialchars($sectionLabels[$section], ENT_QUOTES, 'UTF-8'); ?>
            </li>
        <?php endforeach; ?>
    </ul>
<?php endif; ?>

<?php if ($documentType === DocumentClassifier::TYPE_FAX_TIFF): ?>
    <h2>Pages in this fax packet</h2>
    <?php if ($faxPages !== []): ?>
        <ul class="fax-pages">
            <?php foreach ($faxPages as $page): ?>
                <li>
                    <div class="page-head">
                        Page <?php echo $page['number']; ?>
                        <?php if ($page['type'] !== ''): ?>
                            <span class="page-type"><?php echo htmlspecialchars($page['type'], ENT_QUOTES, 'UTF-8'); ?></span>
                        <?php endif; ?>
                    </div>
                    <?php if ($page['summary'] !== ''): ?>
                        <div class="page-summary"><?php echo htmlspecialchars($page['summary'], ENT_QUOTES, 'UTF-8'); ?></div>
                    <?php endif; ?>
                </li>
            <?php endforeach; ?>
        </ul>
    <?php elseif ($faxPagesError !== ''): ?>
        <p class="fax-pages-error">
            Could not load page summaries (<?php echo htmlspecialchars($faxPagesError, ENT_QUOTES, 'UTF-8'); ?>).
            Open the document in the chart's Documents tab to view it.
        </p>
    <?php else: ?>
        <p class="fax-pages-error">
            No page summaries were extracted. Open the document in the chart's
            Documents tab to view it.
        </p>
    <?php endif; ?>
<?php endif; ?>

<div class="actions">
    <a href="<?php echo htmlspecialchars($chartUrl, ENT_QUOTES, 'UTF-8'); ?>">Open chart</a>
    <a class="secondary" href="<?php echo htmlspecialchars($documentsUrl, ENT_QUOTES, 'UTF-8'); ?>">
        View attached documents
    </a>
    <a class="secondary" href="<?php echo htmlspecialchars($uploadAnotherUrl, ENT_QUOTES, 'UTF-8'); ?>">
        Upload another document
    </a>
</div>
</body>
</html>
