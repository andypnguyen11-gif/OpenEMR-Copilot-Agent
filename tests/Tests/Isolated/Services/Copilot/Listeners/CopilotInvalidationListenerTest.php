<?php

/**
 * Isolated tests for CopilotInvalidationListener.
 *
 * The subscriber is a thin glue layer; the only logic worth pinning is
 * "extracts pid from event payload and forwards to dispatcher" and the
 * various malformed-payload short-circuits. Dispatcher behaviour is
 * covered separately in :class:`InvalidationDispatcherTest`.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot\Listeners;

use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Events\Patient\PatientUpdatedEvent;
use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\AgentResponse;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use OpenEMR\Services\Copilot\InvalidationDispatcher;
use OpenEMR\Services\Copilot\Listeners\CopilotInvalidationListener;
use PHPUnit\Framework\MockObject\MockObject;
use PHPUnit\Framework\TestCase;
use Psr\Log\NullLogger;

final class CopilotInvalidationListenerTest extends TestCase
{
    private AgentHttpClient&MockObject $client;
    private InvalidationDispatcher $dispatcher;

    protected function setUp(): void
    {
        $this->client = $this->createMock(AgentHttpClient::class);
        $config = new CopilotConfig(new OEGlobalsBag([
            'copilot_internal_token' => str_repeat('a', 64),
        ]));
        $this->dispatcher = new InvalidationDispatcher(
            $this->client,
            $config,
            new NullLogger(),
        );
    }

    public function testSubscribesToPatientUpdatedEvent(): void
    {
        // Static contract: this is what the kernel binds to. If the
        // event handle changes upstream the test breaks loudly.
        self::assertSame(
            ['patient.updated' => 'onPatientUpdated'],
            CopilotInvalidationListener::getSubscribedEvents(),
        );
    }

    public function testInvalidateFiresWithPidFromEvent(): void
    {
        $this->client->expects($this->once())
            ->method('postInternal')
            ->with(
                '/api/agent/internal/invalidate/101',
                [],
                str_repeat('a', 64),
            )
            ->willReturn(new AgentResponse(204, []));

        $listener = new CopilotInvalidationListener($this->dispatcher);
        $event = new PatientUpdatedEvent(
            ['pid' => '101', 'fname' => 'before'],
            ['pid' => '101', 'fname' => 'after'],
        );

        $listener->onPatientUpdated($event);
    }

    public function testCoercesNumericPidToString(): void
    {
        // PatientService writes pid back as either a string or a raw
        // int depending on the call site (databaseUpdate vs update).
        // The listener has to handle both.
        $this->client->expects($this->once())
            ->method('postInternal')
            ->with('/api/agent/internal/invalidate/42', [], str_repeat('a', 64))
            ->willReturn(new AgentResponse(204, []));

        $listener = new CopilotInvalidationListener($this->dispatcher);
        $event = new PatientUpdatedEvent([], ['pid' => 42]);

        $listener->onPatientUpdated($event);
    }

    public function testNoOpsOnMissingPid(): void
    {
        $this->client->expects($this->never())->method('postInternal');

        $listener = new CopilotInvalidationListener($this->dispatcher);
        $event = new PatientUpdatedEvent([], ['fname' => 'John']);

        $listener->onPatientUpdated($event);
    }

    public function testNoOpsOnNonArrayNewData(): void
    {
        $this->client->expects($this->never())->method('postInternal');

        $listener = new CopilotInvalidationListener($this->dispatcher);
        // PatientUpdatedEvent's payload is `mixed` so a non-array is
        // legal at the type level — a future caller could pass an
        // object or null. Listener must not blow up.
        $event = new PatientUpdatedEvent([], null);

        $listener->onPatientUpdated($event);
    }

    public function testNoOpsOnEmptyPid(): void
    {
        $this->client->expects($this->never())->method('postInternal');

        $listener = new CopilotInvalidationListener($this->dispatcher);
        $event = new PatientUpdatedEvent([], ['pid' => '']);

        $listener->onPatientUpdated($event);
    }
}
