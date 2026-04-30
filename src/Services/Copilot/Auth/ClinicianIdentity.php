<?php

/**
 * Typed value object describing the clinician on whose behalf the gateway is
 * about to call the Clinical Co-Pilot agent service.
 *
 * Built once per request by :class:`SessionMapper`, consumed once by
 * :class:`JwtSigner`. Keeping it ``readonly`` means a downstream collaborator
 * cannot quietly mutate the user/patient binding between mint and send.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\Auth;

final readonly class ClinicianIdentity
{
    /**
     * @param list<string> $scopes SMART-on-FHIR-style scope strings the
     *                             agent's tool layer will key off of.
     */
    public function __construct(
        public string $userId,
        public string $role,
        public string $patientId,
        public array $scopes,
    ) {
    }
}
