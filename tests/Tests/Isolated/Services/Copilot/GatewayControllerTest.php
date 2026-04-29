<?php

/**
 * Isolated tests for GatewayController.
 *
 * The gateway is a thin proxy in front of the Clinical Co-Pilot agent
 * service. Its only job for the healthz route is: forward to
 * :class:`AgentHttpClient`, pass the agent's JSON body and status code back
 * verbatim on success, and surface a 502 with a generic body when the
 * transport fails. These tests pin that contract by injecting a mocked
 * client — no Docker, no network, no DB.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot;

use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\AgentResponse;
use OpenEMR\Services\Copilot\AgentServiceException;
use OpenEMR\Services\Copilot\GatewayController;
use PHPUnit\Framework\MockObject\MockObject;
use PHPUnit\Framework\TestCase;
use Psr\Log\LoggerInterface;
use Psr\Log\NullLogger;
use Symfony\Component\HttpFoundation\Response;

final class GatewayControllerTest extends TestCase
{
    private AgentHttpClient&MockObject $client;
    private GatewayController $controller;

    protected function setUp(): void
    {
        $this->client = $this->createMock(AgentHttpClient::class);
        $this->controller = new GatewayController($this->client, new NullLogger());
    }

    public function testHealthzProxiesAgentBodyAndStatus(): void
    {
        $this->client->expects($this->once())
            ->method('get')
            ->with('/healthz')
            ->willReturn(new AgentResponse(
                Response::HTTP_OK,
                ['status' => 'ok', 'env' => 'production'],
            ));

        $response = $this->controller->healthz();

        self::assertSame(Response::HTTP_OK, $response->getStatusCode());
        self::assertSame(
            ['status' => 'ok', 'env' => 'production'],
            json_decode((string) $response->getContent(), true),
        );
    }

    public function testHealthzPassesThroughNon2xxStatusFromAgent(): void
    {
        // Agent reachable but unhealthy — the gateway must not coerce this to
        // 200 OK or 502; the upstream code should round-trip so callers can
        // distinguish "agent down" from "agent reports problem".
        $this->client->method('get')
            ->willReturn(new AgentResponse(
                Response::HTTP_SERVICE_UNAVAILABLE,
                ['status' => 'not_ready'],
            ));

        $response = $this->controller->healthz();

        self::assertSame(Response::HTTP_SERVICE_UNAVAILABLE, $response->getStatusCode());
        self::assertSame(
            ['status' => 'not_ready'],
            json_decode((string) $response->getContent(), true),
        );
    }

    public function testHealthzReturns502OnTransportFailure(): void
    {
        $this->client->method('get')
            ->willThrowException(new AgentServiceException('connection refused'));

        // Use a real logger mock to confirm we record the failure (so an
        // alert can fire on agent-service outages) without leaking the
        // exception message into the response body.
        $logger = $this->createMock(LoggerInterface::class);
        $logger->expects($this->once())
            ->method('warning')
            ->with(
                'Clinical Co-Pilot agent service unreachable',
                $this->callback(static fn(array $context): bool
                    => $context['exception'] instanceof AgentServiceException),
            );
        $controller = new GatewayController($this->client, $logger);

        $response = $controller->healthz();

        self::assertSame(Response::HTTP_BAD_GATEWAY, $response->getStatusCode());
        $body = json_decode((string) $response->getContent(), true);
        self::assertSame(['status' => 'unavailable'], $body);
        // Generic body — the underlying error must not leak to the user.
        self::assertStringNotContainsString('connection refused', (string) $response->getContent());
    }
}
