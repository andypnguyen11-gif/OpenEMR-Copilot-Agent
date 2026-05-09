<?php

/**
 * Isolated tests for ExtractedFieldHelper.
 *
 * Pin the wire-shape projections the helper exposes to the review pages.
 * Two surfaces matter here:
 *
 *   * Backward compatibility — `value()` / `abstainReason()` /
 *     `citationText()` keep working on legacy responses captured before the
 *     discriminated-union schema (no `source_type` / `field_or_chunk_id`).
 *   * Forward shape — `sourceType()` / `fieldOrChunkId()` / `citation()`
 *     surface the new fields when present, returning empty / null on
 *     legacy responses.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot;

use OpenEMR\Services\Copilot\ExtractedFieldHelper;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\TestCase;

final class ExtractedFieldHelperTest extends TestCase
{
    /**
     * @param array<string, mixed>|null $citation
     *
     * @return array{value: mixed, citation: array<string, mixed>|null, abstain_reason: string|null}
     */
    private static function makeField(
        mixed $value,
        ?array $citation,
        ?string $abstainReason = null,
    ): array {
        return [
            'value' => $value,
            'citation' => $citation,
            'abstain_reason' => $abstainReason,
        ];
    }

    /**
     * @return array<string, mixed>
     */
    private static function legacyCitation(int $page = 1, string $rawText = '142 mg/dL'): array
    {
        // Pre-discriminated-union shape — what cached eval predictions and
        // pre-PR-1a runtime fixtures looked like.
        return [
            'document_id' => 'doc-1',
            'page' => $page,
            'bbox' => [0.1, 0.2, 0.3, 0.25],
            'confidence' => 0.92,
            'raw_text' => $rawText,
        ];
    }

    /**
     * @return array<string, mixed>
     */
    private static function extractedDocumentCitation(): array
    {
        return [
            'source_type' => 'extracted_document',
            'document_id' => 'doc-1',
            'page' => 2,
            'bbox' => [0.10, 0.20, 0.40, 0.27],
            'confidence' => 0.88,
            'raw_text' => 'Glucose 142 mg/dL',
            'field_or_chunk_id' => 'observations[0].value',
        ];
    }

    public function testValueReturnsTrimmedScalarString(): void
    {
        $field = self::makeField(value: 142, citation: self::legacyCitation());
        self::assertSame('142', ExtractedFieldHelper::value($field));
    }

    public function testValueReturnsEmptyStringWhenAbstain(): void
    {
        $field = self::makeField(value: null, citation: null, abstainReason: 'NO_DATA');
        self::assertSame('', ExtractedFieldHelper::value($field));
    }

    public function testAbstainReasonSurfacesAbstentionString(): void
    {
        $field = self::makeField(value: null, citation: null, abstainReason: 'LOW_CONFIDENCE');
        self::assertSame('LOW_CONFIDENCE', ExtractedFieldHelper::abstainReason($field));
    }

    public function testCitationTextRendersPageAndRawText(): void
    {
        $field = self::makeField(value: '142', citation: self::legacyCitation(page: 3, rawText: 'Glucose 142'));
        self::assertSame('p.3: Glucose 142', ExtractedFieldHelper::citationText($field));
    }

    /**
     * Forward-shape coverage: the new helpers return the discriminated-
     * union fields when present.
     */
    public function testSourceTypeReturnsDiscriminatorWhenPresent(): void
    {
        $field = self::makeField(value: 'Glucose', citation: self::extractedDocumentCitation());
        self::assertSame('extracted_document', ExtractedFieldHelper::sourceType($field));
    }

    public function testFieldOrChunkIdReturnsCanonicalIdWhenPresent(): void
    {
        $field = self::makeField(value: 'Glucose', citation: self::extractedDocumentCitation());
        self::assertSame('observations[0].value', ExtractedFieldHelper::fieldOrChunkId($field));
    }

    public function testCitationReturnsFullArrayWhenPresent(): void
    {
        $citation = self::extractedDocumentCitation();
        $field = self::makeField(value: 'Glucose', citation: $citation);
        $surfaced = ExtractedFieldHelper::citation($field);
        self::assertNotNull($surfaced);
        self::assertSame($citation, $surfaced);
    }

    /**
     * Backward-compat — legacy citations without `source_type` /
     * `field_or_chunk_id` give empty fallbacks rather than crashing.
     */
    public function testSourceTypeReturnsEmptyOnLegacyCitation(): void
    {
        $field = self::makeField(value: '142', citation: self::legacyCitation());
        self::assertSame('', ExtractedFieldHelper::sourceType($field));
    }

    public function testFieldOrChunkIdReturnsEmptyOnLegacyCitation(): void
    {
        $field = self::makeField(value: '142', citation: self::legacyCitation());
        self::assertSame('', ExtractedFieldHelper::fieldOrChunkId($field));
    }

    public function testCitationReturnsNullWhenAbsent(): void
    {
        $field = self::makeField(value: null, citation: null, abstainReason: 'NO_DATA');
        self::assertNull(ExtractedFieldHelper::citation($field));
    }

    /**
     * @dataProvider nonArrayInputProvider
     */
    #[DataProvider('nonArrayInputProvider')]
    public function testHelpersTolerateNonArrayInputs(mixed $input): void
    {
        self::assertSame('', ExtractedFieldHelper::value($input));
        self::assertSame('', ExtractedFieldHelper::abstainReason($input));
        self::assertSame('', ExtractedFieldHelper::citationText($input));
        self::assertSame('', ExtractedFieldHelper::sourceType($input));
        self::assertSame('', ExtractedFieldHelper::fieldOrChunkId($input));
        self::assertNull(ExtractedFieldHelper::citation($input));
    }

    /**
     * @return array<string, array{mixed}>
     *
     * @codeCoverageIgnore Data providers run before coverage instrumentation starts.
     */
    public static function nonArrayInputProvider(): array
    {
        return [
            'null' => [null],
            'string' => ['not-a-field'],
            'int' => [42],
            'bool' => [true],
        ];
    }
}
