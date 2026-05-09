<?php

/**
 * Services-tier tests for {@see ChartWriteCoordinator} — verifies the
 * idempotency contract the Co-Pilot save-document endpoint relies on
 * (the endpoint itself is a thin shell over the coordinator; see
 * ``interface/copilot/api/save_document.php``).
 *
 * Runs against the real MariaDB inside the development-easy Docker
 * stack so the tests exercise the same conditional UPDATE +
 * affected_rows semantics production will see. The matching
 * ChartWriteServiceTest covers the per-section SQL contracts; this
 * file is specifically the lock-acquire / replay / 409-on-concurrent
 * cycle.
 *
 * Filename mirrors the plan's "PR 2 — Idempotent chart-write save
 * endpoint" task block in ``plans/tasks_copilot_remediation_w2.md``.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Services\Copilot\ChartWrite;

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Services\Copilot\ChartWrite\ChartWriteCoordinator;
use OpenEMR\Services\Copilot\ChartWrite\ChartWriteOrchestrator;
use OpenEMR\Services\Copilot\ChartWrite\ChartWriteService;
use OpenEMR\Services\Copilot\ChartWrite\SaveOutcomeKind;
use OpenEMR\Services\Copilot\DocumentClassifier;
use PHPUnit\Framework\Attributes\Test;
use PHPUnit\Framework\TestCase;

final class SaveDocumentEndpointIdempotencyTest extends TestCase
{
    /**
     * Synthetic test pid — distinct from the 999_999 used in
     * ChartWriteServiceTest so concurrent test runs (and shared
     * tearDown) don't fight over the same chart rows.
     */
    private const TEST_PID = 999_998;

    /**
     * Synthetic ``documents.id``. We INSERT a stub row in setUp and
     * tear it down in tearDown so each test sees a clean documents
     * row in the same shape the upload endpoint would have created.
     */
    private const TEST_DOCUMENT_ROW_ID = 999_998;

    private const TEST_AUTHOR_USER_ID = 1;

    /** @var non-empty-string */
    private const DOCUMENT_ID_STRING = 'openemr:doc:999998';

    private ChartWriteCoordinator $coordinator;

    /** @var array<mixed,mixed> */
    private array $facts;

    /** @var list<non-empty-string> */
    private array $checkedSections;

    /** @var non-empty-string */
    private string $documentType;

    /** @var callable(int): array{pid: int, created: bool} */
    private $linkPatient;

    protected function setUp(): void
    {
        $this->purgeAllTestRows();
        $this->insertStubDocument();

        $this->coordinator = new ChartWriteCoordinator(
            new ChartWriteOrchestrator(new ChartWriteService(self::TEST_AUTHOR_USER_ID)),
        );

        $this->documentType = DocumentClassifier::TYPE_REFERRAL_DOCX;
        $this->facts = [
            'current_medications' => [
                ['value' => 'TestDrug 99 mg PO daily'],
            ],
        ];
        $this->checkedSections = ['medications'];

        $pid = self::TEST_PID;
        $this->linkPatient = static fn (int $rowId): array
            => ['pid' => $pid, 'created' => false];
    }

    protected function tearDown(): void
    {
        $this->purgeAllTestRows();
    }

    // ---- Acquired-and-wrote (first-time happy path) ----------------

    #[Test]
    public function testFirstSubmitAcquiresLockAndWritesChart(): void
    {
        $outcome = $this->coordinator->attemptSave(
            self::TEST_DOCUMENT_ROW_ID,
            self::DOCUMENT_ID_STRING,
            $this->documentType,
            $this->checkedSections,
            $this->facts,
            $this->linkPatient,
        );

        self::assertSame(SaveOutcomeKind::AcquiredAndWrote, $outcome->kind);
        self::assertSame(self::TEST_PID, $outcome->pid);
        self::assertFalse($outcome->patientCreated);
        self::assertSame(['medications' => 1], $outcome->counts);

        $row = $this->fetchDocumentRow();
        self::assertNotNull($row['chart_written_at']);
        self::assertNotNull($row['chart_write_started_at']);

        $summary = $row['chart_write_summary'];
        self::assertIsString($summary);
        $decoded = json_decode($summary, true);
        self::assertIsArray($decoded);
        self::assertSame(self::TEST_PID, $decoded['pid']);
        self::assertSame($this->documentType, $decoded['document_type']);
        self::assertSame(self::DOCUMENT_ID_STRING, $decoded['document_id']);
        self::assertSame(['medications'], $decoded['selected_sections']);
        self::assertSame(['medications' => 1], $decoded['counts']);
        self::assertFalse($decoded['patient_created']);

        self::assertCount(1, $this->fetchMedicationRows());
    }

    // ---- Idempotent replay -----------------------------------------

    #[Test]
    public function testIdenticalResubmitReturnsIdempotentReplayWithoutDoubleWriting(): void
    {
        $first = $this->coordinator->attemptSave(
            self::TEST_DOCUMENT_ROW_ID,
            self::DOCUMENT_ID_STRING,
            $this->documentType,
            $this->checkedSections,
            $this->facts,
            $this->linkPatient,
        );
        self::assertSame(SaveOutcomeKind::AcquiredAndWrote, $first->kind);
        self::assertCount(1, $this->fetchMedicationRows());

        $second = $this->coordinator->attemptSave(
            self::TEST_DOCUMENT_ROW_ID,
            self::DOCUMENT_ID_STRING,
            $this->documentType,
            $this->checkedSections,
            $this->facts,
            $this->linkPatient,
        );

        self::assertSame(SaveOutcomeKind::IdempotentReplay, $second->kind);
        self::assertSame(self::TEST_PID, $second->pid);
        self::assertSame(['medications' => 1], $second->counts);
        // The original chart row is still there; no new row was added.
        self::assertCount(
            1,
            $this->fetchMedicationRows(),
            'Idempotent replay must not invoke chart writers a second time.',
        );
    }

    #[Test]
    public function testIdempotentReplayPreservesPatientCreatedFlag(): void
    {
        $linkCreatesPatient = static fn (int $rowId): array
            => ['pid' => self::TEST_PID, 'created' => true];

        $first = $this->coordinator->attemptSave(
            self::TEST_DOCUMENT_ROW_ID,
            self::DOCUMENT_ID_STRING,
            $this->documentType,
            $this->checkedSections,
            $this->facts,
            $linkCreatesPatient,
        );
        self::assertTrue($first->patientCreated);

        $second = $this->coordinator->attemptSave(
            self::TEST_DOCUMENT_ROW_ID,
            self::DOCUMENT_ID_STRING,
            $this->documentType,
            $this->checkedSections,
            $this->facts,
            $linkCreatesPatient,
        );

        self::assertSame(SaveOutcomeKind::IdempotentReplay, $second->kind);
        self::assertTrue(
            $second->patientCreated,
            'The replay must surface the original patient_created flag from the stored summary.',
        );
    }

    // ---- Concurrent in-flight (409 path) ---------------------------

    #[Test]
    public function testConcurrentSubmitReturnsConcurrentInFlightOutcome(): void
    {
        // Simulate another worker holding the lock: set
        // chart_write_started_at = NOW() with chart_written_at still
        // NULL. Anything within the TTL window must be treated as a
        // live in-flight write rather than a stale lock.
        QueryUtils::sqlStatementThrowException(
            'UPDATE documents '
            . 'SET chart_write_started_at = NOW(), chart_written_at = NULL '
            . 'WHERE id = ?',
            [self::TEST_DOCUMENT_ROW_ID],
        );

        $linkPatientShouldNotRun = function (int $rowId): array {
            self::fail('linkPatient closure must not be invoked when the lock is held.');
        };

        $outcome = $this->coordinator->attemptSave(
            self::TEST_DOCUMENT_ROW_ID,
            self::DOCUMENT_ID_STRING,
            $this->documentType,
            $this->checkedSections,
            $this->facts,
            $linkPatientShouldNotRun,
        );

        self::assertSame(SaveOutcomeKind::ConcurrentInFlight, $outcome->kind);
        self::assertSame(0, $outcome->pid);
        self::assertSame([], $outcome->counts);
        self::assertCount(0, $this->fetchMedicationRows());
    }

    // ---- Stale-lock TTL recovery -----------------------------------

    #[Test]
    public function testStaleLockOlderThanTtlIsClaimedByNewSubmit(): void
    {
        // Backdate the lock 10 minutes ago (well past the 5-minute
        // default TTL). A fresh submit must steal the lock and run
        // chart-write rather than 409.
        QueryUtils::sqlStatementThrowException(
            'UPDATE documents '
            . 'SET chart_write_started_at = NOW() - INTERVAL 600 SECOND, '
            . '    chart_written_at = NULL '
            . 'WHERE id = ?',
            [self::TEST_DOCUMENT_ROW_ID],
        );

        $outcome = $this->coordinator->attemptSave(
            self::TEST_DOCUMENT_ROW_ID,
            self::DOCUMENT_ID_STRING,
            $this->documentType,
            $this->checkedSections,
            $this->facts,
            $this->linkPatient,
        );

        self::assertSame(SaveOutcomeKind::AcquiredAndWrote, $outcome->kind);
        self::assertCount(1, $this->fetchMedicationRows());
    }

    #[Test]
    public function testRecentLockWithinTtlIsRejected(): void
    {
        // 60 seconds is well inside the 5-minute TTL — must 409.
        QueryUtils::sqlStatementThrowException(
            'UPDATE documents '
            . 'SET chart_write_started_at = NOW() - INTERVAL 60 SECOND, '
            . '    chart_written_at = NULL '
            . 'WHERE id = ?',
            [self::TEST_DOCUMENT_ROW_ID],
        );

        $outcome = $this->coordinator->attemptSave(
            self::TEST_DOCUMENT_ROW_ID,
            self::DOCUMENT_ID_STRING,
            $this->documentType,
            $this->checkedSections,
            $this->facts,
            $this->linkPatient,
        );

        self::assertSame(SaveOutcomeKind::ConcurrentInFlight, $outcome->kind);
    }

    // ---- Document not found ---------------------------------------

    #[Test]
    public function testMissingDocumentRowReturnsDocumentNotFoundOutcome(): void
    {
        $outcome = $this->coordinator->attemptSave(
            999_001, // never inserted
            'openemr:doc:999001',
            $this->documentType,
            $this->checkedSections,
            $this->facts,
            $this->linkPatient,
        );

        self::assertSame(SaveOutcomeKind::DocumentNotFound, $outcome->kind);
    }

    #[Test]
    public function testNonPositiveDocumentRowIdReturnsDocumentNotFoundOutcome(): void
    {
        $outcome = $this->coordinator->attemptSave(
            0,
            'openemr:doc:0',
            $this->documentType,
            $this->checkedSections,
            $this->facts,
            $this->linkPatient,
        );

        self::assertSame(SaveOutcomeKind::DocumentNotFound, $outcome->kind);
    }

    // ---- Helpers ---------------------------------------------------

    private function insertStubDocument(): void
    {
        // ``documents.id`` is NOT NULL DEFAULT 0 (no AUTO_INCREMENT),
        // so an explicit pk works. ``revision`` is a NOT NULL
        // timestamp without an explicit default in this schema; the
        // server fills CURRENT_TIMESTAMP on INSERT when omitted.
        QueryUtils::sqlStatementThrowException(
            'INSERT INTO documents (id, foreign_id, list_id, encounter_id, encounter_check, '
            . 'audit_master_approval_status, encrypted, deleted, storagemethod, '
            . 'chart_write_started_at, chart_written_at, chart_write_summary) '
            . 'VALUES (?, 0, 0, 0, 0, 1, 0, 0, 0, NULL, NULL, NULL)',
            [self::TEST_DOCUMENT_ROW_ID],
        );
    }

    /**
     * @return array<mixed>
     */
    private function fetchDocumentRow(): array
    {
        $row = QueryUtils::querySingleRow(
            'SELECT chart_write_started_at, chart_written_at, chart_write_summary '
            . 'FROM documents WHERE id = ?',
            [self::TEST_DOCUMENT_ROW_ID],
        );
        self::assertIsArray($row);
        return $row;
    }

    /**
     * @return list<array<mixed>>
     */
    private function fetchMedicationRows(): array
    {
        return QueryUtils::fetchRecords(
            'SELECT id, title FROM lists WHERE pid = ? AND type = ?',
            [self::TEST_PID, 'medication'],
        );
    }

    /**
     * Drop the stub document row and any chart rows the test wrote.
     * Run BEFORE setUp's INSERT (to handle a prior crashed run that
     * left a row behind) AND on tearDown.
     */
    private function purgeAllTestRows(): void
    {
        QueryUtils::sqlStatementThrowException(
            'DELETE FROM documents WHERE id = ?',
            [self::TEST_DOCUMENT_ROW_ID],
        );
        QueryUtils::sqlStatementThrowException(
            'DELETE FROM lists WHERE pid = ?',
            [self::TEST_PID],
        );
    }
}
