<?php

/**
 * Decoded response from the Clinical Co-Pilot agent service.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot;

final readonly class AgentResponse
{
    /**
     * @param array<string, mixed> $body Decoded JSON body. Empty array when
     *                                   the response had no JSON content.
     *
     * The slow-lane wire shape (`/api/agent/query`) carries a discriminated
     * `citation` field on `prose[i]` and a `citations` list on `cards[i]`,
     * keyed on `source_type` ∈ {`extracted_document`, `guideline`,
     * `patient_chart`}. Both fields are optional and absent on legacy /
     * fast-lane responses, so consumers must tolerate `null` / `[]` and
     * fall back to the canonical `source_id` / `source_ids` strings the
     * verifier joins on.
     *
     * The slow-lane shape also carries an optional top-level
     * `rerank_backend` ∈ {`cohere`, `llm_judge`, `bm25_only`, null}.
     * Null on every response that didn't run retrieval (fast lane,
     * chart-only, abstention before retrieval). The chat / side-panel
     * JS clients render a fallback / degraded badge when the value is
     * non-null and not `cohere`.
     *
     * The class intentionally stores the body as-is rather than projecting
     * it into typed sub-objects: `Citation` is a discriminated union and
     * structural typing in PHP would force a class hierarchy that consumers
     * don't need. ``ExtractedFieldHelper::citation()`` exposes the citation
     * block when individual fields are needed.
     */
    public function __construct(
        public int $statusCode,
        public array $body,
    ) {
    }
}
