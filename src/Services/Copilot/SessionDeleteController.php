<?php

/**
 * REST entry point for ``DELETE /api/agent/session/{session_id}``.
 *
 * The chat UI calls this when the user clicks "Clear chat" or switches
 * patients — it severs the agent service's in-memory conversation
 * history for the active session so the next turn starts fresh.
 *
 * Flow mirrors :class:`QueryController` for the auth side: read the
 * patient context from the ``?patient_id=...`` query string (DELETE
 * carries no body), map the OpenEMR session into a typed identity via
 * :class:`SessionMapper::mapWithPatient`, mint a per-request HS256 JWT,
 * call the agent's DELETE endpoint with the bearer token. The agent
 * resolves the session under the JWT's ``(user_id, patient_id,
 * session_id)`` triple — a different principal calling with the same
 * id will 404 because the lookup tuple itself differs.
 *
 * Failure modes:
 *
 * * Missing / invalid ``patient_id`` query param or ``session_id`` path
 *   segment → 400, no agent call.
 * * Session unauthenticated → 400, no agent call. (Same rationale as
 *   :class:`QueryController` — 401 would be misleading; the OAuth2
 *   session is fine, the gateway's per-request precondition isn't.)
 * * Per-patient access denied → 403, no agent call. Mirrors the gate
 *   in :class:`QueryController`: the gateway will not mint a JWT whose
 *   ``patient_id`` claim could let a clinician sever another clinician's
 *   session.
 * * Agent transport error → 502.
 * * Agent 404 (session not found under principal) → 404 passthrough.
 * * Agent 204 (deleted) → 204 to client.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot;

use OpenEMR\Services\Copilot\Auth\PatientAccessCheckerInterface;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use Psr\Log\LoggerInterface;
use RuntimeException;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\HttpFoundation\Response;

final readonly class SessionDeleteController
{
    public const SESSION_ID_MAX_LENGTH = QueryRequest::SESSION_ID_MAX_LENGTH;
    public const SESSION_ID_PATTERN = QueryRequest::SESSION_ID_PATTERN;

    public function __construct(
        private AgentHttpClient $client,
        private JwtSigner $signer,
        private SessionMapper $sessionMapper,
        private PatientAccessCheckerInterface $accessChecker,
        private CopilotConfig $config,
        private LoggerInterface $logger,
    ) {
    }

    public function delete(Request $request, string $sessionId): Response
    {
        if (
            $sessionId === ''
            || strlen($sessionId) > self::SESSION_ID_MAX_LENGTH
            || preg_match(self::SESSION_ID_PATTERN, $sessionId) !== 1
        ) {
            // Reject before we even look at the session — keeps the
            // gateway from minting a JWT for a request the agent would
            // immediately bounce as malformed.
            return new JsonResponse(
                ['error' => 'bad_request'],
                Response::HTTP_BAD_REQUEST,
            );
        }

        $patientId = $request->query->get('patient_id');
        if (!is_string($patientId) || $patientId === '') {
            return new JsonResponse(
                ['error' => 'bad_request'],
                Response::HTTP_BAD_REQUEST,
            );
        }

        try {
            $identity = $this->sessionMapper->mapWithPatient(
                $patientId,
                $this->config->getStandardScopes(),
            );
        } catch (RuntimeException $e) {
            $this->logger->info('Co-Pilot session delete rejected: session mapping failed', [
                'exception' => $e,
            ]);
            return new JsonResponse(
                ['error' => 'bad_request'],
                Response::HTTP_BAD_REQUEST,
            );
        }

        if (!$this->accessChecker->canAccess($identity->userId, $identity->patientId)) {
            $this->logger->warning('Co-Pilot session delete rejected: patient access denied', [
                'user_id' => $identity->userId,
            ]);
            return new JsonResponse(
                ['error' => 'patient_access_denied'],
                Response::HTTP_FORBIDDEN,
            );
        }

        $token = $this->signer->sign($identity, $this->sessionMapper->generateNonce());

        try {
            $response = $this->client->delete(
                '/api/agent/session/' . $sessionId,
                $token,
            );
        } catch (AgentServiceException $e) {
            $this->logger->warning('Clinical Co-Pilot agent service unreachable on DELETE', [
                'exception' => $e,
            ]);
            return new JsonResponse(
                ['error' => 'agent_unavailable'],
                Response::HTTP_BAD_GATEWAY,
            );
        }

        // 204 → 204 (no body); anything else passes through with the
        // agent's body so the client can distinguish "session not found
        // under your principal" (404) from product-level errors. The
        // body shape for non-2xx responses is the agent's small JSON
        // error payload — small enough that passing through is honest.
        if ($response->statusCode === Response::HTTP_NO_CONTENT) {
            return new Response('', Response::HTTP_NO_CONTENT);
        }

        return new JsonResponse($response->body, $response->statusCode);
    }
}
