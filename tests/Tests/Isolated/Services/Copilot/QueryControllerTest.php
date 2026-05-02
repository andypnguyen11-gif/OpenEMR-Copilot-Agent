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
use OpenEMR\Services\Copilot\Auth\PatientAccessCheckerInterface;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use OpenEMR\Services\Copilot\JwtSigner;
use OpenEMR\Services\Copilot\QueryController;
use OpenEMR\Services\Copilot\SessionMapper;
use PHPUnit\Framework\MockObject\MockObject;
use PHPUnit\Framework\TestCase;
use Psr\Log\NullLogger;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\HttpFoundation\Response;

final class QueryControllerTest extends TestCase
{
    private const HMAC_SECRET = 'x-test-secret-32bytes-long-padding!!';

    private AgentHttpClient&MockObject $agent;

    protected function setUp(): void
    {
        $this->agent = $this->createMock(AgentHttpClient::class);
    }

    /**
     * Build a controller wired with a session bag of the given shape.
     * Session has to be passed at SessionMapper construction time
     * (mapper is readonly), so each test calls this with whatever
     * authUserID / role / scopes setup it needs.
     *
     * @param array<string, mixed>             $session
     * @param PatientAccessCheckerInterface|null $accessChecker Optional gate
     *        — defaults to allow-all so the existing happy-path expectations
     *        keep working. Pass an explicit deny mock to exercise the 403
     *        path.
     */
    private function controllerWithSession(
        array $session,
        ?PatientAccessCheckerInterface $accessChecker = null,
    ): QueryController {
        $globals = new OEGlobalsBag([
            'copilot_agent_base_url' => 'http://agent.local:8500',
            'copilot_agent_timeout_seconds' => 5,
            'copilot_jwt_secret' => self::HMAC_SECRET,
        ]);
        $signer = new JwtSigner(
            self::HMAC_SECRET,
            new FrozenClock(new \DateTimeImmutable('2026-04-30T12:00:00Z')),
        );
        return new QueryController(
            $this->agent,
            $signer,
            new SessionMapper($session),
            $accessChecker ?? self::allowAllAccessChecker(),
            new CopilotConfig($globals),
            new NullLogger(),
        );
    }

    private static function allowAllAccessChecker(): PatientAccessCheckerInterface
    {
        return new class implements PatientAccessCheckerInterface {
            public function canAccess(string $userId, string $patientId): bool
            {
                return true;
            }
        };
    }

    private static function denyAllAccessChecker(): PatientAccessCheckerInterface
    {
        return new class implements PatientAccessCheckerInterface {
            public function canAccess(string $userId, string $patientId): bool
            {
                return false;
            }
        };
    }

    public function testHappyPathProxiesAgentBodyAndStatus(): void
    {
        // Note: no 'pid' in session — the chat surface takes patient_id from
        // the request body, not from chart context.
        $controller = $this->controllerWithSession(['authUserID' => 'dr-patel']);

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

        $response = $controller->query(self::makeRequest([
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
        $controller = $this->controllerWithSession(['authUserID' => 'dr-patel']);
        $this->agent->expects($this->never())->method('post');

        $request = Request::create(
            '/apis/default/api/agent/query',
            'POST',
            content: '{not-json',
        );

        $response = $controller->query($request);

        self::assertSame(Response::HTTP_BAD_REQUEST, $response->getStatusCode());
    }

    public function testMissingPatientIdReturns400AndDoesNotCallAgent(): void
    {
        $controller = $this->controllerWithSession(['authUserID' => 'dr-patel']);
        $this->agent->expects($this->never())->method('post');

        $response = $controller->query(self::makeRequest([
            'query' => 'hello',
        ]));

        self::assertSame(Response::HTTP_BAD_REQUEST, $response->getStatusCode());
    }

    public function testUnauthenticatedSessionReturns400AndDoesNotCallAgent(): void
    {
        // No authUserID — SessionMapper::mapWithPatient raises.
        $controller = $this->controllerWithSession([]);
        $this->agent->expects($this->never())->method('post');

        $response = $controller->query(self::makeRequest([
            'patient_id' => '101',
            'query' => 'hello',
        ]));

        self::assertSame(Response::HTTP_BAD_REQUEST, $response->getStatusCode());
    }

    public function testAgentTransportFailureReturns502WithGenericBody(): void
    {
        $controller = $this->controllerWithSession(['authUserID' => 'dr-patel']);
        $this->agent->method('post')
            ->willThrowException(new AgentServiceException('connection refused'));

        $response = $controller->query(self::makeRequest([
            'patient_id' => '101',
            'query' => 'hello',
        ]));

        self::assertSame(Response::HTTP_BAD_GATEWAY, $response->getStatusCode());
        self::assertStringNotContainsString('connection refused', (string) $response->getContent());
    }

    public function testSessionIdRoundTripsToAgentBody(): void
    {
        $controller = $this->controllerWithSession(['authUserID' => 'dr-patel']);

        $this->agent->expects($this->once())
            ->method('post')
            ->with(
                '/api/agent/query',
                ['query' => 'follow-up', 'session_id' => 'abc-123'],
                $this->callback(static fn (string $token): bool => $token !== ''),
            )
            ->willReturn(new AgentResponse(
                Response::HTTP_OK,
                ['cards' => [], 'prose' => [], 'tool_results' => [], 'session_id' => 'abc-123'],
            ));

        $response = $controller->query(self::makeRequest([
            'patient_id' => '101',
            'query' => 'follow-up',
            'session_id' => 'abc-123',
        ]));

        self::assertSame(Response::HTTP_OK, $response->getStatusCode());
    }

    public function testSessionIdOmittedFromAgentBodyWhenNotProvided(): void
    {
        $controller = $this->controllerWithSession(['authUserID' => 'dr-patel']);

        // Agent body must NOT carry session_id when client didn't supply
        // one — agent-side QueryRequest treats absent and explicit-null
        // the same, but staying minimal on the wire keeps test failures
        // sharper.
        $this->agent->expects($this->once())
            ->method('post')
            ->with(
                '/api/agent/query',
                ['query' => 'first turn'],
                $this->callback(static fn (string $token): bool => $token !== ''),
            )
            ->willReturn(new AgentResponse(Response::HTTP_OK, ['session_id' => 'srv-1']));

        $response = $controller->query(self::makeRequest([
            'patient_id' => '101',
            'query' => 'first turn',
        ]));

        self::assertSame(Response::HTTP_OK, $response->getStatusCode());
    }

    public function testInvalidSessionIdCharacterReturns400AndDoesNotCallAgent(): void
    {
        $controller = $this->controllerWithSession(['authUserID' => 'dr-patel']);
        $this->agent->expects($this->never())->method('post');

        $response = $controller->query(self::makeRequest([
            'patient_id' => '101',
            'query' => 'hello',
            'session_id' => 'not allowed!',
        ]));

        self::assertSame(Response::HTTP_BAD_REQUEST, $response->getStatusCode());
    }

    public function testOversizedSessionIdReturns400AndDoesNotCallAgent(): void
    {
        $controller = $this->controllerWithSession(['authUserID' => 'dr-patel']);
        $this->agent->expects($this->never())->method('post');

        $response = $controller->query(self::makeRequest([
            'patient_id' => '101',
            'query' => 'hello',
            'session_id' => str_repeat('a', 65),  // 1 over the 64-char cap
        ]));

        self::assertSame(Response::HTTP_BAD_REQUEST, $response->getStatusCode());
    }

    public function testNonStringSessionIdReturns400AndDoesNotCallAgent(): void
    {
        $controller = $this->controllerWithSession(['authUserID' => 'dr-patel']);
        $this->agent->expects($this->never())->method('post');

        $response = $controller->query(self::makeRequest([
            'patient_id' => '101',
            'query' => 'hello',
            'session_id' => 12345,  // not a string
        ]));

        self::assertSame(Response::HTTP_BAD_REQUEST, $response->getStatusCode());
    }

    public function testDeniedPatientAccessReturns403AndDoesNotCallAgent(): void
    {
        // Authenticated session, well-formed body — but the access checker
        // says this clinician is not authorised for the requested patient.
        // The gateway must refuse before any JWT is minted; otherwise the
        // signed claim would carry the foreign patient_id straight to the
        // agent, which only checks request==claim.
        $controller = $this->controllerWithSession(
            ['authUserID' => 'dr-patel'],
            self::denyAllAccessChecker(),
        );
        $this->agent->expects($this->never())->method('post');

        $response = $controller->query(self::makeRequest([
            'patient_id' => '999',  // pretend this is another clinician's panel
            'query' => 'show me their meds',
        ]));

        self::assertSame(Response::HTTP_FORBIDDEN, $response->getStatusCode());
        self::assertSame(
            ['error' => 'patient_access_denied'],
            json_decode((string) $response->getContent(), true),
        );
    }

    public function testAccessCheckerReceivesSessionUserAndBodyPatient(): void
    {
        // Pin the (user_id, patient_id) pair the controller hands the gate:
        // user_id is the session's authUserID, patient_id is the body's
        // patient_id verbatim. A regression where the controller passed,
        // say, a stale chart pid would silently broaden access.
        $checker = $this->createMock(PatientAccessCheckerInterface::class);
        $checker->expects($this->once())
            ->method('canAccess')
            ->with('42', '101')
            ->willReturn(true);
        $controller = $this->controllerWithSession(['authUserID' => '42'], $checker);
        $this->agent->method('post')->willReturn(new AgentResponse(Response::HTTP_OK, []));

        $controller->query(self::makeRequest([
            'patient_id' => '101',
            'query' => 'hello',
        ]));
    }

    public function testAgentNon2xxStatusPassesThrough(): void
    {
        $controller = $this->controllerWithSession(['authUserID' => 'dr-patel']);
        // Agent returned an UNAUTHORIZED-state structured response with a
        // non-2xx status — the gateway must surface the agent's view rather
        // than coerce to OK.
        $this->agent->method('post')
            ->willReturn(new AgentResponse(
                Response::HTTP_UNAUTHORIZED,
                ['error' => 'invalid token'],
            ));

        $response = $controller->query(self::makeRequest([
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
