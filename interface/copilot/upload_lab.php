<?php

/**
 * Clinical Co-Pilot — lab document upload entry point (PR W2-02).
 *
 * Exists-patient flow: a clinician on a chart picks a lab PDF / image,
 * which is saved into OpenEMR's documents table and forwarded to the
 * agent service for VLM extraction. On a successful 200 from the
 * extractor, the page redirects to ``lab_review.php`` where the
 * clinician confirms / edits the extracted observations before they
 * write to the patient's procedure_result rows.
 *
 * This page is the only OpenEMR-side entrypoint into the multimodal
 * lab pipeline. The W2-02 production design uses a Symfony
 * EventDispatcher listener bound to the documents-table write hook;
 * the page-driven flow here is the demo-cut equivalent and has the
 * same data shape (`documents` row + `procedure_*` rows on confirm).
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

require_once(__DIR__ . "/../globals.php");
require_once(__DIR__ . "/../../library/documents.php");

use GuzzleHttp\Client as GuzzleClient;
use GuzzleHttp\Psr7\HttpFactory;
use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Core\Header;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\AgentServiceException;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use OpenEMR\Services\Copilot\IngestClient;
use Symfony\Component\HttpFoundation\File\UploadedFile;
use Symfony\Component\HttpFoundation\Request;

if (!AclMain::aclCheckCore('patients', 'med')) {
    http_response_code(403);
    exit('forbidden');
}

$pidParam = filter_input(INPUT_GET, 'pid');
$pid = (is_string($pidParam) && ctype_digit($pidParam)) ? (int) $pidParam : 0;
if ($pid <= 0) {
    http_response_code(400);
    exit('missing patient id');
}

$globals = OEGlobalsBag::getInstance();
$webrootRaw = $globals->get('webroot', '');
$webroot = is_string($webrootRaw) ? $webrootRaw : '';

$session = SessionWrapperFactory::getInstance()->getActiveSession();
$authUserIdRaw = $session->get('authUserID');
$authUserId = is_int($authUserIdRaw) ? $authUserIdRaw
    : (is_numeric($authUserIdRaw) ? (int) $authUserIdRaw : 0);

$request = Request::createFromGlobals();
$errorMessage = '';

if ($request->isMethod('POST')) {
    CsrfUtils::checkCsrfInput(INPUT_POST, session: $session, dieOnFail: true);

    $upload = $request->files->get('lab_file');
    if (!$upload instanceof UploadedFile) {
        $errorMessage = 'No file selected.';
    } elseif ($upload->getError() !== UPLOAD_ERR_OK) {
        $errorMessage = 'Upload failed (error code ' . $upload->getError() . ').';
    } else {
        $tmpName = (string) $upload->getRealPath();
        $origName = $upload->getClientOriginalName();
        $mimeType = $upload->getClientMimeType();
        $size = (int) $upload->getSize();

        // Read the raw upload bytes BEFORE handing to addNewDocument,
        // because addNewDocument's move_uploaded_file() destroys the
        // temp file. Reading via $stored['url'] post-write would also
        // hit OpenEMR's optional document encryption, returning
        // ciphertext to the extractor.
        $fileBytes = (string) file_get_contents($tmpName);

        $stored = addNewDocument(
            $origName,
            $mimeType,
            $tmpName,
            (string) $upload->getError(),
            (string) $size,
            $authUserId,
            (string) $pid,
            1,
        );
        if (!is_array($stored)) {
            $errorMessage = 'Failed to save document into OpenEMR.';
        } else {
            $docIdRaw = $stored['doc_id'] ?? '';
            $documentId = 'openemr:doc:' . (is_scalar($docIdRaw) ? (string) $docIdRaw : '');

            // Step 2: send to agent-service for extraction.
            $config = new CopilotConfig($globals);
            $factory = new HttpFactory();
            // Vision extraction can run 30s on a 3-page intake; pad the
            // ingest timeout well above the agent-service's own 90s
            // p95 budget so transport never trips before the model.
            $httpClient = new GuzzleClient([
                'timeout' => max($config->getAgentTimeoutSeconds() * 24, 120),
                'http_errors' => false,
            ]);
            $agentClient = new AgentHttpClient($httpClient, $factory, $config);
            $ingestClient = new IngestClient($agentClient, $config);

            try {
                $response = $ingestClient->ingestLab(
                    $documentId,
                    $pid,
                    $authUserId,
                    $fileBytes,
                    $origName,
                    $mimeType,
                );
                if ($response->statusCode === 200) {
                    header('Location: ' . $webroot . '/interface/copilot/lab_review.php?'
                        . http_build_query([
                            'document_id' => $documentId,
                            'pid' => $pid,
                        ]));
                    exit;
                }
                $errorMessage = 'Extraction failed (status '
                    . $response->statusCode . '). The document is saved; you can retry.';
            } catch (AgentServiceException $e) {
                $errorMessage = 'Could not reach the extraction service: ' . $e->getMessage();
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
    <title>Upload Lab Document</title>
    <style>
        body { font-family: system-ui, sans-serif; padding: 2rem; max-width: 720px; }
        h1 { margin-top: 0; }
        .alert-error { background: #fee; border: 1px solid #faa; padding: 0.75rem 1rem; margin: 1rem 0; }
        .form-group { margin: 1rem 0; }
        label { display: block; margin-bottom: 0.4rem; font-weight: 600; }
        button { padding: 0.5rem 1rem; }
        button[disabled] { opacity: 0.6; cursor: progress; }
        .hint { color: #666; font-size: 0.9em; margin-top: 0.4rem; }
        .copilot-loading { display: none; margin-top: 1rem; padding: 0.75rem 1rem; background: #eef5ff; border: 1px solid #b6d2ff; border-radius: 4px; color: #154f9c; }
        .copilot-loading[data-active="true"] { display: block; }
        .copilot-spinner { display: inline-block; width: 1em; height: 1em; vertical-align: -0.15em; margin-right: 0.5em; border: 2px solid #b6d2ff; border-top-color: #154f9c; border-radius: 50%; animation: copilot-spin 0.8s linear infinite; }
        @keyframes copilot-spin { to { transform: rotate(360deg); } }
    </style>
</head>
<body>
<h1>Upload Lab Document</h1>
<p>Patient pid: <?php echo $pid; ?>. Lab PDFs and image scans (PNG / JPG)
are accepted. The agent will extract observations with citations; you'll
review and confirm before anything is written to the chart.</p>

<?php if ($errorMessage !== ''): ?>
    <div class="alert-error"><?php echo htmlspecialchars($errorMessage, ENT_QUOTES, 'UTF-8'); ?></div>
<?php endif; ?>

<form method="post" enctype="multipart/form-data" data-copilot-form>
    <input type="hidden" name="csrf_token_form" value="<?php echo htmlspecialchars($csrfToken, ENT_QUOTES, 'UTF-8'); ?>">
    <div class="form-group">
        <label for="lab_file">Lab PDF or scanned image</label>
        <input type="file" name="lab_file" id="lab_file" accept="application/pdf,image/*" required>
        <div class="hint">Max page count is 5; extraction takes ~10–30s depending on length.</div>
    </div>
    <div class="form-group">
        <button type="submit" data-copilot-submit>Upload and extract</button>
    </div>
    <div class="copilot-loading" data-copilot-loading>
        <span class="copilot-spinner" aria-hidden="true"></span>
        Extracting&hellip; this can take 10&ndash;30 seconds for a single-page lab.
        Please don&rsquo;t close this tab.
    </div>
</form>

<script>
(function () {
    var form = document.querySelector('form[data-copilot-form]');
    if (!form) { return; }
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
