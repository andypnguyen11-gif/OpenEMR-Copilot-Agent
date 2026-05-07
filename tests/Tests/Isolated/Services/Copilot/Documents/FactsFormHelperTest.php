<?php

/**
 * Isolated tests for FactsFormHelper.
 *
 * The helper has two halves the editable-confirm flow depends on:
 * shape detection (``isExtractedField``) and edit-overlay
 * (``overlayEdits``). Locked here so a regression in either silently
 * landing wrong-shape data on the agent service's PUT route gets
 * caught at unit time instead of as a 422 on the demo.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot\Documents;

use OpenEMR\Services\Copilot\Documents\FactsFormHelper;
use PHPUnit\Framework\TestCase;

final class FactsFormHelperTest extends TestCase
{
    public function testIsExtractedFieldDetectsTheCanonicalShape(): void
    {
        self::assertTrue(FactsFormHelper::isExtractedField([
            'value' => 'x',
            'citation' => null,
            'abstain_reason' => null,
        ]));
    }

    public function testIsExtractedFieldRejectsExtraKeys(): void
    {
        self::assertFalse(FactsFormHelper::isExtractedField([
            'value' => 'x',
            'citation' => null,
            'abstain_reason' => null,
            'extra' => 1,
        ]));
    }

    public function testIsExtractedFieldRejectsMissingKeys(): void
    {
        self::assertFalse(FactsFormHelper::isExtractedField(['value' => 'x']));
    }

    public function testOverlayEditsReplacesValueAndClearsAbstain(): void
    {
        $original = [
            'value' => null,
            'citation' => ['document_id' => 't', 'page' => 1, 'bbox' => [0, 0, 1, 1], 'confidence' => 1.0, 'raw_text' => 'r'],
            'abstain_reason' => 'NO_DATA',
        ];
        $edits = ['value' => 'Margaret Chen'];

        $merged = FactsFormHelper::overlayEdits($original, $edits);
        self::assertIsArray($merged);

        self::assertSame('Margaret Chen', $merged['value']);
        self::assertNull($merged['abstain_reason']);
        self::assertNotNull($merged['citation'], 'citation must survive');
    }

    public function testOverlayEditsPreservesOriginalWhenEditEmpty(): void
    {
        $original = [
            'value' => 'Original',
            'citation' => ['document_id' => 't', 'page' => 1, 'bbox' => [0, 0, 1, 1], 'confidence' => 1.0, 'raw_text' => 'r'],
            'abstain_reason' => null,
        ];

        // Empty string from the form means "no edit" — keep original.
        $merged = FactsFormHelper::overlayEdits($original, ['value' => '']);
        self::assertIsArray($merged);

        self::assertSame('Original', $merged['value']);
    }

    public function testOverlayEditsCoercesIntFromNumericString(): void
    {
        $original = [
            'value' => 4,
            'citation' => ['document_id' => 't', 'page' => 1, 'bbox' => [0, 0, 1, 1], 'confidence' => 1.0, 'raw_text' => 'r'],
            'abstain_reason' => null,
        ];

        $merged = FactsFormHelper::overlayEdits($original, ['value' => '7']);
        self::assertIsArray($merged);

        self::assertSame(7, $merged['value']);
    }

    public function testOverlayEditsCoercesFloatFromNumericString(): void
    {
        $original = [
            'value' => 1.5,
            'citation' => ['document_id' => 't', 'page' => 1, 'bbox' => [0, 0, 1, 1], 'confidence' => 1.0, 'raw_text' => 'r'],
            'abstain_reason' => null,
        ];

        $merged = FactsFormHelper::overlayEdits($original, ['value' => '142']);
        self::assertIsArray($merged);

        self::assertSame(142.0, $merged['value']);
    }

    public function testOverlayEditsRecursesIntoLists(): void
    {
        $original = [
            [
                'value' => 'med one',
                'citation' => ['document_id' => 't', 'page' => 1, 'bbox' => [0, 0, 1, 1], 'confidence' => 1.0, 'raw_text' => 'r'],
                'abstain_reason' => null,
            ],
            [
                'value' => 'med two',
                'citation' => ['document_id' => 't', 'page' => 2, 'bbox' => [0, 0, 1, 1], 'confidence' => 1.0, 'raw_text' => 'r'],
                'abstain_reason' => null,
            ],
        ];

        $merged = FactsFormHelper::overlayEdits($original, [
            0 => ['value' => 'med one — edited'],
            1 => ['value' => ''],  // no-edit
        ]);
        self::assertIsArray($merged);
        self::assertIsArray($merged[0]);
        self::assertIsArray($merged[1]);

        self::assertSame('med one — edited', $merged[0]['value']);
        self::assertSame('med two', $merged[1]['value']);
    }

    public function testOverlayEditsRecursesIntoNestedObjects(): void
    {
        $original = [
            'patient' => [
                'name' => [
                    'value' => 'Original Name',
                    'citation' => ['document_id' => 't', 'page' => 1, 'bbox' => [0, 0, 1, 1], 'confidence' => 1.0, 'raw_text' => 'r'],
                    'abstain_reason' => null,
                ],
                'dob' => [
                    'value' => '1968-03-12',
                    'citation' => ['document_id' => 't', 'page' => 1, 'bbox' => [0, 0, 1, 1], 'confidence' => 1.0, 'raw_text' => 'r'],
                    'abstain_reason' => null,
                ],
            ],
        ];

        $merged = FactsFormHelper::overlayEdits($original, [
            'patient' => [
                'name' => ['value' => 'Edited Name'],
            ],
        ]);
        self::assertIsArray($merged);
        self::assertIsArray($merged['patient']);
        self::assertIsArray($merged['patient']['name']);
        self::assertIsArray($merged['patient']['dob']);

        self::assertSame('Edited Name', $merged['patient']['name']['value']);
        // dob untouched.
        self::assertSame('1968-03-12', $merged['patient']['dob']['value']);
    }

    public function testCoerceValueIntegerOriginalReturnsInt(): void
    {
        self::assertSame(42, FactsFormHelper::coerceValue('42', 7));
    }

    public function testCoerceValueNonNumericFallsBackToString(): void
    {
        // int original, non-numeric edit → string passthrough so the
        // validator surfaces the "wrong type" rather than silently
        // coercing to 0.
        self::assertSame('not-a-number', FactsFormHelper::coerceValue('not-a-number', 7));
    }

    public function testCoerceValueStringOriginalIsAlwaysString(): void
    {
        self::assertSame('hello', FactsFormHelper::coerceValue('hello', 'world'));
    }

    public function testRenderFactsEmitsAnInputForALeafField(): void
    {
        $node = [
            'value' => 'Margaret Chen',
            'citation' => ['document_id' => 't', 'page' => 1, 'bbox' => [0, 0, 1, 1], 'confidence' => 1.0, 'raw_text' => 'patient name'],
            'abstain_reason' => null,
        ];

        $html = FactsFormHelper::renderFacts($node, 'facts[patient_name]', 'patient_name');

        self::assertStringContainsString('name="facts[patient_name][value]"', $html);
        self::assertStringContainsString('value="Margaret Chen"', $html);
        self::assertStringContainsString('patient name', $html, 'citation raw_text should be surfaced');
    }

    public function testRenderFactsSurfacesAbstainBadge(): void
    {
        $node = [
            'value' => null,
            'citation' => null,
            'abstain_reason' => 'NO_DATA',
        ];

        $html = FactsFormHelper::renderFacts($node, 'facts[mrn]', 'mrn');

        self::assertStringContainsString('abstain-badge', $html);
        self::assertStringContainsString('NO_DATA', $html);
    }

    public function testRenderFactsHandlesAnEmptyList(): void
    {
        $html = FactsFormHelper::renderFacts([], 'facts[meds]', 'medications');

        self::assertStringContainsString('(no entries)', $html);
        self::assertStringContainsString('medications', $html);
    }
}
