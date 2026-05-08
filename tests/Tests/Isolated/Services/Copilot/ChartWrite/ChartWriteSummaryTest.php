<?php

/**
 * Isolated tests for ChartWriteSummary — the per-section row-count
 * accumulator returned by ChartWriteService writes. Pure value object,
 * no DB; covers the contract the save_success.php redirect builder
 * depends on (counts() keys must match the section names the URL
 * builder expects).
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot\ChartWrite;

use OpenEMR\Services\Copilot\ChartWrite\ChartWriteSummary;
use PHPUnit\Framework\TestCase;

final class ChartWriteSummaryTest extends TestCase
{
    public function testIsEmptyTrueForNoRecords(): void
    {
        $summary = new ChartWriteSummary();

        self::assertTrue($summary->isEmpty());
        self::assertSame([], $summary->counts());
        self::assertSame(0, $summary->totalRowsWritten());
    }

    public function testIsEmptyFalseAfterRecord(): void
    {
        $summary = new ChartWriteSummary();
        $summary->record('allergies', 1);

        self::assertFalse($summary->isEmpty());
    }

    public function testRecordAccumulatesPerSection(): void
    {
        $summary = new ChartWriteSummary();
        $summary->record('allergies', 2);
        $summary->record('allergies', 1);
        $summary->record('medications', 3);

        self::assertSame(['allergies' => 3, 'medications' => 3], $summary->counts());
    }

    public function testTotalRowsWrittenSumsAllSections(): void
    {
        $summary = new ChartWriteSummary();
        $summary->record('allergies', 2);
        $summary->record('medications', 3);
        $summary->record('care_gaps', 1);

        self::assertSame(6, $summary->totalRowsWritten());
    }

    public function testRecordWithZeroRowsKeepsSectionAtZero(): void
    {
        // A FactsExtractor block that yielded an empty list still
        // calls record() with 0 — the success page renders "0 rows"
        // for that section rather than hiding it. Make sure the
        // section appears in counts() with value 0.
        $summary = new ChartWriteSummary();
        $summary->record('lab_observations', 0);

        self::assertFalse($summary->isEmpty());
        self::assertSame(['lab_observations' => 0], $summary->counts());
        self::assertSame(0, $summary->totalRowsWritten());
    }

    public function testSkipAppendsToSkippedList(): void
    {
        $summary = new ChartWriteSummary();
        $summary->skip('lab_observations', 'no observations parsed');
        $summary->skip('care_gaps', 'section not applicable to doc type');

        self::assertSame(
            ['lab_observations (no observations parsed)', 'care_gaps (section not applicable to doc type)'],
            $summary->skipped(),
        );
    }

    public function testSkipsAndCountsAreIndependent(): void
    {
        // The save handler can record a write for one section and
        // skip another in the same submission; isEmpty() reflects the
        // count side only (a "wrote nothing but skipped some" outcome
        // is empty for the success-page summary purposes).
        $summary = new ChartWriteSummary();
        $summary->skip('lab_observations', 'no observations parsed');

        self::assertTrue($summary->isEmpty());
        self::assertSame([], $summary->counts());
        self::assertSame(['lab_observations (no observations parsed)'], $summary->skipped());
    }
}
