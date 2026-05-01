<?php

/**
 * Isolated tests for :class:`SessionMapper`.
 *
 * The mapper is the *one* place in the Co-Pilot gateway that handles
 * session data (CLAUDE.md: superglobal reads must be confined to the
 * outermost entry point and parsed into typed objects immediately).
 * These tests pin that boundary discipline: required keys map cleanly
 * into a :class:`ClinicianIdentity`, missing keys raise rather than
 * silently defaulting, and the per-request nonce generator returns a
 * fresh, URL-safe value on every call.
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
    public function testMapsAuthenticatedSessionIntoClinicianIdentity(): void
    {
        $identity = (new SessionMapper([
            'authUserID' => 42,
            'pid' => 7,
            'copilot_role' => 'physician',
            'copilot_scopes' => ['patient/Patient.read', 'patient/Condition.read'],
        ]))->map();

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
        $identity = (new SessionMapper([
            'authUserID' => 42,
            'pid' => 7,
            'copilot_role' => 'physician',
        ]))->map();

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
        $identity = (new SessionMapper([
            'authUserID' => 42,
            'pid' => 7,
        ]))->map();

        self::assertSame('unknown', $identity->role);
        self::assertSame([], $identity->scopes);
    }

    public function testRaisesWhenAuthUserIDMissing(): void
    {
        // No authUserID means the request is unauthenticated — the gateway
        // must refuse to mint a token rather than emit one with a blank
        // user_id, which would land in an audit-log row that no one can
        // attribute later.
        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessage('authenticated');

        (new SessionMapper(['pid' => 7]))->map();
    }

    public function testRaisesWhenPatientNotInContext(): void
    {
        // Co-Pilot routes are per-patient; without a chart in context the
        // gateway has nothing meaningful to authorize against. Better to
        // raise here than let the agent service receive an empty
        // patient_id and apply some fallback policy.
        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessage('patient context');

        (new SessionMapper([
            'authUserID' => 42,
            'copilot_role' => 'physician',
        ]))->map();
    }

    public function testGenerateNonceReturnsUrlSafeHex(): void
    {
        // Hex keeps the nonce safe to drop into a JSON claim without
        // additional encoding. 32 hex chars == 16 bytes of entropy, which
        // matches the agent-service replay-store key budget.
        $nonce = (new SessionMapper([]))->generateNonce();

        self::assertMatchesRegularExpression('/^[0-9a-f]{32}$/', $nonce);
    }

    public function testGenerateNonceReturnsFreshValuePerCall(): void
    {
        // The whole point of the nonce is per-request uniqueness — two
        // sequential calls must not return the same value. Birthday-paradox
        // collision in 16 bytes of CSPRNG output is effectively zero, so
        // any equal pair here is a real bug, not flake.
        $mapper = new SessionMapper([]);

        self::assertNotSame(
            $mapper->generateNonce(),
            $mapper->generateNonce(),
        );
    }

    public function testCoercesScalarSessionRoleAndPidIndependently(): void
    {
        // Belt-and-suspenders: if a future legacy code path stuffs a
        // boolean or float into pid (it shouldn't, but the session is
        // unconstrained), the mapper must not blow up — it should reduce
        // the value to a non-empty string and let the agent service's
        // tool layer reject the resulting bogus patient_id.
        $identity = (new SessionMapper([
            'authUserID' => 'doc-1',
            'pid' => 3.14,
            'copilot_role' => 'physician',
        ]))->map();

        self::assertSame('doc-1', $identity->userId);
        self::assertSame('3.14', $identity->patientId);
    }

    public function testMapWithPatientUsesBodyPatientIdAndFallbackScopes(): void
    {
        // The chat surface takes patient_id from the request body and
        // grants the standard MVP scope set when the session has none.
        // No copilot_scopes, no copilot_role, no pid — all of which the
        // mapWithPatient path is designed to tolerate.
        $fallback = ['system/Condition.read', 'system/Observation.read'];

        $identity = (new SessionMapper([
            'authUserID' => 'dr-patel',
        ]))->mapWithPatient('101', $fallback);

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
        $identity = (new SessionMapper([
            'authUserID' => 'dr-patel',
            'copilot_scopes' => ['custom/Scope.read'],
            'copilot_role' => 'resident',
        ]))->mapWithPatient('101', ['system/Condition.read']);

        self::assertSame('resident', $identity->role);
        self::assertSame(['custom/Scope.read'], $identity->scopes);
    }

    public function testMapWithPatientRequiresAuthenticatedSession(): void
    {
        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessage('authenticated');

        (new SessionMapper([]))->mapWithPatient('101', []);
    }

    public function testMapWithPatientRequiresNonEmptyPatientId(): void
    {
        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessage('patient_id');

        (new SessionMapper(['authUserID' => 'dr-patel']))
            ->mapWithPatient('', []);
    }

    public function testFromGlobalSessionPrefersBagOverTopLevel(): void
    {
        // The newer OpenEMR session lives under $_SESSION['OpenEMR'] thanks
        // to Symfony's AttributeBag namespacing. When the bag is present and
        // populated, fromGlobalSession must read from there — top-level
        // keys are the legacy layout and would shadow real values.
        $saved = $_SESSION ?? [];
        try {
            $_SESSION = [
                'OpenEMR' => [
                    'authUserID' => 'doc-9',
                    'pid' => '101',
                    'copilot_role' => 'physician',
                ],
                'authUserID' => 'wrong-level',
            ];

            $identity = SessionMapper::fromGlobalSession()->map();

            self::assertSame('doc-9', $identity->userId);
            self::assertSame('101', $identity->patientId);
        } finally {
            $_SESSION = $saved;
        }
    }

    public function testFromGlobalSessionFallsBackToTopLevelSession(): void
    {
        // Older OpenEMR base images (what we ship on Railway today) don't
        // namespace under an AttributeBag; auth data lives at the top of
        // $_SESSION. Pin the fallback so the gateway keeps working against
        // those images.
        $saved = $_SESSION ?? [];
        try {
            $_SESSION = [
                'authUserID' => 'doc-legacy',
                'pid' => '202',
                'copilot_role' => 'physician',
            ];

            $identity = SessionMapper::fromGlobalSession()->map();

            self::assertSame('doc-legacy', $identity->userId);
            self::assertSame('202', $identity->patientId);
        } finally {
            $_SESSION = $saved;
        }
    }
}
