<?php

/**
 * Isolated tests for CopilotConfig.
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
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use OpenEMR\Services\Copilot\Config\CopilotConfigException;
use PHPUnit\Framework\TestCase;

final class CopilotConfigTest extends TestCase
{
    public function testReadsAgentBaseUrlFromGlobals(): void
    {
        $config = new CopilotConfig(new OEGlobalsBag([
            'copilot_agent_base_url' => 'https://agent.example.com',
        ]));

        self::assertSame('https://agent.example.com', $config->getAgentBaseUrl());
    }

    public function testStripsTrailingSlashFromBaseUrl(): void
    {
        // Without this, ``base_url + "/healthz"`` would produce a doubled
        // slash that some HTTP servers normalize and others don't — easier
        // to fix in one place than to hunt down per call site.
        $config = new CopilotConfig(new OEGlobalsBag([
            'copilot_agent_base_url' => 'https://agent.example.com/',
        ]));

        self::assertSame('https://agent.example.com', $config->getAgentBaseUrl());
    }

    public function testFallsBackToLocalDefaultWhenUnset(): void
    {
        $config = new CopilotConfig(new OEGlobalsBag([]));

        self::assertSame('http://localhost:8500', $config->getAgentBaseUrl());
    }

    public function testReadsAgentTimeoutFromGlobals(): void
    {
        $config = new CopilotConfig(new OEGlobalsBag([
            'copilot_agent_timeout_seconds' => 12,
        ]));

        self::assertSame(12, $config->getAgentTimeoutSeconds());
    }

    public function testTimeoutFallsBackToFiveSecondsWhenInvalid(): void
    {
        // Zero or negative timeouts in Guzzle disable the timeout entirely,
        // which would let a hung agent service stall an OpenEMR worker.
        // Defending here means a misconfigured globals row can't open that
        // failure mode silently.
        $config = new CopilotConfig(new OEGlobalsBag([
            'copilot_agent_timeout_seconds' => 0,
        ]));

        self::assertSame(5, $config->getAgentTimeoutSeconds());
    }

    public function testReturnsJwtSecretWhenConfigured(): void
    {
        $secret = str_repeat('a', 64);
        $config = new CopilotConfig(new OEGlobalsBag([
            'copilot_jwt_secret' => $secret,
        ]));

        self::assertSame($secret, $config->getJwtSecret());
    }

    public function testRaisesWhenJwtSecretMissing(): void
    {
        // No silent fallback: the gateway must refuse to mint tokens before
        // it has a real secret. Returning a default would expose a window
        // where every clinic running stock config trusts the same key.
        $config = new CopilotConfig(new OEGlobalsBag([]));

        $this->expectException(CopilotConfigException::class);
        $this->expectExceptionMessage('copilot_jwt_secret');

        $config->getJwtSecret();
    }

    public function testRaisesWhenJwtSecretTooShort(): void
    {
        // 16 bytes still encodes a key, but well below HS256's 256-bit
        // security margin — easier to brute-force a leaked token offline.
        $config = new CopilotConfig(new OEGlobalsBag([
            'copilot_jwt_secret' => 'too-short',
        ]));

        $this->expectException(CopilotConfigException::class);
        $this->expectExceptionMessage('at least 32 bytes');

        $config->getJwtSecret();
    }
}
