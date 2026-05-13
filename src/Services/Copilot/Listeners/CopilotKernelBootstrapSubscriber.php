<?php

/**
 * Subscribes :class:`CopilotScopeListener` to the kernel-DI event dispatcher
 * early in the request lifecycle.
 *
 * The Co-Pilot scope listener lives on a *different* event dispatcher than
 * the ApiApplication's local one: ``ScopeRepository`` dispatches the scope
 * event on ``OEGlobalsBag::getKernel()->getEventDispatcher()`` (the kernel
 * DI container's dispatcher), not on the ApiApplication dispatcher that
 * holds the kernel-request listener chain. This subscriber bridges the two
 * — it listens on the ApiApplication dispatcher (where it's registered from
 * the entry-point files) and, when kernel.request fires, attaches the scope
 * listener to the kernel-DI dispatcher.
 *
 * Priority 95 places this strictly after
 * :class:`OpenEMR\\RestControllers\\Subscriber\\SiteSetupListener` (priority
 * 100, which bootstraps the kernel) and strictly before
 * :class:`OpenEMR\\RestControllers\\Subscriber\\OAuth2AuthorizationListener`
 * (priority 50, which is the first listener that triggers a scope-event
 * dispatch). That window is the only valid one — earlier and the kernel
 * isn't ready; later and the scope event has already fired without our
 * listener attached.
 *
 * Idempotency is enforced by :class:`CopilotScopeListener` itself (it
 * skips appending ``user/query.c`` if the scope is already in the
 * supported list), so a fresh listener instance is subscribed on every
 * ``kernel.request`` fire. Re-subscribing across requests in the same
 * php-fpm worker is **necessary**, not a bug: the kernel-DI dispatcher
 * is rebuilt each request (``OEGlobalsBag::getKernel`` reads from
 * ``$GLOBALS['kernel']``, which :class:`SiteSetupListener` re-populates
 * per request), so a stale static "already registered" flag would leave
 * the second-and-later requests in a php-fpm worker without the
 * listener attached and the scope event would fire unbound. The
 * trade-off is one extra ``CopilotScopeListener`` allocation per
 * request, which is negligible.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\Listeners;

use OpenEMR\Core\OEGlobalsBag;
use RuntimeException;
use Symfony\Component\EventDispatcher\EventSubscriberInterface;
use Symfony\Component\HttpKernel\Event\RequestEvent;
use Symfony\Component\HttpKernel\KernelEvents;

final class CopilotKernelBootstrapSubscriber implements EventSubscriberInterface
{
    /**
     * @return array<string, list<array{0: string, 1: int}>>
     */
    public static function getSubscribedEvents(): array
    {
        return [
            KernelEvents::REQUEST => [['onKernelRequest', 95]],
        ];
    }

    public function onKernelRequest(RequestEvent $event): void
    {
        // ``getKernel`` was added to ``OEGlobalsBag`` in upstream commit
        // 86964ca5a (2026-04-13). The CopilotInvalidationListener registration
        // in ``apis/routes/_rest_routes_copilot.inc.php`` documents the same
        // version-skew defense; this is the parallel of that guard. We
        // ``return`` (not throw) on either failure because a missing kernel
        // here only loses the Co-Pilot scope registration — every other path
        // through the request is unaffected, and a clinician's request
        // should not 500 because an OAuth scope extension couldn't wire up.
        $globals = OEGlobalsBag::getInstance();
        // @phpstan-ignore-next-line function.alreadyNarrowedType
        if (!method_exists($globals, 'getKernel')) {
            return;
        }
        try {
            $dispatcher = $globals->getKernel()->getEventDispatcher();
        } catch (RuntimeException) {
            return;
        }

        $dispatcher->addSubscriber(new CopilotScopeListener());
    }
}
