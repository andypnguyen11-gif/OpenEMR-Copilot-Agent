<?php

/**
 * Isolated tests for :class:`Role` and its OpenEMR ``users``-row factory.
 *
 * The factory is a pure function: given a ``physician_type`` string and an
 * ``isSupervisor`` flag, it deterministically picks one of four roles. The
 * precedence rules — resident wins over supervisor wins over physician,
 * null collapses to UNKNOWN — are PRD §6 / USERS §1.4 commitments. Pinning
 * them here means a future maintainer cannot accidentally flip the order
 * (which would silently grant attending-level read to a senior resident
 * supervising juniors, or demote an unassigned user from UNKNOWN to
 * PHYSICIAN).
 *
 * The data provider covers every permutation of the two inputs against the
 * stock OpenEMR ``physician_type`` list options that matter for our 3-role
 * MVP. Other physician_type values (general_physician, specialized_physician,
 * etc.) all behave identically — they're attending-class clinicians — and a
 * single representative case stands in for them.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot\Auth;

use OpenEMR\Services\Copilot\Auth\Role;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\TestCase;

final class RoleTest extends TestCase
{
    public function testEnumCasesMatchTheJwtClaimWireFormat(): void
    {
        // The string values are the contract with the agent service's
        // matching Role StrEnum. Any rename here breaks JWT round-trip
        // until both sides ship together — pin the values explicitly.
        // Compare as a single map so PHPStan doesn't flag each line as
        // "always true" (the enum's values are statically known); the
        // map shape also doubles as a regression guard against an
        // accidentally added or removed case.
        $actual = [];
        foreach (Role::cases() as $case) {
            $actual[$case->name] = $case->value;
        }
        self::assertSame(
            [
                'UNKNOWN' => 'unknown',
                'PHYSICIAN' => 'physician',
                'RESIDENT' => 'resident',
                'SUPERVISOR' => 'supervisor',
            ],
            $actual,
        );
    }

    #[DataProvider('mappingProvider')]
    public function testFromPhysicianTypeMapsOpenemrRowOntoRole(
        ?string $physicianType,
        bool $isSupervisor,
        Role $expected,
    ): void {
        self::assertSame(
            $expected,
            Role::fromPhysicianType($physicianType, $isSupervisor),
        );
    }

    /**
     * @return array<string, array{0: ?string, 1: bool, 2: Role}>
     *
     * @codeCoverageIgnore Data providers run before coverage instrumentation starts.
     */
    public static function mappingProvider(): array
    {
        return [
            // Resident wins over supervisor. A senior resident supervising
            // juniors stays a resident — operationally still in training,
            // every action still audit-logged. Demoting them to PHYSICIAN
            // on the supervision signal would silently grant attending
            // scopes the PRD reserves for non-residents.
            'resident, not supervising' => [
                'resident_physician', false, Role::RESIDENT,
            ],
            'resident, also supervising' => [
                'resident_physician', true, Role::RESIDENT,
            ],

            // Supervisor wins over plain physician. The supervisor role is
            // attending + audit visibility on supervised residents
            // (USERS §1.4); it requires the supervision relationship to
            // actually exist in the users table.
            'attending who supervises' => [
                'attending_physician', true, Role::SUPERVISOR,
            ],
            'general physician who supervises' => [
                'general_physician', true, Role::SUPERVISOR,
            ],

            // Plain physician — any non-null physician_type that isn't
            // resident_physician collapses to PHYSICIAN when the user
            // isn't supervising anyone. Representative case stands in
            // for the 13 other attending-class options (consultant,
            // chest, occupational, etc.) which all behave the same.
            'attending, no supervision' => [
                'attending_physician', false, Role::PHYSICIAN,
            ],
            'general physician, no supervision' => [
                'general_physician', false, Role::PHYSICIAN,
            ],

            // UNKNOWN is the explicit "resolver couldn't classify"
            // sentinel. Null physician_type and empty string both land
            // here when the user is also not supervising anyone — never
            // silently promoted to PHYSICIAN.
            'null physician_type, no supervision' => [
                null, false, Role::UNKNOWN,
            ],
            'empty physician_type, no supervision' => [
                '', false, Role::UNKNOWN,
            ],

            // Supervision alone (no physician_type) still resolves to
            // SUPERVISOR. In practice this row shape shouldn't exist in
            // OpenEMR — supervisors are attendings — but the deterministic
            // mapping is what matters: the supervision relationship is
            // strong enough on its own to discriminate.
            'null physician_type but supervises' => [
                null, true, Role::SUPERVISOR,
            ],
        ];
    }
}
