<?php

/**
 * Isolated tests for the Clinical Co-Pilot JWT signer.
 *
 * The signer is the PHP half of the trust boundary defined in ARCHITECTURE
 * §4: every request the gateway sends to the agent service carries a short-
 * lived HS256 JWT bound to the calling clinician, the current patient, and a
 * single-use nonce. The tests pin the *contract* rather than implementation
 * details — claim shape, signing algorithm, expiration window, and that
 * each call produces a fresh token even when given identical input. The
 * Python verifier in ``agent-service/tests/unit/test_jwt_verifier.py``
 * pairs with this file: anything signed by these tests must validate
 * there, and vice versa.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot;

use DateTimeImmutable;
use DateTimeZone;
use Lcobucci\Clock\FrozenClock;
use Lcobucci\JWT\Configuration;
use Lcobucci\JWT\Signer\Hmac\Sha256;
use Lcobucci\JWT\Signer\Key\InMemory;
use Lcobucci\JWT\Validation\Constraint\SignedWith;
use OpenEMR\Services\Copilot\Auth\ClinicianIdentity;
use OpenEMR\Services\Copilot\Auth\Role;
use OpenEMR\Services\Copilot\JwtSigner;
use PHPUnit\Framework\TestCase;

final class JwtSignerTest extends TestCase
{
    private const SECRET = 'unit-test-hmac-secret-32-bytes-min';

    private function identity(): ClinicianIdentity
    {
        return new ClinicianIdentity(
            userId: 'user-42',
            role: Role::PHYSICIAN,
            patientId: 'patient-7',
            scopes: ['patient/Patient.read', 'patient/Condition.read'],
        );
    }

    private function signer(DateTimeImmutable $now): JwtSigner
    {
        return new JwtSigner(self::SECRET, new FrozenClock($now));
    }

    private static function parse(string $token): \Lcobucci\JWT\UnencryptedToken
    {
        $config = Configuration::forSymmetricSigner(
            new Sha256(),
            InMemory::plainText(self::SECRET),
        );
        $parsed = $config->parser()->parse($token);
        self::assertInstanceOf(\Lcobucci\JWT\UnencryptedToken::class, $parsed);
        return $parsed;
    }

    public function testSignedTokenCarriesAllRequiredClaims(): void
    {
        // The five claims listed in TASKS.md PR 4 are not negotiable: the
        // Python verifier rejects anything missing one of them. Bundling
        // them here means a future refactor cannot silently drop a field.
        $now = new DateTimeImmutable('2026-04-29T12:00:00Z');
        $signer = $this->signer($now);

        $token = $signer->sign($this->identity(), 'nonce-abc');

        $claims = self::parse($token)->claims();
        self::assertSame('user-42', $claims->get('user_id'));
        self::assertSame('physician', $claims->get('role'));
        self::assertSame('patient-7', $claims->get('patient_id'));
        self::assertSame(
            ['patient/Patient.read', 'patient/Condition.read'],
            $claims->get('scopes'),
        );
        self::assertSame('nonce-abc', $claims->get('nonce'));
    }

    public function testTokenExpiresFiveMinutesAfterIssuance(): void
    {
        // ARCHITECTURE §4 fixes the lifetime at five minutes — long enough
        // to absorb clock skew between two Railway services, short enough
        // that a leaked token is barely useful before it dies. Pinning the
        // exp explicitly means a "small tweak" can't silently widen the
        // replay window.
        $now = new DateTimeImmutable('2026-04-29T12:00:00Z');
        $signer = $this->signer($now);

        $token = self::parse($signer->sign($this->identity(), 'nonce-abc'));

        $iat = $token->claims()->get('iat');
        $exp = $token->claims()->get('exp');
        self::assertInstanceOf(DateTimeImmutable::class, $iat);
        self::assertInstanceOf(DateTimeImmutable::class, $exp);
        self::assertSame($now->getTimestamp(), $iat->getTimestamp());
        self::assertSame(
            $now->getTimestamp() + JwtSigner::TOKEN_LIFETIME_SECONDS,
            $exp->getTimestamp(),
        );
    }

    public function testHeaderUsesHs256(): void
    {
        // Algorithm-confusion attacks on JWTs (e.g. the historical "alg: none"
        // and HS/RS swap families) are easy to introduce by accident if the
        // signer is reconfigured. The agent-service verifier hard-codes
        // HS256; this assertion guards the matching producer side.
        $token = self::parse(
            $this->signer(new DateTimeImmutable('2026-04-29T12:00:00Z'))
                ->sign($this->identity(), 'nonce-abc'),
        );

        self::assertSame('HS256', $token->headers()->get('alg'));
        self::assertSame('JWT', $token->headers()->get('typ'));
    }

    public function testTokenIsSignedByTheConfiguredSecret(): void
    {
        $token = self::parse(
            $this->signer(new DateTimeImmutable('2026-04-29T12:00:00Z'))
                ->sign($this->identity(), 'nonce-abc'),
        );

        $config = Configuration::forSymmetricSigner(
            new Sha256(),
            InMemory::plainText(self::SECRET),
        );
        $constraint = new SignedWith($config->signer(), $config->signingKey());

        // A token signed under a different secret must not validate. The
        // double assertion here — pass with the real key, fail with a fake —
        // is what gives the test signal: a no-op signer would pass the first
        // half by accident.
        self::assertTrue($config->validator()->validate($token, $constraint));

        $forgedConfig = Configuration::forSymmetricSigner(
            new Sha256(),
            InMemory::plainText('attacker-secret-pretending-to-be-real'),
        );
        $forgedConstraint = new SignedWith(
            $forgedConfig->signer(),
            $forgedConfig->signingKey(),
        );
        self::assertFalse($config->validator()->validate($token, $forgedConstraint));
    }

    public function testEachCallProducesAUniqueJtiEvenWithIdenticalInput(): void
    {
        // Even when the caller forgets to vary the nonce, the JTI claim
        // is the last line of defense against accidental replay — two
        // tokens issued in the same millisecond must still be
        // distinguishable by the verifier's seen-jti store.
        $signer = $this->signer(new DateTimeImmutable('2026-04-29T12:00:00Z'));

        $a = self::parse($signer->sign($this->identity(), 'same-nonce'));
        $b = self::parse($signer->sign($this->identity(), 'same-nonce'));

        self::assertNotSame(
            $a->claims()->get('jti'),
            $b->claims()->get('jti'),
        );
    }

    public function testTokensAreIssuedInUtc(): void
    {
        // FrozenClock honors the timezone of the supplied DateTimeImmutable;
        // the JWT spec requires NumericDate (seconds since epoch, UTC). If
        // the signer accidentally uses a local-time clock, two services in
        // different regions disagree about exp.
        $localNow = new DateTimeImmutable(
            '2026-04-29T08:00:00',
            new DateTimeZone('America/New_York'),
        );
        $signer = $this->signer($localNow);

        $token = self::parse($signer->sign($this->identity(), 'nonce-abc'));

        $iat = $token->claims()->get('iat');
        self::assertInstanceOf(DateTimeImmutable::class, $iat);
        // 08:00 EDT == 12:00 UTC.
        self::assertSame('2026-04-29T12:00:00+00:00', $iat->format('c'));
    }

    public function testIssuerAndAudienceArePinnedForCrossServiceClarity(): void
    {
        // Pinning iss/aud lets the Python verifier reject tokens minted for
        // an unrelated service that happens to share the HMAC secret (e.g.
        // dev/prod confusion). The strings are documented constants — if
        // they change, both sides must change in lockstep.
        $token = self::parse(
            $this->signer(new DateTimeImmutable('2026-04-29T12:00:00Z'))
                ->sign($this->identity(), 'nonce-abc'),
        );

        // lcobucci stores iss as a scalar string and aud as a list (the JWT
        // spec allows aud to be either, but multiple is the more general
        // shape — pinning the list form keeps the verifier's expectations
        // consistent across one-audience and multi-audience deploys).
        self::assertSame('openemr-gateway', $token->claims()->get('iss'));
        self::assertSame(['clinical-copilot'], $token->claims()->get('aud'));
    }
}
