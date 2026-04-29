<?php

/**
 * Clinical Co-Pilot Routes
 *
 * The OpenEMR-side gateway for ``/api/agent/*``. Routes here are merged into
 * the standard route map by ``_rest_routes_standard.inc.php``. PR 3 ships
 * only the unauthenticated ``/healthz`` proxy; PR 4 adds the JWT-signed
 * tool routes.
 *
 * Auth: like ``GET /api/version``, ``GET /api/agent/healthz`` requires only
 * a valid OAuth2 session (handled by the kernel's authorization listener) —
 * no ACL gate, since the endpoint exposes no PHI and only reports whether
 * the agent service is reachable.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

use GuzzleHttp\Client as GuzzleClient;
use GuzzleHttp\Psr7\HttpFactory;
use OpenEMR\BC\ServiceContainer;
use OpenEMR\Common\Http\HttpRestRequest;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use OpenEMR\Services\Copilot\GatewayController;

return [
    "GET /api/agent/healthz" => function (HttpRestRequest $request, OEGlobalsBag $globals) {
        $config = new CopilotConfig($globals);
        $factory = new HttpFactory();
        $httpClient = new GuzzleClient([
            'timeout' => $config->getAgentTimeoutSeconds(),
            'http_errors' => false,
        ]);
        $agentClient = new AgentHttpClient($httpClient, $factory, $config);
        $controller = new GatewayController($agentClient, ServiceContainer::getLogger());
        return $controller->healthz();
    },
];
