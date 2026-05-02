<?php

/**
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Fixtures;

use OpenEMR\Common\Database\QueryUtils;
use PHPUnit\Framework\TestCase;

class DiscrepancyFixtureManagerTest extends TestCase
{
    private DiscrepancyFixtureManager $fixtureManager;

    protected function setUp(): void
    {
        $this->fixtureManager = new DiscrepancyFixtureManager();
        // Defensive: clear any state left behind by an interrupted prior run
        // before each test, so we always assert against a clean baseline.
        $this->fixtureManager->removeFixtures();
    }

    protected function tearDown(): void
    {
        $this->fixtureManager->removeFixtures();
    }

    public function testScenariosFileShape(): void
    {
        $scenarios = $this->fixtureManager->getScenarios();
        $this->assertCount(5, $scenarios, 'Expected exactly five seeded conflict scenarios');

        $expectedNames = [
            'med_vs_note_conflict',
            'narrative_only_allergy',
            'resolved_problem_still_active',
            'allergen_med_safety_conflict',
            'stale_chronic_lab',
        ];
        $actualNames = array_column($scenarios, 'name');
        $this->assertSame($expectedNames, $actualNames);

        foreach ($scenarios as $scenario) {
            $this->assertArrayHasKey('pid', $scenario);
            $this->assertArrayHasKey('pubpid', $scenario);
            $this->assertArrayHasKey('patient', $scenario);
            $this->assertArrayHasKey('expected_flags', $scenario);
            $pubpid = $scenario['pubpid'];
            $this->assertIsString($pubpid);
            $this->assertStringStartsWith(
                DiscrepancyFixtureManager::PUBPID_PREFIX,
                $pubpid,
                'Every scenario must use the test-fixture-discrepancy- pubpid prefix',
            );
        }
    }

    public function testInstallAndRemoveRoundTrip(): void
    {
        $this->assertSame(0, $this->countSeededPatients(), 'Pre-condition: no seeded patients');

        $insertCount = $this->fixtureManager->installFixtures();
        $this->assertGreaterThan(5, $insertCount, 'Install should produce at least one row per scenario');

        // All five patients land
        $this->assertSame(5, $this->countSeededPatients());

        // Spot-check a child row from each table the scenarios touch
        $this->assertGreaterThan(0, $this->countSeededRows('lists', 'pid'));
        $this->assertGreaterThan(0, $this->countSeededRows('pnotes', 'pid'));
        $this->assertSame(1, $this->countProcedureOrders());

        // Round-trip: removeFixtures wipes everything we just installed
        $this->fixtureManager->removeFixtures();

        $this->assertSame(0, $this->countSeededPatients(), 'No seeded patients should remain after removeFixtures');
        $this->assertSame(0, $this->countSeededRows('lists', 'pid'));
        $this->assertSame(0, $this->countSeededRows('pnotes', 'pid'));
        $this->assertSame(0, $this->countProcedureOrders());
    }

    private function countSeededPatients(): int
    {
        return $this->scalarCount(
            'SELECT COUNT(*) AS c FROM `patient_data` WHERE `pubpid` LIKE ?',
            [DiscrepancyFixtureManager::PUBPID_PREFIX . '%'],
        );
    }

    private function countSeededRows(string $table, string $pidColumn): int
    {
        return $this->scalarCount(
            "SELECT COUNT(*) AS c FROM `$table` WHERE `$pidColumn` IN (SELECT `pid` FROM `patient_data` WHERE `pubpid` LIKE ?)",
            [DiscrepancyFixtureManager::PUBPID_PREFIX . '%'],
        );
    }

    private function countProcedureOrders(): int
    {
        return $this->scalarCount(
            'SELECT COUNT(*) AS c FROM `procedure_order` WHERE `procedure_order_id` = ?',
            [90001],
        );
    }

    /** @param array<int, mixed> $params */
    private function scalarCount(string $sql, array $params): int
    {
        $row = QueryUtils::querySingleRow($sql, $params);
        if (!is_array($row)) {
            return 0;
        }
        $count = $row['c'] ?? 0;
        return is_numeric($count) ? (int) $count : 0;
    }
}
