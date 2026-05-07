<?php

/**
 * Isolated tests for DocumentClassifier.
 *
 * Locks the file-format → document_type mapping the universal upload
 * page (``upload_document.php``) depends on. Adding a new doc-type
 * branch here is a Week-2-step touchpoint: the corresponding extractor
 * MR also lands a row in the parametrized provider.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot;

use OpenEMR\Services\Copilot\ClassifierException;
use OpenEMR\Services\Copilot\DocumentClassifier;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\TestCase;

final class DocumentClassifierTest extends TestCase
{
    /**
     * @return array<string, array{string, string, string, string, string}>
     *
     * @codeCoverageIgnore Data providers run before coverage instrumentation starts.
     */
    public static function classificationProvider(): array
    {
        return [
            // PDF / image — ambiguous, default to lab unless hinted intake.
            'pdf default → lab' => ['scan.pdf', 'application/pdf', '%PDF-1.4', DocumentClassifier::HINT_AUTO, DocumentClassifier::TYPE_LAB_PDF],
            'pdf hint=intake → intake' => ['intake.pdf', 'application/pdf', '%PDF-1.4', DocumentClassifier::HINT_INTAKE, DocumentClassifier::TYPE_INTAKE_FORM],
            'png default → lab' => ['lab.png', 'image/png', "\x89PNG\r\n", DocumentClassifier::HINT_AUTO, DocumentClassifier::TYPE_LAB_PDF],
            'jpg hint=intake → intake' => ['form.jpg', 'image/jpeg', "\xFF\xD8\xFFsomething", DocumentClassifier::HINT_INTAKE, DocumentClassifier::TYPE_INTAKE_FORM],

            // Office formats — extension-driven; MIME is advisory.
            'docx → referral' => ['p01-chen-referral.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', "PK\x03\x04", DocumentClassifier::HINT_AUTO, DocumentClassifier::TYPE_REFERRAL_DOCX],
            'docx with bad mime' => ['p01-chen-referral.docx', 'application/octet-stream', "PK\x03\x04", DocumentClassifier::HINT_AUTO, DocumentClassifier::TYPE_REFERRAL_DOCX],
            'xlsx → workbook' => ['p01-chen-workbook.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', "PK\x03\x04", DocumentClassifier::HINT_AUTO, DocumentClassifier::TYPE_WORKBOOK_XLSX],

            // TIFF — both endiannesses.
            'tiff little-endian magic → fax' => ['p01-chen-fax.tiff', 'image/tiff', "II*\0", DocumentClassifier::HINT_AUTO, DocumentClassifier::TYPE_FAX_TIFF],
            'tiff big-endian magic → fax' => ['p01-chen-fax.tiff', 'image/tiff', "MM\0*", DocumentClassifier::HINT_AUTO, DocumentClassifier::TYPE_FAX_TIFF],
            'tif extension only → fax' => ['scan.tif', 'application/octet-stream', '', DocumentClassifier::HINT_AUTO, DocumentClassifier::TYPE_FAX_TIFF],

            // HL7 — distinguished by MSH-9 message type.
            'hl7 ORU → labs' => ['p01-chen-oru-r01.hl7', 'text/plain', "MSH|^~\\&|LIS|HOSP|EMR|HOSP|20260501||ORU^R01|123|P|2.5\rPID|...", DocumentClassifier::HINT_AUTO, DocumentClassifier::TYPE_HL7_ORU],
            'hl7 ADT → demographics' => ['p01-chen-adt-a08.hl7', 'text/plain', "MSH|^~\\&|REG|HOSP|EMR|HOSP|20260501||ADT^A08|456|P|2.5\rPID|...", DocumentClassifier::HINT_AUTO, DocumentClassifier::TYPE_HL7_ADT],
            'hl7 sniffed without extension' => ['unknown', 'text/plain', "MSH|^~\\&|LIS|HOSP|EMR|HOSP|20260501||ORU^R01|789|P|2.5\r", DocumentClassifier::HINT_AUTO, DocumentClassifier::TYPE_HL7_ORU],
        ];
    }

    #[DataProvider('classificationProvider')]
    public function testClassify(
        string $fileName,
        string $mimeType,
        string $headBytes,
        string $hint,
        string $expected,
    ): void {
        self::assertSame(
            $expected,
            DocumentClassifier::classify($fileName, $mimeType, $headBytes, $hint),
        );
    }

    public function testUnknownExtensionThrows(): void
    {
        $this->expectException(ClassifierException::class);
        $this->expectExceptionMessageMatches('/unrecognized document format/');

        DocumentClassifier::classify('mystery.bin', 'application/octet-stream', "\x00\x01\x02\x03");
    }

    public function testHl7WithUnsupportedMessageTypeThrows(): void
    {
        // ORM (orders) is HL7 but not a type the agent service handles.
        $this->expectException(ClassifierException::class);
        $this->expectExceptionMessageMatches('/HL7 message type ORM/');

        DocumentClassifier::classify(
            'orders.hl7',
            'text/plain',
            "MSH|^~\\&|LIS|HOSP|EMR|HOSP|20260501||ORM^O01|111|P|2.5\r",
        );
    }
}
