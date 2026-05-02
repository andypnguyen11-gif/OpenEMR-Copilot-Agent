<?php

/**
 * Discrepancy flag value object — the PHP-side mirror of the agent
 * service's :py:class:`FlagRecord`.
 *
 * Surfaced on the Daily Brief page (PR 16) as a per-patient list of
 * engine-detected conflicts (med-vs-note, narrative-only allergy,
 * resolved-still-active problem, etc.). The shape is parsed at the
 * trust boundary so card rendering downstream works with a typed
 * object that guarantees its own validity — no re-validation in the
 * template loop.
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

final readonly class Flag
{
    /**
     * @param list<string> $referencedSourceIds Source-id citations for the
     *                                          records that triggered the rule.
     */
    public function __construct(
        public string $sourceId,
        public string $ruleId,
        public string $category,
        public string $rationale,
        public array $referencedSourceIds,
    ) {
    }

    /**
     * Parse a single flag from a decoded JSON payload returned by the
     * agent service's ``GET /api/agent/internal/flags/{patient_id}``.
     *
     * Throws on any shape divergence rather than coercing — the caller
     * (:class:`InvalidationDispatcher`) catches and degrades to "no
     * flags available" so the page still renders. That keeps the
     * "parse-don't-validate" contract intact: anything that survives
     * this constructor is guaranteed-shaped, with no nullable string
     * fields the template has to defensively check.
     *
     * @param array<string, mixed> $row Single decoded flag entry.
     */
    public static function fromArray(array $row): self
    {
        $sourceId = self::requireString($row, 'source_id');
        $ruleId = self::requireString($row, 'rule_id');
        $category = self::requireString($row, 'category');
        $rationale = self::requireString($row, 'rationale');

        $rawRefs = $row['referenced_source_ids'] ?? null;
        if (!is_array($rawRefs)) {
            throw new InvalidArgumentException(
                'flag: referenced_source_ids must be an array',
            );
        }
        $refs = [];
        foreach ($rawRefs as $ref) {
            if (!is_string($ref) || $ref === '') {
                throw new InvalidArgumentException(
                    'flag: referenced_source_ids must be a list of non-empty strings',
                );
            }
            $refs[] = $ref;
        }

        return new self(
            sourceId: $sourceId,
            ruleId: $ruleId,
            category: $category,
            rationale: $rationale,
            referencedSourceIds: $refs,
        );
    }

    /**
     * @param array<string, mixed> $row
     */
    private static function requireString(array $row, string $key): string
    {
        $value = $row[$key] ?? null;
        if (!is_string($value) || $value === '') {
            throw new InvalidArgumentException("flag: {$key} must be a non-empty string");
        }
        return $value;
    }
}
