<?php

/**
 * Helpers for projecting an `ExtractedField` JSON dump (as the agent
 * service returns it on the multimodal extract route) into the small
 * scalar shapes the OpenEMR review pages render.
 *
 * The wire shape per field is::
 *
 *     { "value": <T>|null, "citation": <SourceCitation>|null,
 *       "abstain_reason": <string>|null }
 *
 * with the invariant that exactly one of ``value`` and ``abstain_reason``
 * is non-null. The review pages only need three projections — the
 * value as a trimmed string, the abstain reason if any, and a
 * human-readable citation snippet. Centralising those here also lets
 * PHPStan narrow the ``mixed`` types coming off ``json_decode`` once
 * instead of in every page.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot;

final class ExtractedFieldHelper
{
    /**
     * Return the field's scalar value as a string ('' when the field
     * abstained or the value isn't a scalar).
     */
    public static function value(mixed $field): string
    {
        if (!is_array($field)) {
            return '';
        }
        $raw = $field['value'] ?? null;
        if ($raw === null) {
            return '';
        }
        if (is_scalar($raw)) {
            return (string) $raw;
        }
        return '';
    }

    /**
     * Return the field's abstain_reason string ('' when the field
     * carries a value).
     */
    public static function abstainReason(mixed $field): string
    {
        if (!is_array($field)) {
            return '';
        }
        $reason = $field['abstain_reason'] ?? null;
        return is_string($reason) ? $reason : '';
    }

    /**
     * Render the field's citation as a "p.N: <raw_text>" snippet for
     * inline display next to the editable value. Returns '' when the
     * citation isn't present (typically because the field abstained).
     */
    public static function citationText(mixed $field): string
    {
        if (!is_array($field)) {
            return '';
        }
        $citation = $field['citation'] ?? null;
        if (!is_array($citation)) {
            return '';
        }
        $page = $citation['page'] ?? null;
        $raw = $citation['raw_text'] ?? null;
        $pageInt = is_int($page) ? $page : (is_numeric($page) ? (int) $page : 0);
        $rawStr = is_string($raw) ? $raw : '';
        if ($pageInt > 0 && $rawStr !== '') {
            return 'p.' . $pageInt . ': ' . $rawStr;
        }
        return $rawStr;
    }

    /**
     * Return the citation's `source_type` discriminator (one of
     * `extracted_document` / `guideline` / `patient_chart`). Returns ''
     * when the citation is absent or the field is missing — legacy /
     * fast-lane responses without typed citations get the empty
     * fallback.
     */
    public static function sourceType(mixed $field): string
    {
        if (!is_array($field)) {
            return '';
        }
        $citation = $field['citation'] ?? null;
        if (!is_array($citation)) {
            return '';
        }
        $sourceType = $citation['source_type'] ?? null;
        return is_string($sourceType) ? $sourceType : '';
    }

    /**
     * Return the citation's `field_or_chunk_id` (JSON-pointer path for
     * extracted documents, chunk_id for guidelines, ResourceType/{id}
     * for patient-chart resources). Returns '' on legacy responses
     * captured before the discriminated-union schema landed.
     */
    public static function fieldOrChunkId(mixed $field): string
    {
        if (!is_array($field)) {
            return '';
        }
        $citation = $field['citation'] ?? null;
        if (!is_array($citation)) {
            return '';
        }
        $fieldOrChunkId = $citation['field_or_chunk_id'] ?? null;
        return is_string($fieldOrChunkId) ? $fieldOrChunkId : '';
    }

    /**
     * Return the citation block as an associative array, or null when
     * the field has no citation. Use when the consumer needs the full
     * shape (bbox, page, source_type, field_or_chunk_id) — typically
     * the bbox-overlay JS payload, which serializes the whole block
     * to the page.
     *
     * @return array<string, mixed>|null
     */
    public static function citation(mixed $field): ?array
    {
        if (!is_array($field)) {
            return null;
        }
        $citation = $field['citation'] ?? null;
        if (!is_array($citation)) {
            return null;
        }
        /** @var array<string, mixed> $citation */
        return $citation;
    }

    /**
     * Narrow a mixed value to a list of array rows (the typical shape
     * for reported_allergies / current_medications / active_problems
     * / family_history). Non-array entries are dropped silently —
     * the agent's schema validation runs upstream.
     *
     * @return list<array<string, mixed>>
     */
    public static function rowList(mixed $value): array
    {
        if (!is_array($value)) {
            return [];
        }
        $out = [];
        foreach ($value as $row) {
            if (is_array($row)) {
                /** @var array<string, mixed> $row */
                $out[] = $row;
            }
        }
        return $out;
    }
}
