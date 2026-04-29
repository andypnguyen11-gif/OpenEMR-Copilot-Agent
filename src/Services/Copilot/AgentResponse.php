<?php

/**
 * Decoded response from the Clinical Co-Pilot agent service.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot;

final readonly class AgentResponse
{
    /**
     * @param array<string, mixed> $body Decoded JSON body. Empty array when
     *                                   the response had no JSON content.
     */
    public function __construct(
        public int $statusCode,
        public array $body,
    ) {
    }
}
