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
 * * Per-patient access denied → 403 (generic message). The clinician is
 *   authenticated but not authorised for the requested patient; minting a
 *   JWT here would let the agent layer trust the caller's claim, so the
 *   gate refuses before any signature is produced.
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
use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Services\Copilot\Auth\ClinicianIdentity;
use OpenEMR\Services\Copilot\Auth\PatientAccessCheckerInterface;
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
        private PatientAccessCheckerInterface $accessChecker,
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

        // Per-patient access gate. Without this the JWT's patient_id claim
        // would be whatever the browser asked for, which the downstream
        // tool-layer "RBAC" check (request.patient_id == claims.patient_id)
        // can't catch — both sides trace back to the same untrusted body.
        if (!$this->accessChecker->canAccess($identity->userId, $identity->patientId)) {
            $this->logger->warning('Co-Pilot query rejected: patient access denied', [
                'user_id' => $identity->userId,
            ]);
            return new JsonResponse(
                ['error' => 'patient_access_denied'],
                Response::HTTP_FORBIDDEN,
            );
        }

        // Translate the dropdown's legacy ``pid`` into the patient's FHIR
        // ``uuid`` before minting the JWT. OpenEMR's R4 endpoints reject
        // any ``patient`` argument that isn't a UUID (``Patient/90001`` →
        // 400 "UUID columns must be a valid UUID string"; ``MedicationRequest
        // ?patient=90001`` → 200 with an empty bundle, no warning), so the
        // agent's tool layer would silently see no records for every fixture
        // patient if we kept the pid as the ``patient_id`` claim. Looking up
        // the uuid here, after :meth:`PatientAccessCheckerInterface::canAccess`
        // has already authorised the pid, keeps the gate on the legacy id and
        // hands the agent a value the FHIR server will resolve.
        $patientUuid = self::resolvePatientUuid($identity->patientId);
        if ($patientUuid === null) {
            $this->logger->warning('Co-Pilot query rejected: patient uuid lookup empty', [
                'user_id' => $identity->userId,
            ]);
            return new JsonResponse(
                ['error' => 'patient_access_denied'],
                Response::HTTP_FORBIDDEN,
            );
        }
        $fhirIdentity = new ClinicianIdentity(
            userId: $identity->userId,
            role: $identity->role,
            patientId: $patientUuid,
            scopes: $identity->scopes,
        );

        $token = $this->signer->sign($fhirIdentity, $this->sessionMapper->generateNonce());

        $payload = ['query' => $body->query];
        if ($body->sessionId !== null) {
            $payload['session_id'] = $body->sessionId;
        }
        if ($body->lane !== null) {
            $payload['lane'] = $body->lane;
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
     * Convert a legacy ``patient_data.pid`` into the patient's FHIR uuid.
     *
     * The FHIR layer (``FhirPatientService``, ``PrescriptionService``, ...)
     * keys exclusively off ``patient_data.uuid``; passing the pid as the
     * ``patient`` search parameter or the ``Patient/{id}`` path component
     * silently produces empty bundles or 400s. The legacy access check
     * one frame above this call still uses the pid; once that gate has
     * passed, the controller swaps in the uuid for the JWT claim so the
     * agent can address the patient through the FHIR R4 surface.
     *
     * Returns ``null`` when the pid does not resolve to a uuid — either
     * the patient was deleted between the access check and this lookup,
     * or the row pre-dates OpenEMR's UUID rollout. The caller surfaces
     * this as a generic ``patient_access_denied`` 403 rather than a 500;
     * the access check has already run, so the only way to land here is
     * a transient race or a data-shape gap, neither of which the
     * clinician can act on.
     */
    private static function resolvePatientUuid(string $pid): ?string
    {
        if ($pid === '' || !ctype_digit($pid)) {
            return null;
        }
        // ``BIN_TO_UUID(uuid)`` (no swap-flag) — OpenEMR stores patient_data
        // uuids in canonical byte order, so the default formatter returns
        // the value the FHIR endpoints index on. Passing ``, 1`` here would
        // re-swap the halves and produce a uuid the server doesn't resolve
        // (caught on prod 2026-05-03 when MedicationRequest?patient=
        // <swapped-uuid> returned an empty bundle).
        $row = QueryUtils::fetchSingleValue(
            'SELECT BIN_TO_UUID(uuid) AS uuid FROM patient_data WHERE pid = ? LIMIT 1',
            'uuid',
            [$pid],
        );
        if (!is_string($row) || $row === '') {
            return null;
        }
        return $row;
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
