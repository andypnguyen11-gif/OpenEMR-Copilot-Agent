<?php

/**
 * Domain-typed wrapper around the Co-Pilot agent service's multimodal
 * ingest route (PR W2-02).
 *
 * The upload PHP pages care about "ingest a lab" or "ingest an intake
 * form", not about multipart/form-data details or X-Internal-Token
 * header construction. This class is a thin domain layer over
 * :class:`AgentHttpClient::postMultipartInternal` so the upload page
 * stays small and the wire-shape concerns stay encapsulated.
 *
 * Unlike :class:`InvalidationDispatcher`, ingest is **not**
 * fire-and-forget. The upload page redirects to the review page with
 * the response body, so transport failures and non-200 status codes
 * propagate to the caller.
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
use OpenEMR\Services\Copilot\Config\CopilotConfigException;

final readonly class IngestClient
{
    private const PATH = '/api/agent/internal/ingest';

    public function __construct(
        private AgentHttpClient $client,
        private CopilotConfig $config,
    ) {
    }

    /**
     * Submit a lab document for extraction. ``$patientId`` is required
     * (the lab flow only ingests against an existing chart).
     */
    public function ingestLab(
        string $documentId,
        int $patientId,
        int $uploaderUserId,
        string $fileContents,
        string $fileName,
        string $fileMimeType,
    ): AgentResponse {
        return $this->ingest(
            $documentId,
            'lab_pdf',
            $patientId,
            $uploaderUserId,
            $fileContents,
            $fileName,
            $fileMimeType,
        );
    }

    /**
     * Submit an intake form for extraction. ``$patientId`` is null
     * because the new-patient flow ingests *before* a chart exists.
     */
    public function ingestIntake(
        string $documentId,
        int $uploaderUserId,
        string $fileContents,
        string $fileName,
        string $fileMimeType,
    ): AgentResponse {
        return $this->ingest(
            $documentId,
            'intake_form',
            null,
            $uploaderUserId,
            $fileContents,
            $fileName,
            $fileMimeType,
        );
    }

    /**
     * @throws AgentServiceException When the agent service is
     *                               misconfigured (no internal token)
     *                               or the transport / response shape
     *                               fails. Non-2xx responses are returned
     *                               in the :class:`AgentResponse`, not
     *                               thrown — the caller decides how
     *                               to render the error.
     */
    private function ingest(
        string $documentId,
        string $documentType,
        ?int $patientId,
        int $uploaderUserId,
        string $fileContents,
        string $fileName,
        string $fileMimeType,
    ): AgentResponse {
        if ($documentId === '') {
            throw new AgentServiceException('ingest called without a document_id');
        }

        try {
            $token = $this->config->getInternalToken();
        } catch (CopilotConfigException $e) {
            throw new AgentServiceException(
                'Co-Pilot ingest is not configured (missing internal token)',
                0,
                $e,
            );
        }

        $fields = [
            'document_id' => $documentId,
            'document_type' => $documentType,
            'uploader_user_id' => (string) $uploaderUserId,
        ];
        if ($patientId !== null) {
            $fields['patient_id'] = (string) $patientId;
        }

        return $this->client->postMultipartInternal(
            self::PATH,
            $fields,
            [$fileContents, $fileName, $fileMimeType],
            $token,
        );
    }
}
