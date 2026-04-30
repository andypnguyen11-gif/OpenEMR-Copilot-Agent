<?php

/**
 * Mints HS256 JWTs that carry a clinician's identity from the OpenEMR
 * gateway to the Clinical Co-Pilot agent service.
 *
 * ARCHITECTURE §4 fixes the contract this class implements:
 *   - HS256 (the agent service's verifier hard-rejects any other alg)
 *   - 5-minute lifetime — long enough to absorb inter-service clock skew,
 *     short enough that a leaked token barely outlives the request
 *   - claims ``user_id``, ``role``, ``patient_id``, ``scopes``, ``nonce``
 *     plus the JWT-standard ``iat``/``exp``/``jti``/``iss``/``aud``
 *
 * The signer takes a :class:`ClockInterface` so tests can pin time and the
 * signer never accidentally couples to wall-clock state. Issuer and
 * audience are constants because they're part of the inter-service
 * protocol — changing either requires a coordinated change on the Python
 * verifier.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot;

use DateInterval;
use DateTimeZone;
use InvalidArgumentException;
use Lcobucci\Clock\Clock;
use Lcobucci\JWT\Configuration;
use Lcobucci\JWT\Signer\Hmac\Sha256;
use Lcobucci\JWT\Signer\Key\InMemory;
use OpenEMR\Services\Copilot\Auth\ClinicianIdentity;
use Ramsey\Uuid\Uuid;

final readonly class JwtSigner
{
    public const TOKEN_LIFETIME_SECONDS = 300;
    public const ISSUER = 'openemr-gateway';
    public const AUDIENCE = 'clinical-copilot';

    private Configuration $config;

    public function __construct(
        string $hmacSecret,
        private Clock $clock,
    ) {
        if ($hmacSecret === '') {
            // Constructor-time failure rather than sign-time so a misconfigured
            // gateway can't appear healthy until the first authenticated
            // request hits the route. The CopilotConfig accessor enforces a
            // 32-byte minimum upstream; this is the last-line defense for
            // direct construction (tests, future call sites).
            throw new InvalidArgumentException('hmacSecret must be non-empty');
        }
        $this->config = Configuration::forSymmetricSigner(
            new Sha256(),
            InMemory::plainText($hmacSecret),
        );
    }

    /**
     * Mint a token for ``$identity`` bound to ``$nonce``.
     *
     * The nonce is the caller's per-request anti-replay marker; the JTI is
     * a separate per-token UUID so two tokens issued with the same nonce
     * (which shouldn't happen, but defense in depth) still appear distinct
     * to the verifier's seen-jti store.
     */
    public function sign(ClinicianIdentity $identity, string $nonce): string
    {
        $now = $this->clock->now()->setTimezone(new DateTimeZone('UTC'));
        $expiresAt = $now->add(new DateInterval('PT' . self::TOKEN_LIFETIME_SECONDS . 'S'));

        $token = $this->config->builder()
            ->issuedBy(self::ISSUER)
            ->permittedFor(self::AUDIENCE)
            ->identifiedBy(Uuid::uuid4()->toString())
            ->issuedAt($now)
            ->expiresAt($expiresAt)
            ->withClaim('user_id', $identity->userId)
            ->withClaim('role', $identity->role)
            ->withClaim('patient_id', $identity->patientId)
            ->withClaim('scopes', $identity->scopes)
            ->withClaim('nonce', $nonce)
            ->getToken($this->config->signer(), $this->config->signingKey());

        return $token->toString();
    }
}
