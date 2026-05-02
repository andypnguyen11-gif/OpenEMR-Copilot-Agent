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
    /** @var list<string> */
    private const MANAGED_ENV_VARS = [
        'COPILOT_AGENT_BASE_URL',
        'COPILOT_AGENT_TIMEOUT_SECONDS',
        'COPILOT_JWT_SECRET',
        'COPILOT_INTERNAL_TOKEN',
        'COPILOT_INTERNAL_TIMEOUT_SECONDS',
    ];

    protected function setUp(): void
    {
        // putenv() persists for the lifetime of the process, so a test that
        // sets an env var would silently bleed into the next one. Wipe the
        // copilot env namespace at the start of every case.
        foreach (self::MANAGED_ENV_VARS as $name) {
            putenv($name);
        }
    }

    protected function tearDown(): void
    {
        foreach (self::MANAGED_ENV_VARS as $name) {
            putenv($name);
        }
    }

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

    public function testEnvAgentBaseUrlOverridesGlobals(): void
    {
        // Env vars are how Railway/Docker deployments configure the gateway.
        // When both are set, the env var wins so an operator can roll out a
        // new agent URL without editing sites/default/config.php in the
        // running container.
        putenv('COPILOT_AGENT_BASE_URL=https://prod.example.com');
        $config = new CopilotConfig(new OEGlobalsBag([
            'copilot_agent_base_url' => 'https://stale-globals.example.com',
        ]));

        self::assertSame('https://prod.example.com', $config->getAgentBaseUrl());
    }

    public function testEnvAgentBaseUrlStripsTrailingSlash(): void
    {
        putenv('COPILOT_AGENT_BASE_URL=https://prod.example.com/');
        $config = new CopilotConfig(new OEGlobalsBag([]));

        self::assertSame('https://prod.example.com', $config->getAgentBaseUrl());
    }

    public function testEmptyEnvAgentBaseUrlFallsThroughToGlobals(): void
    {
        // putenv("FOO=") on some platforms leaves an empty value visible to
        // getenv(); treating that as "set" would shadow a perfectly good
        // globals value. Falling through is the right call.
        putenv('COPILOT_AGENT_BASE_URL=');
        $config = new CopilotConfig(new OEGlobalsBag([
            'copilot_agent_base_url' => 'https://from-globals.example.com',
        ]));

        self::assertSame('https://from-globals.example.com', $config->getAgentBaseUrl());
    }

    public function testEnvJwtSecretOverridesGlobals(): void
    {
        $envSecret = str_repeat('e', 64);
        $globalsSecret = str_repeat('g', 64);
        putenv('COPILOT_JWT_SECRET=' . $envSecret);
        $config = new CopilotConfig(new OEGlobalsBag([
            'copilot_jwt_secret' => $globalsSecret,
        ]));

        self::assertSame($envSecret, $config->getJwtSecret());
    }

    public function testEnvJwtSecretStillEnforcesMinimumLength(): void
    {
        // The 32-byte minimum is the only guard against an operator setting
        // a weak secret via env. It must trigger regardless of source.
        putenv('COPILOT_JWT_SECRET=too-short-env');
        $config = new CopilotConfig(new OEGlobalsBag([]));

        $this->expectException(CopilotConfigException::class);
        $this->expectExceptionMessage('at least 32 bytes');

        $config->getJwtSecret();
    }

    public function testEnvAgentTimeoutOverridesGlobals(): void
    {
        putenv('COPILOT_AGENT_TIMEOUT_SECONDS=20');
        $config = new CopilotConfig(new OEGlobalsBag([
            'copilot_agent_timeout_seconds' => 5,
        ]));

        self::assertSame(20, $config->getAgentTimeoutSeconds());
    }

    public function testNonNumericEnvAgentTimeoutFallsThroughToGlobals(): void
    {
        // Anything getenv returns is a string. Guard against an operator
        // typo like ``COPILOT_AGENT_TIMEOUT_SECONDS=fast`` silently zeroing
        // the timeout — fall through to globals (or the 5s default).
        putenv('COPILOT_AGENT_TIMEOUT_SECONDS=fast');
        $config = new CopilotConfig(new OEGlobalsBag([
            'copilot_agent_timeout_seconds' => 12,
        ]));

        self::assertSame(12, $config->getAgentTimeoutSeconds());
    }

    public function testReturnsInternalTokenWhenConfigured(): void
    {
        $token = str_repeat('a', 64);
        $config = new CopilotConfig(new OEGlobalsBag([
            'copilot_internal_token' => $token,
        ]));

        self::assertSame($token, $config->getInternalToken());
    }

    public function testRaisesWhenInternalTokenMissing(): void
    {
        // Same fail-loud posture as the JWT secret: the dispatcher
        // refuses to mint requests against the internal routes before
        // it has a real token, so a wiring oversight surfaces at the
        // first invalidate / warm rather than as a silent 401 chain.
        $config = new CopilotConfig(new OEGlobalsBag([]));

        $this->expectException(CopilotConfigException::class);
        $this->expectExceptionMessage('copilot_internal_token');

        $config->getInternalToken();
    }

    public function testRaisesWhenInternalTokenTooShort(): void
    {
        $config = new CopilotConfig(new OEGlobalsBag([
            'copilot_internal_token' => 'too-short',
        ]));

        $this->expectException(CopilotConfigException::class);
        $this->expectExceptionMessage('at least 32 bytes');

        $config->getInternalToken();
    }

    public function testEnvInternalTokenOverridesGlobals(): void
    {
        $envToken = str_repeat('e', 64);
        $globalsToken = str_repeat('g', 64);
        putenv('COPILOT_INTERNAL_TOKEN=' . $envToken);
        $config = new CopilotConfig(new OEGlobalsBag([
            'copilot_internal_token' => $globalsToken,
        ]));

        self::assertSame($envToken, $config->getInternalToken());
    }

    public function testInternalTimeoutFallsBackToThreeSecondsWhenInvalid(): void
    {
        // Same defensive default as getAgentTimeoutSeconds — zero or
        // negative would disable the timeout in Guzzle, which on an
        // invalidate hook would block the clinical write that triggered
        // it.
        $config = new CopilotConfig(new OEGlobalsBag([
            'copilot_internal_timeout_seconds' => -1,
        ]));

        self::assertSame(3, $config->getInternalTimeoutSeconds());
    }

    public function testEnvInternalTimeoutOverridesGlobals(): void
    {
        putenv('COPILOT_INTERNAL_TIMEOUT_SECONDS=8');
        $config = new CopilotConfig(new OEGlobalsBag([
            'copilot_internal_timeout_seconds' => 3,
        ]));

        self::assertSame(8, $config->getInternalTimeoutSeconds());
    }

    public function testStandardScopesContainsTheMvpReadSurface(): void
    {
        // The standard scope set is the MVP fallback when the session has
        // none. Pinning it here means a future change that drops or
        // renames a scope is loud — those names are also baked into the
        // agent service's tool layer (Tool.required_scope) and the M5
        // eval suite expects every tool to have a corresponding scope
        // grant in the JWT.
        $config = new CopilotConfig(new OEGlobalsBag([]));

        $scopes = $config->getStandardScopes();

        self::assertContains('system/Condition.read', $scopes);
        self::assertContains('system/MedicationRequest.read', $scopes);
        self::assertContains('system/AllergyIntolerance.read', $scopes);
        self::assertContains('system/Observation.read', $scopes);
        self::assertContains('system/Encounter.read', $scopes);
        self::assertContains('system/DocumentReference.read', $scopes);
    }
}
