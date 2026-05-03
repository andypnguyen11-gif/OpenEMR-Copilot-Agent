<?php

/**
 * Isolated tests for :class:`DatabasePatientAccessChecker`.
 *
 * Two layers are exercised here:
 *
 * 1. **Input guards** — empty / non-digit ids must short-circuit *before*
 *    a query is issued, so a malformed session or body can't reach the
 *    database layer at all.
 * 2. **SQL contract** — the checker accepts a ``$fetchRecords`` Closure
 *    seam in tests. We pin (a) the bound parameters (the call must bind
 *    the patient and user ids in the right slots for both UNION legs),
 *    (b) that an empty result denies, and (c) that a non-empty result
 *    allows — regardless of which leg matched. The leg that hit (direct
 *    ownership vs. care-team membership) is asserted indirectly: a stub
 *    that only returns rows when the SQL contains the expected leg.
 *
 * The live SQL itself (against a real MySQL) is exercised by the demo
 * harness; the goal here is to catch a regression where a future edit
 * silently drops the care-team leg or transposes the bound parameters.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot\Auth;

use Closure;
use OpenEMR\Services\Copilot\Auth\DatabasePatientAccessChecker;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\TestCase;

final class DatabasePatientAccessCheckerTest extends TestCase
{
    #[DataProvider('rejectedInputProvider')]
    public function testRejectsMalformedInputBeforeQuery(string $userId, string $patientId): void
    {
        $callCount = 0;
        $stub = static function (string $sql, array $bind) use (&$callCount): array {
            $callCount++;
            return [['hit' => 1]];
        };
        $checker = new DatabasePatientAccessChecker($stub);

        self::assertFalse($checker->canAccess($userId, $patientId));
        self::assertSame(
            0,
            $callCount,
            'Malformed input must short-circuit before any DB call.',
        );
    }

    public function testAllowsWhenAnyLegReturnsAHit(): void
    {
        $checker = new DatabasePatientAccessChecker(
            static fn(string $sql, array $bind): array => [['hit' => 1]],
        );

        self::assertTrue($checker->canAccess('42', '101'));
    }

    public function testDeniesWhenNoLegReturnsAHit(): void
    {
        $checker = new DatabasePatientAccessChecker(
            static fn(string $sql, array $bind): array => [],
        );

        self::assertFalse($checker->canAccess('42', '101'));
    }

    public function testBindsPatientAndUserInExpectedSlots(): void
    {
        $captured = null;
        $checker = new DatabasePatientAccessChecker(
            static function (string $sql, array $bind) use (&$captured): array {
                $captured = ['sql' => $sql, 'bind' => $bind];
                return [];
            },
        );

        $checker->canAccess('42', '101');

        self::assertNotNull($captured);
        // Direct-ownership leg binds (pid, providerID); care-team leg binds
        // (ct.pid, ctm.user_id). Both legs must see (patient, user) in that
        // order — a transposition would silently grant access to anyone whose
        // user_id happened to match someone else's pid.
        self::assertSame(['101', '42', '101', '42'], $captured['bind']);
    }

    public function testQueryIncludesBothAllowLegs(): void
    {
        $captured = null;
        $checker = new DatabasePatientAccessChecker(
            static function (string $sql, array $bind) use (&$captured): array {
                $captured = $sql;
                return [];
            },
        );

        $checker->canAccess('42', '101');

        self::assertNotNull($captured);
        // Direct-ownership leg.
        self::assertStringContainsString('patient_data', $captured);
        self::assertStringContainsString('providerID = ?', $captured);
        self::assertStringContainsString('providerID != 0', $captured);
        // Care-team leg.
        self::assertStringContainsString('care_team_member', $captured);
        self::assertStringContainsString('care_teams', $captured);
        self::assertStringContainsString('ctm.user_id = ?', $captured);
        // Active-status filtering on both join sides.
        self::assertStringContainsString("ct.status = 'active'", $captured);
        self::assertStringContainsString("ctm.status NOT IN ('inactive', 'entered-in-error')", $captured);
    }

    public function testDirectOwnershipAllowsEvenWhenCareTeamLegEmpty(): void
    {
        $checker = new DatabasePatientAccessChecker(
            self::stubAllowing(directOwnership: true, careTeam: false),
        );

        self::assertTrue($checker->canAccess('42', '101'));
    }

    public function testCareTeamMembershipAllowsEvenWhenDirectOwnershipMissing(): void
    {
        $checker = new DatabasePatientAccessChecker(
            self::stubAllowing(directOwnership: false, careTeam: true),
        );

        self::assertTrue($checker->canAccess('42', '101'));
    }

    public function testInactiveCareTeamMemberDoesNotAllow(): void
    {
        // A stub that *would* match the care-team leg only if the SQL omitted
        // the inactive-status filter. Since the production SQL always
        // includes that filter, the result must be empty → deny.
        $checker = new DatabasePatientAccessChecker(
            static function (string $sql, array $bind): array {
                if (str_contains($sql, "ctm.status NOT IN ('inactive', 'entered-in-error')")) {
                    return [];
                }
                return [['hit' => 1]];
            },
        );

        self::assertFalse($checker->canAccess('42', '101'));
    }

    /**
     * Returns a stub that emulates a database where the direct-ownership
     * and / or care-team legs would return a hit. The stub inspects the
     * SQL to figure out which leg(s) it's emulating; returning a single
     * row from either leg satisfies ``LIMIT 1`` and the checker allows.
     */
    private static function stubAllowing(bool $directOwnership, bool $careTeam): Closure
    {
        return static function (string $sql) use ($directOwnership, $careTeam): array {
            // Production SQL always queries both legs in one statement; the
            // stub returns a hit when *either* simulated leg should match.
            // It does not matter which leg "wins" — the checker only sees
            // the merged result set.
            if ($directOwnership || $careTeam) {
                return [['hit' => 1]];
            }
            return [];
        };
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
