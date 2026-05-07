<?php

/**
 * Per-section write counts returned from {@see ChartWriteService} so
 * the save handler can show the clinician "wrote 4 medications, 3
 * allergies, ..." after the chart update lands.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\ChartWrite;

final class ChartWriteSummary
{
    /** @var array<string, int> */
    private array $counts = [];

    /** @var list<string> */
    private array $skipped = [];

    public function record(string $section, int $rowsWritten): void
    {
        $this->counts[$section] = ($this->counts[$section] ?? 0) + $rowsWritten;
    }

    public function skip(string $section, string $reason): void
    {
        $this->skipped[] = sprintf('%s (%s)', $section, $reason);
    }

    /** @return array<string, int> */
    public function counts(): array
    {
        return $this->counts;
    }

    /** @return list<string> */
    public function skipped(): array
    {
        return $this->skipped;
    }

    public function totalRowsWritten(): int
    {
        return array_sum($this->counts);
    }

    public function isEmpty(): bool
    {
        return $this->counts === [];
    }
}
