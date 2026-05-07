<?php

/**
 * Clinical Co-Pilot — universal document review surface
 * (Week 2 multimodal expansion, Step 1; enriched in Step 4).
 *
 * Single review page reached by ``upload_document.php`` after a
 * successful extraction. Responsibilities by step:
 *
 * **Step 1 (this version):** show extracted facts as JSON, surface the
 * abstain summary, and route the clinician to the existing
 * type-specific confirm pages for ``lab_pdf`` and ``intake_form``.
 * Newer types (referral, fax, workbook, hl7) render the facts and a
 * "patient routing not yet wired" notice.
 *
 * **Step 4 (planned):** call the patient-resolver worker to suggest
 * a matching chart from extracted demographics, render
 * ``[Confirm match] [Pick different] [Create new]`` actions, and
 * hand off to the appropriate write-back page (``lab_save_ai.php``,
 * ``new_patient_save_ai.php``, or a new document-attach handler).
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

require_once(__DIR__ . "/../globals.php");

use GuzzleHttp\Client as GuzzleClient;
use GuzzleHttp\Psr7\HttpFactory;
use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Core\Header;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\AgentServiceException;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use OpenEMR\Services\Copilot\DocumentClassifier;
use OpenEMR\Services\Copilot\PatientMatch\PatientMatchScorer;
use OpenEMR\Services\Copilot\PatientMatch\PatientMatchService;

if (!AclMain::aclCheckCore('patients', 'demo')) {
    http_response_code(403);
    exit('forbidden');
}

$documentId = (string) (filter_input(INPUT_GET, 'document_id') ?? '');
if ($documentId === '') {
    http_response_code(400);
    exit('missing document_id');
}

$documentType = (string) (filter_input(INPUT_GET, 'document_type') ?? '');
if ($documentType === '') {
    http_response_code(400);
    exit('missing document_type');
}

$pidParam = filter_input(INPUT_GET, 'pid');
$pid = (is_string($pidParam) && ctype_digit($pidParam)) ? (int) $pidParam : 0;

$globals = OEGlobalsBag::getInstance();
$webrootRaw = $globals->get('webroot', '');
$webroot = is_string($webrootRaw) ? $webrootRaw : '';

$config = new CopilotConfig($globals);
$factory = new HttpFactory();
$httpClient = new GuzzleClient([
    'timeout' => max($config->getAgentTimeoutSeconds() * 4, 30),
    'http_errors' => false,
]);
$agentClient = new AgentHttpClient($httpClient, $factory, $config);

$facts = null;
$loadError = '';
try {
    $response = $agentClient->getInternal(
        '/api/agent/internal/extracted/' . rawurlencode($documentId),
        $config->getInternalToken(),
    );
    if ($response->statusCode === 200) {
        $facts = $response->body;
    } elseif ($response->statusCode === 404) {
        $loadError = 'Extraction record not found. Re-upload the document.';
    } else {
        $loadError = 'Unexpected status ' . $response->statusCode . ' from extractor.';
    }
} catch (AgentServiceException $e) {
    $loadError = 'Could not reach extractor: ' . $e->getMessage();
}

// "Continue" target by type. Existing review pages stay the canonical
// confirm surfaces for lab/intake while Step 4 builds the resolver UI
// for the new types in this page itself.
$continueUrl = '';
$continueLabel = '';
$continueNote = '';
switch ($documentType) {
    case DocumentClassifier::TYPE_LAB_PDF:
        if ($pid > 0) {
            $continueUrl = $webroot . '/interface/copilot/lab_review.php?'
                . http_build_query(['pid' => $pid, 'document_id' => $documentId]);
            $continueLabel = 'Continue to lab review';
        } else {
            $continueNote = 'Lab review needs a patient id; re-upload from a chart.';
        }
        break;
    case DocumentClassifier::TYPE_INTAKE_FORM:
        $continueUrl = $webroot . '/interface/copilot/new_patient_with_ai.php?'
            . http_build_query(['document_id' => $documentId]);
        $continueLabel = 'Continue to new-patient form';
        break;
    case DocumentClassifier::TYPE_REFERRAL_DOCX:
    case DocumentClassifier::TYPE_FAX_TIFF:
    case DocumentClassifier::TYPE_WORKBOOK_XLSX:
    case DocumentClassifier::TYPE_HL7_ORU:
    case DocumentClassifier::TYPE_HL7_ADT:
        $continueNote = 'Patient routing for "' . htmlspecialchars($documentType, ENT_QUOTES, 'UTF-8')
            . '" lands in Week 2 Step 4 (patient resolver). The extracted facts above are the '
            . 'agent\'s suggestion; nothing has been written to the chart.';
        break;
    default:
        $continueNote = 'Unknown document type — no continue target.';
}

$abstainSummary = is_array($facts) && isset($facts['abstain_summary']) && is_array($facts['abstain_summary'])
    ? $facts['abstain_summary']
    : null;

/**
 * Pull (first, last, dob, mrn) out of an extracted-fact payload by
 * document type. The shape varies per type — intake forms surface
 * legal_first_name / legal_last_name as separate fields, fax packets
 * give a single patient_name we have to split on whitespace, etc.
 *
 * Returns an array with four nullable string keys. Skipping a field
 * when the value is missing or in an abstain state is intentional —
 * the patient matcher treats null as "don't filter on this".
 *
 * @param array<mixed,mixed>|null $factsBody
 * @return array{first_name:?string, last_name:?string, dob:?string, mrn:?string}
 */
$extractDemographics = static function (?array $factsBody, string $type): array {
    $out = ['first_name' => null, 'last_name' => null, 'dob' => null, 'mrn' => null];
    if ($factsBody === null) {
        return $out;
    }
    $factsInner = $factsBody['facts'] ?? null;
    if (!is_array($factsInner)) {
        return $out;
    }

    $valueOf = static function (mixed $field): ?string {
        if (!is_array($field)) {
            return null;
        }
        $v = $field['value'] ?? null;
        return is_string($v) && $v !== '' ? $v : null;
    };

    $splitName = static function (?string $full): array {
        if ($full === null) {
            return [null, null];
        }
        $parts = preg_split('/\s+/', trim($full)) ?: [];
        if (count($parts) === 1) {
            return [null, $parts[0]];
        }
        return [$parts[0], end($parts) ?: null];
    };

    if ($type === DocumentClassifier::TYPE_INTAKE_FORM) {
        $out['first_name'] = $valueOf($factsInner['legal_first_name'] ?? null);
        $out['last_name'] = $valueOf($factsInner['legal_last_name'] ?? null);
        $out['dob'] = $valueOf($factsInner['date_of_birth'] ?? null);
        $out['mrn'] = $valueOf($factsInner['medical_record_number'] ?? null);
    } elseif ($type === DocumentClassifier::TYPE_REFERRAL_DOCX) {
        [$out['first_name'], $out['last_name']] = $splitName($valueOf($factsInner['patient_name'] ?? null));
        $out['dob'] = $valueOf($factsInner['patient_dob'] ?? null);
        $out['mrn'] = $valueOf($factsInner['patient_mrn'] ?? null);
    } elseif ($type === DocumentClassifier::TYPE_FAX_TIFF) {
        [$out['first_name'], $out['last_name']] = $splitName($valueOf($factsInner['patient_name'] ?? null));
        $out['dob'] = $valueOf($factsInner['patient_dob'] ?? null);
    }
    // lab_pdf doesn't carry demographics; workbook/hl7 land in their
    // own steps with their own demographics extractors.
    return $out;
};

$demographics = $extractDemographics(
    is_array($facts) ? $facts : null,
    $documentType,
);

$matchCandidates = [];
$matchHasDemographics = $demographics['first_name'] !== null
    || $demographics['last_name'] !== null
    || $demographics['mrn'] !== null;
if ($matchHasDemographics) {
    $matchService = new PatientMatchService();
    $matchCandidates = $matchService->match(
        $demographics['first_name'],
        $demographics['last_name'],
        $demographics['dob'],
        $demographics['mrn'],
    );
}

/**
 * Narrowing helper for the abstain-summary counts. The facts payload is
 * a generic ``array<mixed,mixed>`` because it round-trips through JSON;
 * phpstan level 10 forbids ``(int) <mixed>`` so each count is parsed
 * through this helper which returns 0 when the field is absent or of
 * the wrong type rather than coercing silently.
 *
 * @param array<mixed,mixed>|null $summary
 */
$abstainCount = static function (?array $summary, string $key): int {
    if ($summary === null) {
        return 0;
    }
    $value = $summary[$key] ?? null;
    return is_int($value) ? $value : 0;
};

Header::setupHeader();
?>
<!DOCTYPE html>
<html>
<head>
    <title>Review extracted document</title>
    <style>
        body { font-family: system-ui, sans-serif; padding: 2rem; max-width: 1100px; }
        h1 { margin-top: 0; }
        .meta { color: #555; font-size: 0.95em; margin-bottom: 1rem; }
        .meta code { background: #f4f4f4; padding: 0.05rem 0.25rem; border-radius: 2px; }
        .alert-error { background: #fee; border: 1px solid #faa; padding: 0.75rem 1rem; margin: 1rem 0; }
        .alert-info { background: #eef5ff; border: 1px solid #b6d2ff; padding: 0.75rem 1rem; margin: 1rem 0; color: #154f9c; }
        .summary { background: #f8f8f8; padding: 0.75rem 1rem; border-radius: 4px; margin: 1rem 0; font-size: 0.9em; }
        pre { background: #fbfbfb; padding: 1rem; border: 1px solid #eee; overflow-x: auto; max-height: 600px; }
        .actions { margin: 1.5rem 0; }
        .actions a { display: inline-block; padding: 0.5rem 1rem; background: #2057a8; color: white; text-decoration: none; border-radius: 3px; margin-right: 0.5rem; }
        .actions a.secondary { background: #e0e0e0; color: #333; }
        .match-table { width: 100%; border-collapse: collapse; margin: 1rem 0; font-size: 0.95em; }
        .match-table th, .match-table td { padding: 0.4rem 0.6rem; border-bottom: 1px solid #eee; text-align: left; }
        .match-table th { background: #f4f4f4; }
        .match-table tr.preselected { background: #f0f8ff; }
        .match-table tr.preselected td:first-child { color: #154f9c; font-weight: 600; }
    </style>
</head>
<body>
<h1>Review extracted document</h1>
<p class="meta">
    Document: <code><?php echo htmlspecialchars($documentId, ENT_QUOTES, 'UTF-8'); ?></code>
    | Type: <code><?php echo htmlspecialchars($documentType, ENT_QUOTES, 'UTF-8'); ?></code>
    | Patient pid: <?php echo $pid > 0 ? $pid : '<em>none</em>'; ?>
</p>

<?php if ($loadError !== ''): ?>
    <div class="alert-error"><?php echo htmlspecialchars($loadError, ENT_QUOTES, 'UTF-8'); ?></div>
<?php endif; ?>

<?php if ($abstainSummary !== null): ?>
    <div class="summary">
        Abstentions:
        low-confidence: <?php echo $abstainCount($abstainSummary, 'low_confidence_field_count'); ?>;
        no-data: <?php echo $abstainCount($abstainSummary, 'no_data_field_count'); ?>;
        citation-invalid: <?php echo $abstainCount($abstainSummary, 'citation_invalid_field_count'); ?>;
        out-of-schema: <?php echo $abstainCount($abstainSummary, 'out_of_schema_field_count'); ?>.
    </div>
<?php endif; ?>

<?php if ($matchHasDemographics): ?>
    <h2>Patient match</h2>
    <p class="meta">
        Extracted demographics:
        <strong><?php echo htmlspecialchars(trim(($demographics['first_name'] ?? '') . ' ' . ($demographics['last_name'] ?? '')) ?: '(none)', ENT_QUOTES, 'UTF-8'); ?></strong>
        <?php if ($demographics['dob'] !== null): ?>
            | DOB <?php echo htmlspecialchars($demographics['dob'], ENT_QUOTES, 'UTF-8'); ?>
        <?php endif; ?>
        <?php if ($demographics['mrn'] !== null): ?>
            | MRN <?php echo htmlspecialchars($demographics['mrn'], ENT_QUOTES, 'UTF-8'); ?>
        <?php endif; ?>
    </p>
    <?php if (count($matchCandidates) === 0): ?>
        <div class="alert-info">
            No existing patient matched these demographics above the
            review threshold (<?php echo PatientMatchScorer::REVIEW_THRESHOLD * 100; ?>%).
            Default action: <strong>create new patient</strong>.
        </div>
    <?php else: ?>
        <table class="match-table">
            <thead>
                <tr>
                    <th>Confidence</th>
                    <th>Patient</th>
                    <th>DOB</th>
                    <th>MRN</th>
                    <th>Why</th>
                    <th>Action</th>
                </tr>
            </thead>
            <tbody>
                <?php foreach ($matchCandidates as $c): ?>
                    <tr<?php echo PatientMatchScorer::shouldPreselect($c->score) ? ' class="preselected"' : ''; ?>>
                        <td><?php echo number_format($c->score * 100, 0); ?>%</td>
                        <td><?php echo htmlspecialchars($c->firstName . ' ' . $c->lastName, ENT_QUOTES, 'UTF-8'); ?></td>
                        <td><?php echo htmlspecialchars($c->dob, ENT_QUOTES, 'UTF-8'); ?></td>
                        <td><?php echo htmlspecialchars($c->mrn ?? '—', ENT_QUOTES, 'UTF-8'); ?></td>
                        <td><?php echo htmlspecialchars($c->matchReason, ENT_QUOTES, 'UTF-8'); ?></td>
                        <td>
                            <a href="<?php echo htmlspecialchars(
                                $webroot . '/interface/patient_file/summary/demographics.php?set_pid=' . $c->pid,
                                ENT_QUOTES,
                                'UTF-8',
                                     ); ?>">Open chart</a>
                        </td>
                    </tr>
                <?php endforeach; ?>
            </tbody>
        </table>
        <p class="meta">
            Preselect threshold: <?php echo PatientMatchScorer::PRESELECT_THRESHOLD * 100; ?>%.
            Above that, the row is auto-suggested as the match. Below, the
            clinician picks. The chart-side document attach (rewriting
            <code>documents.foreign_id</code> from the placeholder pid 0
            to the matched pid) lands in a follow-up; for now the doc
            stays in the unassigned-uploads bucket until the clinician
            opens the chart and confirms.
        </p>
    <?php endif; ?>
<?php endif; ?>

<?php if ($continueNote !== ''): ?>
    <div class="alert-info"><?php echo $continueNote; /* already escaped or trusted at site */ ?></div>
<?php endif; ?>

<?php if ($facts !== null): ?>
    <h2>Extracted facts</h2>
    <pre><?php echo htmlspecialchars(
        (string) json_encode($facts, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES),
        ENT_QUOTES,
        'UTF-8',
         ); ?></pre>
<?php endif; ?>

<div class="actions">
    <?php if ($continueUrl !== ''): ?>
        <a href="<?php echo htmlspecialchars($continueUrl, ENT_QUOTES, 'UTF-8'); ?>">
            <?php echo htmlspecialchars($continueLabel, ENT_QUOTES, 'UTF-8'); ?>
        </a>
    <?php endif; ?>
    <a class="secondary" href="<?php echo htmlspecialchars($webroot . '/interface/copilot/upload_document.php', ENT_QUOTES, 'UTF-8'); ?>">
        Upload another document
    </a>
</div>
</body>
</html>
