<?php

/**
 * Isolated tests for InvalidationDispatcher.
 *
 * The dispatcher's contract is "fire-and-forget, never throw, log
 * everything else." These tests pin the each behavior explicitly:
 * happy path forwards correctly, transport failure is swallowed,
 * non-2xx is logged at the right level, blank inputs are no-ops, and
 * a missing internal token does not bubble out as an exception.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot;

use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\AgentResponse;
use OpenEMR\Services\Copilot\AgentServiceException;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use OpenEMR\Services\Copilot\InvalidationDispatcher;
use PHPUnit\Framework\MockObject\MockObject;
use PHPUnit\Framework\TestCase;
use Psr\Log\AbstractLogger;

final class InvalidationDispatcherTest extends TestCase
{
    private AgentHttpClient&MockObject $client;
    private RecordingLogger $logger;

    protected function setUp(): void
    {
        $this->client = $this->createMock(AgentHttpClient::class);
        $this->logger = new RecordingLogger();
    }

    /**
     * @param array<string, mixed> $globals
     */
    private function makeDispatcher(array $globals = []): InvalidationDispatcher
    {
        $defaults = ['copilot_internal_token' => str_repeat('a', 64)];
        return new InvalidationDispatcher(
            $this->client,
            new CopilotConfig(new OEGlobalsBag([...$defaults, ...$globals])),
            $this->logger,
        );
    }

    public function testInvalidatePostsToTheAgent(): void
    {
        $this->client->expects($this->once())
            ->method('postInternal')
            ->with(
                '/api/agent/internal/invalidate/101',
                [],
                str_repeat('a', 64),
            )
            ->willReturn(new AgentResponse(204, []));

        $this->makeDispatcher()->invalidate('101');

        // Happy path is intentionally silent — no log entries means a
        // successful invalidate doesn't add noise to the operator log.
        self::assertSame([], $this->logger->records);
    }

    public function testInvalidateUrlEncodesPatientId(): void
    {
        // Patient IDs in production are typically numeric, but the FHIR
        // ``logical_id`` flow allows arbitrary strings. Encoding here
        // protects against a malformed id breaking the route's path
        // segment parsing on the agent side.
        $this->client->expects($this->once())
            ->method('postInternal')
            ->with(
                '/api/agent/internal/invalidate/foo%2Fbar%20baz',
                [],
                str_repeat('a', 64),
            )
            ->willReturn(new AgentResponse(204, []));

        $this->makeDispatcher()->invalidate('foo/bar baz');
    }

    public function testInvalidateNoOpsOnEmptyPatientId(): void
    {
        // Listener firing on a malformed event must not amplify into
        // a useless network round-trip.
        $this->client->expects($this->never())->method('postInternal');

        $this->makeDispatcher()->invalidate('');

        self::assertCount(1, $this->logger->records);
        self::assertSame('info', $this->logger->records[0]['level']);
    }

    public function testInvalidateSwallowsTransportFailure(): void
    {
        $this->client->method('postInternal')
            ->willThrowException(new AgentServiceException('boom'));

        // No exception escapes — the contract is fire-and-forget; a
        // clinical write that triggered this listener has already
        // landed and must not be rolled back.
        $this->makeDispatcher()->invalidate('101');

        self::assertCount(1, $this->logger->records);
        self::assertSame('warning', $this->logger->records[0]['level']);
    }

    public function testInvalidateLogs5xxAtWarning(): void
    {
        $this->client->method('postInternal')
            ->willReturn(new AgentResponse(500, ['detail' => 'engine boom']));

        $this->makeDispatcher()->invalidate('101');

        self::assertCount(1, $this->logger->records);
        self::assertSame('warning', $this->logger->records[0]['level']);
        self::assertSame(500, $this->logger->records[0]['context']['status']);
    }

    public function testInvalidateLogs4xxAtInfo(): void
    {
        $this->client->method('postInternal')
            ->willReturn(new AgentResponse(401, ['detail' => 'invalid token']));

        $this->makeDispatcher()->invalidate('101');

        self::assertCount(1, $this->logger->records);
        self::assertSame('info', $this->logger->records[0]['level']);
        self::assertSame(401, $this->logger->records[0]['context']['status']);
    }

    public function testInvalidateLogsAndReturnsWhenTokenUnconfigured(): void
    {
        // Override the default — no globals, no env. Dispatch must not
        // throw the wiring error into the listener; that would roll
        // back the clinical write that fired the event.
        $this->client->expects($this->never())->method('postInternal');
        $dispatcher = new InvalidationDispatcher(
            $this->client,
            new CopilotConfig(new OEGlobalsBag([])),
            $this->logger,
        );

        $dispatcher->invalidate('101');

        self::assertCount(1, $this->logger->records);
        self::assertSame('warning', $this->logger->records[0]['level']);
    }

    public function testWarmPanelPostsTheList(): void
    {
        $this->client->expects($this->once())
            ->method('postInternal')
            ->with(
                '/api/agent/internal/warm',
                ['patient_ids' => ['101', '102', '103']],
                str_repeat('a', 64),
            )
            ->willReturn(new AgentResponse(200, ['warmed' => 3, 'failed' => []]));

        $this->makeDispatcher()->warmPanel(['101', '102', '103']);

        self::assertSame([], $this->logger->records);
    }

    public function testWarmPanelDropsBlankIdsBeforeDispatch(): void
    {
        // The dispatcher cleans blanks so the agent never sees a panel
        // with empty entries; the agent's BackgroundRunner *would*
        // surface them as `empty_patient_id` failures, but cleaning
        // here keeps the wire payload tighter.
        $this->client->expects($this->once())
            ->method('postInternal')
            ->with(
                '/api/agent/internal/warm',
                ['patient_ids' => ['101', '102']],
                str_repeat('a', 64),
            )
            ->willReturn(new AgentResponse(200, ['warmed' => 2, 'failed' => []]));

        $this->makeDispatcher()->warmPanel(['101', '', '102', '']);
    }

    public function testWarmPanelNoOpsOnEmptyInput(): void
    {
        $this->client->expects($this->never())->method('postInternal');

        // No exception, no log entry — a zero-patient warm is the
        // patient-deselect-before-fire scenario, not worth logging.
        $this->makeDispatcher()->warmPanel([]);

        self::assertSame([], $this->logger->records);
    }

    public function testWarmPanelNoOpsWhenAllIdsBlank(): void
    {
        $this->client->expects($this->never())->method('postInternal');

        $this->makeDispatcher()->warmPanel(['', '', '']);
    }

    public function testWarmPanelSwallowsTransportFailure(): void
    {
        $this->client->method('postInternal')
            ->willThrowException(new AgentServiceException('boom'));

        $this->makeDispatcher()->warmPanel(['101']);

        self::assertCount(1, $this->logger->records);
        self::assertSame('warning', $this->logger->records[0]['level']);
        self::assertSame(1, $this->logger->records[0]['context']['panel_size']);
    }

    public function testReadFlagsReturnsParsedFlagsOnHappyPath(): void
    {
        $this->client->expects($this->once())
            ->method('getInternal')
            ->with(
                '/api/agent/internal/flags/90001',
                str_repeat('a', 64),
            )
            ->willReturn(new AgentResponse(200, [
                'patient_id' => '90001',
                'flags' => [
                    [
                        'source_id' => 'flag:med_vs_note:90001:abc',
                        'rule_id' => 'med_vs_note_conflict',
                        'category' => 'consistency',
                        'rationale' => "Active medication 'Metoprolol' but recent note from 2026-04-15 mentions 'discontinued'.",
                        'referenced_source_ids' => ['med:1', 'note:2'],
                    ],
                ],
            ]));

        $flags = $this->makeDispatcher()->readFlags('90001');

        // PHPStan infers list<Flag> from the readFlags return type, so
        // assertInstanceOf would be redundant; the field assertions
        // below exercise the parsed object directly.
        self::assertCount(1, $flags);
        self::assertSame('med_vs_note_conflict', $flags[0]->ruleId);
        self::assertSame('consistency', $flags[0]->category);
        self::assertSame(['med:1', 'note:2'], $flags[0]->referencedSourceIds);
        self::assertSame([], $this->logger->records);
    }

    public function testReadFlagsReturnsEmptyListWhenAgentReportsZeroFlags(): void
    {
        // The healthy-patient case — no flags, no exception, no log.
        $this->client->method('getInternal')
            ->willReturn(new AgentResponse(200, ['patient_id' => '90001', 'flags' => []]));

        self::assertSame([], $this->makeDispatcher()->readFlags('90001'));
        self::assertSame([], $this->logger->records);
    }

    public function testReadFlagsUrlEncodesPatientId(): void
    {
        // Same defensive behaviour as invalidate — patient ids carrying
        // path-segment-special characters must not break the route.
        $this->client->expects($this->once())
            ->method('getInternal')
            ->with(
                '/api/agent/internal/flags/foo%2Fbar%20baz',
                str_repeat('a', 64),
            )
            ->willReturn(new AgentResponse(200, ['patient_id' => 'foo/bar baz', 'flags' => []]));

        $this->makeDispatcher()->readFlags('foo/bar baz');
    }

    public function testReadFlagsNoOpsOnEmptyPatientId(): void
    {
        $this->client->expects($this->never())->method('getInternal');

        self::assertSame([], $this->makeDispatcher()->readFlags(''));

        self::assertCount(1, $this->logger->records);
        self::assertSame('info', $this->logger->records[0]['level']);
    }

    public function testReadFlagsReturnsEmptyListOnTransportFailure(): void
    {
        $this->client->method('getInternal')
            ->willThrowException(new AgentServiceException('boom'));

        // Daily Brief page must still render — the cards just lose their
        // flag list; the rest of the panel is unaffected.
        self::assertSame([], $this->makeDispatcher()->readFlags('90001'));

        self::assertCount(1, $this->logger->records);
        self::assertSame('warning', $this->logger->records[0]['level']);
    }

    public function testReadFlagsLogs5xxAtWarningAndReturnsEmptyList(): void
    {
        $this->client->method('getInternal')
            ->willReturn(new AgentResponse(500, ['detail' => 'engine boom']));

        self::assertSame([], $this->makeDispatcher()->readFlags('90001'));

        self::assertCount(1, $this->logger->records);
        self::assertSame('warning', $this->logger->records[0]['level']);
        self::assertSame(500, $this->logger->records[0]['context']['status']);
    }

    public function testReadFlagsLogs4xxAtInfoAndReturnsEmptyList(): void
    {
        $this->client->method('getInternal')
            ->willReturn(new AgentResponse(401, ['detail' => 'invalid token']));

        self::assertSame([], $this->makeDispatcher()->readFlags('90001'));

        self::assertCount(1, $this->logger->records);
        self::assertSame('info', $this->logger->records[0]['level']);
        self::assertSame(401, $this->logger->records[0]['context']['status']);
    }

    public function testReadFlagsReturnsEmptyListWhenBodyMissingFlagsKey(): void
    {
        // Diverging response shape (missing key, wrong type) is a
        // wiring problem worth knowing about — log warning, render
        // flag-less.
        $this->client->method('getInternal')
            ->willReturn(new AgentResponse(200, ['patient_id' => '90001']));

        self::assertSame([], $this->makeDispatcher()->readFlags('90001'));

        self::assertCount(1, $this->logger->records);
        self::assertSame('warning', $this->logger->records[0]['level']);
    }

    public function testReadFlagsRefusesPartialListWhenOneEntryIsMalformed(): void
    {
        // Refusing partial flag sets keeps clinicians from acting on a
        // truncated view — better to render zero flags + a warning log
        // than to silently drop the bad row and let the doctor see four
        // out of five.
        $this->client->method('getInternal')
            ->willReturn(new AgentResponse(200, [
                'patient_id' => '90001',
                'flags' => [
                    [
                        'source_id' => 'flag:ok',
                        'rule_id' => 'r',
                        'category' => 'c',
                        'rationale' => 'x',
                        'referenced_source_ids' => ['s:1'],
                    ],
                    // Missing rationale field
                    [
                        'source_id' => 'flag:bad',
                        'rule_id' => 'r',
                        'category' => 'c',
                        'referenced_source_ids' => ['s:1'],
                    ],
                ],
            ]));

        self::assertSame([], $this->makeDispatcher()->readFlags('90001'));

        self::assertCount(1, $this->logger->records);
        self::assertSame('warning', $this->logger->records[0]['level']);
    }

    public function testReadFlagsReturnsEmptyListWhenTokenUnconfigured(): void
    {
        $this->client->expects($this->never())->method('getInternal');
        $dispatcher = new InvalidationDispatcher(
            $this->client,
            new CopilotConfig(new OEGlobalsBag([])),
            $this->logger,
        );

        self::assertSame([], $dispatcher->readFlags('90001'));

        self::assertCount(1, $this->logger->records);
        self::assertSame('warning', $this->logger->records[0]['level']);
    }
}

/**
 * Minimal in-memory PSR-3 logger so tests can assert level + context
 * without dragging Monolog into the isolated suite.
 */
final class RecordingLogger extends AbstractLogger
{
    /** @var list<array{level: string, message: string, context: array<string, mixed>}> */
    public array $records = [];

    /**
     * @param mixed             $level
     * @param string|\Stringable $message
     * @param array<mixed>      $context
     */
    public function log($level, $message, array $context = []): void
    {
        // PSR-3 declares ``$level`` as ``mixed`` (typically a
        // LogLevel::* constant which is a string). Narrow rather than
        // cast so PHPStan doesn't have to cast.string the parameter
        // type away.
        /** @var array<string, mixed> $context */
        $this->records[] = [
            'level' => is_string($level) ? $level : 'unknown',
            'message' => (string) $message,
            'context' => $context,
        ];
    }
}
