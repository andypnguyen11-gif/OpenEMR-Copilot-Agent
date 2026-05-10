<?php

/**
 * Clinical Co-Pilot — extracted-lab review page (PR W2-02).
 *
 * Renders an editable table of the observations the VLM extractor
 * pulled from the uploaded lab document. The clinician confirms or
 * adjusts each row, then submits to ``lab_save_ai.php`` which writes
 * to ``procedure_order`` / ``procedure_order_code`` / ``procedure_report``
 * / ``procedure_result``.
 *
 * Citations render two ways. Each row carries a text snippet
 * (page number + raw_text) inline next to the value, and the page
 * is rendered in full above the table with bbox rectangles drawn on
 * a canvas overlay (see ``partials/citation_overlay.php``). Clicking
 * a row's analyte cell color-flips the matching rectangle so the
 * clinician can verify the extraction against the source.
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
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Core\Header;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\AgentServiceException;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use OpenEMR\Services\Copilot\ExtractedFieldHelper;

if (!AclMain::aclCheckCore('patients', 'demo')) {
    http_response_code(403);
    exit('forbidden');
}

$pidParam = filter_input(INPUT_GET, 'pid');
$pid = (is_string($pidParam) && ctype_digit($pidParam)) ? (int) $pidParam : 0;
if ($pid <= 0) {
    http_response_code(400);
    exit('missing patient id');
}

$documentId = (string) (filter_input(INPUT_GET, 'document_id') ?? '');
if ($documentId === '') {
    http_response_code(400);
    exit('missing document_id');
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

$observations = is_array($facts)
    ? ExtractedFieldHelper::rowList($facts['observations'] ?? null)
    : [];

$csrfToken = CsrfUtils::collectCsrfToken(
    session: SessionWrapperFactory::getInstance()->getActiveSession(),
);

Header::setupHeader();
?>
<!DOCTYPE html>
<html>
<head>
    <title>Review extracted lab — pid <?php echo $pid; ?></title>
    <style>
        body { font-family: system-ui, sans-serif; padding: 1rem 1.5rem; }
        h1 { margin-top: 0; }
        table { border-collapse: collapse; width: 100%; }
        th, td { border: 1px solid #ccc; padding: 0.4rem 0.6rem; vertical-align: top; }
        th { background: #f4f4f4; text-align: left; }
        input[type=text] { width: 100%; box-sizing: border-box; }
        .citation { font-size: 0.85em; color: #555; max-width: 320px; }
        .abstain { color: #b30; font-weight: 600; font-size: 0.85em; }
        .alert-error { background: #fee; border: 1px solid #faa; padding: 0.75rem 1rem; margin: 1rem 0; }
        .actions { margin: 1.5rem 0; }
        button { padding: 0.5rem 1rem; }
    </style>
</head>
<body>
<h1>Review extracted lab observations</h1>
<p>
    Document: <code><?php echo htmlspecialchars($documentId, ENT_QUOTES, 'UTF-8'); ?></code>
    | Patient pid: <?php echo $pid; ?>
    | Observations extracted: <?php echo count($observations); ?>
</p>

<?php if ($loadError !== ''): ?>
    <div class="alert-error"><?php echo htmlspecialchars($loadError, ENT_QUOTES, 'UTF-8'); ?></div>
<?php elseif (count($observations) === 0): ?>
    <div class="alert-error">The extractor returned no observations. The document may not be a lab report.</div>
<?php else: ?>

    <?php
// Bbox citation overlay — needs $documentId, $facts, $webroot in
// scope. Renders the source page(s) with rectangles for every
// SourceCitation the extractor emitted. Click a row in the table
// (specifically the analyte cell) to color-flip the matching
// rectangle. The partial floats right; the form below claims the
// left column via .copilot-review-form-col so the two sit
// side-by-side on wide viewports.
    include __DIR__ . '/partials/citation_overlay.php';
    ?>

<form method="post" action="<?php echo htmlspecialchars($webroot . '/interface/copilot/lab_save_ai.php', ENT_QUOTES, 'UTF-8'); ?>" class="copilot-review-form-col">
    <input type="hidden" name="csrf_token_form" value="<?php echo htmlspecialchars($csrfToken, ENT_QUOTES, 'UTF-8'); ?>">
    <input type="hidden" name="pid" value="<?php echo $pid; ?>">
    <input type="hidden" name="document_id" value="<?php echo htmlspecialchars($documentId, ENT_QUOTES, 'UTF-8'); ?>">
    <input type="hidden" name="panel_name" value="Co-Pilot lab import">

    <table>
        <thead>
            <tr>
                <th style="width: 4%;">Save?</th>
                <th>Analyte</th>
                <th>LOINC</th>
                <th>Value</th>
                <th>Unit</th>
                <th>Reference</th>
                <th>Flag</th>
                <th>Citation</th>
            </tr>
        </thead>
        <tbody>
        <?php foreach ($observations as $idx => $obs): ?>
            <?php
            $display = ExtractedFieldHelper::value($obs['display'] ?? null);
            $code = ExtractedFieldHelper::value($obs['code'] ?? null);
            $value = ExtractedFieldHelper::value($obs['value'] ?? null);
            $unit = ExtractedFieldHelper::value($obs['unit'] ?? null);
            $refLow = ExtractedFieldHelper::value($obs['reference_low'] ?? null);
            $refHigh = ExtractedFieldHelper::value($obs['reference_high'] ?? null);
            $reference = ($refLow !== '' || $refHigh !== '') ? ($refLow . '-' . $refHigh) : '';
            $flag = ExtractedFieldHelper::value($obs['flag'] ?? null);
            $citation = ExtractedFieldHelper::citationText($obs['display'] ?? null);
            $abstain = ExtractedFieldHelper::abstainReason($obs['value'] ?? null);
            $checked = $abstain === '' ? 'checked' : '';
            // Surface the analyte cell's citation `field_or_chunk_id` on
            // the cell itself so a click highlights the matching bbox in
            // the overlay above. Each lab observation has multiple
            // citations (display, value, unit, ...) — we anchor on
            // ``display`` because that's the column the clinician's eye
            // tracks first when scanning the table.
            $displayCitationId = ExtractedFieldHelper::fieldOrChunkId($obs['display'] ?? null);
            $analyteDataAttr = $displayCitationId !== ''
                ? ' data-citation-id="' . htmlspecialchars($displayCitationId, ENT_QUOTES, 'UTF-8') . '"'
                : '';
            ?>
            <tr>
                <td><input type="checkbox" name="confirm[<?php echo $idx; ?>]" value="1" <?php echo $checked; ?>></td>
                <td<?php echo $analyteDataAttr; ?> style="cursor: pointer;">
                    <input type="text" name="display[<?php echo $idx; ?>]" value="<?php echo htmlspecialchars($display, ENT_QUOTES, 'UTF-8'); ?>">
                </td>
                <td><input type="text" name="code[<?php echo $idx; ?>]" value="<?php echo htmlspecialchars($code, ENT_QUOTES, 'UTF-8'); ?>" style="width:80px;"></td>
                <td>
                    <input type="text" name="value[<?php echo $idx; ?>]" value="<?php echo htmlspecialchars($value, ENT_QUOTES, 'UTF-8'); ?>" style="width:80px;">
                    <?php if ($abstain !== ''): ?>
                        <div class="abstain"><?php echo htmlspecialchars($abstain, ENT_QUOTES, 'UTF-8'); ?></div>
                    <?php endif; ?>
                </td>
                <td><input type="text" name="unit[<?php echo $idx; ?>]" value="<?php echo htmlspecialchars($unit, ENT_QUOTES, 'UTF-8'); ?>" style="width:70px;"></td>
                <td><input type="text" name="reference[<?php echo $idx; ?>]" value="<?php echo htmlspecialchars($reference, ENT_QUOTES, 'UTF-8'); ?>" style="width:90px;"></td>
                <td>
                    <select name="flag[<?php echo $idx; ?>]">
                        <option value="" <?php echo $flag === '' ? 'selected' : ''; ?>>—</option>
                        <option value="N" <?php echo strtoupper($flag) === 'N' ? 'selected' : ''; ?>>N</option>
                        <option value="H" <?php echo strtoupper($flag) === 'H' ? 'selected' : ''; ?>>H</option>
                        <option value="L" <?php echo strtoupper($flag) === 'L' ? 'selected' : ''; ?>>L</option>
                        <option value="HH" <?php echo strtoupper($flag) === 'HH' ? 'selected' : ''; ?>>HH</option>
                        <option value="LL" <?php echo strtoupper($flag) === 'LL' ? 'selected' : ''; ?>>LL</option>
                    </select>
                </td>
                <td class="citation"><?php echo htmlspecialchars($citation, ENT_QUOTES, 'UTF-8'); ?></td>
            </tr>
        <?php endforeach; ?>
        </tbody>
    </table>

    <div class="actions">
        <button type="submit">Confirm and save to chart</button>
    </div>
</form>
<div style="clear: both;"></div>
<?php endif; ?>
</body>
</html>
