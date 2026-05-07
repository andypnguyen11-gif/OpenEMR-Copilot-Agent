<?php

/**
 * Augments OpenEMR's documents-upload MIME whitelist with the
 * additional formats the multimodal Co-Pilot accepts.
 *
 * The stock ``files_white_list`` (in ``list_options``) only allows
 * ``application/pdf``, ``image/gif``, ``image/jpeg``, ``image/png``,
 * ``application/zip``, ``application/dicom``, ``application/dicom+zip``,
 * and ``text/plain``. With ``secure_upload=1`` enabled (the dev and
 * prod default), an upload of a TIFF / DOCX / XLSX / HL7 file fails
 * silently — ``addNewDocument`` returns ``false`` because
 * ``upload_action_process`` quietly skips the file when ``isWhiteFile``
 * rejects it.
 *
 * Rather than editing the database list (which would require a
 * migration / manual seed step on every install), this subscriber
 * appends the multimodal formats at runtime via the
 * ``IsAcceptedFileFilterEvent::EVENT_GET_ACCEPTED_LIST`` hook
 * documented at ``library/sanitize.inc.php``. The list mutation
 * fires once per request (the whitelist is cached after the first
 * ``isWhiteFile`` call) so the runtime cost is negligible.
 *
 * Why not the per-file ``EVENT_FILTER_IS_ACCEPTED_FILE`` hook?
 * Because we want every upload of these MIME types to pass — there's
 * no per-file decision to make. The list-augmentation hook is the
 * documented home for that pattern.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\EventSubscriber;

use OpenEMR\Events\Core\Sanitize\IsAcceptedFileFilterEvent;
use Symfony\Component\EventDispatcher\EventSubscriberInterface;

final class UploadMimeWhitelistSubscriber implements EventSubscriberInterface
{
    /**
     * MIME types the Co-Pilot's universal upload page (``upload_document.php``)
     * needs to land successfully through OpenEMR's documents subsystem.
     *
     *   - ``image/tiff`` — multi-page fax packets (cohort-5 fax files,
     *     fax_tiff extractor)
     *   - ``application/vnd.openxmlformats-officedocument.wordprocessingml.document``
     *     — referral letters (.docx, referral_docx extractor)
     *   - ``application/vnd.openxmlformats-officedocument.spreadsheetml.sheet``
     *     — patient workbooks (.xlsx, workbook_xlsx extractor)
     *   - ``application/octet-stream`` — fallback for HL7 .hl7 files
     *     uploaded from systems that don't set a recognised MIME on the
     *     wire; ``mime_content_type`` would otherwise return this for
     *     pipe-delimited binary-ish text files. Note ``text/plain`` is
     *     already in the stock whitelist for ASCII HL7s.
     *
     * Lab PDFs and intake-form scans are unaffected — ``application/pdf``
     * + ``image/png``/``jpeg`` are already in the stock list.
     */
    public const ADDITIONAL_MIME_TYPES = [
        'image/tiff',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/octet-stream',
    ];

    /**
     * @return array<string, string>
     */
    public static function getSubscribedEvents(): array
    {
        return [
            IsAcceptedFileFilterEvent::EVENT_GET_ACCEPTED_LIST => 'onGetAcceptedList',
        ];
    }

    public function onGetAcceptedList(IsAcceptedFileFilterEvent $event): void
    {
        // ``getAcceptedList()`` is untyped on the event class, so narrow
        // each entry to string before merging — the whitelist is a list
        // of MIME-type strings by contract, but the static type can't
        // express that.
        $current = array_filter(
            $event->getAcceptedList(),
            is_string(...),
        );
        /** @var list<string> $currentStrings */
        $currentStrings = array_values($current);
        $merged = array_values(array_unique(array_merge($currentStrings, self::ADDITIONAL_MIME_TYPES)));
        $event->setAcceptedList($merged);
    }
}
