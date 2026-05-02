<?php

/**
 * Isolated tests for AgentHttpClient.
 *
 * The HTTP wrapper handles three small but high-value behaviors: URL
 * composition (base URL + path), JSON decoding, and transport-error
 * translation to :class:`AgentServiceException`. These tests pin each
 * behavior by mocking the PSR-18 client.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot;

use GuzzleHttp\Psr7\HttpFactory;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\AgentServiceException;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use PHPUnit\Framework\MockObject\MockObject;
use PHPUnit\Framework\TestCase;
use Psr\Http\Client\ClientExceptionInterface;
use Psr\Http\Client\ClientInterface;
use Psr\Http\Message\RequestInterface;
use Psr\Http\Message\ResponseInterface;
use Psr\Http\Message\StreamInterface;

final class AgentHttpClientTest extends TestCase
{
    private ClientInterface&MockObject $httpClient;
    private CopilotConfig $config;
    private AgentHttpClient $client;

    protected function setUp(): void
    {
        $this->httpClient = $this->createMock(ClientInterface::class);
        $globals = new OEGlobalsBag(['copilot_agent_base_url' => 'http://agent.local:8500']);
        $this->config = new CopilotConfig($globals);
        $this->client = new AgentHttpClient($this->httpClient, new HttpFactory(), $this->config);
    }

    public function testGetComposesUrlAndDecodesJsonBody(): void
    {
        $this->httpClient->expects($this->once())
            ->method('sendRequest')
            ->with($this->callback(static function (RequestInterface $request): bool {
                self::assertSame('GET', $request->getMethod());
                self::assertSame('http://agent.local:8500/healthz', (string) $request->getUri());
                self::assertSame(['application/json'], $request->getHeader('Accept'));
                return true;
            }))
            ->willReturn($this->stubResponse(200, '{"status":"ok"}'));

        $response = $this->client->get('/healthz');

        self::assertSame(200, $response->statusCode);
        self::assertSame(['status' => 'ok'], $response->body);
    }

    public function testGetTreatsEmptyBodyAsEmptyArray(): void
    {
        $this->httpClient->method('sendRequest')
            ->willReturn($this->stubResponse(204, ''));

        $response = $this->client->get('/something');

        self::assertSame(204, $response->statusCode);
        self::assertSame([], $response->body);
    }

    public function testGetRejectsRelativePath(): void
    {
        $this->expectException(AgentServiceException::class);
        $this->expectExceptionMessage('agent path must start with /');
        $this->client->get('healthz');
    }

    public function testGetWrapsTransportFailures(): void
    {
        $transportError = new class extends \RuntimeException implements ClientExceptionInterface {};
        $this->httpClient->method('sendRequest')->willThrowException($transportError);

        $this->expectException(AgentServiceException::class);
        $this->client->get('/healthz');
    }

    public function testGetRejectsInvalidJsonBody(): void
    {
        $this->httpClient->method('sendRequest')
            ->willReturn($this->stubResponse(200, '{not-json'));

        $this->expectException(AgentServiceException::class);
        $this->client->get('/healthz');
    }

    public function testGetRejectsNonObjectJsonBody(): void
    {
        // The agent contract is JSON objects; arrays or scalars at the top
        // level mean the response shape diverged from what the gateway
        // expects, which is a bug we want to surface, not silently coerce.
        $this->httpClient->method('sendRequest')
            ->willReturn($this->stubResponse(200, '"plain string"'));

        $this->expectException(AgentServiceException::class);
        $this->client->get('/healthz');
    }

    public function testPostSendsJsonBodyAndBearerHeader(): void
    {
        $this->httpClient->expects($this->once())
            ->method('sendRequest')
            ->with($this->callback(static function (RequestInterface $request): bool {
                self::assertSame('POST', $request->getMethod());
                self::assertSame(
                    'http://agent.local:8500/api/agent/query',
                    (string) $request->getUri(),
                );
                self::assertSame(['application/json'], $request->getHeader('Content-Type'));
                self::assertSame(['application/json'], $request->getHeader('Accept'));
                self::assertSame(['Bearer test-token-abc'], $request->getHeader('Authorization'));
                self::assertSame(
                    '{"query":"hello"}',
                    (string) $request->getBody(),
                );
                return true;
            }))
            ->willReturn($this->stubResponse(200, '{"ok":true}'));

        $response = $this->client->post('/api/agent/query', ['query' => 'hello'], 'test-token-abc');

        self::assertSame(200, $response->statusCode);
        self::assertSame(['ok' => true], $response->body);
    }

    public function testPostRejectsRelativePath(): void
    {
        $this->expectException(AgentServiceException::class);
        $this->client->post('api/agent/query', ['query' => 'hi'], 'token');
    }

    public function testPostRejectsEmptyBearerToken(): void
    {
        // Without a bearer token the agent's verifier returns 401; failing
        // here is faster and produces a more attributable log line.
        $this->expectException(AgentServiceException::class);
        $this->client->post('/api/agent/query', ['query' => 'hi'], '');
    }

    public function testPostWrapsTransportFailures(): void
    {
        $transportError = new class extends \RuntimeException implements ClientExceptionInterface {};
        $this->httpClient->method('sendRequest')->willThrowException($transportError);

        $this->expectException(AgentServiceException::class);
        $this->client->post('/api/agent/query', ['query' => 'hi'], 'token');
    }

    public function testPostPropagatesNon2xxStatusFromAgent(): void
    {
        // The agent's structured error responses (4xx/5xx) must round-trip
        // verbatim to the controller — the controller decides whether to
        // surface them or wrap them.
        $this->httpClient->method('sendRequest')
            ->willReturn($this->stubResponse(401, '{"error":"invalid token"}'));

        $response = $this->client->post('/api/agent/query', ['query' => 'hi'], 'token');

        self::assertSame(401, $response->statusCode);
        self::assertSame(['error' => 'invalid token'], $response->body);
    }

    public function testPostInternalSendsJsonBodyAndInternalTokenHeader(): void
    {
        $this->httpClient->expects($this->once())
            ->method('sendRequest')
            ->with($this->callback(static function (RequestInterface $request): bool {
                self::assertSame('POST', $request->getMethod());
                self::assertSame(
                    'http://agent.local:8500/api/agent/internal/warm',
                    (string) $request->getUri(),
                );
                self::assertSame(['application/json'], $request->getHeader('Content-Type'));
                self::assertSame(['application/json'], $request->getHeader('Accept'));
                self::assertSame(['internal-token-xyz'], $request->getHeader('X-Internal-Token'));
                // Confirm Authorization header is *not* set — the internal
                // route must not be reachable via a bearer mis-default.
                self::assertSame([], $request->getHeader('Authorization'));
                self::assertSame(
                    '{"patient_ids":["101","102"]}',
                    (string) $request->getBody(),
                );
                return true;
            }))
            ->willReturn($this->stubResponse(200, '{"warmed":2,"failed":[]}'));

        $response = $this->client->postInternal(
            '/api/agent/internal/warm',
            ['patient_ids' => ['101', '102']],
            'internal-token-xyz',
        );

        self::assertSame(200, $response->statusCode);
        self::assertSame(['warmed' => 2, 'failed' => []], $response->body);
    }

    public function testPostInternalRejectsRelativePath(): void
    {
        $this->expectException(AgentServiceException::class);
        $this->client->postInternal('api/agent/internal/warm', ['patient_ids' => ['1']], 't');
    }

    public function testPostInternalRejectsEmptyToken(): void
    {
        $this->expectException(AgentServiceException::class);
        $this->client->postInternal('/api/agent/internal/warm', ['patient_ids' => ['1']], '');
    }

    public function testPostInternalWrapsTransportFailures(): void
    {
        $transportError = new class extends \RuntimeException implements ClientExceptionInterface {};
        $this->httpClient->method('sendRequest')->willThrowException($transportError);

        $this->expectException(AgentServiceException::class);
        $this->client->postInternal('/api/agent/internal/warm', ['patient_ids' => ['1']], 'token');
    }

    public function testPostInternalPropagatesNon2xxStatusFromAgent(): void
    {
        // 401 from the agent's internal-token guard must round-trip
        // verbatim — the dispatcher decides whether to retry / log /
        // fall through to TTL freshness.
        $this->httpClient->method('sendRequest')
            ->willReturn($this->stubResponse(401, '{"detail":"invalid token"}'));

        $response = $this->client->postInternal(
            '/api/agent/internal/warm',
            ['patient_ids' => ['1']],
            'wrong-token',
        );

        self::assertSame(401, $response->statusCode);
        self::assertSame(['detail' => 'invalid token'], $response->body);
    }

    public function testGetInternalSendsInternalTokenHeaderAndNoAuthorization(): void
    {
        $this->httpClient->expects($this->once())
            ->method('sendRequest')
            ->with($this->callback(static function (RequestInterface $request): bool {
                self::assertSame('GET', $request->getMethod());
                self::assertSame(
                    'http://agent.local:8500/api/agent/internal/flags/90001',
                    (string) $request->getUri(),
                );
                self::assertSame(['application/json'], $request->getHeader('Accept'));
                self::assertSame(['internal-token-xyz'], $request->getHeader('X-Internal-Token'));
                // Same threat-model isolation as postInternal: the
                // user-bearer header must not be set on the internal
                // route, even by accident.
                self::assertSame([], $request->getHeader('Authorization'));
                self::assertSame('', (string) $request->getBody());
                return true;
            }))
            ->willReturn($this->stubResponse(200, '{"patient_id":"90001","flags":[]}'));

        $response = $this->client->getInternal(
            '/api/agent/internal/flags/90001',
            'internal-token-xyz',
        );

        self::assertSame(200, $response->statusCode);
        self::assertSame(['patient_id' => '90001', 'flags' => []], $response->body);
    }

    public function testGetInternalRejectsRelativePath(): void
    {
        $this->expectException(AgentServiceException::class);
        $this->client->getInternal('api/agent/internal/flags/1', 'token');
    }

    public function testGetInternalRejectsEmptyToken(): void
    {
        // Without the token the agent returns 401; failing here is
        // faster and produces a more attributable log line.
        $this->expectException(AgentServiceException::class);
        $this->client->getInternal('/api/agent/internal/flags/1', '');
    }

    public function testGetInternalWrapsTransportFailures(): void
    {
        $transportError = new class extends \RuntimeException implements ClientExceptionInterface {};
        $this->httpClient->method('sendRequest')->willThrowException($transportError);

        $this->expectException(AgentServiceException::class);
        $this->client->getInternal('/api/agent/internal/flags/1', 'token');
    }

    public function testGetInternalRejectsInvalidJsonBody(): void
    {
        $this->httpClient->method('sendRequest')
            ->willReturn($this->stubResponse(200, '{not-json'));

        $this->expectException(AgentServiceException::class);
        $this->client->getInternal('/api/agent/internal/flags/1', 'token');
    }

    public function testGetInternalPropagatesNon2xxStatusFromAgent(): void
    {
        $this->httpClient->method('sendRequest')
            ->willReturn($this->stubResponse(500, '{"detail":"engine boom"}'));

        $response = $this->client->getInternal('/api/agent/internal/flags/1', 'token');

        self::assertSame(500, $response->statusCode);
        self::assertSame(['detail' => 'engine boom'], $response->body);
    }

    private function stubResponse(int $status, string $body): ResponseInterface
    {
        $stream = $this->createMock(StreamInterface::class);
        $stream->method('__toString')->willReturn($body);

        $response = $this->createMock(ResponseInterface::class);
        $response->method('getStatusCode')->willReturn($status);
        $response->method('getBody')->willReturn($stream);
        return $response;
    }
}
