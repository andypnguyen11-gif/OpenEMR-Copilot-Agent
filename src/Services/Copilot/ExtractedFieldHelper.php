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
