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
 * Role sourcing is exercised through an injected
 * :class:`RoleResolverInterface` fake — these tests never touch the
 * database. The real :class:`DatabaseRoleResolver`'s SQL contract is
 * pinned by the services-test suite; here we only verify that whatever
 * the resolver returns lands on the identity verbatim and that the
 * mapper does not silently coerce :enumcase:`Role::UNKNOWN` into a
 * permissive default.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot;

use OpenEMR\Services\Copilot\Auth\Role;
use OpenEMR\Services\Copilot\Auth\RoleResolverInterface;
use OpenEMR\Services\Copilot\SessionMapper;
use PHPUnit\Framework\TestCase;
use RuntimeException;

final class SessionMapperTest extends TestCase
{
    public function testMapsAuthenticatedSessionIntoClinicianIdentity(): void
    {
        $identity = (new SessionMapper(
            [
                'authUserID' => 42,
                'pid' => 7,
                'copilot_scopes' => ['patient/Patient.read', 'patient/Condition.read'],
            ],
            self::resolver(Role::PHYSICIAN),
        ))->map();

        self::assertSame('42', $identity->userId);
        self::assertSame('7', $identity->patientId);
        self::assertSame(Role::PHYSICIAN, $identity->role);
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
        $identity = (new SessionMapper(
            ['authUserID' => 42, 'pid' => 7],
            self::resolver(Role::PHYSICIAN),
        ))->map();

        self::assertSame('42', $identity->userId);
        self::assertSame('7', $identity->patientId);
    }

    public function testRolePropagatesUnknownFromResolverWithoutSilentPromotion(): void
    {
        // The resolver returns UNKNOWN when it can't classify the user
        // (no physician_type, no supervision relationship, missing row).
        // The mapper must surface that verbatim — silently promoting to
        // PHYSICIAN here would grant attending-level read on every patient
        // the user is paneled to once the next slice's per-role scope check
        // lands. UNKNOWN is the deny-by-default sentinel; preserve it.
        $identity = (new SessionMapper(
            ['authUserID' => 42, 'pid' => 7],
            self::resolver(Role::UNKNOWN),
        ))->map();

        self::assertSame(Role::UNKNOWN, $identity->role);
        self::assertSame([], $identity->scopes);
    }

    public function testRoleResolverReceivesTheSessionUserId(): void
    {
        // Tight coupling test: the resolver must be called with the same
        // user_id that lands on the identity. A subtle bug here (e.g.,
        // resolving against pid instead of authUserID, or a hard-coded
        // string) would silently classify every user the same way.
        $resolver = new class implements RoleResolverInterface {
            public ?string $captured = null;

            public function resolve(string $userId): Role
            {
                $this->captured = $userId;
                return Role::RESIDENT;
            }
        };

        (new SessionMapper(
            ['authUserID' => 'doc-9', 'pid' => '101'],
            $resolver,
        ))->map();

        self::assertSame('doc-9', $resolver->captured);
    }

    public function testRaisesWhenAuthUserIDMissing(): void
    {
        // No authUserID means the request is unauthenticated — the gateway
        // must refuse to mint a token rather than emit one with a blank
        // user_id, which would land in an audit-log row that no one can
        // attribute later.
        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessage('authenticated');

        (new SessionMapper(['pid' => 7], self::resolver(Role::PHYSICIAN)))->map();
    }

    public function testRaisesWhenPatientNotInContext(): void
    {
        // Co-Pilot routes are per-patient; without a chart in context the
        // gateway has nothing meaningful to authorize against. Better to
        // raise here than let the agent service receive an empty
        // patient_id and apply some fallback policy.
        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessage('patient context');

        (new SessionMapper(
            ['authUserID' => 42],
            self::resolver(Role::PHYSICIAN),
        ))->map();
    }

    public function testGenerateNonceReturnsUrlSafeHex(): void
    {
        // Hex keeps the nonce safe to drop into a JSON claim without
        // additional encoding. 32 hex chars == 16 bytes of entropy, which
        // matches the agent-service replay-store key budget.
        $nonce = (new SessionMapper([], self::resolver(Role::UNKNOWN)))
            ->generateNonce();

        self::assertMatchesRegularExpression('/^[0-9a-f]{32}$/', $nonce);
    }

    public function testGenerateNonceReturnsFreshValuePerCall(): void
    {
        // The whole point of the nonce is per-request uniqueness — two
        // sequential calls must not return the same value. Birthday-paradox
        // collision in 16 bytes of CSPRNG output is effectively zero, so
        // any equal pair here is a real bug, not flake.
        $mapper = new SessionMapper([], self::resolver(Role::UNKNOWN));

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
        $identity = (new SessionMapper(
            ['authUserID' => 'doc-1', 'pid' => 3.14],
            self::resolver(Role::PHYSICIAN),
        ))->map();

        self::assertSame('doc-1', $identity->userId);
        self::assertSame('3.14', $identity->patientId);
    }

    public function testMapWithPatientUsesBodyPatientIdAndFallbackScopes(): void
    {
        // The chat surface takes patient_id from the request body and
        // grants the standard MVP scope set when the session has none.
        // No copilot_scopes, no pid — both of which the mapWithPatient
        // path is designed to tolerate.
        $fallback = ['system/Condition.read', 'system/Observation.read'];

        $identity = (new SessionMapper(
            ['authUserID' => 'dr-patel'],
            self::resolver(Role::PHYSICIAN),
        ))->mapWithPatient('101', $fallback);

        self::assertSame('dr-patel', $identity->userId);
        self::assertSame('101', $identity->patientId);
        self::assertSame(Role::PHYSICIAN, $identity->role);
        self::assertSame($fallback, $identity->scopes);
    }

    public function testMapWithPatientPrefersSessionScopesOverFallback(): void
    {
        // Session-supplied scopes (rare in MVP — the chat surface relies on
        // the fallback) must win over the controller's default. This pins
        // the precedence in case a future bug flips the order.
        $identity = (new SessionMapper(
            [
                'authUserID' => 'dr-patel',
                'copilot_scopes' => ['custom/Scope.read'],
            ],
            self::resolver(Role::RESIDENT),
        ))->mapWithPatient('101', ['system/Condition.read']);

        self::assertSame(Role::RESIDENT, $identity->role);
        self::assertSame(['custom/Scope.read'], $identity->scopes);
    }

    public function testMapWithPatientResolvesRoleFromTheInjectedResolver(): void
    {
        // Pin the chat-path role sourcing: whatever the resolver returns
        // for the session's authUserID is what lands on the identity, no
        // hard-coded "physician" default. UNKNOWN must come through as
        // UNKNOWN — the next slice's tool-layer scope check is what
        // denies it; the mapper must not silently fix it up here.
        $identity = (new SessionMapper(
            ['authUserID' => 'dr-patel'],
            self::resolver(Role::UNKNOWN),
        ))->mapWithPatient('101', []);

        self::assertSame(Role::UNKNOWN, $identity->role);
    }

    public function testMapWithPatientRequiresAuthenticatedSession(): void
    {
        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessage('authenticated');

        (new SessionMapper([], self::resolver(Role::PHYSICIAN)))
            ->mapWithPatient('101', []);
    }

    public function testMapWithPatientRequiresNonEmptyPatientId(): void
    {
        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessage('patient_id');

        (new SessionMapper(
            ['authUserID' => 'dr-patel'],
            self::resolver(Role::PHYSICIAN),
        ))->mapWithPatient('', []);
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
                ],
                'authUserID' => 'wrong-level',
            ];

            $identity = SessionMapper::fromGlobalSession(
                self::resolver(Role::PHYSICIAN),
            )->map();

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
            ];

            $identity = SessionMapper::fromGlobalSession(
                self::resolver(Role::PHYSICIAN),
            )->map();

            self::assertSame('doc-legacy', $identity->userId);
            self::assertSame('202', $identity->patientId);
        } finally {
            $_SESSION = $saved;
        }
    }

    public function testFromGlobalSessionReadsApiBagForOAuth2BearerAuth(): void
    {
        // OAuth2 bearer auth on /api/* routes routes through
        // BearerTokenAuthorizationStrategy::setupSessionForUserRole, which
        // writes authUserID into the API session bag — $_SESSION['apiOpenEMR']
        // — not the core OpenEMR bag. The Co-Pilot chat route lives at
        // /api/agent/query, so OAuth2 clients arrive with their identity in
        // this bag. Without this probe, OAuth2-authenticated requests would
        // fall through to the top-level $_SESSION fallback (empty here) and
        // mapWithPatient would throw "unauthenticated session" despite a
        // valid Bearer token.
        $saved = $_SESSION ?? [];
        try {
            $_SESSION = [
                'apiOpenEMR' => [
                    'authUserID' => 'oauth-clinician-7',
                    'pid' => '303',
                ],
            ];

            $identity = SessionMapper::fromGlobalSession(
                self::resolver(Role::PHYSICIAN),
            )->map();

            self::assertSame('oauth-clinician-7', $identity->userId);
            self::assertSame('303', $identity->patientId);
        } finally {
            $_SESSION = $saved;
        }
    }

    public function testFromGlobalSessionPrefersCoreBagOverApiBag(): void
    {
        // When both bags are populated (e.g. a request that arrived via the
        // web-UI cookie AND happens to have an api session for a previous
        // request still cached), the core bag wins. Pin the precedence
        // because the core bag represents the active web-UI clinician, and
        // OAuth2 callers don't share a process with cookie-session callers
        // in normal traffic.
        $saved = $_SESSION ?? [];
        try {
            $_SESSION = [
                'OpenEMR' => [
                    'authUserID' => 'cookie-clinician',
                    'pid' => '101',
                ],
                'apiOpenEMR' => [
                    'authUserID' => 'oauth-clinician',
                    'pid' => '999',
                ],
            ];

            $identity = SessionMapper::fromGlobalSession(
                self::resolver(Role::PHYSICIAN),
            )->map();

            self::assertSame('cookie-clinician', $identity->userId);
            self::assertSame('101', $identity->patientId);
        } finally {
            $_SESSION = $saved;
        }
    }

    private static function resolver(Role $role): RoleResolverInterface
    {
        return new class ($role) implements RoleResolverInterface {
            public function __construct(private readonly Role $role)
            {
            }

            public function resolve(string $userId): Role
            {
                return $this->role;
            }
        };
    }
}
