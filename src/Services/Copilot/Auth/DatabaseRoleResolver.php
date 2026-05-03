<?php

/**
 * Database-backed :class:`RoleResolverInterface`.
 *
 * Two queries against the ``users`` table:
 *
 * 1. ``physician_type`` for ``$userId`` — the resident discriminator.
 * 2. Existence of any other ``users`` row whose ``supervisor_id = $userId`` —
 *    the supervisor discriminator. ``LIMIT 1`` keeps the lookup O(1) on the
 *    indexed column.
 *
 * The two signals are folded into a :class:`Role` by the pure-function
 * :meth:`Role::fromPhysicianType` so the precedence rules live in one place
 * (resident wins over supervisor wins over physician). This class only does
 * the I/O.
 *
 * Fail-safe behavior: malformed ``$userId`` (empty, non-numeric, negative)
 * or a missing row both resolve to :enumcase:`Role::UNKNOWN` rather than
 * raising. The agent service's tool layer denies UNKNOWN at the next
 * boundary, so the deny-by-default property is preserved without converting
 * a benign lookup miss into a 5xx for an authenticated session.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\Auth;

use OpenEMR\Common\Database\QueryUtils;

final readonly class DatabaseRoleResolver implements RoleResolverInterface
{
    public function resolve(string $userId): Role
    {
        if ($userId === '' || !ctype_digit($userId)) {
            // Mirror DatabasePatientAccessChecker's input-shape guards:
            // anything that wouldn't bind cleanly into the int column is
            // rejected before the query. A non-numeric user_id reaching this
            // resolver is a session-layer bug; surfacing as UNKNOWN keeps the
            // request denied at the next boundary without a 5xx.
            return Role::UNKNOWN;
        }

        $row = QueryUtils::fetchRecords(
            'SELECT physician_type FROM users WHERE id = ? LIMIT 1',
            [$userId],
        );
        if ($row === []) {
            return Role::UNKNOWN;
        }
        $physicianTypeRaw = $row[0]['physician_type'] ?? null;
        $physicianType = is_string($physicianTypeRaw) && $physicianTypeRaw !== ''
            ? $physicianTypeRaw
            : null;

        // Existence query for the supervision relationship. ``id != ?`` guards
        // the degenerate self-supervision row; ``supervisor_id != 0`` excludes
        // the schema's "unassigned" sentinel which would otherwise match every
        // user whose id is 0 (none in practice, but cheap to be exact).
        $supervisorRows = QueryUtils::fetchRecords(
            'SELECT 1 AS hit FROM users '
            . 'WHERE supervisor_id = ? AND id != ? AND supervisor_id != 0 LIMIT 1',
            [$userId, $userId],
        );
        $isSupervisor = $supervisorRows !== [];

        return Role::fromPhysicianType($physicianType, $isSupervisor);
    }
}
