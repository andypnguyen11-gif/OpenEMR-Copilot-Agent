<?php

/**
 * Clinical Co-Pilot — rendered-page proxy for the citation-overlay UI.
 *
 * Forwards ``GET /api/agent/internal/document_page/{id}?page=N`` from
 * the agent service to the browser. The OpenEMR docker image
 * (``openemr/openemr:flex``) ships ImageMagick but no Ghostscript, so
 * PHP cannot rasterize PDFs in-process — agent-service is the
 * canonical renderer (it already runs ``pypdfium2`` for the VLM
 * extractor) and this file is a thin authenticated forward.
 *
 * Cache-miss policy: the upstream returns a 404 with a structured JSON
 * body identifying whether the document was never rendered or the
 * page index is out of range. We forward that body verbatim so the
 * browser-side overlay can render a clear "preview unavailable"
 * placeholder rather than guess.
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
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\AgentServiceException;
use OpenEMR\Services\Copilot\Config\CopilotConfig;

// Read access on demographics is the same gate ``lab_review.php`` and
// ``document_review.php`` use — the page image is no more sensitive
// than the extracted facts the review page already shows.
if (!AclMain::aclCheckCore('patients', 'demo')) {
    http_response_code(403);
    header('Content-Type: text/plain; charset=utf-8');
    exit('forbidden');
}

$documentIdRaw = filter_input(INPUT_GET, 'document_id');
$documentId = is_string($documentIdRaw) ? trim($documentIdRaw) : '';
if ($documentId === '' || strlen($documentId) > 128) {
    http_response_code(400);
    header('Content-Type: text/plain; charset=utf-8');
    exit('missing or invalid document_id');
}

$pageRaw = filter_input(INPUT_GET, 'page');
$pageStr = is_string($pageRaw) ? trim($pageRaw) : '';
if ($pageStr === '' || !ctype_digit($pageStr)) {
    http_response_code(400);
    header('Content-Type: text/plain; charset=utf-8');
    exit('missing or invalid page');
}
$page = (int) $pageStr;
if ($page < 1) {
    http_response_code(400);
    header('Content-Type: text/plain; charset=utf-8');
    exit('page must be >= 1');
}

$globals = OEGlobalsBag::getInstance();
$config = new CopilotConfig($globals);
$factory = new HttpFactory();
// Page rendering can take a few seconds for the first request after
// ingest if the cache write is still flushing. The 4× timeout
// matches the multiplier ``lab_review.php`` uses for its own
// ``getInternal`` call against the same service.
$httpClient = new GuzzleClient([
    'timeout' => max($config->getAgentTimeoutSeconds() * 4, 30),
    'http_errors' => false,
]);
$agentClient = new AgentHttpClient($httpClient, $factory, $config);

$path = sprintf(
    '/api/agent/internal/document_page/%s?page=%d',
    rawurlencode($documentId),
    $page,
);

$upstreamError = false;
$upstream = ['statusCode' => 0, 'contentType' => '', 'body' => ''];
try {
    $upstream = $agentClient->getInternalRaw($path, $config->getInternalToken());
} catch (AgentServiceException) {
    // Generic 502 below — the upstream message may name internal
    // hostnames or container ports we do not want to leak to the
    // browser. The exception is swallowed deliberately; the caller
    // sees only "upstream unavailable".
    $upstreamError = true;
}

if ($upstreamError) {
    http_response_code(502);
    header('Content-Type: text/plain; charset=utf-8');
    exit('upstream unavailable');
}

http_response_code($upstream['statusCode']);
$contentType = $upstream['contentType'] !== ''
    ? $upstream['contentType']
    : 'application/octet-stream';
header('Content-Type: ' . $contentType);
// Same-session cache: the page content is immutable for a given
// (document_id, page), so the browser may reuse it within the
// session. ``private`` keeps shared proxies (corporate caches) from
// holding PHI-bearing renders.
header('Cache-Control: private, max-age=300');
echo $upstream['body'];
