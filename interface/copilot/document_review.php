<?php

/**
 * Clinical Co-Pilot — universal document review surface
 * (Week 2 multimodal expansion + editable confirm pass).
 *
 * Reached by ``upload_document.php`` after a successful extraction.
 * The page is one HTML form that bundles two editable surfaces into
 * a single submit:
 *
 *   1. **Patient routing** — radios over the resolver's top
 *      candidates, plus a "Create new patient" choice. The
 *      preselected radio is whichever candidate (if any) crossed
 *      ``PatientMatchScorer::PRESELECT_THRESHOLD``.
 *
 *   2. **Editable facts** — every leaf ``ExtractedField`` becomes a
 *      ``<input>`` (text, number, or date depending on the schema),
 *      list-of-objects sections render as editable tables, abstaining
 *      fields surface their reason as a label so the clinician can
 *      fill them in. Submitting POSTs the whole form to
 *      ``api/save_document.php`` which:
 *        - Persists the edited facts via ``PUT /api/agent/internal/
 *          extracted/{id}``.
 *        - Updates ``documents.foreign_id`` to the picked patient
 *          (or to a freshly-created chart for the new-patient path).
 *        - Redirects to the patient's Documents view.
 *
 * For ``lab_pdf`` and ``intake_form`` we still link out to the
 * existing per-row review pages (``lab_review.php`` /
 * ``new_patient_with_ai.php``) instead of using the generic
 * editable form — those flows already have polished type-specific
 * UX and we want to preserve them for back-compat.
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
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Core\Header;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\AgentServiceException;
use OpenEMR\Services\Copilot\ChartWrite\FactsExtractor;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use OpenEMR\Services\Copilot\DocumentClassifier;
use OpenEMR\Services\Copilot\Documents\FactsFormHelper;
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

// For lab_pdf / intake_form keep the existing per-row review pages —
// they already have polished editable UX. The generic editable form
// below is only rendered for the five new multimodal types.
$useExistingReviewPage = false;
$existingReviewUrl = '';
$existingReviewLabel = '';
switch ($documentType) {
    case DocumentClassifier::TYPE_LAB_PDF:
        if ($pid > 0) {
            $useExistingReviewPage = true;
            $existingReviewUrl = $webroot . '/interface/copilot/lab_review.php?'
                . http_build_query(['pid' => $pid, 'document_id' => $documentId]);
            $existingReviewLabel = 'Continue to lab review (per-row editable)';
        }
        break;
    case DocumentClassifier::TYPE_INTAKE_FORM:
        $useExistingReviewPage = true;
        $existingReviewUrl = $webroot . '/interface/copilot/new_patient_with_ai.php?'
            . http_build_query(['document_id' => $documentId]);
        $existingReviewLabel = 'Continue to new-patient form (per-field editable)';
        break;
}

// The GET ``/extracted/{id}`` route returns the raw facts dump (NOT
// the wrapped IngestResponse shape POST returns) — same convention
// the existing lab_review.php / intake_review.php follow. So the
// inner facts dict IS the response body, and there's no separate
// ``abstain_summary`` block on a read-back (that's only computed on
// fresh extractions and lives on the IngestResponse, not the model).

/**
 * Pull (first, last, dob, mrn) out of an extracted-fact payload by
 * document type. Used for both display in the patient-match panel
 * and as the input to the matcher.
 *
 * @param array<mixed,mixed>|null $factsBody
 * @return array{first_name:?string, last_name:?string, dob:?string, mrn:?string}
 */
$extractDemographics = static function (?array $factsBody, string $type): array {
    $out = ['first_name' => null, 'last_name' => null, 'dob' => null, 'mrn' => null];
    if ($factsBody === null) {
        return $out;
    }
    // The GET response body IS the facts dump — no outer "facts" wrapper.
    $factsInner = $factsBody;

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
    } elseif ($type === DocumentClassifier::TYPE_HL7_ORU || $type === DocumentClassifier::TYPE_HL7_ADT) {
        [$out['first_name'], $out['last_name']] = $splitName($valueOf($factsInner['patient_name'] ?? null));
        $out['dob'] = $valueOf($factsInner['patient_dob'] ?? null);
        $out['mrn'] = $valueOf($factsInner['patient_mrn'] ?? null);
    } elseif ($type === DocumentClassifier::TYPE_WORKBOOK_XLSX) {
        $patientBlock = $factsInner['patient'] ?? null;
        if (is_array($patientBlock)) {
            [$out['first_name'], $out['last_name']] = $splitName($valueOf($patientBlock['name'] ?? null));
            $out['dob'] = $valueOf($patientBlock['dob'] ?? null);
            $out['mrn'] = $valueOf($patientBlock['mrn'] ?? null);
        }
    }
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

$preselectedPid = 0;
foreach ($matchCandidates as $c) {
    if (PatientMatchScorer::shouldPreselect($c->score)) {
        $preselectedPid = $c->pid;
        break;
    }
}

// The inner facts dict (the agent-service GET returns the raw dump
// directly, no IngestResponse wrapper) is what we render as editable.
// The save handler reads it back from the same name="facts[...]"
// key shape and PUTs it to the agent service, where it's validated
// against the typed-union schema.
$factsInner = is_array($facts) ? $facts : [];

// Which "Write to chart" sections does this document type carry data
// for? Drives the checkbox list below the patient picker. The tuple
// is [section_id, label, populated]. Populated is a quick heuristic
// — "is there at least one row of data for this section in the
// extracted facts" — used to disable the checkbox when the section
// would be a no-op.
$availableWriteSections = [];
$candidateSections = [
    DocumentClassifier::TYPE_INTAKE_FORM => [
        ['allergies', 'Allergies → Allergies card'],
        ['medications', 'Medications → Medications card'],
        ['active_problems', 'Active problems / PMH → Issues card'],
    ],
    DocumentClassifier::TYPE_REFERRAL_DOCX => [
        ['allergies', 'Allergies → Allergies card'],
        ['medications', 'Medications → Medications card'],
        ['active_problems', 'Active problems / PMH → Issues card'],
    ],
    DocumentClassifier::TYPE_WORKBOOK_XLSX => [
        ['allergies', 'Allergies → Allergies card'],
        ['medications', 'Medications → Medications card'],
        ['care_gaps', 'Care gaps → Patient Reminders'],
        ['lab_observations', 'Lab observations → Labs card'],
    ],
    DocumentClassifier::TYPE_LAB_PDF => [
        ['lab_observations', 'Lab observations → Labs card'],
    ],
    DocumentClassifier::TYPE_HL7_ORU => [
        ['lab_observations', 'Lab observations → Labs card'],
    ],
];
foreach ($candidateSections[$documentType] ?? [] as [$id, $label]) {
    $availableWriteSections[] = [
        'id' => $id,
        'label' => $label,
        'populated' => FactsExtractor::sectionPopulated($factsInner, $documentType, $id),
    ];
}

$session = SessionWrapperFactory::getInstance()->getActiveSession();
$csrfToken = CsrfUtils::collectCsrfToken(session: $session);

Header::setupHeader();
?>
<!DOCTYPE html>
<html>
<head>
    <title>Review extracted document</title>
    <style>
        body { font-family: system-ui, sans-serif; padding: 2rem; max-width: 1100px; }
        h1 { margin-top: 0; }
        h2 { margin-top: 2rem; padding-bottom: 0.3rem; border-bottom: 1px solid #ddd; }
        .meta { color: #555; font-size: 0.95em; margin-bottom: 1rem; }
        .meta code { background: #f4f4f4; padding: 0.05rem 0.25rem; border-radius: 2px; }
        .alert-error { background: #fee; border: 1px solid #faa; padding: 0.75rem 1rem; margin: 1rem 0; }
        .alert-info { background: #eef5ff; border: 1px solid #b6d2ff; padding: 0.75rem 1rem; margin: 1rem 0; color: #154f9c; }
        .summary { background: #f8f8f8; padding: 0.75rem 1rem; border-radius: 4px; margin: 1rem 0; font-size: 0.9em; }
        .actions { margin: 1.5rem 0; }
        .actions a, .actions button { display: inline-block; padding: 0.6rem 1.2rem; background: #2057a8; color: white; text-decoration: none; border-radius: 3px; margin-right: 0.5rem; border: 0; cursor: pointer; font-size: 1em; }
        .actions a.secondary, .actions button.secondary { background: #e0e0e0; color: #333; }
        .match-table { width: 100%; border-collapse: collapse; margin: 1rem 0; font-size: 0.95em; }
        .match-table th, .match-table td { padding: 0.4rem 0.6rem; border-bottom: 1px solid #eee; text-align: left; }
        .match-table th { background: #f4f4f4; }
        .match-table tr.preselected { background: #f0f8ff; }
        .match-table tr.preselected td:nth-child(2) { color: #154f9c; font-weight: 600; }
        .field-row { display: flex; align-items: flex-start; gap: 1rem; margin: 0.4rem 0; padding: 0.3rem 0; }
        .field-label { flex: 0 0 220px; font-weight: 600; font-size: 0.9em; padding-top: 0.4rem; color: #444; word-break: break-word; }
        .field-input { flex: 1; padding: 0.35rem 0.5rem; border: 1px solid #ccc; border-radius: 3px; font-family: inherit; font-size: 0.95em; }
        .citation-hint { flex: 0 0 100%; margin-left: 220px; padding-left: 1rem; color: #777; font-size: 0.8em; }
        .abstain-badge { background: #ffe9c4; color: #8a5a00; padding: 0.15rem 0.4rem; font-size: 0.75em; border-radius: 3px; margin-left: 0.5rem; }
        .empty-hint { color: #999; font-style: italic; padding-top: 0.4rem; }
        fieldset.list-group, fieldset.object-group { margin: 1rem 0; padding: 0.5rem 1rem; border: 1px solid #ddd; border-radius: 4px; background: #fafafa; }
        fieldset.list-group legend, fieldset.object-group legend { font-weight: 600; padding: 0 0.5rem; color: #154f9c; }
        .list-item { margin-bottom: 1rem; padding-left: 1rem; border-left: 2px solid #b6d2ff; }
        .list-item-index { font-size: 0.8em; color: #888; margin-bottom: 0.2rem; }
        details.raw-json { margin: 1rem 0; }
        details.raw-json summary { cursor: pointer; color: #555; }
        details.raw-json pre { background: #fbfbfb; padding: 1rem; border: 1px solid #eee; overflow-x: auto; max-height: 400px; }
        ul.write-sections { list-style: none; padding-left: 0; margin: 0.5rem 0 1rem; }
        ul.write-sections li { padding: 0.3rem 0; }
        ul.write-sections label { font-weight: 500; cursor: pointer; }
        ul.write-sections input[disabled] + * { color: #aaa; }
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


<?php if ($useExistingReviewPage): ?>
    <div class="alert-info">
        This document type has a polished per-row editable review surface.
        Continue to the type-specific page to edit and confirm:
    </div>
    <div class="actions">
        <a href="<?php echo htmlspecialchars($existingReviewUrl, ENT_QUOTES, 'UTF-8'); ?>">
            <?php echo htmlspecialchars($existingReviewLabel, ENT_QUOTES, 'UTF-8'); ?>
        </a>
        <a class="secondary" href="<?php echo htmlspecialchars($webroot . '/interface/copilot/upload_document.php', ENT_QUOTES, 'UTF-8'); ?>">
            Upload another document
        </a>
    </div>

    <?php if ($facts !== null): ?>
        <details class="raw-json"><summary>View raw extracted facts (debug)</summary>
            <pre><?php echo htmlspecialchars(
                (string) json_encode($facts, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES),
                ENT_QUOTES,
                'UTF-8',
                 ); ?></pre>
        </details>
    <?php endif; ?>
<?php else: ?>

<form method="post" action="<?php echo htmlspecialchars($webroot . '/interface/copilot/api/save_document.php', ENT_QUOTES, 'UTF-8'); ?>">
    <input type="hidden" name="csrf_token_form" value="<?php echo htmlspecialchars($csrfToken, ENT_QUOTES, 'UTF-8'); ?>">
    <input type="hidden" name="document_id" value="<?php echo htmlspecialchars($documentId, ENT_QUOTES, 'UTF-8'); ?>">
    <input type="hidden" name="document_type" value="<?php echo htmlspecialchars($documentType, ENT_QUOTES, 'UTF-8'); ?>">

    <?php if ($matchHasDemographics): ?>
    <h2>1. Pick the patient</h2>
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
    <table class="match-table">
        <thead>
            <tr>
                <th></th>
                <th>Patient</th>
                <th>DOB</th>
                <th>MRN</th>
                <th>Confidence</th>
                <th>Why</th>
            </tr>
        </thead>
        <tbody>
                <?php foreach ($matchCandidates as $c): ?>
                <tr<?php echo $c->pid === $preselectedPid ? ' class="preselected"' : ''; ?>>
                    <td>
                        <input type="radio" name="patient_choice" value="<?php echo (int) $c->pid; ?>"
                            <?php echo $c->pid === $preselectedPid ? 'checked' : ''; ?>>
                    </td>
                    <td><?php echo htmlspecialchars($c->firstName . ' ' . $c->lastName, ENT_QUOTES, 'UTF-8'); ?></td>
                    <td><?php echo htmlspecialchars($c->dob, ENT_QUOTES, 'UTF-8'); ?></td>
                    <td><?php echo htmlspecialchars($c->mrn ?? '—', ENT_QUOTES, 'UTF-8'); ?></td>
                    <td><?php echo number_format($c->score * 100, 0); ?>%</td>
                    <td><?php echo htmlspecialchars($c->matchReason, ENT_QUOTES, 'UTF-8'); ?></td>
                </tr>
            <?php endforeach; ?>
            <tr>
                <td>
                    <input type="radio" name="patient_choice" value="new"
                        <?php echo $preselectedPid === 0 ? 'checked' : ''; ?>>
                </td>
                <td colspan="5"><strong>Create new patient</strong> using the extracted demographics</td>
            </tr>
        </tbody>
    </table>
    <?php else: ?>
    <h2>1. Pick the patient</h2>
    <div class="alert-info">
        No demographics were extracted from this document, so no patient match was attempted.
        The document will stay in the unassigned-uploads bucket until you attach it manually.
    </div>
    <input type="hidden" name="patient_choice" value="unassigned">
    <?php endif; ?>

    <?php if ($availableWriteSections !== []): ?>
    <h2>2. Write extracted data to chart sections</h2>
    <p class="meta">
        Each ticked section gets written to the corresponding chart
        card when you confirm. Untick a section to skip writing it
        (the source document is still attached either way). Greyed-
        out checkboxes have no extracted data to write.
    </p>
    <ul class="write-sections">
        <?php foreach ($availableWriteSections as $section): ?>
            <li>
                <label>
                    <input type="checkbox"
                        name="write_sections[]"
                        value="<?php echo htmlspecialchars($section['id'], ENT_QUOTES, 'UTF-8'); ?>"
                        <?php echo $section['populated'] ? 'checked' : 'disabled'; ?>>
                    <?php echo htmlspecialchars($section['label'], ENT_QUOTES, 'UTF-8'); ?>
                    <?php if (!$section['populated']): ?>
                        <span class="empty-hint">(no data extracted)</span>
                    <?php endif; ?>
                </label>
            </li>
        <?php endforeach; ?>
    </ul>
    <?php endif; ?>

    <h2><?php echo $availableWriteSections !== [] ? '3' : '2'; ?>. Review &amp; edit extracted facts</h2>
    <p class="meta">
        Edit any field that the extractor got wrong. The 📎 icon shows
        the source citation the value was pulled from. Yellow badges
        mark fields the extractor abstained on — fill them in if you
        have the answer; leave them blank to keep the abstention.
    </p>
    <?php echo FactsFormHelper::renderFacts($factsInner, '', ''); ?>

    <div class="actions">
        <button type="submit">Save edits, write to chart &amp; attach</button>
        <a class="secondary" href="<?php echo htmlspecialchars($webroot . '/interface/copilot/upload_document.php', ENT_QUOTES, 'UTF-8'); ?>">
            Upload another document
        </a>
    </div>

    <?php if ($facts !== null): ?>
        <details class="raw-json"><summary>View raw extracted facts (debug)</summary>
            <pre><?php echo htmlspecialchars(
                (string) json_encode($facts, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES),
                ENT_QUOTES,
                'UTF-8',
                 ); ?></pre>
        </details>
    <?php endif; ?>
</form>
<?php endif; ?>

</body>
</html>
