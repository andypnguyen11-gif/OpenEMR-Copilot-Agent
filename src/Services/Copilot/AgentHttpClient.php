<?php

/**
 * Thin PSR-18 wrapper around HTTP calls to the Clinical Co-Pilot agent
 * service. Owns URL composition, JSON decoding, and transport-error
 * translation; does not own routing, auth, or response shaping (those live
 * in :class:`GatewayController`).
 *
 * PR 3 only carries the unauthenticated ``/healthz`` proxy. PR 4 adds the
 * HMAC-signed JWT header. This class will gain a token-issuer dependency at
 * that point; the public surface should not need to change.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot;

use OpenEMR\Services\Copilot\Config\CopilotConfig;
use Psr\Http\Client\ClientExceptionInterface;
use Psr\Http\Client\ClientInterface;
use Psr\Http\Message\RequestFactoryInterface;
use Throwable;

readonly class AgentHttpClient
{
    public function __construct(
        private ClientInterface $httpClient,
        private RequestFactoryInterface $requestFactory,
        private CopilotConfig $config,
    ) {
    }

    /**
     * GET ``$path`` on the agent service. Path must start with a leading
     * slash; the base URL is taken from :class:`CopilotConfig`.
     *
     * @throws AgentServiceException When the transport fails or the body is
     *                               not decodable JSON. Non-2xx status codes
     *                               are returned, not thrown.
     */
    public function get(string $path): AgentResponse
    {
        if (!str_starts_with($path, '/')) {
            throw new AgentServiceException('agent path must start with /');
        }
        $url = $this->config->getAgentBaseUrl() . $path;
        $request = $this->requestFactory->createRequest('GET', $url)
            ->withHeader('Accept', 'application/json');

        try {
            $response = $this->httpClient->sendRequest($request);
        } catch (ClientExceptionInterface $e) {
            // Generic message — caller may log $e via PSR-3 context, but the
            // user-facing path returns a 502 without leaking internals.
            throw new AgentServiceException('agent service transport failure', 0, $e);
        }

        $rawBody = (string) $response->getBody();
        $decoded = [];
        if ($rawBody !== '') {
            try {
                $decoded = json_decode($rawBody, true, flags: JSON_THROW_ON_ERROR);
            } catch (Throwable $e) {
                throw new AgentServiceException('agent service returned invalid JSON', 0, $e);
            }
            if (!is_array($decoded)) {
                throw new AgentServiceException('agent service returned non-object JSON');
            }
        }

        /** @var array<string, mixed> $decoded */
        return new AgentResponse($response->getStatusCode(), $decoded);
    }
}
