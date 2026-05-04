<?php

/**
 * Backfill ``patient_data.providerID`` so each active clinician owns a small
 * panel for the Co-Pilot daily brief and chat lanes.
 *
 * Synthea-imported patients (devtools import-random-patients) and any other
 * patient created without a generalPractitioner reference land with
 * ``providerID = NULL``. The PR-17.5 access gate
 * (DatabasePatientAccessChecker, daily_brief.php, side_panel.php) requires
 * ``providerID = $authUserId AND providerID != 0``, so NULL-providerID
 * patients are silently invisible to every clinician — empty daily brief,
 * 403 on every chat query.
 *
 * This script picks a small random subset of unassigned patients per active
 * clinician and sets their providerID. Re-runnable: clinicians who already
 * own ``--per-clinician`` (or more) patients are skipped; only the gap is
 * filled for clinicians under the target.
 *
 * Usage::
 *
 *   php scripts/copilot/assign_patients_to_clinicians.php [--per-clinician=7] [--dry-run]
 *
 * Flags:
 *   --per-clinician=N   target panel size per clinician (default 7)
 *   --dry-run           print the assignments without executing them
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

if (PHP_SAPI !== 'cli') {
    fwrite(STDERR, "This script must be run from the CLI.\n");
    exit(1);
}

// interface/globals.php reads ``$_GET['site']`` directly to pick the OpenEMR
// site root. CLI scripts have no HTTP request, so we seed the superglobal
// before bootstrap. This is the same pattern every other CLI utility in
// contrib/util follows.
// @phpstan-ignore openemr.forbiddenRequestGlobals (CLI bootstrap)
$_GET['site'] = 'default';
$ignoreAuth = true;
require_once __DIR__ . '/../../interface/globals.php';

use OpenEMR\Common\Database\QueryUtils;

// ``getopt()`` is the standard CLI argument parser — it reads $argv without
// our code touching the superglobal directly, which keeps the
// openemr.forbiddenRequestGlobals rule happy.
$opts = getopt('', ['per-clinician::', 'dry-run']);
if ($opts === false) {
    fwrite(STDERR, "Failed to parse arguments.\n");
    exit(2);
}

$perClinician = 7;
if (isset($opts['per-clinician'])) {
    $raw = $opts['per-clinician'];
    if (!is_string($raw) || !ctype_digit($raw)) {
        fwrite(STDERR, "--per-clinician must be a positive integer\n");
        exit(2);
    }
    $value = (int) $raw;
    if ($value < 1 || $value > 100) {
        fwrite(STDERR, "--per-clinician must be between 1 and 100\n");
        exit(2);
    }
    $perClinician = $value;
}
$dryRun = array_key_exists('dry-run', $opts);

// Active clinicians only. ``phimail-service``, ``portal-user`` and
// ``oe-system`` are seeded service accounts (see official_additional_users.sql)
// — they should never own a panel. Check both ``active`` and a non-empty
// username to filter out partially-deleted rows.
$rawClinicians = QueryUtils::fetchRecords(
    'SELECT id, username, fname, lname FROM users '
    . "WHERE active = 1 AND username IS NOT NULL AND username != '' "
    . "AND username NOT IN ('phimail-service', 'portal-user', 'oe-system') "
    . 'ORDER BY id',
);

/** @var list<array{id: int, username: string, fname: string, lname: string}> $clinicians */
$clinicians = [];
foreach ($rawClinicians as $row) {
    $id = $row['id'] ?? null;
    $username = $row['username'] ?? null;
    if (!is_numeric($id) || !is_string($username) || $username === '') {
        continue;
    }
    $fname = $row['fname'] ?? '';
    $lname = $row['lname'] ?? '';
    $clinicians[] = [
        'id' => (int) $id,
        'username' => $username,
        'fname' => is_string($fname) ? $fname : '',
        'lname' => is_string($lname) ? $lname : '',
    ];
}

if ($clinicians === []) {
    fwrite(STDERR, "No active clinicians found — nothing to assign.\n");
    exit(1);
}

$mode = $dryRun ? 'DRY-RUN' : 'EXECUTE';
echo "[$mode] Target panel size: $perClinician patients per clinician\n";
echo 'Found ' . count($clinicians) . " active clinician(s):\n";
foreach ($clinicians as $clinician) {
    echo "  id={$clinician['id']} username={$clinician['username']} ({$clinician['fname']} {$clinician['lname']})\n";
}
echo "\n";

$totalAssigned = 0;
foreach ($clinicians as $clinician) {
    $clinicianId = $clinician['id'];
    $username = $clinician['username'];

    $rawCount = QueryUtils::fetchSingleValue(
        'SELECT COUNT(*) AS n FROM patient_data WHERE providerID = ?',
        'n',
        [$clinicianId],
    );
    $currentCount = is_numeric($rawCount) ? (int) $rawCount : 0;
    $gap = $perClinician - $currentCount;
    if ($gap <= 0) {
        echo "$username (id=$clinicianId): already owns $currentCount patient(s) — skipping.\n";
        continue;
    }

    // Pick ``$gap`` random unassigned patients. ``ORDER BY RAND()`` is fine
    // at this scale (a few hundred rows max); avoids needing a separate
    // randomization pass. ``$gap`` is bounded (1..100) and integer, so
    // inlining it in LIMIT is safe.
    $rawCandidates = QueryUtils::fetchRecords(
        'SELECT pid, fname, lname FROM patient_data '
        . 'WHERE providerID IS NULL OR providerID = 0 '
        . 'ORDER BY RAND() '
        . 'LIMIT ' . $gap,
    );

    /** @var list<array{pid: int, fname: string, lname: string}> $candidates */
    $candidates = [];
    foreach ($rawCandidates as $row) {
        $pid = $row['pid'] ?? null;
        if (!is_numeric($pid)) {
            continue;
        }
        $fname = $row['fname'] ?? '';
        $lname = $row['lname'] ?? '';
        $candidates[] = [
            'pid' => (int) $pid,
            'fname' => is_string($fname) ? $fname : '',
            'lname' => is_string($lname) ? $lname : '',
        ];
    }

    if ($candidates === []) {
        echo "$username (id=$clinicianId): wanted $gap more, but no unassigned patients remain.\n";
        continue;
    }

    $pids = array_map(static fn(array $row): int => $row['pid'], $candidates);
    $placeholders = implode(', ', array_fill(0, count($pids), '?'));
    $sql = "UPDATE patient_data SET providerID = ? WHERE pid IN ($placeholders)";
    $binds = array_merge([$clinicianId], $pids);

    if ($dryRun) {
        echo "$username (id=$clinicianId): would assign " . count($pids) . " patient(s):\n";
    } else {
        QueryUtils::sqlStatementThrowException($sql, $binds);
        echo "$username (id=$clinicianId): assigned " . count($pids) . " patient(s):\n";
    }
    foreach ($candidates as $row) {
        echo "    pid={$row['pid']} {$row['fname']} {$row['lname']}\n";
    }

    $totalAssigned += count($pids);
}

echo "\n[$mode] Done. Total patients " . ($dryRun ? 'that would be assigned' : 'assigned') . ": $totalAssigned\n";
exit(0);
