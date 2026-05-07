<?php

/**
 * Clinical Co-Pilot — editable-confirm save handler
 * (Week 2 multimodal expansion).
 *
 * Receives the POSTed form from ``document_review.php`` and:
 *   1. PUTs the edited facts to ``/api/agent/internal/extracted/{id}``
 *      so the agent service's facts store reflects what the clinician
 *      confirmed.
 *   2. Either updates ``documents.foreign_id`` to the picked patient
 *      pid (existing-chart match path) OR redirects to the new-patient
 *      form pre-populated with the extracted demographics
 *      (create-new path).
 *   3. Redirects to the patient's chart so the clinician sees the
 *      freshly-attached doc on the chart.
 *
 * The "edited facts" arrive as an HTML-form-nested ``facts`` tree
 * mirroring the extracted-facts JSON (each leaf has only the
 * ``[value]`` key set; ``citation`` and ``abstain_reason`` are not in
 * the form payload because we don't let the clinician edit those).
 * The handler reads the original facts back from the agent service and
 * overlays the edits via {@see FactsFormHelper::overlayEdits()} so the
 * PUT route receives a body that still satisfies the typed-union
 * validator.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

require_once(__DIR__ . "/../../globals.php");

use GuzzleHttp\Client as GuzzleClient;
use GuzzleHttp\Psr7\HttpFactory;
use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\AgentServiceException;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use OpenEMR\Services\Copilot\Documents\FactsFormHelper;
use Symfony\Component\HttpFoundation\Request;

if (!AclMain::aclCheckCore('patients', 'demo')) {
    http_response_code(403);
    exit('forbidden');
}

$session = SessionWrapperFactory::getInstance()->getActiveSession();
CsrfUtils::checkCsrfInput(INPUT_POST, session: $session, dieOnFail: true);

$request = Request::createFromGlobals();
$documentIdRaw = $request->request->get('document_id');
$documentTypeRaw = $request->request->get('document_type');
$patientChoiceRaw = $request->request->get('patient_choice', 'unassigned');
$editedFactsForm = $request->request->all('facts');

$documentId = is_string($documentIdRaw) ? $documentIdRaw : '';
$documentType = is_string($documentTypeRaw) ? $documentTypeRaw : '';
$patientChoice = is_string($patientChoiceRaw) ? $patientChoiceRaw : 'unassigned';

if ($documentId === '' || $documentType === '') {
    http_response_code(400);
    exit('missing document_id or document_type');
}

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

// Step 1: read the original facts so we can overlay the form's
// value-only edits on top of the full ExtractedField shape.
// Without this, sending only the edited values to the PUT route
// would fail validation (ExtractedField.value_xor_abstain).
$readErrorMessage = '';
$readResponse = null;
try {
    $readResponse = $agentClient->getInternal(
        '/api/agent/internal/extracted/' . rawurlencode($documentId),
        $config->getInternalToken(),
    );
} catch (AgentServiceException $e) {
    $readErrorMessage = $e->getMessage();
}
if ($readErrorMessage !== '') {
    http_response_code(502);
    exit('Could not reach extractor: ' . htmlspecialchars($readErrorMessage, ENT_QUOTES, 'UTF-8'));
}
if ($readResponse === null || $readResponse->statusCode !== 200) {
    http_response_code(502);
    $statusForMessage = $readResponse !== null ? $readResponse->statusCode : 0;
    exit('Could not load original facts (status ' . $statusForMessage . ')');
}
// The GET ``/extracted/{id}`` route returns the raw Pydantic dump
// directly (no IngestResponse wrapper) — same convention the
// existing lab_review / intake_review pages follow. So the response
// body IS the facts dict, and the per-type Pydantic model layout is
// already at top level.
// AgentResponse->body is typed array<string, mixed>, but the per-doc
// facts dump may also be an empty array on a corrupted record;
// either way we just hand it to the overlay.
$originalFacts = $readResponse->body;

$mergedFacts = FactsFormHelper::overlayEdits($originalFacts, $editedFactsForm);

if (!is_array($mergedFacts)) {
    http_response_code(500);
    exit('overlayEdits returned a non-array root — refusing to write');
}
// The validator body is the merged dict itself — it already has
// document_id at the top from the original. Force-overwrite the
// document_id slot with the URL value so a stale or tampered body
// can't mismatch the path.
$validatorBody = $mergedFacts;
$validatorBody['document_id'] = $documentId;

// Step 2: PUT the merged facts back. Validation runs on the agent
// service; on a 422 we surface the message verbatim so the clinician
// can fix the offending field.
$writeErrorMessage = '';
$putResponse = null;
try {
    $putResponse = $agentClient->putInternalJson(
        '/api/agent/internal/extracted/' . rawurlencode($documentId),
        $validatorBody,
        $config->getInternalToken(),
    );
} catch (AgentServiceException $e) {
    $writeErrorMessage = $e->getMessage();
}
if ($writeErrorMessage !== '') {
    http_response_code(502);
    exit('Could not save edits: ' . htmlspecialchars($writeErrorMessage, ENT_QUOTES, 'UTF-8'));
}
if ($putResponse === null || $putResponse->statusCode !== 200) {
    http_response_code(502);
    $detail = '';
    if ($putResponse !== null && isset($putResponse->body['detail'])) {
        $detailRaw = $putResponse->body['detail'];
        if (is_string($detailRaw)) {
            $detail = $detailRaw;
        }
    }
    $statusForMessage = $putResponse !== null ? $putResponse->statusCode : 0;
    exit(sprintf(
        'Save failed (status %d): %s',
        $statusForMessage,
        htmlspecialchars($detail !== '' ? $detail : 'unknown', ENT_QUOTES, 'UTF-8'),
    ));
}

// Step 3: route based on the patient choice.
//   - numeric pid → flip documents.foreign_id from "00" to that pid,
//     redirect to the chart.
//   - "new" → forward to new_patient_with_ai.php which reuses the
//     facts to pre-populate the new-patient form.
//   - "unassigned" → keep documents.foreign_id at "00", redirect
//     back to the upload page.
if (ctype_digit($patientChoice) && (int) $patientChoice > 0) {
    $targetPid = (int) $patientChoice;
    // ``document_id`` from the agent service is "openemr:doc:<id>" —
    // strip the prefix to get the bare documents.id we need for the
    // SQL update.
    $bareDocId = str_starts_with($documentId, 'openemr:doc:')
        ? substr($documentId, strlen('openemr:doc:'))
        : $documentId;
    if (!ctype_digit($bareDocId)) {
        http_response_code(400);
        exit('document_id is not a numeric documents.id (got '
            . htmlspecialchars($bareDocId, ENT_QUOTES, 'UTF-8') . ')');
    }
    QueryUtils::sqlStatementThrowException(
        'UPDATE documents SET foreign_id = ? WHERE id = ?',
        [$targetPid, (int) $bareDocId],
    );
    header('Location: ' . $webroot . '/interface/main/main_screen.php?'
        . http_build_query(['set_pid' => $targetPid]));
    exit;
}

if ($patientChoice === 'new') {
    // Hand off to the existing new-patient form. It already knows how
    // to read the facts via document_id and pre-populate the create
    // form.
    header('Location: ' . $webroot . '/interface/copilot/new_patient_with_ai.php?'
        . http_build_query(['document_id' => $documentId]));
    exit;
}

// Unassigned: keep documents.foreign_id="00", redirect back to
// the upload page.
header('Location: ' . $webroot . '/interface/copilot/upload_document.php');
exit;
