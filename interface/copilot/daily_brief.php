<?php

/**
 * Clinical Co-Pilot — Daily Brief page (PR 16b).
 *
 * The pre-clinic surface from USERS §2 (7:35 AM): one card per panel
 * patient, showing a record-based snapshot (problems, meds, allergies,
 * latest labs) and the discrepancy engine's flag list inline. "Open
 * chat" deep-links to chat.php with the patient pre-selected.
 *
 * Two architectural points worth stating in the file:
 *
 * * **Cards are records, not LLM prose** (ARCHITECTURE §3 layer 2).
 *   The snapshot fields are SQL queries against the seeded discrepancy
 *   fixtures. The flag list is engine output — deterministic templated
 *   strings the rule engine emits, not generative text. Neither path
 *   passes through an LLM; the chat surface is the LLM-mediated path.
 *
 * * **Server-side warm + read** rather than waiting for a login event
 *   listener. PR 15a's ``/api/agent/internal/warm`` accepts a batch of
 *   patient_ids; PR 16a's ``GET /api/agent/internal/flags/{patient_id}``
 *   reads what warm filled. Wiring both into this page keeps panel
 *   freshness in lockstep with what's rendered, instead of relying on
 *   a separate login-time warm listener that could drift from the
 *   panel set.
 *
 * **Deviation from TASKS.md PR 16:** the spec listed Smarty .tpl files
 * for the cards. The existing copilot UI (chat.php) uses inline PHP
 * with no template engine; introducing Smarty for one page would add
 * a new layer no other copilot file uses. Inline PHP rendering keeps
 * the module consistent and avoids a partial Smarty wiring.
 *
 * Today's panel is the first ``$panelSize`` patients assigned to the
 * logged-in clinician — same provider-scoping the PR-17.5 access gate
 * enforces, lifted into the listing query. Backfilled by
 * ``scripts/copilot/assign_patients_to_clinicians.php``.
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
use Monolog\Handler\StreamHandler;
use Monolog\Logger;
use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Core\Header;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use OpenEMR\Services\Copilot\Flag;
use OpenEMR\Services\Copilot\InvalidationDispatcher;

// ACL gate: same scope as the chat menu entry. Direct URL access
// without an authenticated session falls through OpenEMR's standard
// session redirect; the ACL check below catches the authenticated-
// but-unauthorized case.
if (!AclMain::aclCheckCore('patients', 'demo')) {
    http_response_code(403);
    echo xlt('Not authorized to view this page.');
    exit;
}

$globals = OEGlobalsBag::getInstance();
// Plain get() + cast: the older OEGlobalsBag on the openemr/openemr base
// image we layer on for prod doesn't ship the typed getString() accessor.
$webrootRaw = $globals->get('webroot', '');
$webroot = is_string($webrootRaw) ? $webrootRaw : '';

// Probe both session layouts — newer OpenEMR namespaces clinician
// data under the core AttributeBag (key ``"OpenEMR"``); older base
// images write straight to the top of ``$_SESSION``. Same pattern
// :class:`SessionMapper` uses; duplicating here rather than calling
// the mapper because the mapper insists on a patient-id in scope
// (it serves the chat path), which the panel-listing page doesn't
// have.
$authUserId = '';
$bag = $_SESSION['OpenEMR'] ?? null;
$candidate = is_array($bag) && isset($bag['authUserID']) ? $bag['authUserID'] : $_SESSION['authUserID'] ?? null;
if (is_string($candidate)) {
    $authUserId = $candidate;
} elseif (is_int($candidate)) {
    $authUserId = (string) $candidate;
}

// Today's panel is the clinician's own assigned patients. The same
// providerID gate the PR-17.5 access checker enforces is lifted into
// the listing query so the page silently drops patients the clinician
// does not own (no UI notice that they exist). ``LIMIT $panelSize``
// keeps the warm/read fan-out bounded; bumping requires a thought-
// through redesign of the daily-brief layout, not just a number tweak.
$panelSize = 7;
$panelRows = QueryUtils::fetchRecords(
    'SELECT pid, fname, lname, DOB, sex, uuid FROM patient_data '
    . 'WHERE providerID = ? AND providerID != 0 '
    . 'ORDER BY date DESC, pid DESC '
    . 'LIMIT ' . $panelSize,
    [$authUserId],
);
// The discrepancy engine reads through FHIR (search by patient uuid),
// so the warm/readFlags fan-out has to send uuids — bare pids return
// empty bundles silently. Build a parallel pid → uuid lookup here so
// the inner card loop can map by either side without a re-query.
/** @var list<string> $panelUuids */
$panelUuids = [];
/** @var array<string, string> $uuidByPid */
$uuidByPid = [];
foreach ($panelRows as $row) {
    $pid = $row['pid'] ?? null;
    $rawUuid = $row['uuid'] ?? null;
    if ((!is_string($pid) && !is_int($pid)) || !is_string($rawUuid) || $rawUuid === '') {
        continue;
    }
    $uuid = UuidRegistry::uuidToString($rawUuid);
    $pidStr = (string) $pid;
    $panelUuids[] = $uuid;
    $uuidByPid[$pidStr] = $uuid;
}

// Dispatcher wiring: identical shape to the route closures in
// _rest_routes_copilot.inc.php. Two construction sites is the breakeven
// where extracting a factory class would be premature; if a third one
// shows up, lift these into OpenEMR\Services\Copilot\DispatcherFactory.
//
// Internal timeout × 3 (floor 9s) covers a cold-cache miss that
// recomputes through the engine for every patient sequentially. The
// agent service's own per-patient compute is sub-100ms today (PR 14
// notes), so 9s is generous; bumping rather than tightening keeps
// the demo honest if any rule grows expensive.
$config = new CopilotConfig($globals);
$httpClient = new GuzzleClient([
    'timeout' => max($config->getInternalTimeoutSeconds() * 3, 9),
    'http_errors' => false,
]);
$agentClient = new AgentHttpClient($httpClient, new HttpFactory(), $config);
$dailyLogger = new Logger('copilot-daily-brief');
$dailyLogger->pushHandler(new StreamHandler('php://stderr'));
$dispatcher = new InvalidationDispatcher($agentClient, $config, $dailyLogger);

// Warm first, read flags second. Warm is fire-and-forget on the wire
// but the agent computes eagerly inside the BackgroundRunner, so by
// the time readFlags hits the same in-process cache the entries are
// either materialized or the BackgroundRunner is mid-compute on the
// same key. A miss here just adds the chart load to readFlags' time.
$dispatcher->warmPanel($panelUuids);

/** @var list<array{
 *     patient: array<string, mixed>,
 *     age: int|null,
 *     problems: list<array<string, mixed>>,
 *     meds: list<array<string, mixed>>,
 *     allergies: list<array<string, mixed>>,
 *     labs: list<array<string, mixed>>,
 *     flags: list<Flag>,
 * }> $cards */
$cards = [];
foreach ($panelRows as $patient) {
    $pidValue = $patient['pid'] ?? null;
    if (!is_string($pidValue) && !is_int($pidValue)) {
        continue;
    }
    $pid = (string) $pidValue;

    $problems = QueryUtils::fetchRecords(
        "SELECT title, diagnosis, begdate FROM lists "
        . "WHERE pid = ? AND type = 'medical_problem' AND activity = 1 "
        . "ORDER BY begdate DESC",
        [$pid],
    );
    $meds = QueryUtils::fetchRecords(
        "SELECT title, diagnosis, begdate, comments FROM lists "
        . "WHERE pid = ? AND type = 'medication' AND activity = 1 "
        . "ORDER BY begdate DESC",
        [$pid],
    );
    $allergies = QueryUtils::fetchRecords(
        "SELECT title, reaction, verification, begdate FROM lists "
        . "WHERE pid = ? AND type = 'allergy' AND activity = 1 "
        . "ORDER BY begdate DESC",
        [$pid],
    );
    $labs = QueryUtils::fetchRecords(
        'SELECT pr.result_text, pr.result, pr.units, pr.range, pr.abnormal, pr.date '
        . 'FROM procedure_result pr '
        . 'JOIN procedure_report rpt ON rpt.procedure_report_id = pr.procedure_report_id '
        . 'JOIN procedure_order po ON po.procedure_order_id = rpt.procedure_order_id '
        . 'WHERE po.patient_id = ? '
        . 'ORDER BY pr.date DESC LIMIT 5',
        [$pid],
    );

    $flags = $dispatcher->readFlags($uuidByPid[$pid] ?? '');

    $age = null;
    $dobValue = $patient['DOB'] ?? null;
    if (is_string($dobValue) && $dobValue !== '' && $dobValue !== '0000-00-00') {
        $dob = \DateTimeImmutable::createFromFormat('Y-m-d', $dobValue);
        if ($dob !== false) {
            $age = (int) $dob->diff(new \DateTimeImmutable('today'))->y;
        }
    }

    $cards[] = [
        'patient' => $patient,
        'age' => $age,
        'problems' => $problems,
        'meds' => $meds,
        'allergies' => $allergies,
        'labs' => $labs,
        'flags' => $flags,
    ];
}

// Helper: encode a single record cell as escaped HTML. Centralising
// the cast guard keeps PHPStan happy without scattering ``is_string``
// guards into every echo site.
$cell = static function (mixed $value, string $fallback = '—'): string {
    if (is_string($value) && $value !== '') {
        return text($value);
    }
    if (is_int($value) || is_float($value)) {
        return text((string) $value);
    }
    return $fallback;
};

// Helper: narrow a ``mixed`` row value to a string (no HTML escape).
// Used where the value goes through urlencode / attr / a comparison
// rather than direct echo. Cast operator is forbidden at PHPStan
// level 10 because ``mixed → string`` could silently flatten an
// array; this narrows explicitly.
$asString = static function (mixed $value): string {
    if (is_string($value)) {
        return $value;
    }
    if (is_int($value) || is_float($value)) {
        return (string) $value;
    }
    return '';
};

// Helper: ``empty()`` is loose enough to be banned at PHPStan level 10
// (treats "0" as empty, etc.); this narrows to "has a printable
// string" which is the actual intent at every conditional render site.
$hasText = static fn(mixed $value): bool =>
    (is_string($value) && $value !== '') || is_int($value) || is_float($value);

?>
<!DOCTYPE html>
<html>
<head>
    <title><?php echo xlt('Co-Pilot Daily Brief'); ?></title>
    <?php Header::setupHeader(); ?>
    <link rel="stylesheet" href="<?php echo attr($webroot); ?>/public/copilot/copilot.css">
</head>
<body class="bg-light">
    <div class="container-fluid copilot-shell" data-copilot-shell>
        <header class="copilot-header">
            <h1><?php echo xlt('Daily Brief'); ?></h1>
        </header>

        <?php if ($cards === []) : ?>
            <section class="copilot-empty-panel">
                <p><?php echo xlt('No patients are assigned to you in the seeded panel. The discrepancy fixtures expect providerID = your user id; see the PR 17.5 gate.'); ?></p>
            </section>
        <?php else : ?>
            <section class="copilot-panel">
                <?php foreach ($cards as $card) : ?>
                    <?php
                    $p = $card['patient'];
                    $patientId = $asString($p['pid'] ?? null);
                    $fname = $asString($p['fname'] ?? null);
                    $lname = $asString($p['lname'] ?? null);
                    $name = trim($fname . ' ' . $lname);
                    $sex = $asString($p['sex'] ?? null);
                    $chatHref = $webroot . '/interface/copilot/chat.php?pid=' . urlencode($patientId);
                    ?>
                    <article class="copilot-brief-card" data-copilot-brief-card data-patient-id="<?php echo attr($patientId); ?>">
                        <header class="copilot-brief-card-header">
                            <h2 class="copilot-brief-card-name"><?php echo $cell($name); ?></h2>
                            <p class="copilot-brief-card-meta">
                                <?php if ($card['age'] !== null) : ?>
                                    <span><?php echo text((string) $card['age']); ?> <?php echo xlt('y'); ?></span>
                                <?php endif; ?>
                                <?php if ($sex !== '') : ?>
                                    <span><?php echo $cell($sex); ?></span>
                                <?php endif; ?>
                                <span class="copilot-brief-card-pid"><?php echo xlt('PID'); ?> <?php echo $cell($patientId); ?></span>
                            </p>
                            <a class="btn btn-primary btn-sm" href="<?php echo attr($chatHref); ?>" target="_blank" rel="noopener">
                                <?php echo xlt('Open chat'); ?>
                            </a>
                        </header>

                        <section class="copilot-brief-card-flags">
                            <h3><?php echo xlt('Flags'); ?> <span class="copilot-brief-card-count">(<?php echo text((string) count($card['flags'])); ?>)</span></h3>
                            <?php if ($card['flags'] === []) : ?>
                                <p class="copilot-brief-card-empty"><?php echo xlt('Engine reports no discrepancies — chart appears internally consistent.'); ?></p>
                            <?php else : ?>
                                <ul class="copilot-brief-flag-list">
                                    <?php foreach ($card['flags'] as $flag) : ?>
                                        <li class="copilot-brief-flag" data-flag-category="<?php echo attr($flag->category); ?>">
                                            <span class="copilot-brief-flag-rule"><?php echo text($flag->ruleId); ?></span>
                                            <span class="copilot-brief-flag-category"><?php echo text($flag->category); ?></span>
                                            <p class="copilot-brief-flag-rationale"><?php echo text($flag->rationale); ?></p>
                                        </li>
                                    <?php endforeach; ?>
                                </ul>
                            <?php endif; ?>
                        </section>

                        <section class="copilot-brief-card-section">
                            <h3><?php echo xlt('Problems'); ?></h3>
                            <?php if ($card['problems'] === []) : ?>
                                <p class="copilot-brief-card-empty"><?php echo xlt('No active problems on file.'); ?></p>
                            <?php else : ?>
                                <ul class="copilot-brief-card-list">
                                    <?php foreach ($card['problems'] as $row) : ?>
                                        <li>
                                            <span class="copilot-brief-card-title"><?php echo $cell($row['title'] ?? null); ?></span>
                                            <span class="copilot-brief-card-code"><?php echo $cell($row['diagnosis'] ?? null); ?></span>
                                            <?php if ($hasText($row['begdate'] ?? null)) : ?>
                                                <span class="copilot-brief-card-date"><?php echo xlt('since'); ?> <?php echo $cell($row['begdate']); ?></span>
                                            <?php endif; ?>
                                        </li>
                                    <?php endforeach; ?>
                                </ul>
                            <?php endif; ?>
                        </section>

                        <section class="copilot-brief-card-section">
                            <h3><?php echo xlt('Medications'); ?></h3>
                            <?php if ($card['meds'] === []) : ?>
                                <p class="copilot-brief-card-empty"><?php echo xlt('No active medications on file.'); ?></p>
                            <?php else : ?>
                                <ul class="copilot-brief-card-list">
                                    <?php foreach ($card['meds'] as $row) : ?>
                                        <li>
                                            <span class="copilot-brief-card-title"><?php echo $cell($row['title'] ?? null); ?></span>
                                            <?php if ($hasText($row['comments'] ?? null)) : ?>
                                                <span class="copilot-brief-card-comments"><?php echo $cell($row['comments']); ?></span>
                                            <?php endif; ?>
                                            <?php if ($hasText($row['begdate'] ?? null)) : ?>
                                                <span class="copilot-brief-card-date"><?php echo xlt('started'); ?> <?php echo $cell($row['begdate']); ?></span>
                                            <?php endif; ?>
                                        </li>
                                    <?php endforeach; ?>
                                </ul>
                            <?php endif; ?>
                        </section>

                        <section class="copilot-brief-card-section">
                            <h3><?php echo xlt('Allergies'); ?></h3>
                            <?php if ($card['allergies'] === []) : ?>
                                <p class="copilot-brief-card-empty"><?php echo xlt('No charted allergies. Watch for narrative-only allergies in notes.'); ?></p>
                            <?php else : ?>
                                <ul class="copilot-brief-card-list">
                                    <?php foreach ($card['allergies'] as $row) : ?>
                                        <li>
                                            <span class="copilot-brief-card-title"><?php echo $cell($row['title'] ?? null); ?></span>
                                            <?php if ($hasText($row['reaction'] ?? null)) : ?>
                                                <span class="copilot-brief-card-reaction"><?php echo xlt('reaction'); ?>: <?php echo $cell($row['reaction']); ?></span>
                                            <?php endif; ?>
                                            <?php if ($hasText($row['verification'] ?? null)) : ?>
                                                <span class="copilot-brief-card-verification"><?php echo $cell($row['verification']); ?></span>
                                            <?php endif; ?>
                                        </li>
                                    <?php endforeach; ?>
                                </ul>
                            <?php endif; ?>
                        </section>

                        <section class="copilot-brief-card-section">
                            <h3><?php echo xlt('Recent labs'); ?></h3>
                            <?php if ($card['labs'] === []) : ?>
                                <p class="copilot-brief-card-empty"><?php echo xlt('No labs on file.'); ?></p>
                            <?php else : ?>
                                <ul class="copilot-brief-card-list">
                                    <?php foreach ($card['labs'] as $row) : ?>
                                        <li>
                                            <span class="copilot-brief-card-title"><?php echo $cell($row['result_text'] ?? null); ?></span>
                                            <span class="copilot-brief-card-result <?php echo (($row['abnormal'] ?? '') === 'high' || ($row['abnormal'] ?? '') === 'low') ? 'copilot-brief-card-result-abnormal' : ''; ?>">
                                                <?php echo $cell($row['result'] ?? null); ?> <?php echo $cell($row['units'] ?? null, ''); ?>
                                            </span>
                                            <?php if ($hasText($row['range'] ?? null)) : ?>
                                                <span class="copilot-brief-card-range"><?php echo xlt('ref'); ?> <?php echo $cell($row['range']); ?></span>
                                            <?php endif; ?>
                                            <?php if ($hasText($row['date'] ?? null)) : ?>
                                                <span class="copilot-brief-card-date"><?php echo $cell($row['date']); ?></span>
                                            <?php endif; ?>
                                        </li>
                                    <?php endforeach; ?>
                                </ul>
                            <?php endif; ?>
                        </section>
                    </article>
                <?php endforeach; ?>
            </section>
        <?php endif; ?>
    </div>
</body>
</html>
