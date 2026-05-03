<?php

/**
 * Isolated tests for :class:`SidePanelSubscriber`.
 *
 * Five contract points pinned here:
 *
 *   1. Subscribes to ``RenderEvent::EVENT_HANDLE`` and only that event.
 *   2. Emits exactly once per request even if the demographics tab
 *      dispatches the event for many cards.
 *   3. Does not emit when the dispatched card isn't the configured
 *      mount card (so the subscriber's once-guard isn't burned by an
 *      unrelated card render).
 *   4. Honours the demographics-tab ACL — denied access means no mount.
 *   5. Honours the pid resolver — no active patient pid means no mount
 *      (defence in depth for non-chart pages that dispatch a
 *      ``RenderEvent`` of their own).
 *
 * @package   OpenEMR
 *
 * @link      https://www.open-emr.org
 *
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Modules\Copilot\EventSubscriber;

use OpenEMR\Events\Patient\Summary\Card\RenderEvent;
use OpenEMR\Modules\Copilot\EventSubscriber\SidePanelSubscriber;
use PHPUnit\Framework\TestCase;

require_once __DIR__ . '/../../../../../../interface/modules/custom_modules/oe-module-copilot/src/EventSubscriber/SidePanelSubscriber.php';

final class SidePanelSubscriberTest extends TestCase
{
    /**
     * Captured emissions across an arbitrary number of dispatches.
     * @var list<string>
     */
    private array $emitted = [];

    protected function setUp(): void
    {
        $this->emitted = [];
    }

    public function testSubscribesOnlyToCardRenderEvent(): void
    {
        $subscribed = SidePanelSubscriber::getSubscribedEvents();
        self::assertSame(
            [RenderEvent::EVENT_HANDLE => 'onCardRender'],
            $subscribed,
        );
    }

    public function testEmitsOnceOnConfiguredCardAndIncludesIframeUrl(): void
    {
        $subscriber = $this->makeSubscriber(allow: true, pid: '90001');

        $subscriber->onCardRender(new RenderEvent(SidePanelSubscriber::MOUNT_CARD));

        self::assertCount(1, $this->emitted);
        self::assertStringContainsString(
            '/openemr/interface/copilot/side_panel.php?pid=90001',
            $this->emitted[0],
        );
        self::assertStringContainsString(
            'data-agent-side-panel-mount',
            $this->emitted[0],
        );
        // ``in_panel`` is the demo-fixture-membership flag the launcher
        // reads to decide whether to render disabled. 90001 is in the
        // panel, so the attribute must be ``1``.
        self::assertStringContainsString(
            'data-agent-in-panel="1"',
            $this->emitted[0],
        );
    }

    public function testNonPanelPidStillEmitsButFlagsOutOfPanel(): void
    {
        // The strict gateway gate (PR 17.5) will 403 a non-panel pid
        // anyway. The subscriber doesn't try to second-guess that —
        // the launcher renders, but its ``data-agent-in-panel`` reads
        // ``0`` so the iframe target shows the "switch to a seeded
        // patient" disclaimer instead of an enabled chat box.
        $subscriber = $this->makeSubscriber(allow: true, pid: '404');

        $subscriber->onCardRender(new RenderEvent(SidePanelSubscriber::MOUNT_CARD));

        self::assertCount(1, $this->emitted);
        self::assertStringContainsString('data-agent-in-panel="0"', $this->emitted[0]);
    }

    public function testEmitsAtMostOnceAcrossManyDispatches(): void
    {
        // Demographics tab fires ``patientSummaryCard.render`` once per
        // card on the page (notes, demographics, vitals, etc.). The
        // subscriber must not emit a duplicate launcher per fire.
        $subscriber = $this->makeSubscriber(allow: true, pid: '90001');

        for ($i = 0; $i < 6; $i++) {
            $subscriber->onCardRender(new RenderEvent(SidePanelSubscriber::MOUNT_CARD));
        }

        self::assertCount(1, $this->emitted);
        self::assertTrue($subscriber->hasEmitted());
    }

    public function testDoesNotEmitOnUnrelatedCard(): void
    {
        // The once-guard must not burn on cards we don't own — otherwise
        // a 'demographics' fire that arrived first would silently disable
        // the launcher's actual mount on 'note'. Every non-mount card
        // is a no-op; only the configured card emits.
        $subscriber = $this->makeSubscriber(allow: true, pid: '90001');

        $subscriber->onCardRender(new RenderEvent('demographics'));
        $subscriber->onCardRender(new RenderEvent('reminder'));
        self::assertSame([], $this->emitted);
        self::assertFalse($subscriber->hasEmitted());

        // Subsequent fire on the configured card still emits.
        $subscriber->onCardRender(new RenderEvent(SidePanelSubscriber::MOUNT_CARD));
        self::assertTrue($subscriber->hasEmitted());
        self::assertNotSame([], $this->emitted);
    }

    public function testDoesNotEmitWhenAclDenies(): void
    {
        $subscriber = $this->makeSubscriber(allow: false, pid: '90001');

        $subscriber->onCardRender(new RenderEvent(SidePanelSubscriber::MOUNT_CARD));

        self::assertSame([], $this->emitted);
        self::assertFalse($subscriber->hasEmitted());
    }

    public function testDoesNotEmitWhenNoActivePid(): void
    {
        // A RenderEvent dispatched outside the chart context (no active
        // patient in session) must not emit a launcher. Defends against
        // some other page reusing the event.
        $subscriber = $this->makeSubscriber(allow: true, pid: null);

        $subscriber->onCardRender(new RenderEvent(SidePanelSubscriber::MOUNT_CARD));

        self::assertSame([], $this->emitted);
        self::assertFalse($subscriber->hasEmitted());
    }

    public function testEmittedHtmlEscapesPidIntoAttributes(): void
    {
        // Pid is server-resolved from session, which already coerces to
        // digits, so this test pins the defensive escape rather than a
        // realistic input. A future change that loosens the resolver
        // mustn't introduce an XSS sink in the rendered launcher.
        $subscriber = $this->makeSubscriber(allow: true, pid: '12"><script>x</script>');

        $subscriber->onCardRender(new RenderEvent(SidePanelSubscriber::MOUNT_CARD));

        self::assertCount(1, $this->emitted);
        self::assertStringNotContainsString('<script>x</script>', $this->emitted[0]);
        self::assertStringContainsString('&quot;&gt;&lt;script&gt;', $this->emitted[0]);
    }

    private function makeSubscriber(bool $allow, ?string $pid): SidePanelSubscriber
    {
        $emit = function (string $html): void {
            $this->emitted[] = $html;
        };
        $accessCheck = static fn (): bool => $allow;
        $pidResolver = static fn (): ?string => $pid;
        return new SidePanelSubscriber('/openemr', $emit, $accessCheck, $pidResolver);
    }
}
