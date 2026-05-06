<?php

/**
 * Clinical Co-Pilot — in-chart side panel iframe target (PR 17).
 *
 * The fast-lane chat surface, mounted inside the demographics-tab side
 * panel via :class:`OpenEMR\Modules\Copilot\EventSubscriber\SidePanelSubscriber`.
 * Single-patient: pid is fixed for the lifetime of the iframe (the
 * launcher reloads the iframe on patient switch), so the picker that
 * lives on ``chat.php`` is dropped here in favour of a pid attribute on
 * the shell.
 *
 * Auth + transport contract is identical to ``chat.php``: standard
 * OpenEMR session, ``apicsrftoken`` header, POST /apis/default/api/agent/query.
 * The body adds ``"lane": "fast"`` so the agent service routes through
 * the Haiku-backed lane that satisfies PR 17's <5s acceptance target.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

require_once(__DIR__ . "/../globals.php");

use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Core\Header;
use OpenEMR\Core\OEGlobalsBag;

if (!AclMain::aclCheckCore('patients', 'demo')) {
    // Same gate as the demographics tab itself; the iframe target should
    // not be reachable for users who can't open the chart.
    http_response_code(403);
    exit;
}

// CSRF derivation mirrors chat.php — both top-level and namespaced
// session shapes, since older base images put ``csrf_private_key`` at
// the top of $_SESSION and newer ones nest it under ``OpenEMR``.
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

// Pid comes from the launcher's iframe URL. Accept any numeric pid;
// authorization is enforced one query down via the providerID gate
// (PR 17.5). If the lookup fails — pid missing, not numeric, or the
// patient is not assigned to this clinician — ``$patientLabel`` stays
// empty and the rendered shell drops into the "not assigned" branch
// instead of exposing a chat affordance for a patient the gateway
// would 403 anyway.
$pid = '';
$pidParam = filter_input(INPUT_GET, 'pid');
if (is_string($pidParam) && ctype_digit($pidParam)) {
    $pid = $pidParam;
}

$patientLabel = '';
if ($pid !== '') {
    // Reflection-based read: the wrapper API differs between the
    // openemr/openemr base image we layer on for prod (->getWrapper())
    // and the upstream master we develop against (->getActiveSession()).
    $factory = SessionWrapperFactory::getInstance();
    $factoryRef = new \ReflectionClass($factory);
    $accessor = $factoryRef->hasMethod('getWrapper')
        ? 'getWrapper'
        : ($factoryRef->hasMethod('getActiveSession') ? 'getActiveSession' : null);
    $authUserIdRaw = null;
    if ($accessor !== null) {
        $session = $factoryRef->getMethod($accessor)->invoke($factory);
        if (is_object($session) && method_exists($session, 'get')) {
            $authUserIdRaw = (new \ReflectionMethod($session, 'get'))->invoke($session, 'authUserID', null);
        }
    }
    $authUserId = is_int($authUserIdRaw) ? (string) $authUserIdRaw
        : (is_string($authUserIdRaw) ? $authUserIdRaw : '');
    if ($authUserId !== '' && ctype_digit($authUserId)) {
        $row = QueryUtils::querySingleRow(
            'SELECT fname, lname FROM patient_data '
            . 'WHERE pid = ? AND providerID = ? AND providerID != 0 LIMIT 1',
            [$pid, $authUserId],
        );
        if (is_array($row)) {
            $fname = is_string($row['fname'] ?? null) ? $row['fname'] : '';
            $lname = is_string($row['lname'] ?? null) ? $row['lname'] : '';
            $patientLabel = trim($fname . ' ' . $lname);
        }
    }
}
$panelEnabled = $patientLabel !== '';

?>
<!DOCTYPE html>
<html>
<head>
    <title><?php echo xlt('Clinical Co-Pilot'); ?></title>
    <?php Header::setupHeader(); ?>
    <link rel="stylesheet" href="<?php echo attr($webroot); ?>/public/copilot/copilot.css">
</head>
<body class="bg-light">
    <div
        class="copilot-shell copilot-shell-side"
        data-copilot-shell
        data-copilot-side
        data-copilot-pid="<?php echo attr($panelEnabled ? $pid : ''); ?>"
        data-copilot-lane="fast"
    >
        <header class="copilot-header">
            <h1><?php echo xlt('Co-Pilot'); ?></h1>
            <?php if (!$panelEnabled) : ?>
                <p class="copilot-disclaimer">
                    <?php echo xlt('This patient is not assigned to you. Open a patient from your daily-brief panel to use the Co-Pilot side panel.'); ?>
                </p>
            <?php else : ?>
                <p class="copilot-subtitle">
                    <strong><?php echo text($patientLabel); ?></strong>
                    <code class="copilot-side-panel-pid"><?php echo text($pid); ?></code>
                    &middot;
                    <?php echo xlt('fast lane'); ?>
                </p>
                <p class="copilot-quick-actions" style="margin-top:0.4rem;">
                    <a
                        href="<?php echo attr($webroot); ?>/interface/copilot/upload_lab.php?pid=<?php echo attr($pid); ?>"
                        target="_blank"
                        rel="noopener"
                        class="btn btn-sm btn-outline-primary"
                    >
                        <?php echo xlt('Upload lab document'); ?>
                    </a>
                </p>
            <?php endif; ?>
        </header>

        <section class="copilot-thread" data-copilot-thread>
            <div class="copilot-empty">
                <?php echo xlt('Ask a quick question about this patient. Suggested prompts:'); ?>
                <ul>
                    <li><?php echo xlt('Active problems?'); ?></li>
                    <li><?php echo xlt('Most recent labs'); ?></li>
                    <li><?php echo xlt('Anything I should know before walking in?'); ?></li>
                </ul>
            </div>
        </section>

        <?php $disabledAttr = $panelEnabled ? '' : ' disabled'; ?>
        <?php $formAriaAttr = $panelEnabled ? '' : ' aria-disabled="true"'; ?>
        <form class="copilot-form" data-copilot-form<?php echo $formAriaAttr; ?>>
            <textarea
                data-copilot-input
                rows="2"
                placeholder="<?php echo xla('Ask a question about this patient'); ?>"
                <?php echo $disabledAttr; ?>
                required></textarea>
            <button type="submit" class="btn btn-primary btn-sm" data-copilot-submit<?php echo $disabledAttr; ?>>
                <?php echo xlt('Ask'); ?>
            </button>
        </form>
    </div>

    <script>
        window.__copilotSideConfig = {
            queryUrl: <?php echo json_encode($webroot . '/apis/default/api/agent/query'); ?>,
            sessionDeleteUrl: <?php echo json_encode($webroot . '/apis/default/api/agent/session'); ?>,
            csrfToken: <?php echo json_encode($apiCsrfToken); ?>
        };
    </script>
    <script src="<?php echo attr($webroot); ?>/public/copilot/idle_timer.js"></script>
    <script src="<?php echo attr($webroot); ?>/public/copilot/side_panel.js"></script>
</body>
</html>
