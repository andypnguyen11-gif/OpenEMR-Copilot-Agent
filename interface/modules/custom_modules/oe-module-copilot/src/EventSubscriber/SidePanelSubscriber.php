<?php

/**
 * Side-panel subscriber for the patient demographics tab (PR 17).
 *
 * Listens on :class:`OpenEMR\Events\Patient\Summary\Card\RenderEvent` —
 * the same hook AUDIT §2.2 calls out as the non-fork mounting point —
 * and injects a fixed-position launcher + iframe shell when the chart's
 * notes card is rendered.
 *
 * Emission strategy. ``RenderEvent::addAppendedData`` is the documented
 * mechanism for injecting per-card content, but in OpenEMR core only the
 * ``patient_portal`` card template actually renders the
 * ``appendedInjection`` variable; the rest pass it down and drop it on
 * the floor (see ``templates/patient/card/loader.html.twig``). Instead
 * of attaching to a card whose template happens to render the injection
 * (which forces a card choice on cosmetic grounds), the subscriber
 * writes its mount HTML directly to the output buffer the dispatch
 * runs inside. The HTML is fixed-position, escaping the card body, so
 * which card we hook is purely a question of *when* the listener fires,
 * not where the panel ends up.
 *
 * The mount HTML is emitted at most once per request — guarded by
 * ``$this->emitted`` so subsequent dispatches (the demographics tab
 * fires the event for every card on the page) become no-ops. Returning
 * early on patientless requests prevents the panel from rendering on
 * non-chart pages that happen to dispatch a ``RenderEvent`` of their own.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\EventSubscriber;

use Closure;
use OpenEMR\Events\Patient\Summary\Card\RenderEvent;
use Symfony\Component\EventDispatcher\EventSubscriberInterface;

class SidePanelSubscriber implements EventSubscriberInterface
{
    /**
     * Fixture-panel pids the demo Co-Pilot can answer about. The gateway's
     * PR 17.5 access checker enforces the real authorisation; this list
     * is just the safety net so a clinician opening the chart of a
     * non-fixture patient sees the launcher disabled rather than a 403
     * after the first send.
     */
    private const DEMO_PANEL_PIDS = ['90001', '90002', '90003', '90004', '90005'];

    /**
     * Card on which the listener emits its mount HTML. ``'note'`` is the
     * broadest ``patientSummaryCard.render`` dispatch on the demographics
     * tab — gated only by the ``patients/notes`` ACL, which every
     * clinician role in the demo has — so picking it gives the launcher
     * the most reliable always-on coverage. The mount HTML is fixed-
     * position, so the card we choose only controls *when* we emit, not
     * where the panel appears.
     */
    public const MOUNT_CARD = 'note';

    private bool $emitted = false;
    private readonly Closure $emit;
    private readonly Closure $accessCheck;
    private readonly Closure $pidResolver;

    /**
     * @param string $webroot OpenEMR's webroot prefix (e.g. ``/openemr``).
     * @param (Closure(string): void)|null $emit How to write the mount
     *        HTML. Defaults to ``echo``; tests inject a recorder.
     * @param (Closure(): bool)|null $accessCheck Demographics-tab ACL
     *        gate. Defaults to ``AclMain::aclCheckCore('patients', 'demo')``;
     *        tests inject a constant-true / constant-false.
     * @param (Closure(): ?string)|null $pidResolver Active patient pid
     *        from session. Defaults to reading ``pid`` off the active
     *        :class:`SessionWrapperFactory` session; tests inject a
     *        constant.
     */
    public function __construct(
        private readonly string $webroot,
        ?Closure $emit = null,
        ?Closure $accessCheck = null,
        ?Closure $pidResolver = null,
    ) {
        $this->emit = $emit ?? static function (string $html): void {
            echo $html;
        };
        $this->accessCheck = $accessCheck ?? (static fn(): bool => \OpenEMR\Common\Acl\AclMain::aclCheckCore('patients', 'demo'));
        $this->pidResolver = $pidResolver ?? static function (): ?string {
            // Read pid from OEGlobalsBag, not $_SESSION. OpenEMR boots the
            // session in read_and_close mode: globals.php writes pid via
            // SessionUtil and immediately calls session_write_close(), so
            // $_SESSION['pid'] is null by the time the demographics-tab
            // RenderEvent fires. globals.php mirrors the active pid into
            // OEGlobalsBag (which on prod falls through to $GLOBALS['pid'])
            // for exactly this kind of mid-request consumer.
            $raw = \OpenEMR\Core\OEGlobalsBag::getInstance()->get('pid');
            if (is_int($raw) && $raw > 0) {
                return (string) $raw;
            }
            if (is_string($raw) && ctype_digit($raw) && $raw !== '0') {
                return $raw;
            }
            return null;
        };
    }

    /**
     * @return array<string, string>
     */
    public static function getSubscribedEvents(): array
    {
        return [
            RenderEvent::EVENT_HANDLE => 'onCardRender',
        ];
    }

    public function onCardRender(RenderEvent $event): void
    {
        if ($this->emitted) {
            return;
        }
        if ($event->getCard() !== self::MOUNT_CARD) {
            return;
        }
        if (!($this->accessCheck)()) {
            // Gate matches the demographics-tab visibility rule; without
            // the demo ACL the user can't see the chart anyway, but
            // checking here keeps the side-panel mount honest about
            // who owns its access decisions.
            return;
        }
        $pid = ($this->pidResolver)();
        if ($pid === null) {
            return;
        }

        $this->emitted = true;
        ($this->emit)($this->renderMountHtml($pid));
    }

    public function hasEmitted(): bool
    {
        return $this->emitted;
    }

    private function renderMountHtml(string $pid): string
    {
        // Plain-string concatenation rather than Twig: the mount is two
        // elements (launcher button + iframe shell). Twig's overhead
        // would buy nothing and the subscriber stays self-contained
        // for tests that don't want a kernel.
        $webroot = $this->webroot;
        $iframeUrl = $webroot . '/interface/copilot/side_panel.php?pid=' . urlencode($pid);
        $cssUrl = $webroot . '/public/copilot/copilot.css';
        $jsUrl = $webroot . '/public/copilot/side_panel_launcher.js';
        $inPanel = in_array($pid, self::DEMO_PANEL_PIDS, true) ? '1' : '0';
        $pidAttr = htmlspecialchars($pid, ENT_QUOTES, 'UTF-8');
        $iframeAttr = htmlspecialchars($iframeUrl, ENT_QUOTES, 'UTF-8');
        $cssAttr = htmlspecialchars($cssUrl, ENT_QUOTES, 'UTF-8');
        $jsAttr = htmlspecialchars($jsUrl, ENT_QUOTES, 'UTF-8');

        return <<<HTML
<link rel="stylesheet" href="{$cssAttr}">
<div data-agent-side-panel-mount data-agent-pid="{$pidAttr}" data-agent-in-panel="{$inPanel}">
    <button type="button" class="copilot-side-panel-launcher" data-agent-side-panel-toggle aria-controls="copilot-side-panel-frame" aria-expanded="false">
        <span class="copilot-side-panel-launcher-icon" aria-hidden="true">&#9432;</span>
        <span class="copilot-side-panel-launcher-label">Co-Pilot</span>
    </button>
    <aside class="copilot-side-panel" data-agent-side-panel hidden>
        <header class="copilot-side-panel-header">
            <h2 class="copilot-side-panel-title">Clinical Co-Pilot</h2>
            <button type="button" class="copilot-side-panel-close" data-agent-side-panel-close aria-label="Close Co-Pilot">&times;</button>
        </header>
        <iframe id="copilot-side-panel-frame" class="copilot-side-panel-frame" data-agent-side-panel-frame title="Clinical Co-Pilot" src="about:blank" data-agent-src="{$iframeAttr}"></iframe>
    </aside>
</div>
<script src="{$jsAttr}" defer></script>
HTML;
    }
}
