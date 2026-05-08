<?php

/**
 * Services-tier tests for ChartWriteService — runs against the real
 * MariaDB inside the development-easy Docker stack. Verifies the SQL
 * contracts that the chart-write path commits to: each writer lands
 * the right rows in the right tables with the right column values.
 *
 * The matching isolated tests for FactsExtractor (per-doc-type
 * adapters) and ChartWriteOrchestrator (dispatch logic) cover the
 * non-DB paths; this file is specifically the DB-side contract.
 *
 * Per the plan in plans/week2-chart-write-tests.md, the
 * idempotency-lock-in test (testWriteAllergiesDoesNotDedupeOnRepeatCall)
 * is referenced from SUBMISSIONW2.md as the documented current
 * behaviour.
 *
 * The test uses ``assertEquals`` for DB read-back values rather than
 * ``assertSame`` — MariaDB driver settings can return numeric columns
 * either as PHP ints or as numeric strings depending on emulation
 * mode, and the loose comparison covers both. This matches the
 * pattern in ``PatientServiceTest`` / ``FacilityServiceTest``.
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
use OpenEMR\Services\Copilot\ChartWrite\ChartWriteService;
use PHPUnit\Framework\Attributes\Test;
use PHPUnit\Framework\TestCase;

final class ChartWriteServiceTest extends TestCase
{
    /**
     * Synthetic pid well above any FixtureManager-generated patient.
     * tearDown wipes every chart row tagged with this pid so the
     * suite stays self-cleaning.
     */
    private const TEST_PID = 999_999;

    private const TEST_AUTHOR_USER_ID = 1;

    private ChartWriteService $service;

    protected function setUp(): void
    {
        $this->service = new ChartWriteService(self::TEST_AUTHOR_USER_ID);
        $this->purgeTestRows();
    }

    protected function tearDown(): void
    {
        $this->purgeTestRows();
    }

    // ---- Allergies ---------------------------------------------------

    #[Test]
    public function testWriteAllergiesInsertsListsRowWithReactionAndSeverity(): void
    {
        $written = $this->service->writeAllergies(self::TEST_PID, [
            ['substance' => 'Penicillin', 'reaction' => 'hives', 'severity' => 'moderate'],
        ]);

        self::assertSame(1, $written);

        $rows = $this->fetchListsRows('allergy');
        self::assertCount(1, $rows);
        self::assertSame('Penicillin', $rows[0]['title']);
        self::assertSame('hives (moderate)', $rows[0]['comments']);
        self::assertEquals(1, $rows[0]['activity']);
    }

    #[Test]
    public function testWriteAllergiesSkipsRowsWithEmptySubstance(): void
    {
        $written = $this->service->writeAllergies(self::TEST_PID, [
            ['substance' => '', 'reaction' => 'hives'],
            ['substance' => 'Sulfa', 'reaction' => 'rash'],
        ]);

        self::assertSame(1, $written);
        self::assertCount(1, $this->fetchListsRows('allergy'));
    }

    #[Test]
    public function testWriteAllergiesWithSeverityOnlyWrapsInParentheses(): void
    {
        $this->service->writeAllergies(self::TEST_PID, [
            ['substance' => 'Latex', 'reaction' => '', 'severity' => 'mild'],
        ]);

        $rows = $this->fetchListsRows('allergy');
        self::assertSame('(mild)', $rows[0]['comments']);
    }

    #[Test]
    public function testWriteAllergiesReturnsZeroForNonPositivePid(): void
    {
        $written = $this->service->writeAllergies(0, [
            ['substance' => 'Penicillin', 'reaction' => 'hives'],
        ]);

        self::assertSame(0, $written);
        self::assertCount(0, QueryUtils::fetchRecords(
            'SELECT id FROM lists WHERE pid = 0 AND title = ?',
            ['Penicillin'],
        ));
    }

    /**
     * Lock the documented current behaviour: two consecutive calls
     * with the same payload write 2× rows. Re-confirm intent if you
     * find yourself wanting to weaken or delete this test — the
     * idempotency story in SUBMISSIONW2.md (production hardening =
     * per-document_id markers) hangs off this contract.
     */
    #[Test]
    public function testWriteAllergiesDoesNotDedupeOnRepeatCall(): void
    {
        $payload = [['substance' => 'Penicillin', 'reaction' => 'hives']];

        $first = $this->service->writeAllergies(self::TEST_PID, $payload);
        $second = $this->service->writeAllergies(self::TEST_PID, $payload);

        self::assertSame(1, $first);
        self::assertSame(1, $second);
        self::assertCount(2, $this->fetchListsRows('allergy'));
    }

    // ---- Medications -------------------------------------------------

    #[Test]
    public function testWriteMedicationsBuildsTitleAndDiagnosisFromRxnorm(): void
    {
        $written = $this->service->writeMedications(self::TEST_PID, [
            [
                'name' => 'atorvastatin',
                'dose' => '40 mg',
                'frequency' => 'PO daily',
                'rxnorm' => '83367',
                'indication' => 'Hyperlipidemia',
                'started_year' => 2022,
            ],
        ]);

        self::assertSame(1, $written);
        $rows = $this->fetchListsRows('medication');
        self::assertCount(1, $rows);
        self::assertSame('atorvastatin 40 mg PO daily', $rows[0]['title']);
        self::assertSame('Hyperlipidemia', $rows[0]['comments']);
        self::assertSame('RXCUI:83367', $rows[0]['diagnosis']);
        $begdate = $rows[0]['begdate'];
        self::assertIsString($begdate);
        self::assertStringStartsWith('2022-01-01', $begdate);
    }

    #[Test]
    public function testWriteMedicationsWithoutRxnormLeavesDiagnosisEmpty(): void
    {
        $this->service->writeMedications(self::TEST_PID, [
            ['name' => 'lisinopril', 'dose' => '10 mg'],
        ]);

        $rows = $this->fetchListsRows('medication');
        self::assertSame('', $rows[0]['diagnosis']);
    }

    // ---- Active problems --------------------------------------------

    #[Test]
    public function testWriteActiveProblemsCombinesIcd10AndSnomed(): void
    {
        $this->service->writeActiveProblems(self::TEST_PID, [
            [
                'condition' => 'Hyperlipidemia',
                'icd10' => 'E78.5',
                'snomed' => '55822004',
                'onset_year' => 2018,
            ],
        ]);

        $rows = $this->fetchListsRows('medical_problem');
        self::assertCount(1, $rows);
        self::assertSame('Hyperlipidemia', $rows[0]['title']);
        self::assertSame('ICD10:E78.5;SNOMED-CT:55822004', $rows[0]['diagnosis']);
        $begdate = $rows[0]['begdate'];
        self::assertIsString($begdate);
        self::assertStringStartsWith('2018-01-01', $begdate);
    }

    #[Test]
    public function testWriteActiveProblemsWithSnomedOnlyOmitsIcdPrefix(): void
    {
        $this->service->writeActiveProblems(self::TEST_PID, [
            ['condition' => 'Some condition', 'snomed' => '123456'],
        ]);

        $rows = $this->fetchListsRows('medical_problem');
        self::assertSame('SNOMED-CT:123456', $rows[0]['diagnosis']);
    }

    // ---- Reminders --------------------------------------------------

    #[Test]
    public function testWriteRemindersOverdueMapsToPriorityOne(): void
    {
        $this->service->writeReminders(self::TEST_PID, [
            [
                'measure' => 'Mammography',
                'status' => 'OVERDUE',
                'due_date' => '2025-11-04',
                'notes' => 'Schedule screening',
            ],
        ]);

        $rows = $this->fetchReminderRows();
        self::assertCount(1, $rows);
        self::assertEquals(1, $rows[0]['message_priority']);
        self::assertEquals(self::TEST_AUTHOR_USER_ID, $rows[0]['dr_from_ID']);
        $messageText = $rows[0]['dr_message_text'];
        self::assertIsString($messageText);
        self::assertStringContainsString('OVERDUE: Mammography', $messageText);
        self::assertStringContainsString('Schedule screening', $messageText);
    }

    #[Test]
    public function testWriteRemindersNonOverdueMapsToPriorityTwo(): void
    {
        $this->service->writeReminders(self::TEST_PID, [
            ['measure' => 'Annual visit', 'status' => 'UPCOMING'],
        ]);

        $rows = $this->fetchReminderRows();
        self::assertEquals(2, $rows[0]['message_priority']);
    }

    #[Test]
    public function testWriteRemindersTruncatesMessageAtOneSixty(): void
    {
        // dr_message_text is varchar(160). Build a measure long enough
        // that the formatted string overflows; the service should
        // truncate to 159 chars + a horizontal-ellipsis (…). The
        // ellipsis is a 3-byte UTF-8 sequence so the byte length is
        // larger than 160 — the column is utf8mb4 so a 160-char
        // string fits even if it's >160 bytes. Assert character length.
        $longMeasure = str_repeat('A', 200);

        $this->service->writeReminders(self::TEST_PID, [
            ['measure' => $longMeasure, 'status' => 'OVERDUE'],
        ]);

        $rows = $this->fetchReminderRows();
        $text = $rows[0]['dr_message_text'];
        self::assertIsString($text);
        self::assertSame(160, mb_strlen($text));
        self::assertStringEndsWith('…', $text);
    }

    #[Test]
    public function testWriteRemindersWithoutDueDateUsesToday(): void
    {
        $this->service->writeReminders(self::TEST_PID, [
            ['measure' => 'Some gap', 'status' => 'DUE'],
        ]);

        $rows = $this->fetchReminderRows();
        self::assertSame(date('Y-m-d'), $rows[0]['dr_message_due_date']);
    }

    // ---- Lab observations -------------------------------------------

    #[Test]
    public function testWriteLabObservationsBuildsFullProcedureChain(): void
    {
        $written = $this->service->writeLabObservations(
            self::TEST_PID,
            'Lipid panel with direct LDL — Co-Pilot import',
            '57698-3',
            '2026-04-12',
            [
                [
                    'code' => '2093-3',
                    'display' => 'Cholesterol',
                    'value' => '218',
                    'unit' => 'mg/dL',
                    'reference_low' => 0,
                    'reference_high' => 200,
                    'flag' => 'H',
                ],
                [
                    'code' => '13457-7',
                    'display' => 'LDL',
                    'value' => '142',
                    'unit' => 'mg/dL',
                    'flag' => 'H',
                ],
            ],
        );

        self::assertSame(2, $written);

        $orders = QueryUtils::fetchRecords(
            'SELECT * FROM procedure_order WHERE patient_id = ?',
            [self::TEST_PID],
        );
        self::assertCount(1, $orders);
        $orderId = $orders[0]['procedure_order_id'];
        self::assertEquals(self::TEST_AUTHOR_USER_ID, $orders[0]['provider_id']);

        $orderCodes = QueryUtils::fetchRecords(
            'SELECT * FROM procedure_order_code WHERE procedure_order_id = ?',
            [$orderId],
        );
        self::assertCount(1, $orderCodes);
        self::assertSame('57698-3', $orderCodes[0]['procedure_code']);
        self::assertSame('Lipid panel with direct LDL — Co-Pilot import', $orderCodes[0]['procedure_name']);

        $reports = QueryUtils::fetchRecords(
            'SELECT * FROM procedure_report WHERE procedure_order_id = ?',
            [$orderId],
        );
        self::assertCount(1, $reports);
        $reportId = $reports[0]['procedure_report_id'];

        $results = QueryUtils::fetchRecords(
            'SELECT * FROM procedure_result WHERE procedure_report_id = ? ORDER BY procedure_result_id',
            [$reportId],
        );
        self::assertCount(2, $results);
        self::assertSame('2093-3', $results[0]['result_code']);
        self::assertSame('218', $results[0]['result']);
        self::assertSame('0-200', $results[0]['range']);
        self::assertSame('high', $results[0]['abnormal']);
    }

    #[Test]
    public function testWriteLabObservationsFallsBackToCopilotImportLoincWhenMissing(): void
    {
        $this->service->writeLabObservations(
            self::TEST_PID,
            'Workbook lab import',
            '',
            '2026-04-12',
            [
                ['code' => '2093-3', 'display' => 'Cholesterol', 'value' => '218', 'unit' => 'mg/dL'],
            ],
        );

        $orders = QueryUtils::fetchRecords(
            'SELECT po.procedure_order_id FROM procedure_order po WHERE po.patient_id = ?',
            [self::TEST_PID],
        );
        $orderId = $orders[0]['procedure_order_id'];

        $orderCodes = QueryUtils::fetchRecords(
            'SELECT procedure_code FROM procedure_order_code WHERE procedure_order_id = ?',
            [$orderId],
        );
        self::assertSame('COPILOT-IMPORT', $orderCodes[0]['procedure_code']);
    }

    #[Test]
    public function testWriteLabObservationsReturnsZeroForEmptyObservationList(): void
    {
        $written = $this->service->writeLabObservations(
            self::TEST_PID,
            'Lipid panel',
            '57698-3',
            '2026-04-12',
            [],
        );

        self::assertSame(0, $written);
        self::assertCount(0, QueryUtils::fetchRecords(
            'SELECT procedure_order_id FROM procedure_order WHERE patient_id = ?',
            [self::TEST_PID],
        ));
    }

    #[Test]
    public function testWriteLabObservationsMapsAbnormalFlagsToOpenemrEnum(): void
    {
        $this->service->writeLabObservations(
            self::TEST_PID,
            'Mixed flags panel',
            '11111-1',
            '2026-04-12',
            [
                ['code' => 'A', 'display' => 'High',     'value' => '1', 'flag' => 'H'],
                ['code' => 'B', 'display' => 'Very-low', 'value' => '2', 'flag' => 'LL'],
                ['code' => 'C', 'display' => 'Abnormal', 'value' => '3', 'flag' => 'A'],
                ['code' => 'D', 'display' => 'Normal',   'value' => '4', 'flag' => 'N'],
            ],
        );

        $orders = QueryUtils::fetchRecords(
            'SELECT procedure_order_id FROM procedure_order WHERE patient_id = ?',
            [self::TEST_PID],
        );
        $orderId = $orders[0]['procedure_order_id'];
        $reports = QueryUtils::fetchRecords(
            'SELECT procedure_report_id FROM procedure_report WHERE procedure_order_id = ?',
            [$orderId],
        );
        $reportId = $reports[0]['procedure_report_id'];
        $results = QueryUtils::fetchRecords(
            'SELECT result_code, abnormal FROM procedure_result WHERE procedure_report_id = ? ORDER BY procedure_result_id',
            [$reportId],
        );

        $byCode = [];
        foreach ($results as $row) {
            $code = $row['result_code'];
            self::assertIsString($code);
            $byCode[$code] = $row['abnormal'];
        }
        self::assertSame('high', $byCode['A']);
        self::assertSame('low', $byCode['B']);
        self::assertSame('abnormal', $byCode['C']);
        self::assertSame('', $byCode['D']);
    }

    // ---- Helpers ----------------------------------------------------

    /**
     * @return list<array<mixed>>
     */
    private function fetchListsRows(string $type): array
    {
        return QueryUtils::fetchRecords(
            'SELECT title, comments, diagnosis, activity, begdate FROM lists '
            . 'WHERE pid = ? AND type = ? ORDER BY id',
            [self::TEST_PID, $type],
        );
    }

    /**
     * @return list<array<mixed>>
     */
    private function fetchReminderRows(): array
    {
        return QueryUtils::fetchRecords(
            'SELECT dr_from_ID, dr_message_text, dr_message_due_date, message_priority '
            . 'FROM dated_reminders WHERE pid = ? ORDER BY dr_id',
            [self::TEST_PID],
        );
    }

    /**
     * Drop every chart row this test class might have written. The
     * synthetic TEST_PID is well above any FixtureManager-generated
     * patient so the WHERE clauses are safe to delete on.
     *
     * Procedure tables form a parent → child chain
     * (order → code → report → result); delete leaves first to keep
     * the DB consistent across teardown failures.
     */
    private function purgeTestRows(): void
    {
        $orderIds = QueryUtils::fetchRecords(
            'SELECT procedure_order_id FROM procedure_order WHERE patient_id = ?',
            [self::TEST_PID],
        );
        foreach ($orderIds as $row) {
            $orderId = $row['procedure_order_id'];
            $reports = QueryUtils::fetchRecords(
                'SELECT procedure_report_id FROM procedure_report WHERE procedure_order_id = ?',
                [$orderId],
            );
            foreach ($reports as $r) {
                QueryUtils::sqlStatementThrowException(
                    'DELETE FROM procedure_result WHERE procedure_report_id = ?',
                    [$r['procedure_report_id']],
                );
            }
            QueryUtils::sqlStatementThrowException(
                'DELETE FROM procedure_report WHERE procedure_order_id = ?',
                [$orderId],
            );
            QueryUtils::sqlStatementThrowException(
                'DELETE FROM procedure_order_code WHERE procedure_order_id = ?',
                [$orderId],
            );
            QueryUtils::sqlStatementThrowException(
                'DELETE FROM procedure_order WHERE procedure_order_id = ?',
                [$orderId],
            );
        }

        QueryUtils::sqlStatementThrowException(
            'DELETE FROM lists WHERE pid = ?',
            [self::TEST_PID],
        );
        QueryUtils::sqlStatementThrowException(
            'DELETE FROM dated_reminders WHERE pid = ?',
            [self::TEST_PID],
        );
    }
}
