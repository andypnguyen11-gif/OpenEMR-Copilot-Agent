<?php

/**
 * Isolated tests for CopilotScopeListener.
 *
 * The listener is pure: it appends a single scope to a list inside an event.
 * The tests pin the static contract (event handle + scope string) and the
 * three behaviour branches (standard-API → append, FHIR → skip, already
 * present → no-op).
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot\Listeners;

use OpenEMR\Events\RestApiExtend\RestApiScopeEvent;
use OpenEMR\Services\Copilot\Listeners\CopilotScopeListener;
use PHPUnit\Framework\TestCase;

final class CopilotScopeListenerTest extends TestCase
{
    public function testSubscribesToSupportedScopesEvent(): void
    {
        // Static contract: the kernel-DI dispatcher binds on this exact
        // event handle. If upstream renames the event, the test breaks
        // loudly rather than silently dropping the scope registration.
        self::assertSame(
            [RestApiScopeEvent::EVENT_TYPE_GET_SUPPORTED_SCOPES => 'onGetSupportedScopes'],
            CopilotScopeListener::getSubscribedEvents(),
        );
    }

    public function testAppendsScopeOnStandardApiEvent(): void
    {
        $event = new RestApiScopeEvent();
        $event->setApiType(RestApiScopeEvent::API_TYPE_STANDARD);
        $event->setScopes(['openid', 'api:oemr']);

        (new CopilotScopeListener())->onGetSupportedScopes($event);

        self::assertContains('user/query.c', $event->getScopes());
        self::assertSame(
            ['openid', 'api:oemr', 'user/query.c'],
            $event->getScopes(),
            'scope appended at end, existing scopes preserved in order',
        );
    }

    public function testIsIdempotentOnRepeatedDispatch(): void
    {
        // Two dispatches against the same event must not duplicate the scope.
        // Idempotency matters because the event fires on both /oauth2/...
        // and /api/... flows during the same request lifecycle.
        $event = new RestApiScopeEvent();
        $event->setApiType(RestApiScopeEvent::API_TYPE_STANDARD);
        $event->setScopes([]);

        $listener = new CopilotScopeListener();
        $listener->onGetSupportedScopes($event);
        $listener->onGetSupportedScopes($event);

        $matches = 0;
        foreach ($event->getScopes() as $scope) {
            if ($scope === 'user/query.c') {
                $matches++;
            }
        }
        self::assertSame(1, $matches, 'scope appears exactly once after two dispatches');
    }

    public function testSkipsFhirApiEvent(): void
    {
        // The chat route is not a FHIR endpoint; appending its scope to
        // the FHIR registry would muddy the FHIR scope surface for no
        // reason. The listener filters by api_type accordingly.
        $event = new RestApiScopeEvent();
        $event->setApiType(RestApiScopeEvent::API_TYPE_FHIR);
        $event->setScopes(['system/Patient.read']);

        (new CopilotScopeListener())->onGetSupportedScopes($event);

        self::assertNotContains('user/query.c', $event->getScopes());
    }

    public function testDoesNotAppendSystemEquivalentScope(): void
    {
        // Security-critical negative-space assertion: this patch enables
        // ONLY the user-context chat scope. A client_credentials grant
        // produces a system-role token whose AuthorizationListener check
        // at line 186 constructs ``system/query.c`` (not user/query.c).
        // If we accidentally also registered ``system/query.c`` here,
        // service-to-service tokens could reach the chat surface — which
        // is exactly what the case study's threat model rules out.
        //
        // The listener must touch only ``user/query.c``, leaving
        // ``system/query.c`` (and any other system-scope variants)
        // unregistered. Test fires a STANDARD-API scope event with no
        // pre-existing entries and asserts nothing in the resulting
        // list begins with ``system/``.
        $event = new RestApiScopeEvent();
        $event->setApiType(RestApiScopeEvent::API_TYPE_STANDARD);
        $event->setScopes([]);

        (new CopilotScopeListener())->onGetSupportedScopes($event);

        self::assertNotContains('system/query.c', $event->getScopes());
        foreach ($event->getScopes() as $scope) {
            self::assertTrue(
                is_string($scope) && !str_starts_with($scope, 'system/'),
                'listener must not append any system-context scope',
            );
        }
    }
}
