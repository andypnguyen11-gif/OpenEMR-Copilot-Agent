<?php

/**
 * File-format → ``document_type`` classifier for the universal Co-Pilot
 * upload entrypoint (Week 2 multimodal expansion, Step 1).
 *
 * The agent service's extractor registry keys off ``document_type``
 * strings: ``lab_pdf``, ``intake_form``, ``referral_docx``, ``fax_tiff``,
 * ``workbook_xlsx``, ``hl7_oru``, ``hl7_adt``. Most file formats
 * unambiguously imply the extractor (a .docx is always a referral, a
 * .xlsx is always a workbook, an HL7 ORU stream is always lab results).
 * PDFs and scanned images are ambiguous — they could be a lab report or
 * an intake form — so the classifier returns ``lab_pdf`` as a sensible
 * default and lets the upload page accept a caller-supplied hint.
 *
 * The classifier reads the file extension, the MIME type, and at most
 * the first 1KB of the file contents. It does NOT call out to the
 * agent service or do anything network-bound — it must stay cheap so
 * it can run inside the upload-handler's hot path.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot;

final class DocumentClassifier
{
    public const TYPE_LAB_PDF = 'lab_pdf';
    public const TYPE_INTAKE_FORM = 'intake_form';
    public const TYPE_REFERRAL_DOCX = 'referral_docx';
    public const TYPE_FAX_TIFF = 'fax_tiff';
    public const TYPE_WORKBOOK_XLSX = 'workbook_xlsx';
    public const TYPE_HL7_ORU = 'hl7_oru';
    public const TYPE_HL7_ADT = 'hl7_adt';

    public const HINT_LAB = 'lab';
    public const HINT_INTAKE = 'intake';
    public const HINT_AUTO = 'auto';

    /**
     * Classify a file by extension + MIME + first bytes.
     *
     * @param string $fileName  Original filename (extension is read from here).
     * @param string $mimeType  Browser-reported MIME type (advisory only).
     * @param string $headBytes At least the first ~16 bytes of the file.
     *                          Pass an empty string if not yet read.
     * @param string $hint      One of HINT_* — caller's intent for ambiguous
     *                          formats (PDF, PNG, JPG). Defaults to auto.
     *
     * @return string One of the TYPE_* constants. Throws on unrecognized
     *                input rather than silently picking a wrong type.
     *
     * @throws ClassifierException When no rule matches the input.
     */
    public static function classify(
        string $fileName,
        string $mimeType,
        string $headBytes,
        string $hint = self::HINT_AUTO,
    ): string {
        $ext = strtolower(pathinfo($fileName, PATHINFO_EXTENSION));
        $mime = strtolower(trim($mimeType));

        // HL7 v2: ASCII text starting with "MSH|". Distinguish ORU vs
        // ADT by the message-type field (MSH-9). The .hl7 extension is
        // common but not required — we sniff first-bytes too.
        if ($ext === 'hl7' || str_starts_with($headBytes, 'MSH|')) {
            return self::classifyHl7($headBytes);
        }

        // DOCX / XLSX both look like zip archives at the byte level
        // (magic "PK\x03\x04"). The OOXML inner directory disambiguates,
        // but we trust the extension here — the universal upload's accept=
        // already gates on extension and the user's file picker won't
        // hand us a renamed .zip in normal use.
        if ($ext === 'docx' || $mime === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document') {
            return self::TYPE_REFERRAL_DOCX;
        }
        if ($ext === 'xlsx' || $mime === 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet') {
            return self::TYPE_WORKBOOK_XLSX;
        }

        // TIFF (single or multi-page). Magic is "II*\x00" (little-
        // endian) or "MM\x00*" (big-endian); accept either, plus the
        // .tif/.tiff extension as a fallback when headBytes is empty.
        if ($ext === 'tif' || $ext === 'tiff'
            || str_starts_with($headBytes, "II*\0")
            || str_starts_with($headBytes, "MM\0*")
        ) {
            return self::TYPE_FAX_TIFF;
        }

        // PDF / PNG / JPG are ambiguous — they could be a lab scan or
        // an intake form. The hint resolves the ambiguity.
        $isPdf = $ext === 'pdf' || str_starts_with($headBytes, '%PDF-');
        $isImage = in_array($ext, ['png', 'jpg', 'jpeg'], true)
            || str_starts_with($headBytes, "\x89PNG")
            || str_starts_with($headBytes, "\xFF\xD8\xFF");

        if ($isPdf || $isImage) {
            if ($hint === self::HINT_INTAKE) {
                return self::TYPE_INTAKE_FORM;
            }
            // HINT_LAB or HINT_AUTO: default to lab_pdf. The lab review
            // page is the more common destination; the clinician can
            // re-route on the review page if it turns out to be intake.
            return self::TYPE_LAB_PDF;
        }

        throw new ClassifierException(sprintf(
            'unrecognized document format: ext=%s mime=%s head=%s',
            $ext,
            $mime,
            substr(bin2hex(substr($headBytes, 0, 8)), 0, 16),
        ));
    }

    /**
     * Distinguish ORU from ADT by reading MSH-9 (message-type field).
     *
     * MSH-9 lives at index 8 when splitting the first segment by "|"
     * (the encoding-characters field at index 1 doesn't disrupt this
     * because we split on "|" not on the encoding chars). Format:
     * "ADT^A08" / "ORU^R01" — we match on the first component.
     */
    private static function classifyHl7(string $headBytes): string
    {
        $firstLine = strtok($headBytes, "\r\n") ?: '';
        $fields = explode('|', $firstLine);
        $msh9 = $fields[8] ?? '';
        $messageType = strtoupper(strtok($msh9, '^') ?: '');

        if ($messageType === 'ORU') {
            return self::TYPE_HL7_ORU;
        }
        if ($messageType === 'ADT') {
            return self::TYPE_HL7_ADT;
        }

        throw new ClassifierException(sprintf(
            'HL7 message type %s is not supported (only ADT and ORU)',
            $messageType !== '' ? $messageType : '<missing>',
        ));
    }
}
