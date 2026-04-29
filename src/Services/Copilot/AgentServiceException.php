<?php

/**
 * Raised when a call to the Clinical Co-Pilot agent service fails at the
 * transport layer (connection refused, timeout, malformed JSON). HTTP
 * responses with non-2xx status codes are returned as :class:`AgentResponse`
 * rather than thrown — the caller decides whether to surface a 5xx, retry,
 * or treat the body as a structured error.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot;

use RuntimeException;

final class AgentServiceException extends RuntimeException
{
}
