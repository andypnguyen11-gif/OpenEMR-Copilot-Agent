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
