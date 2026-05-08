<?php

/**
 * Writes Co-Pilot extracted facts into OpenEMR's chart-side tables.
 *
 * The multimodal upload flow (``upload_document.php`` →
 * ``document_review.php`` → ``api/save_document.php``) collects
 * extracted facts and lets the clinician confirm/edit them. This
 * service is the place where the confirmed facts cross from the
 * agent service's JSON store into OpenEMR's structured tables —
 * ``lists`` for allergies / medications / problems, ``dated_reminders``
 * for HEDIS-style care gaps, and the ``procedure_*`` chain for lab
 * observations.
 *
 * Methods are intentionally single-purpose ("writeAllergies" not
 * "writeListEntries(type=allergy)") so the call sites read like
 * what's actually happening clinically. Each write is idempotent at
 * the row level (caller provides distinct rows; the service does
 * not dedupe against existing chart rows because a duplicate
 * medication / allergy is the clinician's call to make on the
 * confirmation surface, not ours).
 *
 * The service does NOT enforce ACLs — that's the calling page's
 * job (``api/save_document.php`` runs ``AclMain::aclCheckCore``
 * before constructing the service). Same posture as
 * ``new_patient_save_ai.php`` and ``lab_save_ai.php``.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\ChartWrite;

use OpenEMR\Common\Database\QueryUtils;

readonly class ChartWriteService
{
    /**
     * @param int $authorUserId The ``users.id`` of the clinician
     *   confirming these writes — recorded as ``dr_from_ID`` on
     *   reminders and as a row in ``patient_access_log`` if we add
     *   one later. Pass the current session's ``authUserID``.
     */
    public function __construct(private int $authorUserId)
    {
    }

    /**
     * Insert one ``lists`` row per allergy. Each row uses the
     * documented OpenEMR pattern: ``type='allergy'``, ``title`` is
     * the substance, ``comments`` carries the reaction + severity.
     *
     * @param int $pid Patient pid.
     * @param list<array<string,mixed>> $allergies
     * @return int Rows written (skipped entries with empty substance
     *   are not counted).
     */
    public function writeAllergies(int $pid, array $allergies): int
    {
        if ($pid <= 0) {
            return 0;
        }
        $written = 0;
        $now = date('Y-m-d H:i:s');
        foreach ($allergies as $row) {
            $substance = self::s($row['substance'] ?? null);
            if ($substance === '') {
                continue;
            }
            $reaction = self::s($row['reaction'] ?? null);
            $severity = self::s($row['severity'] ?? null);
            $comments = $reaction;
            if ($severity !== '') {
                $comments = $reaction !== ''
                    ? sprintf('%s (%s)', $reaction, $severity)
                    : sprintf('(%s)', $severity);
            }
            QueryUtils::sqlInsert(
                <<<'SQL'
                INSERT INTO lists (date, type, title, pid, comments, occurrence, classification, begdate, activity)
                VALUES (NOW(), 'allergy', ?, ?, ?, 0, 0, ?, 1)
                SQL,
                [$substance, $pid, $comments, $now],
            );
            $written++;
        }
        return $written;
    }

    /**
     * Insert one ``lists`` row per medication. Title is the printed
     * name + dose + frequency; ``comments`` carries the indication;
     * ``diagnosis`` carries the RxNorm code as ``RXCUI:NNNNN`` per
     * the OpenEMR convention.
     *
     * @param int $pid Patient pid.
     * @param list<array<string,mixed>> $medications
     * @return int Rows written.
     */
    public function writeMedications(int $pid, array $medications): int
    {
        if ($pid <= 0) {
            return 0;
        }
        $written = 0;
        $now = date('Y-m-d H:i:s');
        foreach ($medications as $row) {
            $name = self::s($row['name'] ?? null);
            if ($name === '') {
                continue;
            }
            $dose = self::s($row['dose'] ?? null);
            $freq = self::s($row['frequency'] ?? null);
            $rxnorm = self::s($row['rxnorm'] ?? null);
            $indication = self::s($row['indication'] ?? null);
            $startedYear = self::i($row['started_year'] ?? null);
            $title = trim($name . ' ' . $dose . ($freq !== '' ? ' ' . $freq : ''));
            $diagnosis = $rxnorm !== '' ? 'RXCUI:' . $rxnorm : '';
            $begdate = $startedYear !== null && $startedYear > 0
                ? sprintf('%04d-01-01 00:00:00', $startedYear)
                : $now;
            QueryUtils::sqlInsert(
                <<<'SQL'
                INSERT INTO lists (date, type, title, pid, comments, diagnosis, occurrence, classification, begdate, activity)
                VALUES (NOW(), 'medication', ?, ?, ?, ?, 0, 0, ?, 1)
                SQL,
                [$title, $pid, $indication, $diagnosis, $begdate],
            );
            $written++;
        }
        return $written;
    }

    /**
     * Insert one ``lists`` row per active problem / past medical
     * history entry. Diagnosis column carries an ICD-10 ``ICD10:CODE``
     * and/or SNOMED ``SNOMED-CT:CODE``, semicolon-delimited when both
     * are present (matches the new_patient_save_ai.php convention).
     *
     * @param int $pid Patient pid.
     * @param list<array<string,mixed>> $problems
     * @return int Rows written.
     */
    public function writeActiveProblems(int $pid, array $problems): int
    {
        if ($pid <= 0) {
            return 0;
        }
        $written = 0;
        $now = date('Y-m-d H:i:s');
        foreach ($problems as $row) {
            $condition = self::s($row['condition'] ?? null);
            if ($condition === '') {
                continue;
            }
            $icd = self::s($row['icd10'] ?? null);
            $snomed = self::s($row['snomed'] ?? null);
            $onsetYear = self::i($row['onset_year'] ?? null);

            $diagnosisCol = '';
            if ($icd !== '') {
                $diagnosisCol = 'ICD10:' . $icd;
            }
            if ($snomed !== '') {
                $diagnosisCol = $diagnosisCol === ''
                    ? 'SNOMED-CT:' . $snomed
                    : $diagnosisCol . ';SNOMED-CT:' . $snomed;
            }
            $begdate = $onsetYear !== null && $onsetYear > 0
                ? sprintf('%04d-01-01 00:00:00', $onsetYear)
                : $now;
            QueryUtils::sqlInsert(
                <<<'SQL'
                INSERT INTO lists (date, type, title, pid, diagnosis, occurrence, classification, begdate, activity)
                VALUES (NOW(), 'medical_problem', ?, ?, ?, 0, 0, ?, 1)
                SQL,
                [$condition, $pid, $diagnosisCol, $begdate],
            );
            $written++;
        }
        return $written;
    }

    /**
     * Insert one ``dated_reminders`` row per care gap so it shows up
     * on the patient dashboard's Reminders card. Maps OVERDUE →
     * priority 1, anything else → priority 2.
     *
     * Care-gap text is capped at 160 chars per the column type;
     * longer notes are truncated with an ellipsis.
     *
     * @param int $pid Patient pid.
     * @param list<array<string,mixed>> $careGaps
     * @return int Rows written.
     */
    public function writeReminders(int $pid, array $careGaps): int
    {
        if ($pid <= 0) {
            return 0;
        }
        $written = 0;
        $now = date('Y-m-d H:i:s');
        foreach ($careGaps as $row) {
            $measure = self::s($row['measure'] ?? null);
            if ($measure === '') {
                continue;
            }
            $status = strtoupper(self::s($row['status'] ?? null));
            $notes = self::s($row['notes'] ?? null);
            $dueDate = self::s($row['due_date'] ?? null);
            if ($dueDate === '') {
                // ``dated_reminders.dr_message_due_date`` is NOT NULL — if
                // the source has no due date, surface the gap with today
                // so the clinician sees it immediately.
                $dueDate = date('Y-m-d');
            }

            $messageBase = $status === 'OVERDUE' || $status === 'DUE'
                ? sprintf('OVERDUE: %s', $measure)
                : sprintf('%s: %s', $status !== '' ? $status : 'CARE GAP', $measure);
            $message = $notes !== '' ? sprintf('%s — %s', $messageBase, $notes) : $messageBase;
            // dr_message_text is varchar(160).
            if (mb_strlen($message) > 160) {
                $message = mb_substr($message, 0, 159) . '…';
            }

            $priority = ($status === 'OVERDUE' || $status === 'DUE') ? 1 : 2;

            QueryUtils::sqlInsert(
                <<<'SQL'
                INSERT INTO dated_reminders
                    (dr_from_ID, dr_message_text, dr_message_sent_date, dr_message_due_date, pid, message_priority, message_processed, dr_processed_by)
                VALUES (?, ?, ?, ?, ?, ?, 0, 0)
                SQL,
                [$this->authorUserId, $message, $now, $dueDate, $pid, $priority],
            );
            $written++;
        }
        return $written;
    }

    /**
     * Write a procedure-order chain (``procedure_order`` →
     * ``procedure_order_code`` → ``procedure_report`` →
     * ``procedure_result`` rows) for an extracted lab panel. Mirrors
     * the existing ``lab_save_ai.php`` SQL exactly so the rows look
     * identical to a manually-confirmed lab upload.
     *
     * @param int $pid Patient pid.
     * @param string $panelName e.g. "Lipid panel with direct LDL —
     *   Co-Pilot import". Used as both procedure_name and report
     *   title.
     * @param string $panelLoinc Optional LOINC for the panel order
     *   (OBR-4 in HL7 ORU). Empty string when unknown.
     * @param string $reportDate Y-m-d for the report; defaults to
     *   today when missing.
     * @param list<array<string,mixed>> $observations
     * @return int Number of OBX-style results written
     *   (procedure_result rows).
     */
    public function writeLabObservations(
        int $pid,
        string $panelName,
        string $panelLoinc,
        string $reportDate,
        array $observations,
    ): int {
        if ($pid <= 0 || $observations === []) {
            return 0;
        }
        if ($reportDate === '') {
            $reportDate = date('Y-m-d');
        }
        $reportDateTime = $reportDate . ' 00:00:00';

        $orderId = QueryUtils::sqlInsert(
            <<<'SQL'
            INSERT INTO procedure_order
                (provider_id, patient_id, encounter_id, date_collected, date_ordered,
                 order_status, lab_id, specimen_type, procedure_order_type, order_intent)
            VALUES (?, ?, 0, ?, ?, 'completed', 0, '', 'laboratory_test', 'order')
            SQL,
            [$this->authorUserId, $pid, $reportDateTime, $reportDate],
        );

        QueryUtils::sqlInsert(
            <<<'SQL'
            INSERT INTO procedure_order_code
                (procedure_order_id, procedure_order_seq, procedure_code, procedure_name,
                 procedure_source, procedure_order_title)
            VALUES (?, 1, ?, ?, '1', 'laboratory_test')
            SQL,
            [$orderId, $panelLoinc !== '' ? $panelLoinc : 'COPILOT-IMPORT', $panelName],
        );

        $reportId = QueryUtils::sqlInsert(
            <<<'SQL'
            INSERT INTO procedure_report
                (procedure_order_id, procedure_order_seq, date_collected, date_report,
                 source, specimen_num, report_status, review_status)
            VALUES (?, 1, ?, ?, ?, '', 'final', 'reviewed')
            SQL,
            [$orderId, $reportDateTime, $reportDateTime, $this->authorUserId],
        );

        $written = 0;
        foreach ($observations as $obs) {
            $code = self::s($obs['code'] ?? null);
            $display = self::s($obs['display'] ?? null);
            $value = self::s($obs['value'] ?? null);
            $unit = self::s($obs['unit'] ?? null);
            $low = $obs['reference_low'] ?? null;
            $high = $obs['reference_high'] ?? null;
            $flag = $this->normalizeAbnormalFlag(self::s($obs['flag'] ?? null));

            $rangeParts = [];
            if (is_numeric($low)) {
                $rangeParts[] = (string) $low;
            }
            if (is_numeric($high)) {
                $rangeParts[] = (string) $high;
            }
            $rangeText = $rangeParts === [] ? '' : implode('-', $rangeParts);

            // ``result`` and ``range`` are MariaDB reserved words — must
            // be backtick-quoted. Column order matches the existing
            // ``lab_save_ai.php`` exactly so Co-Pilot import rows look
            // identical to manually-confirmed lab uploads. The per-
            // observation date isn't a column on ``procedure_result``;
            // the parent ``procedure_report.date_report`` carries it.
            QueryUtils::sqlInsert(
                <<<'SQL'
                INSERT INTO procedure_result
                    (procedure_report_id, result_data_type, result_code, result_text,
                     units, `result`, `range`, abnormal, result_status)
                VALUES (?, 'N', ?, ?, ?, ?, ?, ?, 'final')
                SQL,
                [$reportId, $code, $display, $unit, $value, $rangeText, $flag],
            );
            $written++;
        }
        return $written;
    }

    /**
     * Narrow a ``mixed`` row value to a trimmed string. Used at every
     * read-from-row site so the row maps stay typed
     * ``array<string,mixed>`` (which is what we get from JSON-decoded
     * extractor output) without sprinkling ``(string)`` casts that
     * phpstan rightly forbids.
     */
    private static function s(mixed $value): string
    {
        if (is_string($value)) {
            return trim($value);
        }
        if (is_int($value) || is_float($value)) {
            return (string) $value;
        }
        return '';
    }

    /**
     * Narrow a ``mixed`` row value to int (or null when not coercible).
     */
    private static function i(mixed $value): ?int
    {
        if (is_int($value)) {
            return $value;
        }
        if (is_float($value)) {
            return (int) $value;
        }
        if (is_string($value) && ctype_digit($value)) {
            return (int) $value;
        }
        return null;
    }

    /**
     * Map our extractor's flag glyphs (H/L/N/HH/LL/A) to the
     * ``procedure_result.abnormal`` enum values OpenEMR understands.
     */
    private function normalizeAbnormalFlag(string $flag): string
    {
        $upper = strtoupper($flag);
        return match ($upper) {
            'H', 'HH' => 'high',
            'L', 'LL' => 'low',
            'A' => 'abnormal',
            default => '',
        };
    }
}
