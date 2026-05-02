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

use GuzzleHttp\Psr7\Utils;
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

    /**
     * POST a JSON body to ``$path`` on the agent service with an HS256 bearer
     * token in the ``Authorization`` header.
     *
     * Used by :class:`QueryController` for the M3 chat-query route. The body
     * is encoded with ``JSON_THROW_ON_ERROR`` so an unencodable input fails
     * here rather than producing a malformed wire payload the agent service
     * would reject as malformed JSON anyway.
     *
     * @param array<string, mixed> $body Decoded JSON object to encode and send.
     *
     * @throws AgentServiceException When the transport fails, the body is not
     *                               JSON-encodable, or the response is not
     *                               decodable JSON. Non-2xx HTTP statuses are
     *                               returned, not thrown.
     */
    public function post(string $path, array $body, string $bearerToken): AgentResponse
    {
        if (!str_starts_with($path, '/')) {
            throw new AgentServiceException('agent path must start with /');
        }
        if ($bearerToken === '') {
            throw new AgentServiceException('agent post called without a bearer token');
        }

        try {
            $payload = json_encode($body, JSON_THROW_ON_ERROR);
        } catch (Throwable $e) {
            throw new AgentServiceException('agent request body is not JSON-encodable', 0, $e);
        }

        $url = $this->config->getAgentBaseUrl() . $path;
        $request = $this->requestFactory->createRequest('POST', $url)
            ->withHeader('Accept', 'application/json')
            ->withHeader('Content-Type', 'application/json')
            ->withHeader('Authorization', 'Bearer ' . $bearerToken)
            ->withBody(Utils::streamFor($payload));

        try {
            $response = $this->httpClient->sendRequest($request);
        } catch (ClientExceptionInterface $e) {
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

    /**
     * DELETE ``$path`` on the agent service with an HS256 bearer token.
     *
     * No body, no Content-Type. Used by :class:`SessionDeleteController`
     * for ``DELETE /api/agent/session/{id}``. Non-2xx HTTP statuses are
     * returned in the :class:`AgentResponse` rather than thrown — the
     * 404 case is a normal product-level signal (caller's session not
     * found under the JWT's principal), not a transport error.
     *
     * @throws AgentServiceException When the transport fails or a non-empty
     *                               response body is not decodable JSON.
     */
    public function delete(string $path, string $bearerToken): AgentResponse
    {
        if (!str_starts_with($path, '/')) {
            throw new AgentServiceException('agent path must start with /');
        }
        if ($bearerToken === '') {
            throw new AgentServiceException('agent delete called without a bearer token');
        }

        $url = $this->config->getAgentBaseUrl() . $path;
        $request = $this->requestFactory->createRequest('DELETE', $url)
            ->withHeader('Accept', 'application/json')
            ->withHeader('Authorization', 'Bearer ' . $bearerToken);

        try {
            $response = $this->httpClient->sendRequest($request);
        } catch (ClientExceptionInterface $e) {
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
