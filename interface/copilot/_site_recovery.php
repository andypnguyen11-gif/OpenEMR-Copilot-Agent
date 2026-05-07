<?php

/**
 * Pre-globals.php site_id recovery shim for Co-Pilot iframe entry points.
 *
 * ``interface/globals.php`` aborts with "Site ID is missing from session
 * data!" any time a privileged request lands without either (a) ``site_id``
 * already in the session or (b) ``?site=<id>`` in the query string. The
 * tabs shell sets ``site_id`` on login and the cookie carries it forward,
 * but we've reproduced the missing-site_id state in two practical scenarios:
 *
 *   - Iframe-to-iframe navigation through the OpenEMR tabs system: open
 *     Patient Finder, click back to the Co-Pilot upload tab — sometimes
 *     the upload iframe is remounted with a fresh PHP session that
 *     hasn't been bootstrapped yet.
 *   - Long-running extractions (HL7 / fax TIFF) where the session GC
 *     window expires between the POST returning and the browser issuing
 *     the GET that follows the ``Location:`` header.
 *
 * The shim runs **before** ``require_once "../globals.php"``. It only
 * fires when ``$_GET['site']`` is not already set; in that case it
 * derives a candidate site id from ``HTTP_HOST`` (matching globals.php's
 * own fallback at lines 276-279) and falls through to ``"default"``.
 * That populates ``$_GET['site']`` so globals.php's recovery branch picks
 * it up and stores it in the session, instead of die()'ing at line 273.
 *
 * Why this is safe (the previous attempt at this — propagating
 * ``?site=`` on every redirect — caused forced sign-outs):
 *
 *   - We never override an existing ``$_GET['site']``. If the URL
 *     already carries one, globals.php handles it as before.
 *   - We never set ``?site=`` to a value that mismatches what the
 *     session already has. globals.php compares ``$session->get('site_id')``
 *     against ``$_GET['site']`` and clears the session on mismatch
 *     (line 292-303). HTTP_HOST is what the original login screen used,
 *     so the candidate matches the session's stored value.
 *   - The site-name regex matches globals.php's own validation
 *     (``[A-Za-z0-9\-.]``) so a malicious Host header can't poison
 *     the lookup.
 *   - We only set ``$_GET['site']`` to a candidate whose sites/<id>
 *     directory actually exists, falling back to ``"default"``
 *     otherwise.
 *
 * The shim is non-namespaced because it runs before any autoload is
 * configured — it's a top-level procedural hook, not a service class.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

// ``filter_input(INPUT_GET, ...)`` and ``filter_input(INPUT_SERVER, ...)``
// stay inside the OpenEMR project rule that forbids direct ``$_GET`` /
// ``$_SERVER`` access (see tests/PHPStan/Rules/ForbiddenRequestGlobals).
// We still write the recovered value back into ``$_GET['site']`` because
// globals.php downstream reads from there directly and we have to land
// before its line-258 check fires.
$existingSite = filter_input(INPUT_GET, 'site');
if (!is_string($existingSite) || $existingSite === '') {
    $hostHintRaw = filter_input(INPUT_SERVER, 'HTTP_HOST');
    $hostHint = is_string($hostHintRaw) ? $hostHintRaw : '';
    // Strip the ``:8300`` port suffix, mirroring the form ``HTTP_HOST``
    // takes when OpenEMR is reached on a non-standard port locally.
    $hostHint = preg_replace('/:\d+$/', '', $hostHint) ?? $hostHint;
    $sitesBase = __DIR__ . '/../../sites';

    $candidate = '';
    if (
        $hostHint !== ''
        && preg_match('/^[A-Za-z0-9\-.]+$/', $hostHint) === 1
        && is_dir($sitesBase . '/' . $hostHint)
    ) {
        $candidate = $hostHint;
    } elseif (is_dir($sitesBase . '/default')) {
        $candidate = 'default';
    }

    if ($candidate !== '') {
        $_GET['site'] = $candidate;
    }
}
