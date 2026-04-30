<?php

/**
 * Isolated tests for :class:`SessionMapper`.
 *
 * The mapper is the *one* place in the Co-Pilot gateway that is allowed to
 * read ``$_SESSION`` directly (CLAUDE.md: superglobal reads must be confined
 * to the outermost entry point and parsed into typed objects immediately).
 * These tests pin that boundary discipline: required keys map cleanly into
 * a :class:`ClinicianIdentity`, missing keys raise rather than silently
 * defaulting, and the per-request nonce generator returns a fresh,
 * URL-safe value on every call.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot;

use OpenEMR\Services\Copilot\SessionMapper;
use PHPUnit\Framework\TestCase;
use RuntimeException;

final class SessionMapperTest extends TestCase
{
    /** @var array<array-key, mixed> */
    private array $savedSession = [];

    protected function setUp(): void
    {
        // Snapshot whatever else might be sitting in $_SESSION (PHPUnit
        // shares process state across tests in this suite) so we don't
        // pollute neighboring tests with our test fixtures.
        /** @var array<array-key, mixed> $existing */
        $existing = $_SESSION ?? [];
        $this->savedSession = $existing;
        $_SESSION = [];
    }

    protected function tearDown(): void
    {
        $_SESSION = $this->savedSession;
    }

    public function testMapsAuthenticatedSessionIntoClinicianIdentity(): void
    {
        $_SESSION['authUserID'] = 42;
        $_SESSION['pid'] = 7;
        $_SESSION['copilot_role'] = 'physician';
        $_SESSION['copilot_scopes'] = ['patient/Patient.read', 'patient/Condition.read'];

        $identity = (new SessionMapper())->map();

        self::assertSame('42', $identity->userId);
        self::assertSame('7', $identity->patientId);
        self::assertSame('physician', $identity->role);
        self::assertSame(
            ['patient/Patient.read', 'patient/Condition.read'],
            $identity->scopes,
        );
    }

    public function testCoercesNumericSessionValuesToStrings(): void
    {
        // OpenEMR stores authUserID and pid as ints in legacy code paths; the
        // JWT claim must be a string so the Python verifier can use it as a
        // map key without surprise type coercion. Doing the cast here keeps
        // the rest of the gateway from having to think about it.
        $_SESSION['authUserID'] = 42;
        $_SESSION['pid'] = 7;
        $_SESSION['copilot_role'] = 'physician';

        $identity = (new SessionMapper())->map();

        self::assertSame('42', $identity->userId);
        self::assertSame('7', $identity->patientId);
    }

    public function testRoleDefaultsToUnknownUntilPr18WiresIt(): void
    {
        // PR 18 in TASKS.md replaces this default with a real role lookup.
        // Until then, an unset role must not crash the gateway — but the
        // Python tool layer treats "unknown" as having no scopes, so the
        // request will still be denied at the tool boundary. The default
        // exists only so health-check style flows can sign a token before
        // the role plumbing lands.
        $_SESSION['authUserID'] = 42;
        $_SESSION['pid'] = 7;

        $identity = (new SessionMapper())->map();

        self::assertSame('unknown', $identity->role);
        self::assertSame([], $identity->scopes);
    }

    public function testRaisesWhenAuthUserIDMissing(): void
    {
        // No authUserID means the request is unauthenticated — the gateway
        // must refuse to mint a token rather than emit one with a blank
        // user_id, which would land in an audit-log row that no one can
        // attribute later.
        $_SESSION['pid'] = 7;

        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessage('authenticated');

        (new SessionMapper())->map();
    }

    public function testRaisesWhenPatientNotInContext(): void
    {
        // Co-Pilot routes are per-patient; without a chart in context the
        // gateway has nothing meaningful to authorize against. Better to
        // raise here than let the agent service receive an empty
        // patient_id and apply some fallback policy.
        $_SESSION['authUserID'] = 42;
        $_SESSION['copilot_role'] = 'physician';

        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessage('patient context');

        (new SessionMapper())->map();
    }

    public function testGenerateNonceReturnsUrlSafeHex(): void
    {
        // Hex keeps the nonce safe to drop into a JSON claim without
        // additional encoding. 32 hex chars == 16 bytes of entropy, which
        // matches the agent-service replay-store key budget.
        $nonce = (new SessionMapper())->generateNonce();

        self::assertMatchesRegularExpression('/^[0-9a-f]{32}$/', $nonce);
    }

    public function testGenerateNonceReturnsFreshValuePerCall(): void
    {
        // The whole point of the nonce is per-request uniqueness — two
        // sequential calls must not return the same value. Birthday-paradox
        // collision in 16 bytes of CSPRNG output is effectively zero, so
        // any equal pair here is a real bug, not flake.
        $mapper = new SessionMapper();

        self::assertNotSame(
            $mapper->generateNonce(),
            $mapper->generateNonce(),
        );
    }

    public function testCoercesScalarSessionRoleAndPidIndependently(): void
    {
        // Belt-and-suspenders: if a future legacy code path stuffs a
        // boolean or float into pid (it shouldn't, but $_SESSION is
        // unconstrained), the mapper must not blow up — it should reduce
        // the value to a non-empty string and let the agent service's
        // tool layer reject the resulting bogus patient_id.
        $_SESSION['authUserID'] = 'doc-1';
        $_SESSION['pid'] = 3.14;
        $_SESSION['copilot_role'] = 'physician';

        $identity = (new SessionMapper())->map();

        self::assertSame('doc-1', $identity->userId);
        self::assertSame('3.14', $identity->patientId);
    }

    public function testMapWithPatientUsesBodyPatientIdAndFallbackScopes(): void
    {
        // The chat surface takes patient_id from the request body and
        // grants the standard MVP scope set when the session has none.
        $_SESSION['authUserID'] = 'dr-patel';
        // No copilot_scopes, no copilot_role, no pid — all of which the
        // mapWithPatient path is designed to tolerate.
        $fallback = ['system/Condition.read', 'system/Observation.read'];

        $identity = (new SessionMapper())->mapWithPatient('101', $fallback);

        self::assertSame('dr-patel', $identity->userId);
        self::assertSame('101', $identity->patientId);
        self::assertSame('physician', $identity->role);
        self::assertSame($fallback, $identity->scopes);
    }

    public function testMapWithPatientPrefersSessionScopesOverFallback(): void
    {
        // PR 18 wires per-role scope assignment into the session; if the
        // session already has a scope list we must honor it rather than
        // silently overwriting with the MVP fallback.
        $_SESSION['authUserID'] = 'dr-patel';
        $_SESSION['copilot_scopes'] = ['custom/Scope.read'];
        $_SESSION['copilot_role'] = 'resident';

        $identity = (new SessionMapper())->mapWithPatient(
            '101',
            ['system/Condition.read'],
        );

        self::assertSame('resident', $identity->role);
        self::assertSame(['custom/Scope.read'], $identity->scopes);
    }

    public function testMapWithPatientRequiresAuthenticatedSession(): void
    {
        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessage('authenticated');

        (new SessionMapper())->mapWithPatient('101', []);
    }

    public function testMapWithPatientRequiresNonEmptyPatientId(): void
    {
        $_SESSION['authUserID'] = 'dr-patel';

        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessage('patient_id');

        (new SessionMapper())->mapWithPatient('', []);
    }
}
