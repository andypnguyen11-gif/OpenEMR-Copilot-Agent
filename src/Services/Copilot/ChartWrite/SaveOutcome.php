<?php

/**
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\ChartWrite;

/**
 * Result of an attempt at saving a Co-Pilot document. Encapsulates
 * which of the four {@see SaveOutcomeKind} branches the coordinator
 * landed in, plus the per-section row counts and pid the endpoint
 * needs to build the redirect URL (or replay the prior one).
 *
 * The success URL is reconstructed from these fields by the endpoint;
 * the coordinator does not own URL formatting. For ``IdempotentReplay``
 * the counts and pid come from the stored ``chart_write_summary`` JSON;
 * for ``AcquiredAndWrote`` they come from the just-completed write.
 *
 * Construct via the named static factories — the constructor is
 * private to prevent shape mismatches (e.g. a ``ConcurrentInFlight``
 * outcome with a non-zero pid).
 */
final readonly class SaveOutcome
{
    /**
     * @param array<string, int> $counts Per-section row counts.
     * @param list<string>       $selectedSections Sections the form requested.
     */
    private function __construct(
        public SaveOutcomeKind $kind,
        public int $pid,
        public bool $patientCreated,
        public array $counts,
        public array $selectedSections,
    ) {
    }

    /**
     * @param array<string, int> $counts
     * @param list<string>       $selectedSections
     */
    public static function acquiredAndWrote(
        int $pid,
        bool $patientCreated,
        array $counts,
        array $selectedSections,
    ): self {
        return new self(SaveOutcomeKind::AcquiredAndWrote, $pid, $patientCreated, $counts, $selectedSections);
    }

    /**
     * Decode a stored ``documents.chart_write_summary`` JSON blob into
     * an idempotent-replay outcome. The JSON shape is whatever
     * {@see ChartWriteCoordinator::buildSummaryPayload()} wrote on the
     * first successful save; missing keys fall back to safe defaults so
     * a corrupted blob never throws into the clinician path.
     *
     * @param array<mixed, mixed> $stored Decoded JSON object.
     */
    public static function idempotentReplay(array $stored): self
    {
        $pidRaw = $stored['pid'] ?? null;
        $pid = is_int($pidRaw) ? $pidRaw : (is_numeric($pidRaw) ? (int) $pidRaw : 0);
        $created = isset($stored['patient_created']) && (bool) $stored['patient_created'];

        $countsRaw = $stored['counts'] ?? null;
        $counts = [];
        if (is_array($countsRaw)) {
            foreach ($countsRaw as $section => $count) {
                if (is_string($section) && (is_int($count) || (is_numeric($count) && !is_string($count)))) {
                    $counts[$section] = is_int($count) ? $count : (int) $count;
                } elseif (is_string($section) && is_string($count) && ctype_digit($count)) {
                    $counts[$section] = (int) $count;
                }
            }
        }

        $sectionsRaw = $stored['selected_sections'] ?? null;
        $sections = [];
        if (is_array($sectionsRaw)) {
            foreach ($sectionsRaw as $entry) {
                if (is_string($entry) && $entry !== '') {
                    $sections[] = $entry;
                }
            }
        }

        return new self(SaveOutcomeKind::IdempotentReplay, $pid, $created, $counts, $sections);
    }

    public static function concurrentInFlight(): self
    {
        return new self(SaveOutcomeKind::ConcurrentInFlight, 0, false, [], []);
    }

    public static function documentNotFound(): self
    {
        return new self(SaveOutcomeKind::DocumentNotFound, 0, false, [], []);
    }
}
