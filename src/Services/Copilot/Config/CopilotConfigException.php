<?php

/**
 * Raised when :class:`CopilotConfig` cannot supply a required setting.
 *
 * Distinct from :class:`AgentServiceException` (a transport failure) so
 * callers can surface a 500 with a "service misconfigured" log line rather
 * than a 502 — the agent service is fine; the *gateway* is unconfigured.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\Config;

use RuntimeException;

final class CopilotConfigException extends RuntimeException
{
}
