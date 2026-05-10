<?php

/**
 * Install the prek pre-push hook after `composer install`.
 *
 * Best-effort and never fails the install: skips in CI, skips when `.git`
 * is absent (tarball install), and emits a single WARN line to stderr if
 * `prek` is not on PATH.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <apnguyen713@gmail.com>
 * @copyright Copyright (c) 2026 OpenEMR contributors
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

use Symfony\Component\Process\Exception\RuntimeException as ProcessRuntimeException;
use Symfony\Component\Process\ExecutableFinder;
use Symfony\Component\Process\Process;

$repoRoot = dirname(__DIR__);

$autoload = $repoRoot . '/vendor/autoload.php';
if (!file_exists($autoload)) {
    exit(0);
}
require $autoload;

if (getenv('CI') === 'true') {
    exit(0);
}

if (!is_dir($repoRoot . '/.git')) {
    exit(0);
}

$prekPath = (new ExecutableFinder())->find('prek');
if ($prekPath === null) {
    fwrite(STDERR, "WARN: prek not found on PATH; skipping pre-push hook install. Install with `brew install prek` or `pipx install prek` to enable git pre-push validation.\n");
    exit(0);
}

$process = new Process([$prekPath, 'install', '--hook-type', 'pre-push'], $repoRoot);
$process->setTimeout(30.0);

$launchError = null;
try {
    $process->run();
} catch (ProcessRuntimeException $e) {
    $launchError = $e->getMessage();
}

if ($launchError !== null) {
    fwrite(STDERR, sprintf("WARN: prek install failed to launch (%s); skipping pre-push hook install.\n", $launchError));
    exit(0);
}

if (!$process->isSuccessful()) {
    $exitCode = $process->getExitCode() ?? -1;
    fwrite(STDERR, sprintf("WARN: prek install exited %d; pre-push hook may not be active. %s\n", $exitCode, trim($process->getErrorOutput())));
    exit(0);
}

$stdout = $process->getOutput();
if (trim($stdout) !== '') {
    echo $stdout;
}
exit(0);
