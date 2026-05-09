<?php

/**
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\ChartWrite;

/**
 * Four discrete outcomes of an attempt to save a Co-Pilot document
 * via {@see ChartWriteCoordinator::attemptSave()}. The endpoint maps
 * each to an HTTP response shape — see ``save_document.php``.
 */
enum SaveOutcomeKind
{
    /**
     * First-time save acquired the lock, ran chart-write, and finalized
     * the marker. The endpoint redirects to ``save_success.php``.
     */
    case AcquiredAndWrote;

    /**
     * The document already has ``chart_written_at`` set from a prior
     * successful save. The endpoint replays the stored summary back to
     * the clinician (same redirect target, ``idempotent=1`` flag).
     */
    case IdempotentReplay;

    /**
     * Another writer holds the lock (``chart_write_started_at`` set
     * within the TTL window, ``chart_written_at`` still NULL). The
     * endpoint surfaces an HTTP 409 so the form can prompt a refresh.
     */
    case ConcurrentInFlight;

    /**
     * The document row does not exist (or the bare doc id is not
     * numeric). The endpoint surfaces an HTTP 404.
     */
    case DocumentNotFound;
}
