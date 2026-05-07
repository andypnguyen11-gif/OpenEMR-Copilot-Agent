<?php

/**
 * Isolated tests for UploadMimeWhitelistSubscriber.
 *
 * Lock the contract: the subscriber appends the four extra MIME
 * types the multimodal upload page needs to the
 * ``IsAcceptedFileFilterEvent`` accepted list, while preserving any
 * types that were already on it (the stock ``files_white_list``).
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Modules\Copilot\EventSubscriber;

use OpenEMR\Events\Core\Sanitize\IsAcceptedFileFilterEvent;
use OpenEMR\Modules\Copilot\EventSubscriber\UploadMimeWhitelistSubscriber;
use PHPUnit\Framework\TestCase;

// The custom-module namespace ``OpenEMR\Modules\Copilot\\`` is not in
// the composer autoload map (it's registered at runtime by
// ``openemr.bootstrap.php``), so the isolated test has to require the
// file directly — same pattern as ``SidePanelSubscriberTest``.
require_once __DIR__ . '/../../../../../../interface/modules/custom_modules/oe-module-copilot/src/EventSubscriber/UploadMimeWhitelistSubscriber.php';

final class UploadMimeWhitelistSubscriberTest extends TestCase
{
    public function testAppendsMultimodalMimeTypesToTheAcceptedList(): void
    {
        $event = new IsAcceptedFileFilterEvent(
            file: '/tmp/whatever',
            acceptedList: ['application/pdf', 'image/png', 'text/plain'],
        );

        (new UploadMimeWhitelistSubscriber())->onGetAcceptedList($event);

        $merged = $event->getAcceptedList();
        // Existing entries preserved.
        self::assertContains('application/pdf', $merged);
        self::assertContains('image/png', $merged);
        self::assertContains('text/plain', $merged);
        // New entries appended.
        self::assertContains('image/tiff', $merged);
        self::assertContains(
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            $merged,
        );
        self::assertContains(
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            $merged,
        );
        self::assertContains('application/octet-stream', $merged);
    }

    public function testDoesNotDuplicateAnEntryAlreadyOnTheList(): void
    {
        // image/tiff is in the stock list of some installs; if the
        // subscriber blindly appended we'd get two rows. Verify the
        // unique-merge keeps the list clean.
        $event = new IsAcceptedFileFilterEvent(
            file: '/tmp/whatever',
            acceptedList: ['application/pdf', 'image/tiff'],
        );

        (new UploadMimeWhitelistSubscriber())->onGetAcceptedList($event);

        $merged = $event->getAcceptedList();
        $tiffCount = count(array_filter($merged, static fn ($m) => $m === 'image/tiff'));
        self::assertSame(1, $tiffCount, 'image/tiff should appear exactly once after merge');
    }

    public function testSubscribesToTheGetAcceptedListEvent(): void
    {
        // Sanity check on the event-name binding so a typo doesn't
        // silently disconnect the subscriber from the dispatcher.
        $events = UploadMimeWhitelistSubscriber::getSubscribedEvents();
        self::assertArrayHasKey(
            IsAcceptedFileFilterEvent::EVENT_GET_ACCEPTED_LIST,
            $events,
        );
        self::assertSame('onGetAcceptedList', $events[IsAcceptedFileFilterEvent::EVENT_GET_ACCEPTED_LIST]);
    }
}
