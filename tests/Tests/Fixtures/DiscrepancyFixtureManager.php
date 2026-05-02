<?php

/**
 * Discrepancy fixture manager — installs the seeded conflict scenarios from
 * `discrepancy-scenarios.php` into a live OpenEMR database for PHPUnit
 * integration tests against the discrepancy engine (PR 13b/c).
 *
 * Multi-table fixture; each scenario contributes:
 *   - one patient_data row (anchor — fixed pid + pubpid `test-fixture-discrepancy-*`)
 *   - 0..N rows in lists, pnotes, prescriptions, and the procedure_order /
 *     procedure_report / procedure_result chain (for the stale-lab scenario)
 *
 * The same scenarios source-of-truth feeds `bin/generate-discrepancy-sql.php`,
 * which emits an `mysql <`-loadable SQL file for demo deploys. Drift between
 * the two paths is gated by `composer fixture-check`.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Fixtures;

use OpenEMR\Common\Database\QueryUtils;

class DiscrepancyFixtureManager extends BaseFixtureManager
{
    public const SCENARIOS_FILE = 'discrepancy-scenarios.php';
    public const PUBPID_PREFIX = 'test-fixture-discrepancy-';

    /** @var array<int, array<string, mixed>> */
    private readonly array $scenarios;

    public function __construct()
    {
        // patient_data is the anchor table; child rows resolve via fixed pid.
        parent::__construct(self::SCENARIOS_FILE, 'patient_data');
        /** @var array<int, array<string, mixed>> $loaded */
        $loaded = $this->loadPhpFile(self::SCENARIOS_FILE);
        $this->scenarios = $loaded;
    }

    /** @return array<int, array<string, mixed>> */
    public function getScenarios(): array
    {
        return $this->scenarios;
    }

    /** @return array<string, mixed>|null */
    public function getScenarioByName(string $name): ?array
    {
        foreach ($this->scenarios as $scenario) {
            if (($scenario['name'] ?? null) === $name) {
                return $scenario;
            }
        }
        return null;
    }

    public function installFixtures(): int
    {
        $insertCount = 0;
        foreach ($this->scenarios as $scenario) {
            $insertCount += $this->installScenario($scenario);
        }
        return $insertCount;
    }

    /** @param array<string, mixed> $scenario */
    private function installScenario(array $scenario): int
    {
        $pidValue = $scenario['pid'];
        $pubpidValue = $scenario['pubpid'];
        if (!is_int($pidValue) || !is_string($pubpidValue)) {
            throw new \RuntimeException('Scenario must define integer pid and string pubpid');
        }
        $pid = $pidValue;
        $pubpid = $pubpidValue;
        $count = 0;

        /** @var array<string, mixed> $patient */
        $patient = $scenario['patient'];
        $patient['pid'] = $pid;
        $patient['pubpid'] = $pubpid;
        $patient['uuid'] = $this->getUuid('patient_data');
        $patient['date'] ??= '2024-06-01 00:00:00';
        $this->insertRow('patient_data', $patient);
        $count++;

        /** @var array<int, array<string, mixed>> $listsRows */
        $listsRows = $scenario['lists'] ?? [];
        foreach ($listsRows as $row) {
            $row['pid'] = $pid;
            $this->insertRow('lists', $row);
            $count++;
        }

        /** @var array<int, array<string, mixed>> $pnotesRows */
        $pnotesRows = $scenario['pnotes'] ?? [];
        foreach ($pnotesRows as $row) {
            $row['pid'] = $pid;
            $this->insertRow('pnotes', $row);
            $count++;
        }

        /** @var array<int, array<string, mixed>> $prescriptionsRows */
        $prescriptionsRows = $scenario['prescriptions'] ?? [];
        foreach ($prescriptionsRows as $row) {
            $row['patient_id'] = $pid;
            $this->insertRow('prescriptions', $row);
            $count++;
        }

        /** @var array<int, array<string, mixed>> $orderRows */
        $orderRows = $scenario['procedure_orders'] ?? [];
        foreach ($orderRows as $row) {
            $row['patient_id'] = $pid;
            $this->insertRow('procedure_order', $row);
            $count++;
        }

        /** @var array<int, array<string, mixed>> $reportRows */
        $reportRows = $scenario['procedure_reports'] ?? [];
        foreach ($reportRows as $row) {
            $this->insertRow('procedure_report', $row);
            $count++;
        }

        /** @var array<int, array<string, mixed>> $resultRows */
        $resultRows = $scenario['procedure_results'] ?? [];
        foreach ($resultRows as $row) {
            $this->insertRow('procedure_result', $row);
            $count++;
        }

        return $count;
    }

    /** @param array<string, mixed> $row */
    private function insertRow(string $tableName, array $row): void
    {
        $sqlColumns = '';
        $sqlBinds = [];
        foreach ($row as $field => $value) {
            $sqlColumns .= '`' . $field . '` = ?, ';
            $sqlBinds[] = $value;
        }
        $sqlColumns = rtrim($sqlColumns, ' ,');
        QueryUtils::sqlInsert(
            'INSERT INTO ' . escape_table_name($tableName) . ' SET ' . $sqlColumns,
            $sqlBinds,
        );
    }

    protected function removeInstalledFixtures(): void
    {
        $bind = self::PUBPID_PREFIX . '%';

        // Snapshot pids and uuids before we delete patient_data rows; child cleanups
        // need the pids and uuid_registry needs the uuids.
        /** @var array<int, int|string> $pids */
        $pids = QueryUtils::fetchTableColumn(
            'SELECT `pid` FROM `patient_data` WHERE `pubpid` LIKE ?',
            'pid',
            [$bind],
        );

        // Procedure chain — fixed ids declared in the scenario file. We delete
        // by those explicit ids so we never touch unrelated rows even if a
        // demo deploy left orphaned procedure_orders behind.
        $procedureOrderIds = $this->collectFixedIds('procedure_orders', 'procedure_order_id');
        if (count($procedureOrderIds) > 0) {
            $orderPlaceholders = implode(',', array_fill(0, count($procedureOrderIds), '?'));
            /** @var array<int, int|string> $reportIds */
            $reportIds = QueryUtils::fetchTableColumn(
                "SELECT `procedure_report_id` FROM `procedure_report` WHERE `procedure_order_id` IN ($orderPlaceholders)",
                'procedure_report_id',
                $procedureOrderIds,
            );
            if (count($reportIds) > 0) {
                $reportPlaceholders = implode(',', array_fill(0, count($reportIds), '?'));
                QueryUtils::sqlStatementThrowException(
                    "DELETE FROM `procedure_result` WHERE `procedure_report_id` IN ($reportPlaceholders)",
                    $reportIds,
                );
            }
            QueryUtils::sqlStatementThrowException(
                "DELETE FROM `procedure_report` WHERE `procedure_order_id` IN ($orderPlaceholders)",
                $procedureOrderIds,
            );
            QueryUtils::sqlStatementThrowException(
                "DELETE FROM `procedure_order` WHERE `procedure_order_id` IN ($orderPlaceholders)",
                $procedureOrderIds,
            );
        }

        if (count($pids) === 0) {
            return;
        }
        $pidPlaceholders = implode(',', array_fill(0, count($pids), '?'));

        QueryUtils::sqlStatementThrowException(
            "DELETE FROM `lists` WHERE `pid` IN ($pidPlaceholders)",
            $pids,
        );
        QueryUtils::sqlStatementThrowException(
            "DELETE FROM `pnotes` WHERE `pid` IN ($pidPlaceholders)",
            $pids,
        );
        QueryUtils::sqlStatementThrowException(
            "DELETE FROM `prescriptions` WHERE `patient_id` IN ($pidPlaceholders)",
            $pids,
        );

        // Patient uuids in registry — must be cleaned before the patient_data
        // delete (we read uuids back out of patient_data here).
        /** @var array<int, string> $patientUuids */
        $patientUuids = QueryUtils::fetchTableColumn(
            'SELECT `uuid` FROM `patient_data` WHERE `pubpid` LIKE ?',
            'uuid',
            [$bind],
        );
        foreach ($patientUuids as $uuid) {
            QueryUtils::sqlStatementThrowException(
                "DELETE FROM `uuid_registry` WHERE `table_name` = 'patient_data' AND `uuid` = ?",
                [$uuid],
            );
        }

        QueryUtils::sqlStatementThrowException(
            'DELETE FROM `patient_data` WHERE `pubpid` LIKE ?',
            [$bind],
        );
    }

    /** @return array<int, int> */
    private function collectFixedIds(string $rowsKey, string $idField): array
    {
        $ids = [];
        foreach ($this->scenarios as $scenario) {
            /** @var array<int, array<string, mixed>> $rows */
            $rows = $scenario[$rowsKey] ?? [];
            foreach ($rows as $row) {
                $value = $row[$idField] ?? null;
                if (is_int($value)) {
                    $ids[] = $value;
                }
            }
        }
        return $ids;
    }
}
