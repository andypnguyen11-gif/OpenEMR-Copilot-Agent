<?php

/**
 * REST entry point for the M3 chat-query route.
 *
 * Flow:
 *
 * 1. Decode the request body into a typed :class:`QueryRequest`. Malformed
 *    input → 400 with a generic body.
 * 2. Map ``$_SESSION`` + the body's patient_id into a typed
 *    :class:`Auth\ClinicianIdentity` via :class:`SessionMapper`. Unauth or
 *    missing patient → 400.
 * 3. Mint a per-request HS256 JWT (5-minute lifetime, fresh nonce + jti) via
 *    :class:`JwtSigner`.
 * 4. POST the user query to the agent service's ``/api/agent/query``,
 *    carrying the JWT as a bearer token.
 * 5. Return the agent's JSON body verbatim with the agent's status code.
 *    The body is the structured :class:`AgentResponse` shape from M2.
 *
 * Failure modes:
 *
 * * Body validation → 400 (generic message).
 * * Session unauth → 400 (generic message). 401 would be misleading here —
 *   the OAuth2 session is fine; the gateway's per-request precondition is
 *   what failed.
 * * Agent transport error → 502 (per the healthz precedent in
 *   :class:`GatewayController`).
 * * Agent returns 4xx/5xx → status code passes through; body is the
 *   agent's structured error.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot;

use InvalidArgumentException;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use Psr\Log\LoggerInterface;
use RuntimeException;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\HttpFoundation\Response;
use Throwable;

final readonly class QueryController
{
    public function __construct(
        private AgentHttpClient $client,
        private JwtSigner $signer,
        private SessionMapper $sessionMapper,
        private CopilotConfig $config,
        private LoggerInterface $logger,
    ) {
    }

    public function query(Request $request): JsonResponse
    {
        try {
            $payload = $this->decodeBody($request);
            $body = QueryRequest::fromArray($payload);
        } catch (InvalidArgumentException $e) {
            $this->logger->info('Co-Pilot query rejected: bad request body', [
                'exception' => $e,
            ]);
            return new JsonResponse(
                ['error' => 'bad_request'],
                Response::HTTP_BAD_REQUEST,
            );
        }

        try {
            $identity = $this->sessionMapper->mapWithPatient(
                $body->patientId,
                $this->config->getStandardScopes(),
            );
        } catch (RuntimeException $e) {
            $this->logger->info('Co-Pilot query rejected: session mapping failed', [
                'exception' => $e,
            ]);
            return new JsonResponse(
                ['error' => 'bad_request'],
                Response::HTTP_BAD_REQUEST,
            );
        }

        $token = $this->signer->sign($identity, $this->sessionMapper->generateNonce());

        $payload = ['query' => $body->query];
        if ($body->sessionId !== null) {
            $payload['session_id'] = $body->sessionId;
        }

        try {
            $response = $this->client->post(
                '/api/agent/query',
                $payload,
                $token,
            );
        } catch (AgentServiceException $e) {
            $this->logger->warning('Clinical Co-Pilot agent service unreachable', [
                'exception' => $e,
            ]);
            return new JsonResponse(
                ['error' => 'agent_unavailable'],
                Response::HTTP_BAD_GATEWAY,
            );
        }

        return new JsonResponse($response->body, $response->statusCode);
    }

    /**
     * Decode the request body into a JSON object.
     *
     * The route handler hands us a Symfony :class:`Request` whose
     * ``getContent`` is the raw bytes; we decode here rather than in
     * :class:`QueryRequest` because invalid JSON and a missing field are
     * structurally different failures the controller wants to distinguish
     * in logs even though they collapse to the same 400 on the wire.
     *
     * @return array<string, mixed>
     */
    private function decodeBody(Request $request): array
    {
        $raw = $request->getContent();
        if ($raw === '') {
            throw new InvalidArgumentException('empty request body');
        }
        try {
            $decoded = json_decode($raw, true, flags: JSON_THROW_ON_ERROR);
        } catch (Throwable $e) {
            throw new InvalidArgumentException('request body is not valid JSON', 0, $e);
        }
        if (!is_array($decoded)) {
            throw new InvalidArgumentException('request body must be a JSON object');
        }
        /** @var array<string, mixed> $decoded */
        return $decoded;
    }
}
