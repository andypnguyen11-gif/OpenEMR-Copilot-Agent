<?php

/**
 * Typed request body for the M3 chat-query route.
 *
 * The chat UI sends two values: a ``patient_id`` selected from the dropdown
 * (the five fixture patients for the MVP) and the user's natural-language
 * query. Wrapping these in a value object keeps the controller's signature
 * honest — every other layer in the gateway works against typed objects per
 * CLAUDE.md.
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

final readonly class QueryRequest
{
    public const QUERY_MAX_LENGTH = 4000;
    public const SESSION_ID_MAX_LENGTH = 64;
    public const SESSION_ID_PATTERN = '/^[A-Za-z0-9-]+$/';

    public function __construct(
        public string $patientId,
        public string $query,
        public ?string $sessionId = null,
    ) {
        if ($patientId === '') {
            throw new InvalidArgumentException('patient_id must be non-empty');
        }
        if ($query === '') {
            throw new InvalidArgumentException('query must be non-empty');
        }
        if (strlen($query) > self::QUERY_MAX_LENGTH) {
            throw new InvalidArgumentException(
                'query exceeds ' . self::QUERY_MAX_LENGTH . '-character limit',
            );
        }
        if ($sessionId !== null) {
            if ($sessionId === '') {
                throw new InvalidArgumentException('session_id must be non-empty when present');
            }
            if (strlen($sessionId) > self::SESSION_ID_MAX_LENGTH) {
                throw new InvalidArgumentException(
                    'session_id exceeds ' . self::SESSION_ID_MAX_LENGTH . '-character limit',
                );
            }
            if (preg_match(self::SESSION_ID_PATTERN, $sessionId) !== 1) {
                // Tight charset enforced gateway-side so the agent never has
                // to validate URL-unsafe / control characters that would
                // need escaping in path positions later.
                throw new InvalidArgumentException(
                    'session_id contains characters outside [A-Za-z0-9-]',
                );
            }
        }
    }

    /**
     * Parse a decoded JSON body into a :class:`QueryRequest`.
     *
     * Keeping the parsing in one place means the controller never sees a
     * raw ``mixed`` from the body — the controller works against the typed
     * object or fails before it runs. Any malformed input here surfaces as
     * an :class:`InvalidArgumentException`, which the controller maps to
     * an HTTP 400.
     *
     * @param array<string, mixed> $payload
     */
    public static function fromArray(array $payload): self
    {
        $patientId = $payload['patient_id'] ?? null;
        if (!is_string($patientId)) {
            throw new InvalidArgumentException('patient_id must be a string');
        }
        $query = $payload['query'] ?? null;
        if (!is_string($query)) {
            throw new InvalidArgumentException('query must be a string');
        }
        $sessionId = $payload['session_id'] ?? null;
        if ($sessionId !== null && !is_string($sessionId)) {
            throw new InvalidArgumentException('session_id must be a string when present');
        }
        return new self($patientId, $query, $sessionId);
    }
}
