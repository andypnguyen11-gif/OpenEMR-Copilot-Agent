<?php

/**
 * Database-backed :class:`PatientAccessCheckerInterface` for the gateway.
 *
 * MVP rule: a clinician may operate on a patient when the patient's
 * ``patient_data.providerID`` row matches the authenticated clinician's
 * ``users.id``. The provider-assignment column is the strongest readily
 * available ownership signal in stock OpenEMR and is set during patient
 * registration / chart open. Cross-coverage panels (PRD §6 — "physician —
 * full read on assigned cross-coverage panel") are intentionally rejected
 * here until PR 18 lands the panel data model; they degrade safely to
 * "denied with 403", not to "silent leak".
 *
 * Fail-closed semantics:
 *
 * * Patient row missing → deny (no row to compare).
 * * ``providerID`` is ``NULL`` or ``0`` (unassigned) → deny.
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

use OpenEMR\Common\Database\QueryUtils;

final readonly class DatabasePatientAccessChecker implements PatientAccessCheckerInterface
{
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

        $rows = QueryUtils::fetchRecords(
            'SELECT 1 AS hit FROM patient_data '
            . 'WHERE pid = ? AND providerID = ? AND providerID != 0 LIMIT 1',
            [$patientId, $userId],
        );
        return $rows !== [];
    }
}
