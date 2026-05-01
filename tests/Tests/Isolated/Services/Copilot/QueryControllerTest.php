<?php

/**
 * Isolated tests for :class:`QueryController`.
 *
 * Five contract points pinned here:
 *
 * 1. Happy path — body decoded into typed :class:`QueryRequest`,
 *    :class:`SessionMapper::mapWithPatient` invoked with the body's
 *    patient_id and the standard MVP scope set, JWT minted, the agent
 *    is hit with ``Authorization: Bearer <token>``, and the agent's
 *    body + status code pass through verbatim.
 * 2. Bad request body (malformed JSON, missing fields) → 400, no agent
 *    call.
 * 3. Unauthenticated session → 400, no agent call.
 * 4. Agent transport failure → 502 with a generic body.
 * 5. Agent non-2xx status code passes through unchanged.
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
use OpenEMR\Services\Copilot\QueryController;
use OpenEMR\Services\Copilot\SessionMapper;
use PHPUnit\Framework\MockObject\MockObject;
use PHPUnit\Framework\TestCase;
use Psr\Log\NullLogger;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\HttpFoundation\Response;
use Symfony\Component\HttpFoundation\Session\Session;
use Symfony\Component\HttpFoundation\Session\SessionInterface;
use Symfony\Component\HttpFoundation\Session\Storage\MockArraySessionStorage;

final class QueryControllerTest extends TestCase
{
    private const HMAC_SECRET = 'x-test-secret-32bytes-long-padding!!';

    private AgentHttpClient&MockObject $agent;
    private QueryController $controller;
    private SessionInterface $session;

    protected function setUp(): void
    {
        $globals = new OEGlobalsBag([
            'copilot_agent_base_url' => 'http://agent.local:8500',
            'copilot_agent_timeout_seconds' => 5,
            'copilot_jwt_secret' => self::HMAC_SECRET,
        ]);
        $config = new CopilotConfig($globals);
        $signer = new JwtSigner(
            self::HMAC_SECRET,
            new FrozenClock(new \DateTimeImmutable('2026-04-30T12:00:00Z')),
        );

        $this->session = new Session(new MockArraySessionStorage());
        $this->agent = $this->createMock(AgentHttpClient::class);
        $this->controller = new QueryController(
            $this->agent,
            $signer,
            new SessionMapper($this->session),
            $config,
            new NullLogger(),
        );
    }

    public function testHappyPathProxiesAgentBodyAndStatus(): void
    {
        $this->session->set('authUserID', 'dr-patel');
        // Note: no 'pid' in session — the chat surface takes patient_id from
        // the request body, not from chart context.

        $this->agent->expects($this->once())
            ->method('post')
            ->with(
                '/api/agent/query',
                ['query' => 'What problems does this patient have?'],
                $this->callback(static fn (string $token): bool => $token !== ''),
            )
            ->willReturn(new AgentResponse(
                Response::HTTP_OK,
                ['cards' => [], 'prose' => [], 'tool_results' => [], 'abstention' => null],
            ));

        $response = $this->controller->query(self::makeRequest([
            'patient_id' => '101',
            'query' => 'What problems does this patient have?',
        ]));

        self::assertSame(Response::HTTP_OK, $response->getStatusCode());
        $body = json_decode((string) $response->getContent(), true);
        self::assertSame(
            ['cards' => [], 'prose' => [], 'tool_results' => [], 'abstention' => null],
            $body,
        );
    }

    public function testBadJsonBodyReturns400AndDoesNotCallAgent(): void
    {
        $this->session->set('authUserID', 'dr-patel');
        $this->agent->expects($this->never())->method('post');

        $request = Request::create(
            '/apis/default/api/agent/query',
            'POST',
            content: '{not-json',
        );

        $response = $this->controller->query($request);

        self::assertSame(Response::HTTP_BAD_REQUEST, $response->getStatusCode());
    }

    public function testMissingPatientIdReturns400AndDoesNotCallAgent(): void
    {
        $this->session->set('authUserID', 'dr-patel');
        $this->agent->expects($this->never())->method('post');

        $response = $this->controller->query(self::makeRequest([
            'query' => 'hello',
        ]));

        self::assertSame(Response::HTTP_BAD_REQUEST, $response->getStatusCode());
    }

    public function testUnauthenticatedSessionReturns400AndDoesNotCallAgent(): void
    {
        // No authUserID — SessionMapper::mapWithPatient raises.
        $this->agent->expects($this->never())->method('post');

        $response = $this->controller->query(self::makeRequest([
            'patient_id' => '101',
            'query' => 'hello',
        ]));

        self::assertSame(Response::HTTP_BAD_REQUEST, $response->getStatusCode());
    }

    public function testAgentTransportFailureReturns502WithGenericBody(): void
    {
        $this->session->set('authUserID', 'dr-patel');
        $this->agent->method('post')
            ->willThrowException(new AgentServiceException('connection refused'));

        $response = $this->controller->query(self::makeRequest([
            'patient_id' => '101',
            'query' => 'hello',
        ]));

        self::assertSame(Response::HTTP_BAD_GATEWAY, $response->getStatusCode());
        self::assertStringNotContainsString('connection refused', (string) $response->getContent());
    }

    public function testAgentNon2xxStatusPassesThrough(): void
    {
        $this->session->set('authUserID', 'dr-patel');
        // Agent returned an UNAUTHORIZED-state structured response with a
        // non-2xx status — the gateway must surface the agent's view rather
        // than coerce to OK.
        $this->agent->method('post')
            ->willReturn(new AgentResponse(
                Response::HTTP_UNAUTHORIZED,
                ['error' => 'invalid token'],
            ));

        $response = $this->controller->query(self::makeRequest([
            'patient_id' => '101',
            'query' => 'hello',
        ]));

        self::assertSame(Response::HTTP_UNAUTHORIZED, $response->getStatusCode());
        self::assertSame(
            ['error' => 'invalid token'],
            json_decode((string) $response->getContent(), true),
        );
    }

    /**
     * @param array<string, mixed> $body
     */
    private static function makeRequest(array $body): Request
    {
        $json = json_encode($body, JSON_THROW_ON_ERROR);
        return Request::create(
            '/apis/default/api/agent/query',
            'POST',
            server: ['CONTENT_TYPE' => 'application/json'],
            content: $json,
        );
    }
}
