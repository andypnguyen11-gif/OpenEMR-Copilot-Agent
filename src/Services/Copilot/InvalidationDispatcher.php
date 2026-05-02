<?php

/**
 * Fire-and-forget publisher for the agent service's discrepancy-cache
 * write hooks (PR 15).
 *
 * Two surfaces:
 *
 * * :meth:`invalidate` — drop a single patient's cached flags. Called
 *   from Symfony event listeners on the OpenEMR write paths that exist
 *   today (notably :class:`PatientUpdatedEvent`). Per AUDIT §10 #4 the
 *   med / lab / allergy / note write paths do not dispatch native
 *   Symfony events; those degrade to TTL-only freshness, which is the
 *   architecture's documented fallback (PRD §5).
 *
 * * :meth:`warmPanel` — pre-warm a list of patient_ids. Called from the
 *   chat UI's patient-select route (``POST /api/agent/warm``) so the
 *   first ``get_flags`` call inside the chat lands on a hot cache.
 *
 * * :meth:`readFlags` — read the cached flag list for a single patient
 *   (PR 16a's ``GET /api/agent/internal/flags/{patient_id}``). Used by
 *   the Daily Brief controller (PR 16b) to populate per-patient cards
 *   inline. Unlike invalidate / warm, this method *returns* a value —
 *   the parsed flag list — but it still mustn't throw: a transport
 *   failure or malformed payload degrades to an empty list so the
 *   page renders flag-less rather than 5xx-ing.
 *
 * **Fire-and-forget contract.** Neither method ever throws into the
 * caller. A clinical write that just successfully landed in OpenEMR's
 * database must not be undone or surfaced as a failure to the
 * clinician because the agent service was unreachable. The dispatcher
 * logs at ``warning`` (transport / 5xx — likely a gateway misconfig
 * worth investigating) or ``info`` (4xx — the agent rejected the
 * call shape, also worth knowing) and returns. Operators tail the log;
 * the cache catches up to the underlying truth at the next TTL window
 * regardless.
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
use Psr\Log\LoggerInterface;

final readonly class InvalidationDispatcher
{
    public function __construct(
        private AgentHttpClient $client,
        private CopilotConfig $config,
        private LoggerInterface $logger,
    ) {
    }

    /**
     * Drop the cached flags for ``$patientId`` from both cache tiers.
     *
     * No-ops on a blank id rather than calling the agent — a Symfony
     * listener firing on a malformed event must not amplify into a
     * useless network round-trip.
     */
    public function invalidate(string $patientId): void
    {
        if ($patientId === '') {
            $this->logger->info('Co-Pilot invalidate skipped: empty patient_id');
            return;
        }

        try {
            $token = $this->config->getInternalToken();
        } catch (CopilotConfigException $e) {
            // The dispatcher is firing from a listener on a clinical
            // write — re-raising would roll the write back. Log the
            // wiring problem and let TTL freshness take over.
            $this->logger->warning('Co-Pilot invalidate skipped: dispatcher not configured', [
                'exception' => $e,
            ]);
            return;
        }

        $this->dispatch(
            '/api/agent/internal/invalidate/' . rawurlencode($patientId),
            [],
            $token,
            ['patient_id' => $patientId, 'op' => 'invalidate'],
        );
    }

    /**
     * Warm the cache for every id in ``$patientIds``.
     *
     * The agent service deduplicates and bounds the panel size; the
     * dispatcher's only responsibility is to forward the list and not
     * trip over an empty input. An empty panel is a no-op (caller bug,
     * but not worth a network round-trip to reject) — the chat UI
     * should never send one, but this guards against an edge case
     * where the user picked a patient and immediately deselected it
     * before the request fired.
     *
     * @param list<string> $patientIds Bounded panel.
     */
    public function warmPanel(array $patientIds): void
    {
        $clean = array_values(array_filter(
            $patientIds,
            static fn(string $id): bool => $id !== '',
        ));
        if ($clean === []) {
            return;
        }

        try {
            $token = $this->config->getInternalToken();
        } catch (CopilotConfigException $e) {
            $this->logger->warning('Co-Pilot warm skipped: dispatcher not configured', [
                'exception' => $e,
            ]);
            return;
        }

        $this->dispatch(
            '/api/agent/internal/warm',
            ['patient_ids' => $clean],
            $token,
            ['op' => 'warm', 'panel_size' => count($clean)],
        );
    }

    /**
     * Read the cached discrepancy flags for ``$patientId`` from the
     * agent service.
     *
     * **Never throws.** Returns ``[]`` on every failure mode the
     * dispatcher could encounter — unconfigured token, transport
     * failure, non-2xx response, malformed body, malformed individual
     * flag entry — and logs the failure at the appropriate level. The
     * Daily Brief page renders without a flag list rather than 5xx-ing
     * the entire panel because one card's flag fetch failed.
     *
     * Empty ``$patientId`` is a caller bug; short-circuit so the
     * dispatcher's log surface stays clean.
     *
     * @return list<Flag>
     */
    public function readFlags(string $patientId): array
    {
        if ($patientId === '') {
            $this->logger->info('Co-Pilot readFlags skipped: empty patient_id');
            return [];
        }

        try {
            $token = $this->config->getInternalToken();
        } catch (CopilotConfigException $e) {
            $this->logger->warning('Co-Pilot readFlags skipped: dispatcher not configured', [
                'exception' => $e,
            ]);
            return [];
        }

        try {
            $response = $this->client->getInternal(
                '/api/agent/internal/flags/' . rawurlencode($patientId),
                $token,
            );
        } catch (AgentServiceException $e) {
            $this->logger->warning('Co-Pilot readFlags transport failure', [
                'patient_id' => $patientId,
                'exception' => $e,
            ]);
            return [];
        }

        if ($response->statusCode >= 400) {
            $level = $response->statusCode >= 500 ? 'warning' : 'info';
            $this->logger->log($level, 'Co-Pilot readFlags non-2xx response', [
                'patient_id' => $patientId,
                'status' => $response->statusCode,
            ]);
            return [];
        }

        $rawFlags = $response->body['flags'] ?? null;
        if (!is_array($rawFlags)) {
            $this->logger->warning('Co-Pilot readFlags malformed body', [
                'patient_id' => $patientId,
            ]);
            return [];
        }

        $flags = [];
        foreach ($rawFlags as $row) {
            if (!is_array($row)) {
                $this->logger->warning('Co-Pilot readFlags entry not an object', [
                    'patient_id' => $patientId,
                ]);
                return [];
            }
            try {
                /** @var array<string, mixed> $row */
                $flags[] = Flag::fromArray($row);
            } catch (\InvalidArgumentException $e) {
                // One bad row poisons the whole list — refuse to render
                // a partial flag set on a clinician-facing card. The
                // operator log line names the patient and the parse
                // error so the wiring problem is attributable.
                $this->logger->warning('Co-Pilot readFlags entry parse failure', [
                    'patient_id' => $patientId,
                    'exception' => $e,
                ]);
                return [];
            }
        }
        return $flags;
    }

    /**
     * Single shared dispatch path. Translates transport failures into
     * a log line and swallows them; logs non-2xx responses with the
     * status the agent returned. Never throws.
     *
     * @param array<string, mixed> $body
     * @param array<string, mixed> $logContext
     */
    private function dispatch(
        string $path,
        array $body,
        string $token,
        array $logContext,
    ): void {
        try {
            $response = $this->client->postInternal($path, $body, $token);
        } catch (AgentServiceException $e) {
            // Transport-level failure (DNS, timeout, connection refused),
            // JSON encode failures, and non-decodable responses all
            // arrive here — :class:`AgentHttpClient` documents
            // AgentServiceException as the only exception this method
            // raises, so a single catch is sufficient. Warning rather
            // than error: the user-visible operation is unaffected and
            // the cache will self-heal at TTL.
            $this->logger->warning('Co-Pilot internal dispatch transport failure', [
                ...$logContext,
                'exception' => $e,
            ]);
            return;
        }

        if ($response->statusCode >= 400) {
            // 4xx is almost always a wiring problem (bad token, bad
            // payload shape) the operator should see. 5xx is a real
            // agent-side failure but still not a reason to surface
            // anything to the clinician.
            $level = $response->statusCode >= 500 ? 'warning' : 'info';
            $this->logger->log($level, 'Co-Pilot internal dispatch non-2xx response', [
                ...$logContext,
                'status' => $response->statusCode,
            ]);
        }
    }
}
