<?php

/**
 * Clinical Co-Pilot — chat surface (M3 MVP).
 *
 * Single-page chat interface. The patient picker is the clinician's own
 * assigned panel (same source the daily brief uses). The page is the
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

require_once(__DIR__ . "/_site_recovery.php");
require_once(__DIR__ . "/../globals.php");

use OpenEMR\Common\Database\QueryUtils;
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
// Plain get() + cast: the older OEGlobalsBag on the openemr/openemr base
// image we layer on for prod doesn't ship the typed getString() accessor.
$webrootRaw = OEGlobalsBag::getInstance()->get('webroot', '');
$webroot = is_string($webrootRaw) ? $webrootRaw : '';

// Probe both session layouts to find the logged-in clinician. Same
// pattern daily_brief.php uses; chat.php previously trusted the gateway
// to handle scoping, but the dropdown also needs the user id to populate
// only assigned patients.
$authUserId = '';
$bag = $_SESSION['OpenEMR'] ?? null;
$candidate = is_array($bag) && isset($bag['authUserID']) ? $bag['authUserID'] : $_SESSION['authUserID'] ?? null;
if (is_string($candidate)) {
    $authUserId = $candidate;
} elseif (is_int($candidate)) {
    $authUserId = (string) $candidate;
}

// Patient picker is the clinician's own assigned panel — same providerID
// gate the PR-17.5 access checker enforces, lifted into the listing
// query. Same source the daily-brief panel uses; backfilled by
// scripts/copilot/assign_patients_to_clinicians.php.
$panelSize = 7;
$panelRows = QueryUtils::fetchRecords(
    'SELECT pid, fname, lname FROM patient_data '
    . 'WHERE providerID = ? AND providerID != 0 '
    . 'ORDER BY pid '
    . 'LIMIT ' . $panelSize,
    [$authUserId],
);
/** @var array<string, string> $options */
$options = [];
foreach ($panelRows as $row) {
    $pidValue = $row['pid'] ?? null;
    if (!is_string($pidValue) && !is_int($pidValue)) {
        continue;
    }
    $pid = (string) $pidValue;
    $fname = is_string($row['fname'] ?? null) ? $row['fname'] : '';
    $lname = is_string($row['lname'] ?? null) ? $row['lname'] : '';
    $options[$pid] = trim("$pid — $fname $lname");
}

// Optional deep-link from Daily Brief: ``?pid=NNNNN`` pre-selects the
// patient in the dropdown. Whitelisted against the panel so a crafted
// URL can't push an unassigned patient_id into the chat — the
// gateway's PR-17.5 access checker would block the chat call anyway,
// but reflecting an arbitrary id back into the dropdown would still
// show "PID NNNNN" to the clinician.
$preselectedPid = '';
// filter_input rather than $_GET — the openemr.forbiddenRequestGlobals
// PHPStan rule blocks $_SUPERGLOBAL access in src/, and we follow the
// same convention in interface/* for consistency.
$pidParam = filter_input(INPUT_GET, 'pid');
if (is_string($pidParam) && array_key_exists($pidParam, $options)) {
    $preselectedPid = $pidParam;
}

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
        </header>

        <section class="copilot-toolbar">
            <label for="copilot-patient">
                <?php echo xlt('Patient'); ?>
            </label>
            <select id="copilot-patient" data-copilot-patient>
                <?php if ($options === []) : ?>
                    <option value=""><?php echo xlt('No patients assigned to you yet'); ?></option>
                <?php else : ?>
                    <?php foreach ($options as $pid => $label) :
                        // PHP coerces numeric-string array keys to int, so
                        // ``$pid`` is int here even though the source array
                        // looks string-keyed. Re-stringify so the equality
                        // and the ``attr`` cast both stay in the string lane.
                        $pidStr = (string) $pid;
                        $selected = ($pidStr === $preselectedPid) ? ' selected' : '';
                        ?>
                        <option value="<?php echo attr($pidStr); ?>"<?php echo $selected; ?>><?php echo text($label); ?></option>
                    <?php endforeach; ?>
                <?php endif; ?>
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
            sessionDeleteUrl: <?php echo json_encode($webroot . '/apis/default/api/agent/session'); ?>,
            csrfToken: <?php echo json_encode($apiCsrfToken); ?>
        };
    </script>
    <script src="<?php echo attr($webroot); ?>/public/copilot/idle_timer.js"></script>
    <script src="<?php echo attr($webroot); ?>/public/copilot/chat.js"></script>
</body>
</html>
