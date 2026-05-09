<?php

/**
 * Wraps the lock-acquire / chart-write / finalize-marker cycle that
 * the Co-Pilot save-document endpoint runs. Pulled out of
 * ``interface/copilot/api/save_document.php`` so the idempotency
 * contract is unit-testable against the real DB.
 *
 * Concurrency model:
 *   - The conditional UPDATE on ``documents`` ("SET
 *     chart_write_started_at = NOW() WHERE id = ? AND chart_written_at
 *     IS NULL AND (chart_write_started_at IS NULL OR
 *     chart_write_started_at < NOW() - INTERVAL ? SECOND)") functions
 *     simultaneously as a row-level lock and an idempotency check.
 *   - ``affected_rows == 1`` → the caller acquired the lock.
 *   - ``affected_rows == 0`` plus ``chart_written_at IS NOT NULL`` →
 *     a prior save already completed; replay its stored summary.
 *   - ``affected_rows == 0`` plus ``chart_written_at IS NULL`` →
 *     another writer holds the lock within the TTL; surface 409.
 *
 * The TTL clause guards against a crashed worker holding the lock
 * forever. Today nothing watchdog-cleans the column; the TTL bound
 * (default 300 seconds) is the recovery mechanism.
 *
 * The whole sequence runs inside one DB transaction. Per pre-flight
 * #6 (see ``plans/tasks_copilot_remediation_w2.md``), every chart
 * writer dispatches through ``QueryUtils::sqlInsert()`` →
 * ``QueryUtils::getADODB()`` → the same global ADODB handle, so a
 * coordinator-owned transaction and an orchestrator-issued INSERT
 * commit through the same connection.
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

final readonly class ChartWriteCoordinator
{
    public const DEFAULT_LOCK_TTL_SECONDS = 300;

    public function __construct(
        private ChartWriteOrchestrator $orchestrator,
        private int $lockTtlSeconds = self::DEFAULT_LOCK_TTL_SECONDS,
    ) {
    }

    /**
     * Try to save the chart-write block for ``$documentRowId``. The
     * caller supplies a ``$linkPatient`` closure that runs INSIDE the
     * transaction AFTER the lock is acquired and BEFORE chart-write —
     * that is where the existing-patient ``UPDATE foreign_id`` or the
     * new-patient ``INSERT INTO patient_data`` happens. Letting the
     * caller pick the patient keeps the coordinator agnostic about the
     * patient_choice branching while still letting both paths share
     * the lock / chart-write / finalize cycle.
     *
     * On a recovered partial-failure (a prior attempt acquired the
     * lock, created a new patient and updated ``foreign_id``, then
     * crashed before finalizing) the closure can detect the existing
     * ``foreign_id`` and reuse it instead of double-creating; see the
     * new-patient closure in ``save_document.php``.
     *
     * Returns one of the four {@see SaveOutcomeKind} cases. The
     * endpoint maps each to an HTTP response.
     *
     * @param int $documentRowId Numeric ``documents.id`` (the bare
     *   integer; ``$documentIdString`` carries the prefixed form).
     * @param non-empty-string $documentIdString Original "openemr:doc:N"
     *   style id; stored in the summary so the success page can
     *   reconstruct its own redirect.
     * @param non-empty-string $documentType Co-Pilot doc type tag.
     * @param list<non-empty-string> $checkedSections Sections the
     *   review form ticked.
     * @param array<mixed, mixed> $facts Merged extracted facts.
     * @param callable(int): array{pid: int, created: bool} $linkPatient
     *   Resolves a patient pid for this document. Runs once, inside
     *   the transaction, only on the lock-acquired path. Must not call
     *   the chart writers itself — that is the orchestrator's job.
     */
    public function attemptSave(
        int $documentRowId,
        string $documentIdString,
        string $documentType,
        array $checkedSections,
        array $facts,
        callable $linkPatient,
    ): SaveOutcome {
        if ($documentRowId <= 0) {
            return SaveOutcome::documentNotFound();
        }

        return QueryUtils::inTransaction(function () use (
            $documentRowId,
            $documentIdString,
            $documentType,
            $checkedSections,
            $facts,
            $linkPatient,
        ): SaveOutcome {
            $acquired = $this->acquireLock($documentRowId);
            if (!$acquired) {
                return $this->resolveNonAcquired($documentRowId);
            }

            $linkResult = $linkPatient($documentRowId);
            $pid = $linkResult['pid'];
            $patientCreated = $linkResult['created'];

            $writeSummary = $this->orchestrator->run(
                $pid,
                $checkedSections,
                $facts,
                $documentType,
            );

            $payload = $this->buildSummaryPayload(
                pid: $pid,
                patientCreated: $patientCreated,
                documentType: $documentType,
                documentIdString: $documentIdString,
                selectedSections: $checkedSections,
                summary: $writeSummary,
            );

            QueryUtils::sqlStatementThrowException(
                'UPDATE documents '
                . 'SET chart_written_at = NOW(), chart_write_summary = ? '
                . 'WHERE id = ?',
                [
                    json_encode($payload, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES),
                    $documentRowId,
                ],
            );

            return SaveOutcome::acquiredAndWrote(
                $pid,
                $patientCreated,
                $writeSummary->counts(),
                $checkedSections,
            );
        });
    }

    /**
     * Run the conditional UPDATE that simultaneously claims the lock
     * and asserts the row hasn't already finished. Returns true iff
     * exactly one row was affected (i.e. THIS caller acquired the
     * lock).
     */
    private function acquireLock(int $documentRowId): bool
    {
        QueryUtils::sqlStatementThrowException(
            'UPDATE documents '
            . 'SET chart_write_started_at = NOW() '
            . 'WHERE id = ? '
            . '  AND chart_written_at IS NULL '
            . '  AND (chart_write_started_at IS NULL '
            . '       OR chart_write_started_at < NOW() - INTERVAL ? SECOND)',
            [$documentRowId, $this->lockTtlSeconds],
        );
        return QueryUtils::affectedRows() === 1;
    }

    /**
     * Couldn't grab the lock. Re-read the row to differentiate the
     * three possible reasons: (a) row never existed, (b) prior save
     * already completed → replay, (c) another worker holds the lock
     * within TTL → 409.
     */
    private function resolveNonAcquired(int $documentRowId): SaveOutcome
    {
        $row = QueryUtils::querySingleRow(
            'SELECT chart_written_at, chart_write_summary FROM documents WHERE id = ?',
            [$documentRowId],
        );
        if (!is_array($row)) {
            return SaveOutcome::documentNotFound();
        }
        if (($row['chart_written_at'] ?? null) === null) {
            // Lock held by another worker; TTL hasn't expired yet.
            return SaveOutcome::concurrentInFlight();
        }

        $summaryRaw = $row['chart_write_summary'] ?? null;
        $decoded = is_string($summaryRaw) && $summaryRaw !== ''
            ? json_decode($summaryRaw, true)
            : null;
        if (!is_array($decoded)) {
            $decoded = [];
        }
        return SaveOutcome::idempotentReplay($decoded);
    }

    /**
     * @param list<non-empty-string> $selectedSections
     * @return array<string, mixed>
     */
    private function buildSummaryPayload(
        int $pid,
        bool $patientCreated,
        string $documentType,
        string $documentIdString,
        array $selectedSections,
        ChartWriteSummary $summary,
    ): array {
        return [
            'pid' => $pid,
            'patient_created' => $patientCreated,
            'document_type' => $documentType,
            'document_id' => $documentIdString,
            'selected_sections' => $selectedSections,
            'counts' => $summary->counts(),
        ];
    }

}
