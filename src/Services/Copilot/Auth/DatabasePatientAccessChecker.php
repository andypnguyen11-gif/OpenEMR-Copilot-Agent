<?php

/**
 * Database-backed :class:`PatientAccessCheckerInterface` for the gateway.
 *
 * A clinician may operate on a patient when *either* of these holds:
 *
 * 1. **Direct ownership** — ``patient_data.providerID`` equals the
 *    authenticated clinician's ``users.id``. This is the strongest readily
 *    available ownership signal in stock OpenEMR and is set during patient
 *    registration / chart open.
 * 2. **Care-team / coverage panel membership** — the clinician is an active
 *    ``care_team_member`` of an active ``care_teams`` row for this patient.
 *    This covers the cross-coverage case from PRD §6 ("physician — full read
 *    on assigned cross-coverage panel") so a covering attending isn't 403'd
 *    when the patient's primary ``providerID`` belongs to someone else.
 *
 * Fail-closed semantics:
 *
 * * Patient row missing → deny (no row to compare).
 * * ``providerID`` is ``NULL`` or ``0`` (unassigned) and no active care-team
 *   membership exists → deny.
 * * A care-team member row whose ``status`` is ``inactive`` /
 *   ``entered-in-error`` does not grant access.
 * * A care-team whose ``status`` is ``inactive`` does not grant access.
 *   ``status = 'active'`` and ``status IS NULL`` (legacy rows) both count
 *   as active, matching :class:`CareTeamService`'s convention.
 * * Any underlying SQL error propagates to the caller as an exception; the
 *   controller treats that as deny + 5xx, never as allow.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\Auth;

use Closure;
use OpenEMR\Common\Database\QueryUtils;

final readonly class DatabasePatientAccessChecker implements PatientAccessCheckerInterface
{
    /**
     * SQL is hoisted to a constant so tests can pin its shape without
     * re-running the production path. Two ``UNION ALL`` legs encode the
     * two allow paths; ``LIMIT 1`` short-circuits as soon as either hits.
     */
    private const ACCESS_QUERY = 'SELECT 1 AS hit FROM patient_data '
        . 'WHERE pid = ? AND providerID = ? AND providerID != 0 '
        . 'UNION ALL '
        . 'SELECT 1 AS hit FROM care_team_member ctm '
        . 'JOIN care_teams ct ON ct.id = ctm.care_team_id '
        . 'WHERE ct.pid = ? AND ctm.user_id = ? '
        . "AND (ct.status = 'active' OR ct.status IS NULL) "
        . "AND ctm.status NOT IN ('inactive', 'entered-in-error') "
        . 'LIMIT 1';

    /** @var Closure(string, list<scalar>): list<array<mixed>> */
    private Closure $fetchRecords;

    /**
     * @param (Closure(string, list<scalar>): list<array<mixed>>)|null $fetchRecords
     *        Test seam. Production callers pass nothing and get a wrapper
     *        around :method:`QueryUtils::fetchRecords`.
     */
    public function __construct(?Closure $fetchRecords = null)
    {
        $this->fetchRecords = $fetchRecords ?? self::queryUtilsFetch(...);
    }

    /**
     * Default ``$fetchRecords`` implementation. Pulled out of the
     * constructor so its parameter and return types match the property's
     * declared closure signature exactly (PHPStan rejects an inline
     * ``array``-typed lambda as too wide).
     *
     * @param list<scalar> $bind
     *
     * @return list<array<mixed>>
     */
    private static function queryUtilsFetch(string $sql, array $bind): array
    {
        return QueryUtils::fetchRecords($sql, $bind);
    }

    public function canAccess(string $userId, string $patientId): bool
    {
        if ($userId === '' || $patientId === '') {
            return false;
        }
        // ctype_digit guards against the userId / patientId strings carrying
        // anything that wouldn't bind cleanly into the int columns. Without
        // the guard, MySQL silently coerces "abc" to 0 and a request for
        // patient "abc" would match any patient whose providerID happens to
        // be 0 (unassigned) for a user whose id coerces to 0 (also possible
        // for malformed sessions). Reject before the bind.
        if (!ctype_digit($userId) || !ctype_digit($patientId)) {
            return false;
        }

        $rows = ($this->fetchRecords)(
            self::ACCESS_QUERY,
            [$patientId, $userId, $patientId, $userId],
        );
        return $rows !== [];
    }
}
