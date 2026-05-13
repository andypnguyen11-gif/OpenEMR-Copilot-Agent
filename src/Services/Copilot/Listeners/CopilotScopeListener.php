<?php

/**
 * Registers the Co-Pilot chat route's required OAuth scope with OpenEMR's
 * OAuth2 scope registry.
 *
 * The Co-Pilot chat route ``POST /api/agent/query`` is gated by
 * :class:`OpenEMR\\RestControllers\\Subscriber\\AuthorizationListener` line 186,
 * which constructs the required scope as ``$scopeType / $resource . $permission``
 * for an authenticated user — i.e. ``user/query.c`` (``c`` = create, the POST
 * permission letter). For OAuth2 clients to request that scope, the scope
 * has to be present in the supported-scope list that
 * :class:`OpenEMR\\Common\\Auth\\OpenIDConnect\\Repositories\\ScopeRepository`
 * dispatches via :class:`RestApiScopeEvent` — otherwise dynamic client
 * registration rejects it with ``invalid_scope`` and tokens never carry it.
 *
 * This listener appends ``user/query.c`` to the supported list whenever the
 * standard-API scope event fires. Idempotent (no-op if the scope is already
 * present). It does not weaken any authorization check, expose internal
 * state, or add a test-only code path — the chat route still enforces
 * ``user/query.c`` as before; this listener only lets OAuth2 clients
 * request it through the normal registration + token-grant flow.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\Listeners;

use OpenEMR\Events\RestApiExtend\RestApiScopeEvent;
use Symfony\Component\EventDispatcher\EventSubscriberInterface;

final class CopilotScopeListener implements EventSubscriberInterface
{
    /**
     * The SMART scope the Co-Pilot chat route enforces. Kept as a constant so
     * the test can pin against the same string the listener publishes — if the
     * route's permission letter ever shifts (e.g. POST → ``c``, PUT → ``u``)
     * the wired scope and the registry stay aligned by editing one place.
     */
    public const COPILOT_QUERY_SCOPE = 'user/query.c';

    /**
     * @return array<string, string>
     */
    public static function getSubscribedEvents(): array
    {
        return [
            RestApiScopeEvent::EVENT_TYPE_GET_SUPPORTED_SCOPES => 'onGetSupportedScopes',
        ];
    }

    /**
     * Append the Co-Pilot chat-route scope to the supported-scope list.
     *
     * Only the standard-API scope channel is extended — FHIR scope dispatches
     * also pass through the same event handle but with a different
     * :meth:`RestApiScopeEvent::getApiType`, and the chat route is not a FHIR
     * endpoint. Filtering by ``api_type`` keeps the registry surface narrow
     * and matches the existing convention in
     * :meth:`ScopeRepository::getCurrentSmartScopes`.
     */
    public function onGetSupportedScopes(RestApiScopeEvent $event): void
    {
        if ($event->getApiType() !== RestApiScopeEvent::API_TYPE_STANDARD) {
            return;
        }
        $scopes = $event->getScopes();
        if (in_array(self::COPILOT_QUERY_SCOPE, $scopes, true)) {
            return;
        }
        $scopes[] = self::COPILOT_QUERY_SCOPE;
        $event->setScopes($scopes);
    }
}
