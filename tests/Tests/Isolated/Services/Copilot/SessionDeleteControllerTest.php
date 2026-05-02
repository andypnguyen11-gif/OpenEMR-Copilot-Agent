<?php

/**
 * Isolated tests for :class:`SessionDeleteController`.
 *
 * Five contract points pinned here:
 *
 * 1. Happy path — agent 204 → controller 204 (empty body).
 * 2. Agent 404 (session not found under principal) → 404 passthrough.
 * 3. Missing or invalid ``patient_id`` query param → 400, no agent call.
 * 4. Invalid session_id (charset, length) → 400, no agent call.
 * 5. Agent transport failure → 502 with a generic body.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot;

use Lcobucci\Clock\FrozenClock;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\AgentResponse;
use OpenEMR\Services\Copilot\AgentServiceException;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use OpenEMR\Services\Copilot\JwtSigner;
use OpenEMR\Services\Copilot\SessionDeleteController;
use OpenEMR\Services\Copilot\SessionMapper;
use PHPUnit\Framework\MockObject\MockObject;
use PHPUnit\Framework\TestCase;
use Psr\Log\NullLogger;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\HttpFoundation\Response;

final class SessionDeleteControllerTest extends TestCase
{
    private const HMAC_SECRET = 'x-test-secret-32bytes-long-padding!!';

    private AgentHttpClient&MockObject $agent;

    protected function setUp(): void
    {
        $this->agent = $this->createMock(AgentHttpClient::class);
    }

    /**
     * @param array<string, mixed> $session
     */
    private function controllerWithSession(array $session): SessionDeleteController
    {
        $globals = new OEGlobalsBag([
            'copilot_agent_base_url' => 'http://agent.local:8500',
            'copilot_agent_timeout_seconds' => 5,
            'copilot_jwt_secret' => self::HMAC_SECRET,
        ]);
        $signer = new JwtSigner(
            self::HMAC_SECRET,
            new FrozenClock(new \DateTimeImmutable('2026-04-30T12:00:00Z')),
        );
        return new SessionDeleteController(
            $this->agent,
            $signer,
            new SessionMapper($session),
            new CopilotConfig($globals),
            new NullLogger(),
        );
    }

    public function testHappyPathProxiesAgent204AsEmptyResponse(): void
    {
        $controller = $this->controllerWithSession(['authUserID' => 'dr-patel']);

        $this->agent->expects($this->once())
            ->method('delete')
            ->with(
                '/api/agent/session/abc-123',
                $this->callback(static fn (string $token): bool => $token !== ''),
            )
            ->willReturn(new AgentResponse(Response::HTTP_NO_CONTENT, []));

        $request = self::makeRequest(['patient_id' => '101']);
        $response = $controller->delete($request, 'abc-123');

        self::assertSame(Response::HTTP_NO_CONTENT, $response->getStatusCode());
        self::assertSame('', (string) $response->getContent());
    }

    public function testAgent404PassesThrough(): void
    {
        $controller = $this->controllerWithSession(['authUserID' => 'dr-patel']);

        $this->agent->method('delete')
            ->willReturn(new AgentResponse(
                Response::HTTP_NOT_FOUND,
                ['detail' => 'session not found'],
            ));

        $request = self::makeRequest(['patient_id' => '101']);
        $response = $controller->delete($request, 'abc-123');

        self::assertSame(Response::HTTP_NOT_FOUND, $response->getStatusCode());
    }

    public function testMissingPatientIdReturns400AndDoesNotCallAgent(): void
    {
        $controller = $this->controllerWithSession(['authUserID' => 'dr-patel']);
        $this->agent->expects($this->never())->method('delete');

        $request = self::makeRequest([]);
        $response = $controller->delete($request, 'abc-123');

        self::assertSame(Response::HTTP_BAD_REQUEST, $response->getStatusCode());
    }

    public function testEmptyPatientIdReturns400AndDoesNotCallAgent(): void
    {
        $controller = $this->controllerWithSession(['authUserID' => 'dr-patel']);
        $this->agent->expects($this->never())->method('delete');

        $request = self::makeRequest(['patient_id' => '']);
        $response = $controller->delete($request, 'abc-123');

        self::assertSame(Response::HTTP_BAD_REQUEST, $response->getStatusCode());
    }

    public function testEmptySessionIdReturns400AndDoesNotCallAgent(): void
    {
        $controller = $this->controllerWithSession(['authUserID' => 'dr-patel']);
        $this->agent->expects($this->never())->method('delete');

        $request = self::makeRequest(['patient_id' => '101']);
        $response = $controller->delete($request, '');

        self::assertSame(Response::HTTP_BAD_REQUEST, $response->getStatusCode());
    }

    public function testInvalidSessionIdCharsetReturns400AndDoesNotCallAgent(): void
    {
        $controller = $this->controllerWithSession(['authUserID' => 'dr-patel']);
        $this->agent->expects($this->never())->method('delete');

        $request = self::makeRequest(['patient_id' => '101']);
        $response = $controller->delete($request, 'has spaces!');

        self::assertSame(Response::HTTP_BAD_REQUEST, $response->getStatusCode());
    }

    public function testOversizedSessionIdReturns400AndDoesNotCallAgent(): void
    {
        $controller = $this->controllerWithSession(['authUserID' => 'dr-patel']);
        $this->agent->expects($this->never())->method('delete');

        $request = self::makeRequest(['patient_id' => '101']);
        $response = $controller->delete($request, str_repeat('a', 65));

        self::assertSame(Response::HTTP_BAD_REQUEST, $response->getStatusCode());
    }

    public function testUnauthenticatedSessionReturns400AndDoesNotCallAgent(): void
    {
        // No authUserID → SessionMapper::mapWithPatient raises.
        $controller = $this->controllerWithSession([]);
        $this->agent->expects($this->never())->method('delete');

        $request = self::makeRequest(['patient_id' => '101']);
        $response = $controller->delete($request, 'abc-123');

        self::assertSame(Response::HTTP_BAD_REQUEST, $response->getStatusCode());
    }

    public function testAgentTransportFailureReturns502WithGenericBody(): void
    {
        $controller = $this->controllerWithSession(['authUserID' => 'dr-patel']);

        $this->agent->method('delete')
            ->willThrowException(new AgentServiceException('connection refused'));

        $request = self::makeRequest(['patient_id' => '101']);
        $response = $controller->delete($request, 'abc-123');

        self::assertSame(Response::HTTP_BAD_GATEWAY, $response->getStatusCode());
        self::assertStringNotContainsString('connection refused', (string) $response->getContent());
    }

    /**
     * @param array<string, scalar> $query
     */
    private static function makeRequest(array $query): Request
    {
        // Bake query params into the URI rather than the third-positional
        // ``parameters`` argument — Symfony's ``Request::create`` puts that
        // bag into ``$request->request`` (body parameters) for any method
        // that allows a body (DELETE included), but the controller reads
        // from ``$request->query``. Building the query string in the URI
        // lands the values where the controller looks for them.
        $uri = '/apis/default/api/agent/session/abc-123';
        if ($query !== []) {
            $uri .= '?' . http_build_query($query);
        }
        return Request::create($uri, 'DELETE');
    }
}
