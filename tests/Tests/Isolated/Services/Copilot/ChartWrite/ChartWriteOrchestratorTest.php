<?php

/**
 * Isolated tests for ChartWriteOrchestrator — verifies the dispatch
 * logic that decides which {@see ChartWriteService} method fires for
 * each ticked review-page section. ChartWriteService is mocked so
 * these tests don't need a database; the SQL contracts for each
 * write method are covered separately in the services-tier
 * ChartWriteServiceTest.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot\ChartWrite;

use OpenEMR\Services\Copilot\ChartWrite\ChartWriteOrchestrator;
use OpenEMR\Services\Copilot\ChartWrite\ChartWriteService;
use OpenEMR\Services\Copilot\DocumentClassifier;
use PHPUnit\Framework\MockObject\MockObject;
use PHPUnit\Framework\TestCase;

final class ChartWriteOrchestratorTest extends TestCase
{
    private ChartWriteService&MockObject $service;
    private ChartWriteOrchestrator $orchestrator;

    protected function setUp(): void
    {
        $this->service = $this->createMock(ChartWriteService::class);
        $this->orchestrator = new ChartWriteOrchestrator($this->service);
    }

    public function testRunWithEmptyChecklistInvokesNoWriters(): void
    {
        $this->service->expects(self::never())->method('writeAllergies');
        $this->service->expects(self::never())->method('writeMedications');
        $this->service->expects(self::never())->method('writeActiveProblems');
        $this->service->expects(self::never())->method('writeReminders');
        $this->service->expects(self::never())->method('writeLabObservations');

        $summary = $this->orchestrator->run(42, [], [], DocumentClassifier::TYPE_INTAKE_FORM);

        self::assertTrue($summary->isEmpty());
        self::assertSame([], $summary->counts());
    }

    public function testRunDispatchesOnlyCheckedSections(): void
    {
        $this->service->expects(self::once())->method('writeAllergies')->willReturn(0);
        $this->service->expects(self::once())->method('writeMedications')->willReturn(0);
        $this->service->expects(self::never())->method('writeActiveProblems');
        $this->service->expects(self::never())->method('writeReminders');
        $this->service->expects(self::never())->method('writeLabObservations');

        $this->orchestrator->run(
            42,
            ['allergies', 'medications'],
            [],
            DocumentClassifier::TYPE_INTAKE_FORM,
        );
    }

    public function testRunWithUnknownSectionStringDispatchesNothing(): void
    {
        // Defensive: an unrecognised section name (typo'd checkbox,
        // future placeholder, etc.) must not fire any writer. The
        // strict in_array check inside the orchestrator is what
        // enforces this; the test locks the behaviour.
        $this->service->expects(self::never())->method('writeAllergies');
        $this->service->expects(self::never())->method('writeMedications');
        $this->service->expects(self::never())->method('writeActiveProblems');
        $this->service->expects(self::never())->method('writeReminders');
        $this->service->expects(self::never())->method('writeLabObservations');

        $summary = $this->orchestrator->run(
            42,
            ['NOT_A_REAL_SECTION'],
            [],
            DocumentClassifier::TYPE_INTAKE_FORM,
        );

        self::assertTrue($summary->isEmpty());
    }

    public function testRunPassesPidThroughToEveryDispatchedWriter(): void
    {
        $expectedPid = 42;
        $this->service->expects(self::once())->method('writeAllergies')
            ->with($expectedPid, self::anything())
            ->willReturn(0);
        $this->service->expects(self::once())->method('writeMedications')
            ->with($expectedPid, self::anything())
            ->willReturn(0);
        $this->service->expects(self::once())->method('writeActiveProblems')
            ->with($expectedPid, self::anything())
            ->willReturn(0);
        $this->service->expects(self::once())->method('writeReminders')
            ->with($expectedPid, self::anything())
            ->willReturn(0);
        $this->service->expects(self::once())->method('writeLabObservations')
            ->with(
                $expectedPid,
                self::anything(),
                self::anything(),
                self::anything(),
                self::anything(),
            )
            ->willReturn(0);

        $this->orchestrator->run(
            $expectedPid,
            ['allergies', 'medications', 'active_problems', 'care_gaps', 'lab_observations'],
            [],
            DocumentClassifier::TYPE_INTAKE_FORM,
        );
    }

    public function testRunRecordsRowCountsIntoSummary(): void
    {
        $this->service->method('writeAllergies')->willReturn(2);
        $this->service->method('writeMedications')->willReturn(3);
        $this->service->method('writeActiveProblems')->willReturn(1);

        $summary = $this->orchestrator->run(
            42,
            ['allergies', 'medications', 'active_problems'],
            [],
            DocumentClassifier::TYPE_INTAKE_FORM,
        );

        self::assertSame(
            ['allergies' => 2, 'medications' => 3, 'active_problems' => 1],
            $summary->counts(),
        );
        self::assertSame(6, $summary->totalRowsWritten());
        self::assertFalse($summary->isEmpty());
    }

    public function testRunRecordsZeroCountWhenWriterReturnsZero(): void
    {
        // FactsExtractor with empty input yields empty arrays → the
        // writer returns 0. The orchestrator still records the section
        // (with count 0) so the success page can show "0 rows written
        // for medications" instead of hiding the section entirely.
        $this->service->expects(self::once())->method('writeMedications')
            ->willReturn(0);

        $summary = $this->orchestrator->run(
            42,
            ['medications'],
            [],
            DocumentClassifier::TYPE_INTAKE_FORM,
        );

        self::assertSame(['medications' => 0], $summary->counts());
        self::assertFalse($summary->isEmpty());
    }

    public function testRunDispatchesLabObservationsWithPanelMetadataFromHl7Facts(): void
    {
        // HL7 ORU facts route through FactsExtractor::labObservations,
        // which lifts panel name / LOINC / specimen-collected-at out
        // of the top-level fields and into the writer call.
        $facts = [
            'order_panel' => self::leafField('Lipid panel with direct LDL'),
            'order_loinc' => self::leafField('57698-3'),
            'specimen_collected_at' => self::leafField('2026-04-12'),
            'observations' => [
                [
                    'code' => self::leafField('2093-3'),
                    'display' => self::leafField('Cholesterol [Mass/volume]'),
                    'value' => self::leafField(218.0),
                    'unit' => self::leafField('mg/dL'),
                    'flag' => self::leafField('H'),
                    'effective_date' => self::leafField('2026-04-12'),
                ],
            ],
        ];

        $this->service->expects(self::once())->method('writeLabObservations')
            ->with(
                42,
                'Lipid panel with direct LDL',
                '57698-3',
                '2026-04-12',
                self::callback(static function (array $observations): bool {
                    if (count($observations) !== 1) {
                        return false;
                    }
                    $first = $observations[0] ?? null;
                    if (!is_array($first)) {
                        return false;
                    }
                    return ($first['code'] ?? null) === '2093-3';
                }),
            )
            ->willReturn(1);

        $summary = $this->orchestrator->run(
            42,
            ['lab_observations'],
            $facts,
            DocumentClassifier::TYPE_HL7_ORU,
        );

        self::assertSame(['lab_observations' => 1], $summary->counts());
    }

    public function testRunPreservesSummaryInsertionOrderAcrossSections(): void
    {
        // The success-page redirect URL builder iterates summary->counts()
        // and emits count_<section>=N query params in iteration order.
        // PHP preserves insertion order on associative arrays, and the
        // orchestrator dispatches in a fixed order
        // (allergies → medications → active_problems → care_gaps →
        // lab_observations) regardless of input order. Lock that.
        $this->service->method('writeAllergies')->willReturn(1);
        $this->service->method('writeMedications')->willReturn(1);
        $this->service->method('writeActiveProblems')->willReturn(1);

        // Pass sections in reversed order — the orchestrator should
        // still record them in canonical order.
        $summary = $this->orchestrator->run(
            42,
            ['active_problems', 'medications', 'allergies'],
            [],
            DocumentClassifier::TYPE_INTAKE_FORM,
        );

        self::assertSame(
            ['allergies', 'medications', 'active_problems'],
            array_keys($summary->counts()),
        );
    }

    /**
     * Build a minimal ExtractedField shape (value + null citation +
     * null abstain) — same helper as FactsExtractorTest::ef().
     *
     * @return array<string, mixed>
     */
    private static function leafField(mixed $value): array
    {
        return [
            'value' => $value,
            'citation' => null,
            'abstain_reason' => null,
        ];
    }
}
