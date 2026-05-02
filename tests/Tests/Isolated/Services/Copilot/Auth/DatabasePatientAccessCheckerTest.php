<?php

/**
 * Isolated tests for :class:`DatabasePatientAccessChecker`'s input guards.
 *
 * The SQL path itself (a single-row lookup against ``patient_data``) needs
 * a live database and is exercised in the services test suite. Here we
 * pin the guard branches that must return false *before* any query is
 * issued, so a malformed session or body can't reach the database layer
 * at all:
 *
 * 1. Empty user id → deny.
 * 2. Empty patient id → deny.
 * 3. Non-digit user id → deny (would otherwise coerce to 0 in MySQL and
 *    match unassigned rows).
 * 4. Non-digit patient id → deny (same coercion hazard on the pid side).
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot\Auth;

use OpenEMR\Services\Copilot\Auth\DatabasePatientAccessChecker;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\TestCase;

final class DatabasePatientAccessCheckerTest extends TestCase
{
    #[DataProvider('rejectedInputProvider')]
    public function testRejectsMalformedInputBeforeQuery(string $userId, string $patientId): void
    {
        $checker = new DatabasePatientAccessChecker();
        // No DB stub: if the guard fails, fetchRecords will raise, and the
        // assertion below will never run — which is exactly the failure we
        // want to surface.
        self::assertFalse($checker->canAccess($userId, $patientId));
    }

    /**
     * @return array<string, array{string, string}>
     *
     * @codeCoverageIgnore Data providers run before coverage instrumentation starts.
     */
    public static function rejectedInputProvider(): array
    {
        return [
            'empty user id'                  => ['', '101'],
            'empty patient id'               => ['42', ''],
            'both empty'                     => ['', ''],
            'non-digit user id'              => ['abc', '101'],
            'non-digit patient id'           => ['42', 'abc'],
            'negative user id'               => ['-1', '101'],
            'negative patient id'            => ['42', '-1'],
            'user id with leading whitespace' => [' 42', '101'],
            'patient id with trailing chars' => ['42', '101x'],
        ];
    }
}
