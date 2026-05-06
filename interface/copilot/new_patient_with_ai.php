<?php

/**
 * Clinical Co-Pilot — new patient intake upload (PR W2-02).
 *
 * Front-desk uploads a new-patient intake form. The form is saved into
 * OpenEMR's documents table under a synthetic patient id ("00", the
 * directory the documents subsystem reserves for unassigned uploads),
 * extracted by the agent service, and the user is redirected to
 * ``intake_review.php`` to confirm the extracted demographics + lists
 * before a real chart record is created by ``new_patient_save_ai.php``.
 *
 * Why a separate page from OpenEMR's stock ``interface/new/new.php``:
 * the stock form reads ``$_POST`` only — it has no GET-param or session
 * pre-pop pathway. Building a parallel entrypoint that POSTs the same
 * fields the stock form would have collected (via
 * ``newPatientData()``) keeps us from forking upstream OpenEMR code.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

// Demo-time error surfacing: render exception details inline so a 500
// during the demo can be diagnosed without tailing apache logs. Remove
// before any non-demo deploy.
ini_set('display_errors', '1');
ini_set('display_startup_errors', '1');
error_reporting(E_ALL);

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

// Stock new-patient creation requires admin/super; we keep the same
// gate so the AI path doesn't widen who can create a chart record.
if (!AclMain::aclCheckCore('admin', 'super')) {
    http_response_code(403);
    exit('forbidden');
}

// Wrap the rest of the request in a try/catch so any 500 surfaces a
// readable error in the iframe instead of OpenEMR's generic
// "An error has occurred." page. Demo-time only.
try {

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

        $upload = $request->files->get('intake_file');
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

            // patient_id_or_simple_directory='00' — addNewDocument's
            // documented placeholder for "patient not yet known". The
            // documents row is updated to point at the real pid by
            // new_patient_save_ai.php after the chart is created.
            $stored = addNewDocument(
            $origName,
            $mimeType,
            $tmpName,
            (string) $upload->getError(),
            (string) $size,
            $authUserId,
            '00',
            1,
            );
            if (!is_array($stored)) {
                $errorMessage = 'Failed to save document into OpenEMR.';
            } else {
                $docIdRaw = $stored['doc_id'] ?? '';
                $documentId = 'openemr:doc:' . (is_scalar($docIdRaw) ? (string) $docIdRaw : '');

                $config = new CopilotConfig($globals);
                $factory = new HttpFactory();
                $httpClient = new GuzzleClient([
                'timeout' => max($config->getAgentTimeoutSeconds() * 24, 120),
                'http_errors' => false,
                ]);
                $agentClient = new AgentHttpClient($httpClient, $factory, $config);
                $ingestClient = new IngestClient($agentClient, $config);

                try {
                    $response = $ingestClient->ingestIntake(
                    $documentId,
                    $authUserId,
                    $fileBytes,
                    $origName,
                    $mimeType,
                    );
                    if ($response->statusCode === 200) {
                        header('Location: ' . $webroot . '/interface/copilot/intake_review.php?'
                            . http_build_query(['document_id' => $documentId]));
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
    <title>Add New Patient with AI</title>
    <style>
        body { font-family: system-ui, sans-serif; padding: 2rem; max-width: 720px; }
        h1 { margin-top: 0; }
        .alert-error { background: #fee; border: 1px solid #faa; padding: 0.75rem 1rem; margin: 1rem 0; }
        .form-group { margin: 1rem 0; }
        label { display: block; margin-bottom: 0.4rem; font-weight: 600; }
        button { padding: 0.5rem 1rem; }
        .hint { color: #666; font-size: 0.9em; margin-top: 0.4rem; }
    </style>
</head>
<body>
<h1>Add New Patient with AI</h1>
<p>Upload a completed new-patient intake form (PDF or scanned image).
The agent will extract demographics, active problems, current medications,
allergies, and family history. You'll review and confirm before the chart
record is created.</p>

    <?php if ($errorMessage !== ''): ?>
    <div class="alert-error"><?php echo htmlspecialchars($errorMessage, ENT_QUOTES, 'UTF-8'); ?></div>
<?php endif; ?>

<form method="post" enctype="multipart/form-data">
    <input type="hidden" name="csrf_token_form" value="<?php echo htmlspecialchars($csrfToken, ENT_QUOTES, 'UTF-8'); ?>">
    <div class="form-group">
        <label for="intake_file">Intake form</label>
        <input type="file" name="intake_file" id="intake_file" accept="application/pdf,image/*" required>
        <div class="hint">Multi-page PDFs are supported; extraction takes ~10–60s.</div>
    </div>
    <div class="form-group">
        <button type="submit">Upload and extract</button>
    </div>
</form>
</body>
</html>
    <?php
} catch (\RuntimeException | \LogicException $exc) {
    http_response_code(500);
    echo "<h2>Co-Pilot: caught an exception</h2>";
    echo "<p><strong>" . htmlspecialchars($exc::class, ENT_QUOTES, 'UTF-8') . "</strong>: ";
    echo htmlspecialchars($exc->getMessage(), ENT_QUOTES, 'UTF-8') . "</p>";
    echo "<p>" . htmlspecialchars($exc->getFile(), ENT_QUOTES, 'UTF-8') . ":" . $exc->getLine() . "</p>";
    echo "<pre style='font-size:0.85em;background:#f4f4f4;padding:1rem;'>";
    echo htmlspecialchars($exc->getTraceAsString(), ENT_QUOTES, 'UTF-8');
    echo "</pre>";
}
?>
