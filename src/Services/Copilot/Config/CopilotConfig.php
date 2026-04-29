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
}
