<?php

/**
 * Per-document-type adapters that pull chart-writable rows out of an
 * extracted-facts dict.
 *
 * The agent-service produces one Pydantic model per document type
 * (lab_pdf, intake_form, referral_docx, fax_tiff, workbook_xlsx,
 * hl7_oru, hl7_adt). Each has a different layout — workbook nests
 * its medications under ``$facts['medications']`` as a list of
 * objects with ``brand``/``generic``/``strength``/``sig`` keys;
 * referral exposes ``$facts['current_medications']`` as a list of
 * pre-formatted ExtractedField strings. ``ChartWriteService`` wants a
 * single normalized row shape per write target, so the adapters here
 * bridge that gap.
 *
 * Each adapter returns a list of plain associative arrays — no
 * Pydantic objects, no ExtractedField wrappers. Empty inputs return
 * empty lists. Adapters never throw; a malformed document gets an
 * empty result rather than crashing the save handler.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\ChartWrite;

use OpenEMR\Services\Copilot\DocumentClassifier;

final class FactsExtractor
{
    /**
     * @param array<mixed,mixed> $facts Per-type Pydantic model dump.
     * @return list<array<string,mixed>>
     */
    public static function allergies(array $facts, string $documentType): array
    {
        if ($documentType === DocumentClassifier::TYPE_INTAKE_FORM) {
            return self::extractIntakeAllergies($facts);
        }
        if ($documentType === DocumentClassifier::TYPE_REFERRAL_DOCX) {
            return self::extractReferralAllergies($facts);
        }
        if ($documentType === DocumentClassifier::TYPE_WORKBOOK_XLSX) {
            return self::extractWorkbookAllergies($facts);
        }
        return [];
    }

    /**
     * @param array<mixed,mixed> $facts
     * @return list<array<string,mixed>>
     */
    public static function medications(array $facts, string $documentType): array
    {
        if ($documentType === DocumentClassifier::TYPE_INTAKE_FORM) {
            return self::extractListLeaves($facts['current_medications'] ?? null, [
                'name' => 'name',
                'dose' => 'dose',
                'frequency' => 'frequency',
                'rxnorm' => 'rxnorm',
                'indication' => 'indication',
                'started_year' => 'started_year',
            ]);
        }
        if ($documentType === DocumentClassifier::TYPE_REFERRAL_DOCX) {
            // Referrals carry meds as a list[ExtractedField[str]] of
            // pre-formatted strings ("atorvastatin 40 mg PO daily").
            // Parse into name + dose + frequency by splitting on first
            // space-then-strength pattern.
            return self::parsePreformattedMedicationList(
                self::extractedFieldList($facts['current_medications'] ?? null),
            );
        }
        if ($documentType === DocumentClassifier::TYPE_WORKBOOK_XLSX) {
            $rows = [];
            $meds = $facts['medications'] ?? null;
            if (!is_array($meds)) {
                return [];
            }
            foreach ($meds as $row) {
                if (!is_array($row)) {
                    continue;
                }
                $generic = self::leafString($row['generic'] ?? null);
                $brand = self::leafString($row['brand'] ?? null);
                if ($generic === '' && $brand === '') {
                    continue;
                }
                $rows[] = [
                    'name' => $generic !== '' ? $generic : $brand,
                    'dose' => self::leafString($row['strength'] ?? null),
                    'frequency' => self::leafString($row['sig'] ?? null),
                    'indication' => self::leafString($row['indication'] ?? null),
                ];
            }
            return $rows;
        }
        return [];
    }

    /**
     * @param array<mixed,mixed> $facts
     * @return list<array<string,mixed>>
     */
    public static function activeProblems(array $facts, string $documentType): array
    {
        if ($documentType === DocumentClassifier::TYPE_INTAKE_FORM) {
            return self::extractListLeaves($facts['active_problems'] ?? null, [
                'condition' => 'condition',
                'icd10' => 'icd10',
                'snomed' => 'snomed',
                'onset_year' => 'onset_year',
                'status' => 'status',
            ]);
        }
        if ($documentType === DocumentClassifier::TYPE_REFERRAL_DOCX) {
            // PMH on a referral is a list[ExtractedField[str]] of
            // pre-formatted strings — "Hyperlipidemia (E78.5)". Parse
            // out the trailing parenthesized code as ICD-10 when it
            // looks like one.
            return self::parsePreformattedProblemList(
                self::extractedFieldList($facts['past_medical_history'] ?? null),
            );
        }
        return [];
    }

    /**
     * @param array<mixed,mixed> $facts
     * @return list<array<string,mixed>>
     */
    public static function careGaps(array $facts, string $documentType): array
    {
        if ($documentType !== DocumentClassifier::TYPE_WORKBOOK_XLSX) {
            return [];
        }
        $gaps = $facts['care_gaps'] ?? null;
        if (!is_array($gaps)) {
            return [];
        }
        $rows = [];
        foreach ($gaps as $row) {
            if (!is_array($row)) {
                continue;
            }
            $measure = self::leafString($row['measure'] ?? null);
            if ($measure === '') {
                continue;
            }
            $rows[] = [
                'measure' => $measure,
                'status' => self::leafString($row['status'] ?? null),
                'due_date' => self::leafString($row['due_date'] ?? null),
                'notes' => self::leafString($row['notes'] ?? null),
            ];
        }
        return $rows;
    }

    /**
     * @param array<mixed,mixed> $facts
     * @return array{panel_name:string, panel_loinc:string, report_date:string, observations:list<array<string,mixed>>}
     */
    public static function labObservations(array $facts, string $documentType): array
    {
        if ($documentType === DocumentClassifier::TYPE_LAB_PDF) {
            return [
                'panel_name' => 'Co-Pilot lab import',
                'panel_loinc' => '',
                'report_date' => self::firstObservationDate($facts['observations'] ?? null),
                'observations' => self::normalizeLabObservationList($facts['observations'] ?? null),
            ];
        }
        if ($documentType === DocumentClassifier::TYPE_HL7_ORU) {
            $panelName = self::leafString($facts['order_panel'] ?? null);
            return [
                'panel_name' => $panelName !== '' ? $panelName : 'HL7 ORU import',
                'panel_loinc' => self::leafString($facts['order_loinc'] ?? null),
                'report_date' => self::leafString($facts['specimen_collected_at'] ?? null)
                    ?: self::firstObservationDate($facts['observations'] ?? null),
                'observations' => self::normalizeLabObservationList($facts['observations'] ?? null),
            ];
        }
        if ($documentType === DocumentClassifier::TYPE_WORKBOOK_XLSX) {
            // Workbook lab rows are unpivoted (one row per
            // (test, date) pair). Group by test for the panel name
            // (use "Workbook lab import"); each row becomes one obs.
            $obs = [];
            $latestDate = '';
            $readings = $facts['lab_readings'] ?? null;
            if (!is_array($readings)) {
                return self::emptyLabPayload('Workbook lab import');
            }
            foreach ($readings as $row) {
                if (!is_array($row)) {
                    continue;
                }
                $test = self::leafString($row['test'] ?? null);
                $value = self::leafFloat($row['value'] ?? null);
                $date = self::leafString($row['reading_date'] ?? null);
                if ($test === '' || $value === null) {
                    continue;
                }
                $obs[] = [
                    'code' => self::leafString($row['loinc'] ?? null),
                    'display' => $test,
                    'value' => $value,
                    'unit' => self::leafString($row['unit'] ?? null),
                    'effective_date' => $date,
                    'flag' => '',
                ];
                if ($date > $latestDate) {
                    $latestDate = $date;
                }
            }
            return [
                'panel_name' => 'Workbook lab import',
                'panel_loinc' => '',
                'report_date' => $latestDate,
                'observations' => $obs,
            ];
        }
        return self::emptyLabPayload('Co-Pilot lab import');
    }

    /**
     * Quick "does this section have at least one row to write" check
     * used by the review page to grey out empty checkboxes. Cheaper
     * than running the full extractor and parsing each row.
     *
     * @param array<mixed,mixed> $facts
     */
    public static function sectionPopulated(array $facts, string $documentType, string $section): bool
    {
        if ($section === 'allergies') {
            if ($documentType === DocumentClassifier::TYPE_INTAKE_FORM) {
                return is_array($facts['reported_allergies'] ?? null) && $facts['reported_allergies'] !== [];
            }
            if ($documentType === DocumentClassifier::TYPE_REFERRAL_DOCX) {
                return is_array($facts['allergies'] ?? null);
            }
            if ($documentType === DocumentClassifier::TYPE_WORKBOOK_XLSX) {
                $patient = $facts['patient'] ?? null;
                return is_array($patient) && is_array($patient['allergies'] ?? null);
            }
            return false;
        }
        if ($section === 'medications') {
            $key = $documentType === DocumentClassifier::TYPE_WORKBOOK_XLSX
                ? 'medications'
                : 'current_medications';
            return is_array($facts[$key] ?? null) && $facts[$key] !== [];
        }
        if ($section === 'active_problems') {
            $key = $documentType === DocumentClassifier::TYPE_REFERRAL_DOCX
                ? 'past_medical_history'
                : 'active_problems';
            return is_array($facts[$key] ?? null) && $facts[$key] !== [];
        }
        if ($section === 'care_gaps') {
            return is_array($facts['care_gaps'] ?? null) && $facts['care_gaps'] !== [];
        }
        if ($section === 'lab_observations') {
            $key = $documentType === DocumentClassifier::TYPE_WORKBOOK_XLSX
                ? 'lab_readings'
                : 'observations';
            return is_array($facts[$key] ?? null) && $facts[$key] !== [];
        }
        return false;
    }

    // ---- Private helpers --------------------------------------------------

    /**
     * @return array{panel_name:string, panel_loinc:string, report_date:string, observations:list<array<string,mixed>>}
     */
    private static function emptyLabPayload(string $name): array
    {
        return [
            'panel_name' => $name,
            'panel_loinc' => '',
            'report_date' => '',
            'observations' => [],
        ];
    }

    /**
     * @param array<mixed,mixed> $facts
     * @return list<array<string,mixed>>
     */
    private static function extractIntakeAllergies(array $facts): array
    {
        return self::extractListLeaves($facts['reported_allergies'] ?? null, [
            'substance' => 'substance',
            'reaction' => 'reaction',
            'severity' => 'severity',
        ]);
    }

    /**
     * @param array<mixed,mixed> $facts
     * @return list<array<string,mixed>>
     */
    private static function extractReferralAllergies(array $facts): array
    {
        // Referrals carry allergies as a single ExtractedField[str]
        // — typically "NKDA" or "Sulfa drugs — rash". Treat NKDA as
        // a single no-known-drug-allergies entry; otherwise split on
        // " — " or ":" to surface a reaction.
        $raw = self::leafString($facts['allergies'] ?? null);
        if ($raw === '') {
            return [];
        }
        $upper = strtoupper($raw);
        if ($upper === 'NKDA' || str_contains($upper, 'NO KNOWN')) {
            return [['substance' => 'NKDA', 'reaction' => '', 'severity' => '']];
        }
        // Try " — " then " - " then ":" as the substance/reaction
        // separator. Falls through to a substance-only entry when no
        // separator matches.
        foreach ([' — ', ' – ', ' - ', ': '] as $sep) {
            $parts = explode($sep, $raw, 2);
            if (count($parts) === 2) {
                return [[
                    'substance' => trim($parts[0]),
                    'reaction' => trim($parts[1]),
                    'severity' => '',
                ]];
            }
        }
        return [['substance' => $raw, 'reaction' => '', 'severity' => '']];
    }

    /**
     * @param array<mixed,mixed> $facts
     * @return list<array<string,mixed>>
     */
    private static function extractWorkbookAllergies(array $facts): array
    {
        // Workbook allergies live in the patient block as a single
        // free-text field. Treat the same way as referral allergies.
        $patient = $facts['patient'] ?? null;
        if (!is_array($patient)) {
            return [];
        }
        return self::extractReferralAllergies(['allergies' => $patient['allergies'] ?? null]);
    }

    /**
     * Generic intake-form-style list extractor. Each ``$listNode``
     * entry is a dict whose values are ``ExtractedField`` shapes; the
     * ``$fieldMap`` is ``output_key => source_key`` and we lift each
     * source field's ``value``.
     *
     * @param mixed $listNode
     * @param array<string, string> $fieldMap
     * @return list<array<string, mixed>>
     */
    private static function extractListLeaves(mixed $listNode, array $fieldMap): array
    {
        if (!is_array($listNode)) {
            return [];
        }
        $rows = [];
        foreach ($listNode as $entry) {
            if (!is_array($entry)) {
                continue;
            }
            $row = [];
            foreach ($fieldMap as $outKey => $srcKey) {
                $value = self::leafValue($entry[$srcKey] ?? null);
                if ($value !== null && $value !== '') {
                    $row[$outKey] = $value;
                } elseif ($outKey === array_key_first($fieldMap)) {
                    // Required first-key (substance / name / condition)
                    // — leave empty so the caller can skip the row.
                    $row[$outKey] = '';
                }
            }
            // Skip rows whose first field is empty (e.g. a med with no
            // name is not chart-writeable).
            $firstKey = array_key_first($fieldMap);
            if (($row[$firstKey] ?? '') === '') {
                continue;
            }
            $rows[] = $row;
        }
        return $rows;
    }

    /**
     * Parse a list of ``ExtractedField[str]`` rows (each a
     * pre-formatted med string like "atorvastatin 40 mg PO daily")
     * into normalized medication rows. Best-effort split; the first
     * token is the name, the next number-with-unit-token is the
     * dose, anything after is frequency.
     *
     * @param list<string> $strings
     * @return list<array{name:string, dose?:string, frequency?:string, indication?:string}>
     */
    private static function parsePreformattedMedicationList(array $strings): array
    {
        $rows = [];
        foreach ($strings as $line) {
            $line = trim($line);
            if ($line === '') {
                continue;
            }
            // Common patterns from the cohort-5 referrals:
            //   "atorvastatin 40 mg PO daily"
            //   "metoprolol succinate 50 mg PO daily"
            //   "lisinopril 10 mg PO daily  (HOLD per ACE-i angioedema; reconciliation pending)"
            // Strategy: parenthesized note → indication; rest → split
            // on first NN-then-unit token.
            $indication = '';
            if (preg_match('/^(.*?)\s*\(([^)]*)\)\s*$/', $line, $m) === 1) {
                $line = trim($m[1]);
                $indication = trim($m[2]);
            }
            if (preg_match('/^(.*?)\s+(\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|units?|iu|%)\b.*)$/i', $line, $m) === 1) {
                $name = trim($m[1]);
                $rest = trim($m[2]);
                // First space-separated token of $rest is the dose
                // (e.g. "40 mg"); remainder is frequency.
                if (preg_match('/^(\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|units?|iu|%))\s*(.*)$/i', $rest, $m2) === 1) {
                    $rows[] = [
                        'name' => $name,
                        'dose' => trim($m2[1]),
                        'frequency' => trim($m2[2]),
                        'indication' => $indication,
                    ];
                    continue;
                }
            }
            $rows[] = ['name' => $line, 'indication' => $indication];
        }
        return $rows;
    }

    /**
     * Parse a list of ``ExtractedField[str]`` rows (each a
     * pre-formatted PMH line like "Hyperlipidemia (E78.5)") into
     * normalized problem rows. Best-effort code extraction —
     * parenthesized ICD-10 patterns become the icd10 field; SNOMED
     * codes are not present in the cohort-5 referral corpus.
     *
     * @param list<string> $strings
     * @return list<array<string,mixed>>
     */
    private static function parsePreformattedProblemList(array $strings): array
    {
        $rows = [];
        foreach ($strings as $line) {
            $line = trim($line);
            if ($line === '') {
                continue;
            }
            $icd = '';
            if (preg_match('/^(.*?)\s*\(([A-Z]\d{2}(?:\.[\dA-Z]+)?)\)\s*$/', $line, $m) === 1) {
                $line = trim($m[1]);
                $icd = trim($m[2]);
            }
            $rows[] = ['condition' => $line, 'icd10' => $icd];
        }
        return $rows;
    }

    /**
     * Pull ``ExtractedField[str].value`` strings out of a list,
     * skipping abstaining entries.
     *
     * @return list<string>
     */
    private static function extractedFieldList(mixed $node): array
    {
        if (!is_array($node)) {
            return [];
        }
        $out = [];
        foreach ($node as $entry) {
            $value = self::leafString($entry);
            if ($value !== '') {
                $out[] = $value;
            }
        }
        return $out;
    }

    /**
     * Normalize a list of LabObservation dicts (the lab_pdf /
     * hl7_oru shape) into rows ChartWriteService.writeLabObservations
     * expects.
     *
     * @return list<array<string, mixed>>
     */
    private static function normalizeLabObservationList(mixed $node): array
    {
        if (!is_array($node)) {
            return [];
        }
        $out = [];
        foreach ($node as $row) {
            if (!is_array($row)) {
                continue;
            }
            $value = self::leafFloat($row['value'] ?? null);
            $display = self::leafString($row['display'] ?? null);
            $code = self::leafString($row['code'] ?? null);
            if ($value === null || $display === '') {
                continue;
            }
            $out[] = [
                'code' => $code,
                'display' => $display,
                'value' => $value,
                'unit' => self::leafString($row['unit'] ?? null),
                'reference_low' => self::leafFloat($row['reference_low'] ?? null),
                'reference_high' => self::leafFloat($row['reference_high'] ?? null),
                'flag' => self::leafString($row['flag'] ?? null),
                'effective_date' => self::leafString($row['effective_date'] ?? null),
            ];
        }
        return $out;
    }

    /**
     * Pull the ISO date string out of the first observation in a
     * list, used as the report_date when no panel-level date is on
     * the source.
     */
    private static function firstObservationDate(mixed $node): string
    {
        if (!is_array($node)) {
            return '';
        }
        foreach ($node as $row) {
            if (!is_array($row)) {
                continue;
            }
            $date = self::leafString($row['effective_date'] ?? null);
            if ($date !== '') {
                return $date;
            }
        }
        return '';
    }

    /**
     * Read the ``value`` out of an ``ExtractedField`` shape, returning
     * ``null`` for absent / abstained / wrong-shape entries.
     */
    private static function leafValue(mixed $node): mixed
    {
        if (!is_array($node)) {
            return null;
        }
        $value = $node['value'] ?? null;
        return $value;
    }

    private static function leafString(mixed $node): string
    {
        $value = self::leafValue($node);
        if (is_string($value)) {
            return trim($value);
        }
        if (is_int($value) || is_float($value)) {
            return (string) $value;
        }
        // Some leaves carry the value in a non-ExtractedField shape —
        // e.g. the patient.allergies field is itself an
        // ExtractedField[str], but a list element might be a bare
        // string in some test payloads.
        if (is_string($node)) {
            return trim($node);
        }
        return '';
    }

    private static function leafFloat(mixed $node): ?float
    {
        $value = self::leafValue($node);
        if (is_int($value) || is_float($value)) {
            return (float) $value;
        }
        if (is_string($value) && is_numeric($value)) {
            return (float) $value;
        }
        if (is_int($node) || is_float($node)) {
            return (float) $node;
        }
        return null;
    }
}
