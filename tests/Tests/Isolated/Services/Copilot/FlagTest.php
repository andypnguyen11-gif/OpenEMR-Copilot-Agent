<?php

/**
 * Isolated tests for the Flag value object's parse path.
 *
 * Flag::fromArray is the sole trust-boundary parser for agent-side
 * flag payloads. Every shape the agent could return — well-formed,
 * missing fields, wrong types, malformed reference lists — must
 * either produce a typed object that downstream rendering can
 * trust, or throw a clear InvalidArgumentException the dispatcher
 * catches and degrades to an empty list. Silent coercion would let
 * a bad payload show a blank rationale on a clinician-facing card.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot;

use InvalidArgumentException;
use OpenEMR\Services\Copilot\Flag;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\TestCase;

final class FlagTest extends TestCase
{
    public function testFromArrayBuildsTypedObject(): void
    {
        $flag = Flag::fromArray([
            'source_id' => 'flag:rule_id:patient:hash',
            'rule_id' => 'med_vs_note_conflict',
            'category' => 'consistency',
            'rationale' => "Active medication 'X' but recent note from 2026-04-15 mentions 'discontinued'.",
            'referenced_source_ids' => ['med:1', 'note:2'],
        ]);

        self::assertSame('flag:rule_id:patient:hash', $flag->sourceId);
        self::assertSame('med_vs_note_conflict', $flag->ruleId);
        self::assertSame('consistency', $flag->category);
        self::assertStringContainsString('discontinued', $flag->rationale);
        self::assertSame(['med:1', 'note:2'], $flag->referencedSourceIds);
    }

    public function testFromArrayAcceptsEmptyReferenceList(): void
    {
        // The engine emits a non-empty list for every concrete rule, but
        // an empty list is structurally valid (a future no-citation
        // synthetic rule). Coercing to throw here would couple the
        // parser to current rule output.
        $flag = Flag::fromArray([
            'source_id' => 'flag:x',
            'rule_id' => 'placeholder',
            'category' => 'data_quality',
            'rationale' => 'Some rationale.',
            'referenced_source_ids' => [],
        ]);

        self::assertSame([], $flag->referencedSourceIds);
    }

    /**
     * @return array<string, array{0: array<string, mixed>}>
     *
     * @codeCoverageIgnore Data providers run before coverage instrumentation starts.
     */
    public static function malformedRows(): array
    {
        return [
            'missing source_id' => [[
                'rule_id' => 'r',
                'category' => 'c',
                'rationale' => 'x',
                'referenced_source_ids' => ['s:1'],
            ]],
            'blank source_id' => [[
                'source_id' => '',
                'rule_id' => 'r',
                'category' => 'c',
                'rationale' => 'x',
                'referenced_source_ids' => ['s:1'],
            ]],
            'non-string rule_id' => [[
                'source_id' => 'f',
                'rule_id' => 42,
                'category' => 'c',
                'rationale' => 'x',
                'referenced_source_ids' => ['s:1'],
            ]],
            'missing rationale' => [[
                'source_id' => 'f',
                'rule_id' => 'r',
                'category' => 'c',
                'referenced_source_ids' => ['s:1'],
            ]],
            'referenced_source_ids not a list' => [[
                'source_id' => 'f',
                'rule_id' => 'r',
                'category' => 'c',
                'rationale' => 'x',
                'referenced_source_ids' => 'not-a-list',
            ]],
            'referenced_source_ids contains blank' => [[
                'source_id' => 'f',
                'rule_id' => 'r',
                'category' => 'c',
                'rationale' => 'x',
                'referenced_source_ids' => ['s:1', ''],
            ]],
            'referenced_source_ids contains non-string' => [[
                'source_id' => 'f',
                'rule_id' => 'r',
                'category' => 'c',
                'rationale' => 'x',
                'referenced_source_ids' => ['s:1', 99],
            ]],
        ];
    }

    /**
     * @param array<string, mixed> $row
     */
    #[DataProvider('malformedRows')]
    public function testFromArrayRejectsMalformedRow(array $row): void
    {
        $this->expectException(InvalidArgumentException::class);
        Flag::fromArray($row);
    }
}
