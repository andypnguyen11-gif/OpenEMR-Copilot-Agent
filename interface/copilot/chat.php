<?php

/**
 * Clinical Co-Pilot — chat surface (M3 MVP).
 *
 * Single-page chat interface with a fixture-patient picker. The page is the
 * UI shell; the actual query flows from the JS to the gateway at
 * ``POST /apis/default/api/agent/query``, which mints an HS256 JWT and
 * forwards to the agent service.
 *
 * Auth: relies on OpenEMR's standard session — visiting this page without a
 * logged-in clinician redirects through the usual login path. The backend
 * route enforces the rest (per-request RBAC happens at the agent's tool
 * layer using JWT claims).
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

require_once(__DIR__ . "/../globals.php");

use OpenEMR\Core\Header;
use OpenEMR\Core\OEGlobalsBag;

// Hand-compute the API CSRF token rather than calling
// CsrfUtils::collectCsrfToken / SessionWrapperFactory — both have shifted
// signatures across OpenEMR versions and our base image on Railway lags
// behind the repo. The hash formula
// (``substr(hash_hmac('sha256', $subject, $privateKey), 0, 40)``) has
// been stable for years and is what every CsrfUtils variant in the wild
// computes; reading the private key from the session bag directly
// bypasses the unstable wrapper.
// Older OpenEMR base images don't namespace session data under a
// Symfony AttributeBag, so $_SESSION['csrf_private_key'] is set
// directly. Newer images put it under $_SESSION['OpenEMR']
// (SessionUtil::CORE_SESSION_ID). Probe both so the gateway works
// regardless of which version the deployed base image is on.
$privateKey = '';
$topLevelKey = $_SESSION['csrf_private_key'] ?? null;
if (is_string($topLevelKey) && $topLevelKey !== '') {
    $privateKey = $topLevelKey;
} else {
    /** @var mixed $bag */
    $bag = $_SESSION['OpenEMR'] ?? null;
    if (is_array($bag)) {
        $bagKey = $bag['csrf_private_key'] ?? null;
        if (is_string($bagKey)) {
            $privateKey = $bagKey;
        }
    }
}
$apiCsrfToken = $privateKey !== ''
    ? substr(hash_hmac('sha256', 'api', $privateKey), 0, 40)
    : '';
$webroot = OEGlobalsBag::getInstance()->getString('webroot', '');

?>
<!DOCTYPE html>
<html>
<head>
    <title><?php echo xlt('Clinical Co-Pilot'); ?></title>
    <?php Header::setupHeader(); ?>
    <link rel="stylesheet" href="<?php echo attr($webroot); ?>/public/copilot/copilot.css">
</head>
<body class="bg-light">
    <div class="container-fluid copilot-shell" data-copilot-shell>
        <header class="copilot-header">
            <h1><?php echo xlt('Clinical Co-Pilot'); ?></h1>
            <p class="copilot-subtitle">
                <?php echo xlt('Chart review assistant — every claim is cited; the agent abstains when it cannot be sure.'); ?>
            </p>
            <p class="copilot-disclaimer">
                <strong><?php echo xlt('Demo'); ?>:</strong>
                <?php echo xlt('Hand-encoded fixture patients only. No live FHIR. Not a diagnostic device.'); ?>
            </p>
        </header>

        <section class="copilot-toolbar">
            <label for="copilot-patient">
                <?php echo xlt('Patient'); ?>
            </label>
            <select id="copilot-patient" data-copilot-patient>
                <option value="101">101 — Maria Lopez (T2DM + HTN, well controlled)</option>
                <option value="102">102 — James Wright (T2DM, stale labs)</option>
                <option value="103">103 — Eleanor Banks (med-vs-note conflict)</option>
                <option value="104">104 — Robert Kim (allergy-vs-med safety)</option>
                <option value="999">999 — OUT-OF-PANEL sentinel (RBAC test)</option>
            </select>
            <button type="button" class="btn btn-secondary btn-sm" data-copilot-reset>
                <?php echo xlt('Clear chat'); ?>
            </button>
        </section>

        <section class="copilot-thread" data-copilot-thread>
            <div class="copilot-empty">
                <?php echo xlt('Pick a patient and ask a question. Suggested prompts:'); ?>
                <ul>
                    <li><?php echo xlt('What are this patient\'s active problems?'); ?></li>
                    <li><?php echo xlt('Anything I should know before walking in?'); ?></li>
                    <li><?php echo xlt('Most recent lab values'); ?></li>
                    <li><?php echo xlt('Med list with start dates'); ?></li>
                </ul>
            </div>
        </section>

        <form class="copilot-form" data-copilot-form>
            <textarea
                id="copilot-input"
                data-copilot-input
                rows="3"
                placeholder="<?php echo xla('Ask a question about the selected patient'); ?>"
                required></textarea>
            <button type="submit" class="btn btn-primary" data-copilot-submit>
                <?php echo xlt('Ask Co-Pilot'); ?>
            </button>
        </form>
    </div>

    <script>
        window.__copilotConfig = {
            queryUrl: <?php echo json_encode($webroot . '/apis/default/api/agent/query'); ?>,
            csrfToken: <?php echo json_encode($apiCsrfToken); ?>
        };
    </script>
    <script src="<?php echo attr($webroot); ?>/public/copilot/chat.js"></script>
</body>
</html>
