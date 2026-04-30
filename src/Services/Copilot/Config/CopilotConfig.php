<?php

/**
 * Copilot gateway configuration.
 *
 * Typed accessor over OEGlobalsBag for Clinical Co-Pilot settings. The PHP
 * gateway only needs a couple of values at this stage (agent service base URL
 * and request timeout); more arrive in later PRs (HMAC secret in PR 4, etc.).
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\Config;

use OpenEMR\Core\OEGlobalsBag;

final readonly class CopilotConfig
{
    public function __construct(private OEGlobalsBag $globals)
    {
    }

    /**
     * Base URL of the agent service (no trailing slash). The gateway prepends
     * this to relative paths like ``/healthz``.
     */
    public function getAgentBaseUrl(): string
    {
        $url = $this->globals->getString('copilot_agent_base_url', 'http://localhost:8500');
        return rtrim($url, '/');
    }

    /**
     * Per-request timeout in seconds for calls to the agent service. Kept
     * short — the gateway is a thin proxy and a hung agent service should not
     * stall OpenEMR worker threads.
     */
    public function getAgentTimeoutSeconds(): int
    {
        $timeout = $this->globals->getInt('copilot_agent_timeout_seconds', 5);
        return $timeout > 0 ? $timeout : 5;
    }

    /**
     * HS256 secret shared with the agent service. The Python verifier on
     * the other side reads the same byte string from ``COPILOT_HMAC_SECRET``;
     * rotation must happen on both sides together (see the agent service
     * README for the procedure).
     *
     * @throws CopilotConfigException When the secret is unset or short
     *                                enough to weaken HS256 below the
     *                                256-bit security level.
     */
    public function getJwtSecret(): string
    {
        $secret = $this->globals->getString('copilot_jwt_secret', '');
        if ($secret === '') {
            throw new CopilotConfigException('copilot_jwt_secret is not configured');
        }
        if (strlen($secret) < 32) {
            // HS256 takes any byte string, but anything shorter than the
            // 256-bit output digest weakens the security margin without
            // any operational benefit. Treating it as a misconfiguration
            // matches what the agent service's verifier expects.
            throw new CopilotConfigException(
                'copilot_jwt_secret must be at least 32 bytes',
            );
        }
        return $secret;
    }

    /**
     * Standard MVP scope set the gateway grants to a chat session when the
     * user's stored scopes are empty. PR 18 replaces this with a per-role
     * lookup; for the M-PR demo every logged-in clinician can see the full
     * read surface, and the per-clinician check that matters at the
     * agent-side tool layer is the patient-id binding (see Tool ABC's
     * RBAC enforcement).
     *
     * @return list<string>
     */
    public function getStandardScopes(): array
    {
        return [
            'system/Patient.read',
            'system/Condition.read',
            'system/MedicationRequest.read',
            'system/MedicationStatement.read',
            'system/AllergyIntolerance.read',
            'system/Observation.read',
            'system/Encounter.read',
            'system/DocumentReference.read',
        ];
    }
}
