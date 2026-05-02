<?php

/**
 * Generate `sql/example_discrepancy_data.sql` from
 * `tests/Tests/Fixtures/discrepancy-scenarios.php`.
 *
 * Usage:
 *   php bin/generate-discrepancy-sql.php           # writes the SQL file
 *   php bin/generate-discrepancy-sql.php --check   # exits non-zero if the
 *                                                  # checked-in file is stale
 *
 * The single source of truth for the seeded discrepancy scenarios lives in
 * `tests/Tests/Fixtures/discrepancy-scenarios.php`. The PHP fixture manager
 * (DiscrepancyFixtureManager) consumes it directly for PHPUnit tests; this
 * script renders the same scenarios as a flat INSERT-only SQL file so the
 * Railway demo and the Python eval suite (which load via `mysql <`) see the
 * exact same scenarios. Drift between the two paths is gated by
 * `composer fixture-check`.
 *
 * The generated file is checked in so demo deploys do not need PHP installed
 * at install time.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

$repoRoot = dirname(__DIR__);
$scenariosFile = $repoRoot . '/tests/Tests/Fixtures/discrepancy-scenarios.php';
$outputFile = $repoRoot . '/sql/example_discrepancy_data.sql';

if (!is_file($scenariosFile)) {
    fwrite(STDERR, "Scenarios file not found: $scenariosFile\n");
    exit(1);
}

/** @var array<int, array<string, mixed>> $scenarios */
$scenarios = require $scenariosFile;

$rendered = renderSqlFile($scenarios);

$check = in_array('--check', $argv, true);
if ($check) {
    if (!is_file($outputFile)) {
        fwrite(STDERR, "Generated SQL file does not exist: $outputFile\n");
        fwrite(STDERR, "Run: php bin/generate-discrepancy-sql.php\n");
        exit(1);
    }
    $current = (string) file_get_contents($outputFile);
    if ($current !== $rendered) {
        fwrite(STDERR, "$outputFile is out of date relative to discrepancy-scenarios.php.\n");
        fwrite(STDERR, "Run: php bin/generate-discrepancy-sql.php\n");
        exit(1);
    }
    echo "$outputFile is up to date.\n";
    exit(0);
}

file_put_contents($outputFile, $rendered);
echo "Wrote $outputFile\n";
exit(0);

/**
 * @param array<int, array<string, mixed>> $scenarios
 */
function renderSqlFile(array $scenarios): string
{
    $lines = [];
    $lines[] = '-- Generated from tests/Tests/Fixtures/discrepancy-scenarios.php — do not edit.';
    $lines[] = '-- Run: php bin/generate-discrepancy-sql.php';
    $lines[] = '--';
    $lines[] = '-- Loads five seeded conflict scenarios for the discrepancy engine.';
    $lines[] = '-- Apply after sql/example_patient_data.sql:';
    $lines[] = '--   mysql -u openemr -p openemr < sql/example_discrepancy_data.sql';
    $lines[] = '';

    foreach ($scenarios as $scenario) {
        $lines[] = renderScenario($scenario);
    }

    return implode("\n", $lines);
}

/**
 * @param array<string, mixed> $scenario
 */
function renderScenario(array $scenario): string
{
    $name = (string) $scenario['name'];
    $pid = (int) $scenario['pid'];
    $pubpid = (string) $scenario['pubpid'];

    $section = [];
    $section[] = '-- --------------------------------------------------------';
    $section[] = "-- Scenario: $name (pid=$pid, pubpid=$pubpid)";
    $section[] = '-- ' . (string) $scenario['description'];
    $section[] = '';

    /** @var array<string, mixed> $patient */
    $patient = $scenario['patient'];
    $patient['pid'] = $pid;
    $patient['pubpid'] = $pubpid;
    $patient['date'] ??= '2024-06-01 00:00:00';
    $section[] = renderInsert('patient_data', $patient);

    foreach (($scenario['lists'] ?? []) as $row) {
        $row['pid'] = $pid;
        $section[] = renderInsert('lists', $row);
    }
    foreach (($scenario['pnotes'] ?? []) as $row) {
        $row['pid'] = $pid;
        $section[] = renderInsert('pnotes', $row);
    }
    foreach (($scenario['prescriptions'] ?? []) as $row) {
        $row['patient_id'] = $pid;
        $section[] = renderInsert('prescriptions', $row);
    }
    foreach (($scenario['procedure_orders'] ?? []) as $row) {
        $row['patient_id'] = $pid;
        $section[] = renderInsert('procedure_order', $row);
    }
    foreach (($scenario['procedure_reports'] ?? []) as $row) {
        $section[] = renderInsert('procedure_report', $row);
    }
    foreach (($scenario['procedure_results'] ?? []) as $row) {
        $section[] = renderInsert('procedure_result', $row);
    }

    $section[] = '';
    return implode("\n", $section);
}

/**
 * @param array<string, mixed> $row
 */
function renderInsert(string $table, array $row): string
{
    $columns = array_keys($row);
    $columnList = implode(', ', array_map(fn(string $c): string => "`$c`", $columns));
    $values = array_map(formatSqlValue(...), array_values($row));
    $valueList = implode(', ', $values);
    return "INSERT INTO `$table` ($columnList) VALUES ($valueList);";
}

function formatSqlValue(mixed $value): string
{
    if ($value === null) {
        return 'NULL';
    }
    if (is_bool($value)) {
        return $value ? '1' : '0';
    }
    if (is_int($value) || is_float($value)) {
        return (string) $value;
    }
    return "'" . str_replace(["\\", "'"], ["\\\\", "\\'"], (string) $value) . "'";
}
