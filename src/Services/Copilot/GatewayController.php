<?php

/**
 * REST entry point for ``/api/agent/*`` routes — the OpenEMR-side gateway
 * that proxies authenticated requests to the Clinical Co-Pilot agent
 * service.
 *
 * PR 3 only ships the ``/healthz`` proxy. PR 4 adds JWT signing; PR 7+ add
 * the per-tool routes. The controller's contract is intentionally narrow:
 * translate request → :class:`AgentHttpClient` call → JSON response, log
 * failures via PSR-3, never leak internal error messages.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot;

use Psr\Log\LoggerInterface;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Response;

final readonly class GatewayController
{
    public function __construct(
        private AgentHttpClient $client,
        private LoggerInterface $logger,
    ) {
    }

    /**
     * Proxy ``GET /api/agent/healthz`` to the agent service ``/healthz``
     * endpoint. Returns the agent's JSON body verbatim with its status code.
     * On transport failure the gateway returns ``502 Bad Gateway`` — the
     * caller can distinguish "agent unreachable" from "agent reports
     * unhealthy" by status code alone.
     */
    public function healthz(): JsonResponse
    {
        try {
            $response = $this->client->get('/healthz');
        } catch (AgentServiceException $e) {
            $this->logger->warning('Clinical Co-Pilot agent service unreachable', [
                'exception' => $e,
            ]);
            return new JsonResponse(
                ['status' => 'unavailable'],
                Response::HTTP_BAD_GATEWAY,
            );
        }

        return new JsonResponse($response->body, $response->statusCode);
    }
}
