<?php

/**
 * Clinical Co-Pilot Routes
 *
 * The OpenEMR-side gateway for ``/api/agent/*``. Routes here are merged into
 * the standard route map by ``_rest_routes_standard.inc.php``.
 *
 * Routes:
 *
 * * ``GET /api/agent/healthz`` — unauthenticated proxy for the agent
 *   service's liveness check (PR 3).
 * * ``POST /api/agent/query`` — the M3 chat-query route. Mints a per-request
 *   HS256 JWT from the current session and forwards the user's natural-
 *   language query to the agent service. Body: ``{patient_id, query}``;
 *   response: the structured :class:`AgentResponse` from M2.
 *
 * Auth: ``GET /api/agent/healthz`` requires only a valid OAuth2 session.
 * ``POST /api/agent/query`` likewise relies on the kernel's authorization
 * listener for the OAuth2 gate; the per-request RBAC that matters at the
 * agent boundary is done downstream by the Python tool layer using the
 * minted JWT's claims.
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
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use OpenEMR\Services\Copilot\GatewayController;
use OpenEMR\Services\Copilot\JwtSigner;
use OpenEMR\Services\Copilot\QueryController;
use OpenEMR\Services\Copilot\SessionMapper;

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
    "POST /api/agent/query" => function (HttpRestRequest $request, OEGlobalsBag $globals) {
        $config = new CopilotConfig($globals);
        $factory = new HttpFactory();
        // Slow-lane query timeouts can run 15-20s once the model is invoked;
        // override the short healthz default by a configurable margin.
        $httpClient = new GuzzleClient([
            'timeout' => max($config->getAgentTimeoutSeconds() * 6, 30),
            'http_errors' => false,
        ]);
        $agentClient = new AgentHttpClient($httpClient, $factory, $config);
        $signer = new JwtSigner(
            $config->getJwtSecret(),
            ServiceContainer::getClock(),
        );
        // getCoreSession() rather than getActiveSession() so this works
        // against older openemr/openemr base images that predate the
        // latter. The Co-Pilot route is always called from the core
        // clinician app, never the patient portal.
        $sessionMapper = new SessionMapper(
            SessionWrapperFactory::getInstance()->getCoreSession(),
        );
        $controller = new QueryController(
            $agentClient,
            $signer,
            $sessionMapper,
            $config,
            ServiceContainer::getLogger(),
        );
        // The HttpRestRequest is OpenEMR's narrowed wrapper; the
        // QueryController wants the raw body which Symfony's Request
        // exposes via ``getContent``. They share the same parent, so a
        // direct pass-through works here.
        return $controller->query($request);
    },
];
