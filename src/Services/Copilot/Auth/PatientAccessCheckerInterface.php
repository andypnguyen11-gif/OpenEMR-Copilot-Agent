<?php

/**
 * Per-patient access gate for the Co-Pilot gateway.
 *
 * The gateway mints a JWT whose ``patient_id`` claim is the value the
 * browser supplied in the request. Without a server-side check that the
 * authenticated clinician is allowed to access that patient, an authenticated
 * user could pivot to any other patient by editing the request body — the
 * downstream tool layer (``base.py::_enforce_rbac``) only verifies that the
 * tool-call's ``patient_id`` matches the JWT's claim, both of which trace
 * back to the same untrusted body. This interface is the boundary where the
 * gateway proves the (user, patient) pair is legitimate before any token
 * is signed.
 *
 * Implementations must be deterministic for a given (userId, patientId) pair
 * within a single request; the controller calls ``canAccess`` exactly once
 * per request and acts on the result.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\Auth;

interface PatientAccessCheckerInterface
{
    /**
     * Return true iff the authenticated clinician identified by ``$userId``
     * is permitted to operate on the patient identified by ``$patientId``.
     *
     * Empty inputs, missing patients, and any underlying lookup failure
     * resolve to ``false`` (deny). Callers do not need to pre-validate the
     * arguments — the gate is fail-closed.
     */
    public function canAccess(string $userId, string $patientId): bool;
}
