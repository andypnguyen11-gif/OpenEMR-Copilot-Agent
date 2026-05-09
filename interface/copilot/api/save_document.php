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

require_once(__DIR__ . "/../_site_recovery.php");
require_once(__DIR__ . "/../../globals.php");

use GuzzleHttp\Client as GuzzleClient;
use GuzzleHttp\Psr7\HttpFactory;
use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\AgentServiceException;
use OpenEMR\Services\Copilot\ChartWrite\ChartWriteCoordinator;
use OpenEMR\Services\Copilot\ChartWrite\ChartWriteOrchestrator;
use OpenEMR\Services\Copilot\ChartWrite\ChartWriteService;
use OpenEMR\Services\Copilot\ChartWrite\SaveOutcome;
use OpenEMR\Services\Copilot\ChartWrite\SaveOutcomeKind;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use OpenEMR\Services\Copilot\DocumentClassifier;
use OpenEMR\Services\Copilot\Documents\FactsFormHelper;
use OpenEMR\Services\Copilot\PatientMatch\PatientMatchScorer;
use OpenEMR\Services\Copilot\PatientMatch\PatientMatchService;
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
$writeSectionsRaw = $request->request->all('write_sections');

$documentId = is_string($documentIdRaw) ? $documentIdRaw : '';
$documentType = is_string($documentTypeRaw) ? $documentTypeRaw : '';
$patientChoice = is_string($patientChoiceRaw) ? $patientChoiceRaw : 'unassigned';
// $writeSectionsRaw is the form's checked checkboxes — names like
// "allergies" / "medications" / "active_problems" / "care_gaps" /
// "lab_observations". Empty when no checkboxes were ticked.
/** @var list<non-empty-string> $checkedSections */
$checkedSections = array_values(array_filter(
    $writeSectionsRaw,
    static fn (mixed $entry): bool => is_string($entry) && $entry !== '',
));

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

// ``document_id`` from the agent service is "openemr:doc:<id>" — for any
// branch that needs to flip ``documents.foreign_id`` we need the bare
// numeric id. Compute once.
$bareDocId = str_starts_with($documentId, 'openemr:doc:')
    ? substr($documentId, strlen('openemr:doc:'))
    : $documentId;

$authUserIdRaw = $session->get('authUserID');
$authUserId = is_int($authUserIdRaw) ? $authUserIdRaw
    : (is_numeric($authUserIdRaw) ? (int) $authUserIdRaw : 0);

// The chart-write dispatcher is shared between the existing-patient
// and create-new-patient branches below — both write into the same
// lists/reminders/procedure tables, only the pid differs. Lifted into
// ChartWriteOrchestrator so the dispatch logic is unit-testable;
// {@see ChartWriteCoordinator} wraps it in the lock-acquire /
// chart-write / finalize-marker cycle that delivers idempotency
// against double-clicks and concurrent submits.
$chartWriteCoordinator = new ChartWriteCoordinator(
    new ChartWriteOrchestrator(new ChartWriteService($authUserId)),
);

/**
 * Build a save_success.php redirect URL from a {@see SaveOutcome}.
 * The success page picks the per-section counts back up via
 * ``count_<section>=N`` to render "wrote N rows to chart"; the
 * ``idempotent=1`` flag (only set on a replay) lets the page show a
 * "this document was already saved" hint instead of the first-time
 * confirmation.
 */
$successUrl = static function (
    string $webroot,
    SaveOutcome $outcome,
    string $documentType,
    string $documentId,
): string {
    $params = [
        'pid' => $outcome->pid,
        'created' => $outcome->patientCreated ? '1' : '0',
        'document_type' => $documentType,
        'document_id' => $documentId,
    ];
    if ($outcome->kind === SaveOutcomeKind::IdempotentReplay) {
        $params['idempotent'] = '1';
    }
    foreach ($outcome->counts as $section => $count) {
        $params['count_' . $section] = (string) $count;
    }
    return $webroot . '/interface/copilot/save_success.php?' . http_build_query($params);
};

/**
 * Map a {@see SaveOutcome} to an HTTP response. AcquiredAndWrote and
 * IdempotentReplay both redirect to the success page (the latter with
 * an extra ``idempotent=1`` flag); ConcurrentInFlight surfaces a 409;
 * DocumentNotFound surfaces a 404.
 */
$respondToOutcome = static function (
    SaveOutcome $outcome,
    string $webroot,
    string $documentType,
    string $documentId,
) use ($successUrl): never {
    switch ($outcome->kind) {
        case SaveOutcomeKind::AcquiredAndWrote:
        case SaveOutcomeKind::IdempotentReplay:
            header('Location: ' . $successUrl($webroot, $outcome, $documentType, $documentId));
            exit;
        case SaveOutcomeKind::ConcurrentInFlight:
            http_response_code(409);
            exit('Another save is already in progress for this document. '
                . 'Wait a moment and refresh to see the result.');
        case SaveOutcomeKind::DocumentNotFound:
            http_response_code(404);
            exit('document not found');
    }
};

// Step 3: route based on the patient choice.
//   - numeric pid → flip documents.foreign_id from "00" to that pid,
//     run chart-write, show success.
//   - "new"       → create a fresh chart from the extracted demographics,
//     update foreign_id, run chart-write, show success.
//   - "unassigned"→ keep documents.foreign_id at "00", redirect back to
//     the upload page (no patient context to show).

if (ctype_digit($patientChoice) && (int) $patientChoice > 0) {
    $targetPid = (int) $patientChoice;
    if (!ctype_digit($bareDocId)) {
        http_response_code(400);
        exit('document_id is not a numeric documents.id (got '
            . htmlspecialchars($bareDocId, ENT_QUOTES, 'UTF-8') . ')');
    }

    // The foreign_id flip happens inside the coordinator's transaction
    // (alongside the lock-acquire) so a concurrent submit can't get a
    // half-attached document with no chart-writes.
    $linkExistingPatient = static function (int $rowId) use ($targetPid): array {
        QueryUtils::sqlStatementThrowException(
            'UPDATE documents SET foreign_id = ? WHERE id = ?',
            [$targetPid, $rowId],
        );
        return ['pid' => $targetPid, 'created' => false];
    };

    $outcome = $chartWriteCoordinator->attemptSave(
        (int) $bareDocId,
        $documentId,
        $documentType,
        $checkedSections,
        $mergedFacts,
        $linkExistingPatient,
    );

    $respondToOutcome($outcome, $webroot, $documentType, $documentId);
}

if ($patientChoice === 'new') {
    // Idempotency recovery: a prior submit may have committed the
    // patient_data INSERT and ``UPDATE documents SET foreign_id = ?``
    // before the chart-write txn could land (e.g. PHP timeout, browser
    // close). On retry the form re-posts patient_choice=new, but the
    // document is already linked to a freshly-created chart. Reuse it
    // instead of MAX(pid)+1'ing again — and short-circuit past the
    // demographic-extraction + duplicate-patient guard, which would
    // otherwise block on the patient we just created on the prior
    // attempt.
    $existingForeignRow = ctype_digit($bareDocId)
        ? QueryUtils::querySingleRow(
            'SELECT foreign_id FROM documents WHERE id = ?',
            [(int) $bareDocId],
        )
        : false;
    $existingForeignPid = is_array($existingForeignRow)
        && isset($existingForeignRow['foreign_id'])
        && is_numeric($existingForeignRow['foreign_id'])
            ? (int) $existingForeignRow['foreign_id']
            : 0;

    if ($existingForeignPid > 0) {
        $resumePid = $existingForeignPid;
        $linkResumedPatient = static fn (int $rowId): array
            => ['pid' => $resumePid, 'created' => false];

        $outcome = $chartWriteCoordinator->attemptSave(
            (int) $bareDocId,
            $documentId,
            $documentType,
            $checkedSections,
            $mergedFacts,
            $linkResumedPatient,
        );

        $respondToOutcome($outcome, $webroot, $documentType, $documentId);
    }

    // Pull demographics out of the extracted facts. Each document type
    // has a different schema layout (workbook nests under ``patient.*``,
    // referrals expose ``patient_name``/``patient_dob`` flat at the top,
    // HL7 / fax follow the referral shape). Mirror the ``$extractDemographics``
    // closure in document_review.php so the matcher and the create path
    // see the same bytes.
    $valueOf = static fn (mixed $field): ?string =>
        is_array($field)
            && isset($field['value'])
            && is_string($field['value'])
            && $field['value'] !== ''
                ? $field['value']
                : null;
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

    $demoFirst = null;
    $demoLast = null;
    $demoDob = null;
    $demoSex = null;
    $demoMrn = null;
    $demoPhone = null;
    // The five fields below are best-effort: they come from the
    // workbook's wide Patient sheet and the referral's letter header.
    // For document types that don't carry them they stay null and the
    // INSERT lets the column default handle it.
    $demoPcp = null;       // free-text printed name, e.g. "Dr. Daniel Ortega, DO"
    $demoPcpNpi = null;    // 10-digit NPI when carried
    $demoAddress = null;   // free-text one-line address
    $demoInsurance = null; // free-text plan name(s)
    $demoMemberId = null;  // member id / policy number when carried

    if ($documentType === DocumentClassifier::TYPE_WORKBOOK_XLSX) {
        $patientBlock = $mergedFacts['patient'] ?? null;
        if (is_array($patientBlock)) {
            [$demoFirst, $demoLast] = $splitName($valueOf($patientBlock['name'] ?? null));
            $demoDob = $valueOf($patientBlock['dob'] ?? null);
            $demoSex = $valueOf($patientBlock['sex'] ?? null);
            $demoMrn = $valueOf($patientBlock['mrn'] ?? null);
            $demoPhone = $valueOf($patientBlock['phone'] ?? null);
            $demoPcp = $valueOf($patientBlock['pcp'] ?? null);
            $demoPcpNpi = $valueOf($patientBlock['pcp_npi'] ?? null);
            $demoAddress = $valueOf($patientBlock['address'] ?? null);
            $demoInsurance = $valueOf($patientBlock['insurance'] ?? null);
            // member_id isn't in the agent's schema today — keep the
            // null so a future schema bump can drop the value in here
            // without touching the INSERT shape.
        }
    } elseif ($documentType === DocumentClassifier::TYPE_REFERRAL_DOCX) {
        [$demoFirst, $demoLast] = $splitName($valueOf($mergedFacts['patient_name'] ?? null));
        $demoDob = $valueOf($mergedFacts['patient_dob'] ?? null);
        $demoMrn = $valueOf($mergedFacts['patient_mrn'] ?? null);
        // For a referral letter the "PCP" is whoever wrote the letter —
        // i.e., the referring provider, who is by convention the
        // patient's current attending. Use that to back-fill providerID
        // / referrer on the new chart; recipient_provider is the
        // destination specialist, not a PCP.
        $demoPcp = $valueOf($mergedFacts['referring_provider'] ?? null);
        $demoPcpNpi = $valueOf($mergedFacts['referring_provider_npi'] ?? null);
    } elseif ($documentType === DocumentClassifier::TYPE_FAX_TIFF) {
        [$demoFirst, $demoLast] = $splitName($valueOf($mergedFacts['patient_name'] ?? null));
        $demoDob = $valueOf($mergedFacts['patient_dob'] ?? null);
    } elseif (
        $documentType === DocumentClassifier::TYPE_HL7_ORU
        || $documentType === DocumentClassifier::TYPE_HL7_ADT
    ) {
        [$demoFirst, $demoLast] = $splitName($valueOf($mergedFacts['patient_name'] ?? null));
        $demoDob = $valueOf($mergedFacts['patient_dob'] ?? null);
        $demoMrn = $valueOf($mergedFacts['patient_mrn'] ?? null);
    } elseif ($documentType === DocumentClassifier::TYPE_INTAKE_FORM) {
        // Intake forms have their own polished review surface
        // (``intake_review.php`` → ``new_patient_save_ai.php``) which
        // collects extra fields (chief complaint, family history, etc.)
        // that don't have a generic equivalent. Defer to it.
        header('Location: ' . $webroot . '/interface/copilot/intake_review.php?'
            . http_build_query(['document_id' => $documentId]));
        exit;
    }

    if (
        !is_string($demoFirst)
        || !is_string($demoLast)
        || !is_string($demoDob)
    ) {
        http_response_code(400);
        exit(htmlspecialchars(sprintf(
            'Cannot create a new patient from this %s — the extractor did not '
                . 'find first name, last name, and date of birth. Edit the demographics '
                . 'above and resubmit, or pick an existing patient match instead.',
            $documentType,
        ), ENT_QUOTES, 'UTF-8'));
    }

    $sexNormalized = match (strtolower($demoSex ?? '')) {
        'm', 'male' => 'Male',
        'f', 'female' => 'Female',
        'o', 'other' => 'Other',
        '' => 'Unknown',
        default => ucfirst(strtolower($demoSex ?? '')),
    };

    // Duplicate-patient guard. Re-run the same matcher document_review.php
    // uses for radio preselect, but enforce server-side: if the demographics
    // we're about to INSERT cross PRESELECT_THRESHOLD against an existing
    // chart, refuse to create unless the clinician has ticked the
    // ``force_create_duplicate`` confirmation. This catches the
    // "uploaded a PDF intake yesterday, uploading the workbook today,
    // misclicked Create new patient" duplicate without blocking the
    // legitimate twins case (same name + DOB but really two people)
    // where the clinician resubmits with the override.
    $forceCreateDupRaw = $request->request->get('force_create_duplicate', '');
    $forceCreateDup = is_string($forceCreateDupRaw) && $forceCreateDupRaw === '1';
    if (!$forceCreateDup) {
        $matchService = new PatientMatchService();
        $existingMatches = $matchService->match(
            $demoFirst,
            $demoLast,
            $demoDob,
            $demoMrn,
        );
        $blockingMatches = array_values(array_filter(
            $existingMatches,
            static fn ($candidate): bool => PatientMatchScorer::shouldPreselect($candidate->score),
        ));
        if ($blockingMatches !== []) {
            $session = SessionWrapperFactory::getInstance()->getActiveSession();
            $csrfTokenForRetry = CsrfUtils::collectCsrfToken(session: $session);
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
            display: flex; align-items: center; gap: 1rem;
        }
        ul.match-list .who { flex: 1; }
        ul.match-list .who strong { color: #154f9c; }
        ul.match-list code { background: #f4ecd0; padding: 0.05em 0.3em; border-radius: 2px; }
        button {
            padding: 0.45rem 1rem; border: 1px solid #2057a8; background: #2057a8;
            color: white; border-radius: 3px; font-size: 0.95em; cursor: pointer;
        }
        .force-create {
            margin-top: 1rem; padding-top: 1rem; border-top: 1px solid #eee;
            font-size: 0.95em;
        }
        .force-create button { background: #e0e0e0; color: #333; border-color: #999; }
        .force-create label { display: block; margin: 0.5rem 0; }
    </style>
</head>
<body>
<div class="alert-warn">
    <strong>This patient may already exist.</strong>
    <p style="margin: 0.5rem 0;">
        Demographics extracted from this <?php echo htmlspecialchars($documentType, ENT_QUOTES, 'UTF-8'); ?>
        — <strong><?php echo htmlspecialchars($demoFirst . ' ' . $demoLast, ENT_QUOTES, 'UTF-8'); ?></strong>,
        DOB <?php echo htmlspecialchars($demoDob, ENT_QUOTES, 'UTF-8'); ?> —
        match <?php echo count($blockingMatches) === 1 ? 'an existing chart' : count($blockingMatches) . ' existing charts'; ?>
        at high confidence. Creating a new chart now would duplicate the patient. Pick what to do:
    </p>
</div>

<h2>Existing charts that match</h2>
            <?php foreach ($blockingMatches as $candidate): ?>
    <ul class="match-list">
        <li>
            <span class="who">
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
            </span>
            <form method="post" action="<?php echo htmlspecialchars($webroot . '/interface/copilot/api/save_document.php', ENT_QUOTES, 'UTF-8'); ?>" style="margin: 0;">
                <input type="hidden" name="csrf_token_form" value="<?php echo htmlspecialchars($csrfTokenForRetry, ENT_QUOTES, 'UTF-8'); ?>">
                <input type="hidden" name="document_id" value="<?php echo htmlspecialchars($documentId, ENT_QUOTES, 'UTF-8'); ?>">
                <input type="hidden" name="document_type" value="<?php echo htmlspecialchars($documentType, ENT_QUOTES, 'UTF-8'); ?>">
                <input type="hidden" name="patient_choice" value="<?php echo $candidate->pid; ?>">
                <?php foreach ($checkedSections as $section): ?>
                    <input type="hidden" name="write_sections[]" value="<?php echo htmlspecialchars($section, ENT_QUOTES, 'UTF-8'); ?>">
                <?php endforeach; ?>
                <button type="submit">Attach to this chart</button>
            </form>
        </li>
    </ul>
            <?php endforeach; ?>

<div class="force-create">
    <p>
        Different person who happens to have the same name and date of birth
        (twins, etc.)? Force-create a separate chart:
    </p>
    <form method="post" action="<?php echo htmlspecialchars($webroot . '/interface/copilot/api/save_document.php', ENT_QUOTES, 'UTF-8'); ?>">
        <input type="hidden" name="csrf_token_form" value="<?php echo htmlspecialchars($csrfTokenForRetry, ENT_QUOTES, 'UTF-8'); ?>">
        <input type="hidden" name="document_id" value="<?php echo htmlspecialchars($documentId, ENT_QUOTES, 'UTF-8'); ?>">
        <input type="hidden" name="document_type" value="<?php echo htmlspecialchars($documentType, ENT_QUOTES, 'UTF-8'); ?>">
        <input type="hidden" name="patient_choice" value="new">
        <input type="hidden" name="force_create_duplicate" value="1">
            <?php foreach ($checkedSections as $section): ?>
            <input type="hidden" name="write_sections[]" value="<?php echo htmlspecialchars($section, ENT_QUOTES, 'UTF-8'); ?>">
        <?php endforeach; ?>
        <button type="submit">Create as a separate chart anyway</button>
    </form>
</div>
</body>
</html>
            <?php
            exit;
        }
    }

    // Patient-row insert mirrors ``new_patient_save_ai.php`` — see the
    // commentary there for why we INSERT directly instead of calling
    // ``newPatientData()`` (the helper trips STRICT_TRANS_TABLES on
    // first insert because it tries to UPDATE columns that don't yet
    // have a row).
    $srcdirRaw = $globals->get('srcdir', '');
    $srcdir = is_string($srcdirRaw) ? $srcdirRaw : '';
    require_once($srcdir . '/pid.inc.php');
    require_once($srcdir . '/patient.inc.php');

    $now = date('Y-m-d H:i:s');
    $today = date('Y-m-d');

    $pidRow = QueryUtils::querySingleRow('SELECT MAX(pid) + 1 AS pid FROM patient_data');
    $pidVal = is_array($pidRow) ? ($pidRow['pid'] ?? null) : null;
    $newpid = is_int($pidVal) ? $pidVal : (is_numeric($pidVal) ? (int) $pidVal : 1);
    if ($newpid <= 0) {
        $newpid = 1;
    }

    $pubpid = $demoMrn ?? (string) $newpid;
    $patientUuid = (new UuidRegistry(['table_name' => 'patient_data']))->createUuid();

    // Parse the workbook's one-line address into the four columns
    // ``patient_data`` carries. Address strings come in as
    // "<street>, <city>, <STATE> <ZIP>" — we split on the last comma
    // for the city/state-zip pair, then on the trailing whitespace for
    // state vs zip. Anything that doesn't match the regex falls through
    // with the entire string parked in ``street`` so no data is lost.
    $addrStreet = '';
    $addrCity = '';
    $addrState = '';
    $addrPostal = '';
    if (is_string($demoAddress) && trim($demoAddress) !== '') {
        $matches = [];
        // (street ...), (city), (STATE) (ZIP)
        if (preg_match(
            '/^\s*(.+?),\s*([^,]+?),\s*([A-Za-z]{2})\s+([\w\-]+)\s*$/u',
            $demoAddress,
            $matches,
        ) === 1) {
            $addrStreet = trim($matches[1]);
            $addrCity = trim($matches[2]);
            $addrState = strtoupper(trim($matches[3]));
            $addrPostal = trim($matches[4]);
        } else {
            $addrStreet = trim($demoAddress);
        }
    }

    // Resolve the PCP printed name → ``users.id`` so the chart's
    // primary-provider link is real (drives provider-aware sidebars,
    // billing dropdowns, etc.). NPI is the strong key — exact match
    // wins immediately. Fall back to (fname, lname) extracted from
    // the printed string with credentials stripped. Either way the
    // raw printed string lands in ``patient_data.referrer`` so a
    // miss doesn't lose the agent's attribution.
    $resolvedProviderId = null;
    $referrerText = is_string($demoPcp) ? trim($demoPcp) : '';
    if (is_string($demoPcpNpi) && preg_match('/^\d{10}$/', trim($demoPcpNpi)) === 1) {
        $row = QueryUtils::querySingleRow(
            'SELECT id FROM users WHERE npi = ? AND active = 1 LIMIT 1',
            [trim($demoPcpNpi)],
        );
        if (is_array($row) && isset($row['id']) && is_numeric($row['id'])) {
            $resolvedProviderId = (int) $row['id'];
        }
    }
    if ($resolvedProviderId === null && $referrerText !== '') {
        // Strip "Dr.", trailing ", MD" / ", DO" / etc., and take the
        // remaining tokens as fname + lname. "Dr. Daniel Ortega, DO" →
        // "Daniel Ortega" → fname="Daniel", lname="Ortega".
        $bare = preg_replace('/^\s*(?:Dr\.?|Prof\.?|Mr\.?|Mrs\.?|Ms\.?)\s+/iu', '', $referrerText) ?? $referrerText;
        $bare = preg_replace('/\s*,\s*(?:M\.?D\.?|D\.?O\.?|N\.?P\.?|P\.?A\.?|R\.?N\.?|F\.?N\.?P\.?|D\.?N\.?P\.?|Ph\.?D\.?).*$/iu', '', $bare) ?? $bare;
        $parts = preg_split('/\s+/', trim($bare)) ?: [];
        if (count($parts) >= 2) {
            $candidateFname = $parts[0];
            $candidateLname = end($parts);
            $row = QueryUtils::querySingleRow(
                'SELECT id FROM users WHERE fname = ? AND lname = ? AND active = 1 LIMIT 1',
                [$candidateFname, $candidateLname],
            );
            if (is_array($row) && isset($row['id']) && is_numeric($row['id'])) {
                $resolvedProviderId = (int) $row['id'];
            }
        }
    }

    QueryUtils::sqlStatementThrowException('START TRANSACTION');
    try {
        QueryUtils::sqlInsert(
            <<<'SQL'
            INSERT INTO patient_data SET
                pid = ?, uuid = ?, fname = ?, lname = ?, sex = ?, DOB = ?,
                phone_home = ?, pubpid = ?,
                street = ?, city = ?, state = ?, postal_code = ?,
                providerID = ?, referrer = ?,
                regdate = ?, date = ?, created_by = ?, updated_by = ?
            SQL,
            [
                $newpid, $patientUuid, $demoFirst, $demoLast, $sexNormalized, $demoDob,
                $demoPhone ?? '', $pubpid,
                $addrStreet, $addrCity, $addrState, $addrPostal,
                $resolvedProviderId, $referrerText,
                $today, $now, $authUserId, $authUserId,
            ],
        );

        setpid($newpid);
        if (function_exists('newEmployerData')) {
            newEmployerData((string) $newpid);
        }
        if (function_exists('newHistoryData')) {
            newHistoryData((string) $newpid);
        }

        // Insurance row. ``insurance_data`` accepts a thin record —
        // type+plan_name+pid is enough for the chart's insurance card
        // to surface "Medicare Part B + Aetna Medigap Plan G" against
        // the new pid. Policy/group numbers stay null for now (the
        // workbook schema doesn't extract member_id today; once it
        // does the value will land in ``policy_number``).
        if (is_string($demoInsurance) && trim($demoInsurance) !== '') {
            $insuranceUuid = (new UuidRegistry(['table_name' => 'insurance_data']))->createUuid();
            QueryUtils::sqlInsert(
                <<<'SQL'
                INSERT INTO insurance_data SET
                    uuid = ?, type = 'primary', plan_name = ?,
                    policy_number = ?, pid = ?, date = ?
                SQL,
                [
                    $insuranceUuid,
                    trim($demoInsurance),
                    // policy_number stays empty until the workbook schema
                    // adds member_id; ``$demoMemberId`` is always null
                    // today, so write '' rather than a dead branch.
                    '',
                    $newpid,
                    $today,
                ],
            );
        }

        if (ctype_digit($bareDocId)) {
            QueryUtils::sqlStatementThrowException(
                'UPDATE documents SET foreign_id = ? WHERE id = ?',
                [$newpid, (int) $bareDocId],
            );
        }

        QueryUtils::sqlStatementThrowException('COMMIT');
    } catch (\Throwable $exc) {
        QueryUtils::sqlStatementThrowException('ROLLBACK');
        throw $exc;
    }

    // Patient was just created; the coordinator's lock-acquire +
    // chart-write + finalize cycle runs in its own transaction so a
    // crash before COMMIT leaves no chart-side rows behind. The
    // recovery branch above handles re-submits that find ``foreign_id``
    // already pointing at the just-created chart.
    $linkNewPatient = static fn (int $rowId): array
        => ['pid' => $newpid, 'created' => true];

    $outcome = $chartWriteCoordinator->attemptSave(
        (int) $bareDocId,
        $documentId,
        $documentType,
        $checkedSections,
        $mergedFacts,
        $linkNewPatient,
    );

    $respondToOutcome($outcome, $webroot, $documentType, $documentId);
}

// Unassigned: keep documents.foreign_id="00", redirect back to
// the upload page.
header('Location: ' . $webroot . '/interface/copilot/upload_document.php');
exit;
