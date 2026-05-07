<?php

/**
 * Isolated tests for FactsExtractor — locks the per-doc-type
 * adapters that pull chart-writable rows out of the agent
 * service's extracted-facts dump.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot\ChartWrite;

use OpenEMR\Services\Copilot\ChartWrite\FactsExtractor;
use OpenEMR\Services\Copilot\DocumentClassifier;
use PHPUnit\Framework\TestCase;

final class FactsExtractorTest extends TestCase
{
    /**
     * Build a minimal ExtractedField shape — value + null citation +
     * null abstain. Reused across every fixture below so the tests
     * stay focused on the structural mapping rather than the
     * citation-validation contract (covered in FactsFormHelperTest).
     *
     * @return array<string, mixed>
     */
    private static function ef(mixed $value): array
    {
        return [
            'value' => $value,
            'citation' => null,
            'abstain_reason' => null,
        ];
    }

    // ---- Allergies ---------------------------------------------------

    public function testIntakeAllergiesAreLifted(): void
    {
        $facts = [
            'reported_allergies' => [
                ['substance' => self::ef('Penicillin'), 'reaction' => self::ef('hives'), 'severity' => self::ef('moderate')],
                ['substance' => self::ef('Sulfa'), 'reaction' => self::ef('rash'), 'severity' => self::ef('mild')],
            ],
        ];

        $rows = FactsExtractor::allergies($facts, DocumentClassifier::TYPE_INTAKE_FORM);

        self::assertCount(2, $rows);
        self::assertSame('Penicillin', $rows[0]['substance']);
        self::assertSame('hives', $rows[0]['reaction']);
        self::assertSame('moderate', $rows[0]['severity']);
    }

    public function testReferralAllergiesNkdaCollapsesToSingleEntry(): void
    {
        $facts = ['allergies' => self::ef('NKDA')];

        $rows = FactsExtractor::allergies($facts, DocumentClassifier::TYPE_REFERRAL_DOCX);

        self::assertCount(1, $rows);
        self::assertSame('NKDA', $rows[0]['substance']);
        self::assertSame('', $rows[0]['reaction']);
    }

    public function testReferralAllergiesSplitOnEmDash(): void
    {
        $facts = ['allergies' => self::ef('Sulfa drugs — rash')];

        $rows = FactsExtractor::allergies($facts, DocumentClassifier::TYPE_REFERRAL_DOCX);

        self::assertCount(1, $rows);
        self::assertSame('Sulfa drugs', $rows[0]['substance']);
        self::assertSame('rash', $rows[0]['reaction']);
    }

    public function testWorkbookAllergiesPullFromPatientBlock(): void
    {
        $facts = [
            'patient' => [
                'allergies' => self::ef('Penicillin: anaphylaxis'),
            ],
        ];

        $rows = FactsExtractor::allergies($facts, DocumentClassifier::TYPE_WORKBOOK_XLSX);

        self::assertCount(1, $rows);
        self::assertSame('Penicillin', $rows[0]['substance']);
        self::assertSame('anaphylaxis', $rows[0]['reaction']);
    }

    public function testFaxTiffHasNoAllergiesToExtract(): void
    {
        $facts = ['patient_name' => self::ef('Margaret Chen')];
        self::assertSame([], FactsExtractor::allergies($facts, DocumentClassifier::TYPE_FAX_TIFF));
    }

    // ---- Medications -------------------------------------------------

    public function testIntakeMedicationsAreLifted(): void
    {
        $facts = [
            'current_medications' => [
                [
                    'name' => self::ef('atorvastatin'),
                    'dose' => self::ef('40 mg'),
                    'frequency' => self::ef('PO daily'),
                    'rxnorm' => self::ef('83367'),
                    'indication' => self::ef('Hyperlipidemia'),
                    'started_year' => self::ef(2022),
                ],
            ],
        ];

        $rows = FactsExtractor::medications($facts, DocumentClassifier::TYPE_INTAKE_FORM);

        self::assertCount(1, $rows);
        self::assertSame('atorvastatin', $rows[0]['name']);
        self::assertSame('40 mg', $rows[0]['dose']);
        self::assertSame('83367', $rows[0]['rxnorm']);
        self::assertSame(2022, $rows[0]['started_year']);
    }

    public function testReferralMedicationsParsePreformattedStrings(): void
    {
        $facts = [
            'current_medications' => [
                self::ef('atorvastatin 40 mg PO daily'),
                self::ef('lisinopril 10 mg PO daily  (HOLD per ACE-i angioedema; reconciliation pending)'),
            ],
        ];

        $rows = FactsExtractor::medications($facts, DocumentClassifier::TYPE_REFERRAL_DOCX);

        self::assertCount(2, $rows);
        self::assertSame('atorvastatin', $rows[0]['name']);
        self::assertSame('40 mg', $rows[0]['dose']);
        self::assertSame('PO daily', $rows[0]['frequency']);

        self::assertSame('lisinopril', $rows[1]['name']);
        $indication = $rows[1]['indication'] ?? '';
        self::assertIsString($indication);
        self::assertStringContainsString('ACE-i angioedema', $indication);
    }

    public function testWorkbookMedicationsLiftFromMedicationsSheet(): void
    {
        $facts = [
            'medications' => [
                [
                    'brand' => self::ef('Lipitor'),
                    'generic' => self::ef('atorvastatin'),
                    'strength' => self::ef('40 mg'),
                    'sig' => self::ef('1 tab PO daily'),
                    'indication' => self::ef('Hyperlipidemia'),
                ],
            ],
        ];

        $rows = FactsExtractor::medications($facts, DocumentClassifier::TYPE_WORKBOOK_XLSX);

        self::assertCount(1, $rows);
        self::assertSame('atorvastatin', $rows[0]['name']);
        self::assertSame('40 mg', $rows[0]['dose']);
        self::assertSame('1 tab PO daily', $rows[0]['frequency']);
    }

    public function testWorkbookMedicationsFallBackToBrandWhenGenericMissing(): void
    {
        $facts = [
            'medications' => [['brand' => self::ef('OnlyBrand')]],
        ];

        $rows = FactsExtractor::medications($facts, DocumentClassifier::TYPE_WORKBOOK_XLSX);

        self::assertCount(1, $rows);
        self::assertSame('OnlyBrand', $rows[0]['name']);
    }

    // ---- Active problems --------------------------------------------

    public function testReferralPmhExtractsIcd10FromParenthesizedCode(): void
    {
        $facts = [
            'past_medical_history' => [
                self::ef('Hyperlipidemia (E78.5)'),
                self::ef('Essential hypertension (I10)'),
                self::ef('Some uncoded condition'),
            ],
        ];

        $rows = FactsExtractor::activeProblems($facts, DocumentClassifier::TYPE_REFERRAL_DOCX);

        self::assertCount(3, $rows);
        self::assertSame('Hyperlipidemia', $rows[0]['condition']);
        self::assertSame('E78.5', $rows[0]['icd10']);
        self::assertSame('Essential hypertension', $rows[1]['condition']);
        self::assertSame('I10', $rows[1]['icd10']);
        // No ICD pattern → empty icd10.
        self::assertSame('Some uncoded condition', $rows[2]['condition']);
        self::assertSame('', $rows[2]['icd10']);
    }

    // ---- Care gaps ---------------------------------------------------

    public function testWorkbookCareGapsLiftStatusAndDueDate(): void
    {
        $facts = [
            'care_gaps' => [
                [
                    'measure' => self::ef('Mammography (50-74)'),
                    'status' => self::ef('OVERDUE'),
                    'due_date' => self::ef('2025-11-04'),
                    'notes' => self::ef('Schedule screening mammogram'),
                ],
            ],
        ];

        $rows = FactsExtractor::careGaps($facts, DocumentClassifier::TYPE_WORKBOOK_XLSX);

        self::assertCount(1, $rows);
        self::assertSame('Mammography (50-74)', $rows[0]['measure']);
        self::assertSame('OVERDUE', $rows[0]['status']);
        self::assertSame('2025-11-04', $rows[0]['due_date']);
        self::assertSame('Schedule screening mammogram', $rows[0]['notes']);
    }

    public function testCareGapsForNonWorkbookTypeIsEmpty(): void
    {
        $facts = ['care_gaps' => [['measure' => self::ef('test')]]];
        self::assertSame([], FactsExtractor::careGaps($facts, DocumentClassifier::TYPE_REFERRAL_DOCX));
    }

    // ---- Lab observations -------------------------------------------

    public function testHl7OruLabPayloadCarriesPanelMetadata(): void
    {
        $facts = [
            'order_panel' => self::ef('Lipid panel with direct LDL'),
            'order_loinc' => self::ef('57698-3'),
            'specimen_collected_at' => self::ef('2026-04-12'),
            'observations' => [
                [
                    'code' => self::ef('2093-3'),
                    'display' => self::ef('Cholesterol [Mass/volume]'),
                    'value' => self::ef(218.0),
                    'unit' => self::ef('mg/dL'),
                    'flag' => self::ef('H'),
                    'effective_date' => self::ef('2026-04-12'),
                ],
            ],
        ];

        $payload = FactsExtractor::labObservations($facts, DocumentClassifier::TYPE_HL7_ORU);

        self::assertSame('Lipid panel with direct LDL', $payload['panel_name']);
        self::assertSame('57698-3', $payload['panel_loinc']);
        self::assertSame('2026-04-12', $payload['report_date']);
        self::assertCount(1, $payload['observations']);
        self::assertSame(218.0, $payload['observations'][0]['value']);
        self::assertSame('H', $payload['observations'][0]['flag']);
    }

    public function testWorkbookLabReadingsBecomeFlatObservationList(): void
    {
        $facts = [
            'lab_readings' => [
                [
                    'test' => self::ef('Total cholesterol'),
                    'loinc' => self::ef('2093-3'),
                    'unit' => self::ef('mg/dL'),
                    'value' => self::ef(218),
                    'reading_date' => self::ef('2026-04-12'),
                ],
                [
                    'test' => self::ef('LDL cholesterol'),
                    'loinc' => self::ef('13457-7'),
                    'unit' => self::ef('mg/dL'),
                    'value' => self::ef(142),
                    'reading_date' => self::ef('2026-04-12'),
                ],
            ],
        ];

        $payload = FactsExtractor::labObservations($facts, DocumentClassifier::TYPE_WORKBOOK_XLSX);

        self::assertSame('Workbook lab import', $payload['panel_name']);
        self::assertSame('2026-04-12', $payload['report_date']);
        self::assertCount(2, $payload['observations']);
        self::assertSame(218.0, $payload['observations'][0]['value']);
    }

    // ---- sectionPopulated -------------------------------------------

    public function testSectionPopulatedTrueForFilledLists(): void
    {
        $facts = ['reported_allergies' => [['substance' => self::ef('x')]]];
        self::assertTrue(FactsExtractor::sectionPopulated(
            $facts,
            DocumentClassifier::TYPE_INTAKE_FORM,
            'allergies',
        ));
    }

    public function testSectionPopulatedFalseForEmptyLists(): void
    {
        $facts = ['reported_allergies' => []];
        self::assertFalse(FactsExtractor::sectionPopulated(
            $facts,
            DocumentClassifier::TYPE_INTAKE_FORM,
            'allergies',
        ));
    }

    public function testSectionPopulatedFalseWhenDataKeyMissing(): void
    {
        // sectionPopulated just answers "is there data" — the caller
        // decides whether the section applies to the doc type at all
        // (see ``$candidateSections`` in document_review.php). So a
        // workbook with no care_gaps key returns false here.
        $facts = ['patient' => ['name' => self::ef('x')]];
        self::assertFalse(FactsExtractor::sectionPopulated(
            $facts,
            DocumentClassifier::TYPE_WORKBOOK_XLSX,
            'care_gaps',
        ));
    }
}
