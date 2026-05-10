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

use GuzzleHttp\Psr7\MultipartStream;
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
     * POST a JSON body to ``$path`` on the agent service with an
     * ``X-Internal-Token`` header (PR 15) instead of a bearer JWT.
     *
     * Used by :class:`InvalidationDispatcher` for the warm + invalidate
     * routes. The header name is intentionally not ``Authorization`` so
     * the user-facing JWT verifier on the agent side can never
     * accidentally satisfy the internal-token gate (and vice versa);
     * the two threat models are documented in
     * ``agent-service/src/clinical_copilot/auth/internal_token.py``.
     *
     * Same JSON-encoding and transport-error translation as
     * :meth:`post`. Non-2xx HTTP statuses are returned in
     * :class:`AgentResponse` rather than thrown — the
     * :class:`InvalidationDispatcher` decides whether a 4xx warrants a
     * log line versus silent best-effort.
     *
     * @param array<string, mixed> $body Decoded JSON object to encode and send.
     *
     * @throws AgentServiceException When the transport fails, the body is not
     *                               JSON-encodable, or the response is not
     *                               decodable JSON.
     */
    public function postInternal(string $path, array $body, string $internalToken): AgentResponse
    {
        return $this->jsonInternal('POST', $path, $body, $internalToken);
    }

    /**
     * PUT ``$path`` on the agent service with a JSON body and an
     * ``X-Internal-Token`` header. Used by the editable-confirm save
     * handler to overwrite a previously-extracted facts record after
     * the clinician has reviewed and edited it. Same transport /
     * encoding contract as :meth:`postInternal`.
     *
     * @param array<mixed,mixed> $body
     * @throws AgentServiceException
     */
    public function putInternalJson(string $path, array $body, string $internalToken): AgentResponse
    {
        return $this->jsonInternal('PUT', $path, $body, $internalToken);
    }

    /**
     * Shared body of postInternal / putInternalJson — the only thing
     * that differs is the HTTP verb.
     *
     * @param array<mixed,mixed> $body
     * @throws AgentServiceException
     */
    private function jsonInternal(string $method, string $path, array $body, string $internalToken): AgentResponse
    {
        if (!str_starts_with($path, '/')) {
            throw new AgentServiceException('agent path must start with /');
        }
        if ($internalToken === '') {
            throw new AgentServiceException('agent ' . strtolower($method) . ' called without an internal token');
        }

        try {
            $payload = json_encode($body, JSON_THROW_ON_ERROR);
        } catch (Throwable $e) {
            throw new AgentServiceException('agent request body is not JSON-encodable', 0, $e);
        }

        $url = $this->config->getAgentBaseUrl() . $path;
        $request = $this->requestFactory->createRequest($method, $url)
            ->withHeader('Accept', 'application/json')
            ->withHeader('Content-Type', 'application/json')
            ->withHeader('X-Internal-Token', $internalToken)
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
     * GET ``$path`` on the agent service with an ``X-Internal-Token``
     * header (PR 16a flags-read route) instead of a bearer JWT.
     *
     * Mirror of :meth:`postInternal` for read-only routes — the Daily
     * Brief controller (PR 16b) reads cached flags through this method
     * after warming the panel via :meth:`postInternal`. The header
     * separation between user-JWT and internal-token is the same as
     * for the warm/invalidate routes; documented at
     * ``agent-service/src/clinical_copilot/auth/internal_token.py``.
     *
     * Same JSON-decoding and transport-error translation as
     * :meth:`get`. Non-2xx HTTP statuses are returned in
     * :class:`AgentResponse` rather than thrown — the caller decides
     * whether the page renders without flags or surfaces a 5xx.
     *
     * @throws AgentServiceException When the transport fails or the
     *                               body is not decodable JSON.
     */
    public function getInternal(string $path, string $internalToken): AgentResponse
    {
        if (!str_starts_with($path, '/')) {
            throw new AgentServiceException('agent path must start with /');
        }
        if ($internalToken === '') {
            throw new AgentServiceException('agent getInternal called without an internal token');
        }

        $url = $this->config->getAgentBaseUrl() . $path;
        $request = $this->requestFactory->createRequest('GET', $url)
            ->withHeader('Accept', 'application/json')
            ->withHeader('X-Internal-Token', $internalToken);

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
     * GET ``$path`` on the agent service with an ``X-Internal-Token``
     * header and return the raw response body without JSON decoding.
     *
     * Used by the citation-overlay page proxy
     * (``interface/copilot/api/document_page.php``) to forward rendered
     * PNG bytes from the agent service to the browser without the
     * intermediate JSON-decode the other ``getInternal*`` methods do.
     * The caller forwards both the status code and the upstream
     * Content-Type so a structured 404 (cache-miss JSON body) is
     * preserved as-is rather than re-shaped.
     *
     * Mirrors :meth:`getInternal` for transport-error translation.
     * Non-2xx HTTP statuses are returned, not thrown — the proxy
     * decides whether to render a placeholder image or surface the
     * error body.
     *
     * @return array{statusCode: int, contentType: string, body: string}
     *
     * @throws AgentServiceException When the transport itself fails.
     */
    public function getInternalRaw(string $path, string $internalToken): array
    {
        if (!str_starts_with($path, '/')) {
            throw new AgentServiceException('agent path must start with /');
        }
        if ($internalToken === '') {
            throw new AgentServiceException('agent getInternalRaw called without an internal token');
        }

        $url = $this->config->getAgentBaseUrl() . $path;
        $request = $this->requestFactory->createRequest('GET', $url)
            ->withHeader('X-Internal-Token', $internalToken);

        try {
            $response = $this->httpClient->sendRequest($request);
        } catch (ClientExceptionInterface $e) {
            throw new AgentServiceException('agent service transport failure', 0, $e);
        }

        return [
            'statusCode' => $response->getStatusCode(),
            'contentType' => $response->getHeaderLine('Content-Type'),
            'body' => (string) $response->getBody(),
        ];
    }

    /**
     * POST a multipart upload to ``$path`` on the agent service with an
     * ``X-Internal-Token`` header (PR W2-02 ingest route).
     *
     * The multimodal extractor takes binary document inputs (PDFs, PNGs);
     * shipping them as JSON+base64 inflates the request by ~33% and bloats
     * request logs. ``multipart/form-data`` is the native shape the agent
     * service consumes via FastAPI ``UploadFile``.
     *
     * Each entry in ``$fields`` becomes a string form field. ``$file`` is
     * the actual document bytes — passed as a triple ``[contents, filename,
     * contentType]`` so the agent side gets a usable filename hint for
     * extension dispatch (PDF vs. PNG).
     *
     * Same JSON-decoding and transport-error translation as
     * :meth:`postInternal`. Non-2xx HTTP statuses are returned in the
     * :class:`AgentResponse` rather than thrown — the caller decides how
     * to surface a 4xx (e.g. 422 means the VLM extracted nothing usable;
     * the upload page can show a retry button).
     *
     * @param array<string, string>           $fields  Form fields.
     * @param array{0: string, 1: string, 2: string} $file Tuple of
     *                                         [bytes, filename, mimeType].
     *
     * @throws AgentServiceException When the transport fails or the body
     *                               is not decodable JSON.
     */
    public function postMultipartInternal(
        string $path,
        array $fields,
        array $file,
        string $internalToken,
    ): AgentResponse {
        if (!str_starts_with($path, '/')) {
            throw new AgentServiceException('agent path must start with /');
        }
        if ($internalToken === '') {
            throw new AgentServiceException('agent postMultipartInternal called without an internal token');
        }

        [$fileContents, $fileName, $fileContentType] = $file;

        $parts = [];
        foreach ($fields as $name => $value) {
            $parts[] = ['name' => $name, 'contents' => $value];
        }
        $parts[] = [
            'name' => 'file',
            'contents' => $fileContents,
            'filename' => $fileName,
            'headers' => ['Content-Type' => $fileContentType],
        ];

        $multipart = new MultipartStream($parts);

        $url = $this->config->getAgentBaseUrl() . $path;
        $request = $this->requestFactory->createRequest('POST', $url)
            ->withHeader('Accept', 'application/json')
            ->withHeader('Content-Type', 'multipart/form-data; boundary=' . $multipart->getBoundary())
            ->withHeader('X-Internal-Token', $internalToken)
            ->withBody($multipart);

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
