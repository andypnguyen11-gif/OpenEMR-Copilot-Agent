<?php

/**
 * Clinical Co-Pilot — universal document upload entrypoint
 * (Week 2 multimodal expansion, Step 1).
 *
 * Replaces the per-type ``upload_lab.php`` / ``upload_intake.php`` for
 * the new multi-format workflow. A clinician (or front-desk uploader)
 * picks any supported file — PDF, scanned image, .docx referral,
 * .xlsx workbook, .tiff fax packet, .hl7 ADT/ORU stream — and the
 * classifier routes the bytes to the right extractor on the agent
 * service. The original ``upload_lab.php`` and ``upload_intake.php``
 * still work for back-compat (chart side-panel deep-links).
 *
 * Patient routing: when ``pid`` is in the URL, the uploaded document
 * is associated with that chart on success. When ``pid`` is absent,
 * the agent extracts demographics and the review page (Step 4) calls
 * the patient resolver to suggest an existing match or trigger the
 * new-patient workflow. Either way, no DB writes happen until the
 * clinician confirms on ``document_review.php``.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

// Recover ``$_GET['site']`` from HTTP_HOST when the iframe lands here
// without site_id in the session — see _site_recovery.php for context.
// Must run BEFORE globals.php so its die("Site ID is missing...") branch
// never fires.
require_once(__DIR__ . "/_site_recovery.php");

require_once(__DIR__ . "/../globals.php");
require_once(__DIR__ . "/../../library/documents.php");

use GuzzleHttp\Client as GuzzleClient;
use GuzzleHttp\Psr7\HttpFactory;
use OpenEMR\BC\ServiceContainer;
use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Core\Header;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\AgentServiceException;
use OpenEMR\Services\Copilot\ClassifierException;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use OpenEMR\Services\Copilot\DocumentClassifier;
use OpenEMR\Services\Copilot\IngestClient;
use Symfony\Component\HttpFoundation\File\UploadedFile;
use Symfony\Component\HttpFoundation\Request;

if (!AclMain::aclCheckCore('patients', 'med')) {
    http_response_code(403);
    exit('forbidden');
}

// Patient context is optional — universal upload handles both
// chart-attached uploads and standalone front-desk uploads.
$pidParam = filter_input(INPUT_GET, 'pid');
$pid = (is_string($pidParam) && ctype_digit($pidParam)) ? (int) $pidParam : 0;

$globals = OEGlobalsBag::getInstance();
$webrootRaw = $globals->get('webroot', '');
$webroot = is_string($webrootRaw) ? $webrootRaw : '';

$session = SessionWrapperFactory::getInstance()->getActiveSession();
$authUserIdRaw = $session->get('authUserID');
$authUserId = is_int($authUserIdRaw) ? $authUserIdRaw
    : (is_numeric($authUserIdRaw) ? (int) $authUserIdRaw : 0);

$request = Request::createFromGlobals();
$errorMessage = '';
$detectedType = '';
/** @var list<array<string,mixed>> $duplicateMatches */
$duplicateMatches = [];

if ($request->isMethod('POST')) {
    CsrfUtils::checkCsrfInput(INPUT_POST, session: $session, dieOnFail: true);

    $upload = $request->files->get('document_file');
    $hintRaw = $request->request->get('type_hint', DocumentClassifier::HINT_AUTO);
    $hint = is_string($hintRaw) ? $hintRaw : DocumentClassifier::HINT_AUTO;
    // The duplicate-detection short-circuit below renders an alert and
    // asks the user to re-select the file with this hidden checkbox set,
    // signalling "I know it's a duplicate and want a separate copy."
    $forceDupRaw = $request->request->get('force_duplicate', '');
    $forceDuplicate = is_string($forceDupRaw) && $forceDupRaw === '1';

    if (!$upload instanceof UploadedFile) {
        $errorMessage = 'No file selected.';
    } elseif ($upload->getError() !== UPLOAD_ERR_OK) {
        $errorMessage = 'Upload failed (error code ' . $upload->getError() . ').';
    } else {
        $tmpName = (string) $upload->getRealPath();
        $origName = $upload->getClientOriginalName();
        $mimeType = $upload->getClientMimeType();
        $size = (int) $upload->getSize();

        // Read a head sample for classification BEFORE addNewDocument
        // (which moves the temp file). Then read full bytes for the
        // ingest payload — same reason as upload_lab.php: addNewDocument
        // applies optional encryption that would corrupt the extractor's
        // input if we re-read from $stored['url'].
        $head = (string) file_get_contents($tmpName, false, null, 0, 1024);
        $fileBytes = (string) file_get_contents($tmpName);

        // Duplicate-file short-circuit: addNewDocument hashes uploads
        // with sha3-512 and stores the hex in documents.hash. If we've
        // seen this exact byte sequence before, surface the prior
        // upload(s) instead of silently re-extracting and stacking
        // duplicate chart writes when the user confirms on the review
        // screen. The clinician can override by re-selecting the file
        // with the "upload anyway" checkbox ticked.
        //
        // ``foreign_id > 0`` filter is the "committed uploads only"
        // gate: we ignore orphans where the user uploaded but never
        // clicked Save on the review page (foreign_id stays at "00").
        // Otherwise an abandoned upload would block the user from ever
        // re-trying the same file, and they'd hit a dup alert that
        // blocks them from finishing the workflow they originally
        // started. Committed = attached to a real chart = the only
        // state where re-uploading would actually duplicate clinical
        // writes.
        if (!$forceDuplicate) {
            $fileHashHex = hash('sha3-512', $fileBytes);
            $duplicateMatches = QueryUtils::fetchRecords(
                'SELECT d.id, d.foreign_id, d.name, d.date,
                        pd.fname, pd.lname, pd.pubpid
                   FROM documents d
                   LEFT JOIN patient_data pd
                          ON pd.pid = d.foreign_id
                  WHERE d.hash = ?
                    AND d.foreign_id IS NOT NULL
                    AND d.foreign_id > 0
                    AND (d.deleted IS NULL OR d.deleted = 0)
                  ORDER BY d.id DESC
                  LIMIT 5',
                [$fileHashHex],
            );
            if ($duplicateMatches !== []) {
                ServiceContainer::getLogger()->info('copilot.upload_document.duplicate_detected', [
                    'origName' => $origName,
                    'priorIds' => array_map(
                        static function (array $row): int {
                            $id = $row['id'] ?? null;
                            return is_int($id) ? $id : (is_numeric($id) ? (int) $id : 0);
                        },
                        $duplicateMatches,
                    ),
                ]);
            }
        }

        // When a duplicate is detected, fall through to the page render
        // without calling addNewDocument or the agent ingest — the form
        // below will surface the prior upload(s) and offer "Upload as a
        // separate copy" / link to the existing chart.
        if ($duplicateMatches === []) {
            try {
                $documentType = DocumentClassifier::classify($origName, $mimeType, $head, $hint);
                $detectedType = $documentType;
            } catch (ClassifierException $e) {
                $errorMessage = 'Could not determine document type: ' . $e->getMessage();
                $documentType = '';
            }
        } else {
            // Avoid the "if ($documentType !== '')" branch below — there's
            // no document type to act on when we're rendering the dup alert.
            $documentType = '';
        }

        if ($documentType !== '') {
            // Patient-id is required by addNewDocument's foreign_id; for
            // chart-less uploads we use the synthetic placeholder ``00``
            // — the directory the documents subsystem reserves for
            // unassigned uploads (same convention the existing
            // ``new_patient_with_ai.php`` uses). The patient-resolver
            // attach step rewrites foreign_id from "00" to the matched
            // pid when the clinician confirms.
            $foreignId = $pid > 0 ? (string) $pid : '00';
            $stored = addNewDocument(
                $origName,
                $mimeType,
                $tmpName,
                (string) $upload->getError(),
                (string) $size,
                $authUserId,
                $foreignId,
                DocumentClassifier::categoryFor($documentType),
            );

            if (!is_array($stored)) {
                // Surface whatever addNewDocument actually returned so a
                // failure isn't a black box. Common cause: foreign_id
                // refers to a row that doesn't exist (the documents
                // subsystem reserves "00" for unassigned uploads, but
                // any other "no patient" placeholder fails).
                $rendered = is_scalar($stored) ? (string) $stored : gettype($stored);
                $errorMessage = sprintf(
                    'Failed to save document into OpenEMR. addNewDocument returned %s '
                        . '(foreign_id=%s, category_id=%d). Check apache error log for details.',
                    $rendered === '' ? '<empty string>' : $rendered,
                    $foreignId,
                    DocumentClassifier::categoryFor($documentType),
                );
                ServiceContainer::getLogger()->error('copilot.upload_document.addNewDocument_failed', [
                    'origName' => $origName,
                    'mime' => $mimeType,
                    'docType' => $documentType,
                    'foreignId' => $foreignId,
                    'catId' => DocumentClassifier::categoryFor($documentType),
                    'size' => $size,
                    'returned' => $rendered,
                ]);
            } else {
                $docIdRaw = $stored['doc_id'] ?? '';
                $documentId = 'openemr:doc:' . (is_scalar($docIdRaw) ? (string) $docIdRaw : '');

                $config = new CopilotConfig($globals);
                $factory = new HttpFactory();
                // Same timeout reasoning as upload_lab.php: VLM extraction
                // can run 30s on a multi-page intake; pad well above the
                // agent service's own p95 budget so transport never trips
                // before the model.
                $httpClient = new GuzzleClient([
                    'timeout' => max($config->getAgentTimeoutSeconds() * 24, 120),
                    'http_errors' => false,
                ]);
                $agentClient = new AgentHttpClient($httpClient, $factory, $config);
                $ingestClient = new IngestClient($agentClient, $config);

                try {
                    $response = $ingestClient->ingestTyped(
                        $documentId,
                        $documentType,
                        $pid > 0 ? $pid : null,
                        $authUserId,
                        $fileBytes,
                        $origName,
                        $mimeType,
                    );
                    if ($response->statusCode === 200) {
                        $reviewParams = [
                            'document_id' => $documentId,
                            'document_type' => $documentType,
                        ];
                        if ($pid > 0) {
                            $reviewParams['pid'] = $pid;
                        }
                        header('Location: ' . $webroot . '/interface/copilot/document_review.php?'
                            . http_build_query($reviewParams));
                        exit;
                    }
                    $errorMessage = sprintf(
                        'Extraction failed (status %d) for type "%s". The document is saved; you can retry.',
                        $response->statusCode,
                        $documentType,
                    );
                } catch (AgentServiceException $e) {
                    $errorMessage = 'Could not reach the extraction service: ' . $e->getMessage();
                }
            }
        }
    }
}

$csrfToken = CsrfUtils::collectCsrfToken(session: $session);

Header::setupHeader();
?>
<!DOCTYPE html>
<html>
<head>
    <title>Upload Clinical Document</title>
    <style>
        body { font-family: system-ui, sans-serif; padding: 2rem; max-width: 720px; }
        h1 { margin-top: 0; }
        .alert-error { background: #fee; border: 1px solid #faa; padding: 0.75rem 1rem; margin: 1rem 0; }
        .alert-warn {
            background: #fff8e6; border: 1px solid #e6c46a; padding: 0.9rem 1.1rem;
            margin: 1rem 0; border-radius: 4px; color: #5a4400;
        }
        .alert-warn strong { font-size: 1.05em; color: #5a4400; }
        .alert-warn ul { margin: 0.5rem 0 0.75rem; padding-left: 1.4rem; }
        .alert-warn li { padding: 0.15rem 0; }
        .alert-warn li code { background: #f4ecd0; padding: 0.05em 0.3em; border-radius: 2px; }
        .alert-warn .dup-actions { margin-top: 0.6rem; }
        .alert-warn .dup-actions a {
            display: inline-block; padding: 0.4rem 0.9rem; background: #2057a8;
            color: white; text-decoration: none; border-radius: 3px; margin-right: 0.4rem;
            font-size: 0.95em;
        }
        .alert-warn .dup-actions a.secondary { background: #e0e0e0; color: #333; }
        .form-group { margin: 1rem 0; }
        label { display: block; margin-bottom: 0.4rem; font-weight: 600; }
        select, input[type=file] { width: 100%; padding: 0.4rem; }
        button { padding: 0.5rem 1rem; }
        button[disabled] { opacity: 0.6; cursor: progress; }
        .hint { color: #666; font-size: 0.9em; margin-top: 0.4rem; }
        .types-table { width: 100%; border-collapse: collapse; margin: 1rem 0; font-size: 0.9em; }
        .types-table th, .types-table td { text-align: left; padding: 0.3rem 0.5rem; border-bottom: 1px solid #eee; }
        .copilot-loading { display: none; margin-top: 1rem; padding: 0.75rem 1rem; background: #eef5ff; border: 1px solid #b6d2ff; border-radius: 4px; color: #154f9c; }
        .copilot-loading[data-active="true"] { display: block; }
        .copilot-spinner { display: inline-block; width: 1em; height: 1em; vertical-align: -0.15em; margin-right: 0.5em; border: 2px solid #b6d2ff; border-top-color: #154f9c; border-radius: 50%; animation: copilot-spin 0.8s linear infinite; }
        @keyframes copilot-spin { to { transform: rotate(360deg); } }
    </style>
</head>
<body>
<h1>Upload Clinical Document</h1>
<p>
    <?php if ($pid > 0): ?>
        Uploading for patient pid <?php echo $pid; ?>.
    <?php else: ?>
        No patient selected — the agent will extract demographics and you can
        match or create a chart on the review screen.
    <?php endif; ?>
    Supported formats:
</p>
<table class="types-table">
    <thead><tr><th>Format</th><th>Routes to</th></tr></thead>
    <tbody>
        <tr><td>PDF / PNG / JPG</td><td>Lab report (default) or intake form (with hint below)</td></tr>
        <tr><td>.docx</td><td>Referral letter</td></tr>
        <tr><td>.xlsx</td><td>Patient workbook</td></tr>
        <tr><td>.tiff / .tif</td><td>Multi-page fax packet</td></tr>
        <tr><td>.hl7 (ORU)</td><td>Lab results (HL7 v2 ORU-R01)</td></tr>
        <tr><td>.hl7 (ADT)</td><td>Demographics update (HL7 v2 ADT-A08)</td></tr>
    </tbody>
</table>

<?php if ($errorMessage !== ''): ?>
    <div class="alert-error"><?php echo htmlspecialchars($errorMessage, ENT_QUOTES, 'UTF-8'); ?></div>
<?php endif; ?>

<?php if ($duplicateMatches !== []): ?>
    <div class="alert-warn">
        <strong>Possible duplicate upload</strong>
        <p style="margin: 0.4rem 0;">
            The file you just selected has the same SHA-3-512 hash as
            <?php echo count($duplicateMatches) === 1 ? 'a prior upload' : count($duplicateMatches) . ' prior uploads'; ?>
            in this site. Saving it again would re-extract and may stack
            duplicate chart writes when you confirm on the review screen.
        </p>
        <ul>
            <?php foreach ($duplicateMatches as $dup): ?>
                <?php
                $dupIdRaw = $dup['id'] ?? null;
                $dupId = is_int($dupIdRaw) ? $dupIdRaw : (is_numeric($dupIdRaw) ? (int) $dupIdRaw : 0);
                $dupName = is_string($dup['name'] ?? null) ? $dup['name'] : '(unnamed)';
                $dupDate = is_string($dup['date'] ?? null) ? $dup['date'] : '';
                $dupForeignIdRaw = $dup['foreign_id'] ?? null;
                $dupForeignId = is_int($dupForeignIdRaw)
                    ? $dupForeignIdRaw
                    : (is_numeric($dupForeignIdRaw) ? (int) $dupForeignIdRaw : 0);
                $dupFname = is_string($dup['fname'] ?? null) ? $dup['fname'] : '';
                $dupLname = is_string($dup['lname'] ?? null) ? $dup['lname'] : '';
                $dupPubpidRaw = $dup['pubpid'] ?? null;
                $dupPubpid = is_string($dupPubpidRaw)
                    ? $dupPubpidRaw
                    : (is_int($dupPubpidRaw) || is_float($dupPubpidRaw) ? (string) $dupPubpidRaw : '');
                $dupPatient = trim($dupFname . ' ' . $dupLname);
                ?>
                <li>
                    <code><?php echo htmlspecialchars($dupName, ENT_QUOTES, 'UTF-8'); ?></code>
                    (id <?php echo $dupId; ?>)
                    <?php if ($dupDate !== ''): ?>
                        — uploaded <?php echo htmlspecialchars($dupDate, ENT_QUOTES, 'UTF-8'); ?>
                    <?php endif; ?>
                    <?php if ($dupForeignId > 0 && $dupPatient !== ''): ?>
                        — attached to <strong><?php echo htmlspecialchars($dupPatient, ENT_QUOTES, 'UTF-8'); ?></strong>
                        (pid <?php echo $dupForeignId; ?><?php
                        if ($dupPubpid !== '' && $dupPubpid !== (string) $dupForeignId) {
                            echo ', MRN ' . htmlspecialchars($dupPubpid, ENT_QUOTES, 'UTF-8');
                        } ?>)
                    <?php elseif ($dupForeignId > 0): ?>
                        — attached to pid <?php echo $dupForeignId; ?>
                    <?php else: ?>
                        — not yet attached to a chart
                    <?php endif; ?>
                </li>
            <?php endforeach; ?>
        </ul>
        <div class="dup-actions">
            <?php
            $firstDup = $duplicateMatches[0];
            $firstDupForeignIdRaw = $firstDup['foreign_id'] ?? null;
            $firstDupForeignId = is_int($firstDupForeignIdRaw)
                ? $firstDupForeignIdRaw
                : (is_numeric($firstDupForeignIdRaw) ? (int) $firstDupForeignIdRaw : 0);
            ?>
            <?php if ($firstDupForeignId > 0): ?>
                <a href="<?php echo htmlspecialchars(
                    $webroot . '/interface/patient_file/summary/demographics.php?'
                        . http_build_query(['set_pid' => $firstDupForeignId]),
                    ENT_QUOTES,
                    'UTF-8',
                         ); ?>">Open existing chart</a>
            <?php endif; ?>
            <a class="secondary" href="<?php echo htmlspecialchars(
                $webroot . '/interface/copilot/upload_document.php',
                ENT_QUOTES,
                'UTF-8',
                                       ); ?>">Cancel</a>
        </div>
        <p style="margin: 0.8rem 0 0; font-size: 0.9em;">
            To upload it as a separate copy anyway, re-select the file below
            and tick <em>Upload as a separate copy (I know it&rsquo;s a duplicate)</em>.
        </p>
    </div>
<?php endif; ?>

<form method="post" enctype="multipart/form-data" data-copilot-form>
    <input type="hidden" name="csrf_token_form" value="<?php echo htmlspecialchars($csrfToken, ENT_QUOTES, 'UTF-8'); ?>">
    <div class="form-group">
        <label for="document_file">Document file</label>
        <input
            type="file"
            name="document_file"
            id="document_file"
            accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,.docx,.xlsx,.hl7,.txt"
            required>
        <div class="hint">Max ~10MB. Extraction takes 5–60s depending on length and format.</div>
        <div class="hint" data-detected-type role="status" aria-live="polite"></div>
    </div>
    <div class="form-group" data-type-hint-group>
        <label for="type_hint">Type hint <span class="hint">(only used when the file is a PDF, PNG, or JPG — every other format is auto-detected from the file)</span></label>
        <select name="type_hint" id="type_hint">
            <option value="<?php echo DocumentClassifier::HINT_AUTO; ?>">Auto-detect</option>
            <option value="<?php echo DocumentClassifier::HINT_LAB; ?>">Force lab report</option>
            <option value="<?php echo DocumentClassifier::HINT_INTAKE; ?>">Force intake form</option>
        </select>
    </div>
    <?php if ($duplicateMatches !== []): ?>
        <div class="form-group">
            <label style="font-weight: 500;">
                <input type="checkbox" name="force_duplicate" value="1">
                Upload as a separate copy (I know it&rsquo;s a duplicate)
            </label>
            <div class="hint">
                Without this ticked, re-uploading the same file will keep
                landing on this duplicate alert.
            </div>
        </div>
    <?php endif; ?>
    <div class="form-group">
        <button type="submit" data-copilot-submit>Upload and extract</button>
    </div>
    <div class="copilot-loading" data-copilot-loading>
        <span class="copilot-spinner" aria-hidden="true"></span>
        Extracting&hellip; this can take 10&ndash;60 seconds depending on document length.
        Please don&rsquo;t close this tab.
    </div>
</form>

<script>
(function () {
    var form = document.querySelector('form[data-copilot-form]');
    if (!form) { return; }

    // Mirror of the PHP-side classifier's extension → display-name map.
    // The hint dropdown is only useful for the AMBIGUOUS formats (PDF /
    // PNG / JPG); every other extension drives the document_type
    // deterministically from the extension alone, so we hide the hint
    // for those and surface a "Detected: ..." preview instead.
    var EXT_LABELS = {
        'pdf':  { label: 'PDF — defaults to lab report; switch the hint below to upload as an intake form', ambiguous: true },
        'png':  { label: 'PNG image — defaults to lab report; switch the hint below to upload as an intake form', ambiguous: true },
        'jpg':  { label: 'JPG image — defaults to lab report; switch the hint below to upload as an intake form', ambiguous: true },
        'jpeg': { label: 'JPG image — defaults to lab report; switch the hint below to upload as an intake form', ambiguous: true },
        'tif':  { label: 'Detected: multi-page fax packet (TIFF)', ambiguous: false },
        'tiff': { label: 'Detected: multi-page fax packet (TIFF)', ambiguous: false },
        'docx': { label: 'Detected: referral letter (DOCX)', ambiguous: false },
        'xlsx': { label: 'Detected: patient workbook (XLSX)', ambiguous: false },
        'hl7':  { label: 'Detected: HL7 v2 message (ORU vs ADT decided after upload)', ambiguous: false },
        'txt':  { label: 'Plain text — will be classified after upload (likely HL7 if it starts with MSH|)', ambiguous: false }
    };

    var fileInput = form.querySelector('#document_file');
    var detectedNode = form.querySelector('[data-detected-type]');
    var hintGroup = form.querySelector('[data-type-hint-group]');

    function updateDetected() {
        if (!fileInput || !detectedNode || !hintGroup) { return; }
        var name = fileInput.value || '';
        var dot = name.lastIndexOf('.');
        var ext = dot >= 0 ? name.slice(dot + 1).toLowerCase() : '';
        var info = EXT_LABELS[ext];
        if (!ext) {
            detectedNode.textContent = '';
            hintGroup.style.display = '';
            return;
        }
        if (!info) {
            detectedNode.textContent = 'Unknown extension — the server will reject this file.';
            hintGroup.style.display = '';
            return;
        }
        detectedNode.textContent = info.label;
        // Hide the hint dropdown for unambiguous extensions so the user
        // doesn't misread "Auto-detect" as something that overrides the
        // file's own format.
        hintGroup.style.display = info.ambiguous ? '' : 'none';
    }

    if (fileInput) {
        fileInput.addEventListener('change', updateDetected);
    }
    updateDetected();

    form.addEventListener('submit', function () {
        var btn = form.querySelector('[data-copilot-submit]');
        var loading = form.querySelector('[data-copilot-loading]');
        // Note: do NOT disable the file input — disabled inputs are
        // excluded from form serialization, which would strip the
        // upload from the multipart body and trigger "No file selected".
        if (btn) { btn.disabled = true; btn.textContent = 'Extracting…'; }
        if (loading) { loading.setAttribute('data-active', 'true'); }
    });
})();
</script>
</body>
</html>
