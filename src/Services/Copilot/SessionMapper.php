<?php

/**
 * Boundary that translates OpenEMR's ``$_SESSION`` into a typed
 * :class:`ClinicianIdentity` and mints per-request nonces.
 *
 * This is the only class in the Co-Pilot gateway permitted to read
 * ``$_SESSION`` directly (CLAUDE.md: superglobal access is confined to the
 * outermost entry point and parsed into typed objects immediately). Every
 * other layer — :class:`JwtSigner`, :class:`AgentHttpClient`,
 * :class:`GatewayController` — works against the typed identity.
 *
 * Required session keys:
 *   - ``authUserID`` — set by OpenEMR's auth layer on login. Absent → request
 *     is unauthenticated; the mapper raises rather than emit a token whose
 *     audit row would be unattributable.
 *   - ``pid`` — current patient context. Co-Pilot routes are per-patient;
 *     without one the gateway has no scope to authorize against.
 *
 * Role + scope sourcing:
 *   - ``role`` is resolved from the OpenEMR ``users`` table via the injected
 *     :class:`Auth\\RoleResolverInterface`. The session itself does not carry
 *     ``copilot_role`` — that placeholder is gone now that the resolver
 *     exists.
 *   - ``scopes`` is taken from the session's ``copilot_scopes`` key when set.
 *     Per-role scope assignment lives in the agent service's tool layer (the
 *     next slice); the gateway still passes through whatever the session
 *     already has so chat callers carrying the MVP fallback set continue to
 *     work.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot;

use OpenEMR\Services\Copilot\Auth\ClinicianIdentity;
use OpenEMR\Services\Copilot\Auth\DatabaseRoleResolver;
use OpenEMR\Services\Copilot\Auth\RoleResolverInterface;
use RuntimeException;

final readonly class SessionMapper
{
    /**
     * Bag-namespace key Symfony's AttributeBag uses for OpenEMR's core
     * session. Equivalent to ``SessionUtil::CORE_SESSION_ID``; hard-coded
     * here so the gateway works against older base images that predate
     * that constant.
     */
    public const CORE_SESSION_BAG = 'OpenEMR';

    /**
     * Bag-namespace key for the ``/api/...`` session.
     * :class:`BearerTokenAuthorizationStrategy` writes ``authUserID`` here
     * on OAuth2-authenticated API requests; equivalent to
     * ``SessionUtil::API_SESSION_ID``. Probed alongside the core bag so the
     * gateway works regardless of whether the caller arrived via the
     * web-UI cookie session or an OAuth2 bearer token.
     */
    public const API_SESSION_BAG = 'apiOpenEMR';

    /**
     * @param array<string, mixed> $session The ``$_SESSION[...]`` bag
     *        contents — typically extracted from
     *        ``$_SESSION[self::CORE_SESSION_BAG]`` at the route boundary.
     *        Reading the bag-scoped array directly avoids depending on
     *        SessionWrapperFactory methods that vary across OpenEMR
     *        versions.
     */
    public function __construct(
        private array $session,
        private RoleResolverInterface $roleResolver,
    ) {
    }

    /**
     * Construct a mapper from the live ``$_SESSION``. Probes three layouts
     * before falling back to the top level so the gateway works regardless
     * of how the caller authenticated:
     *
     * * ``$_SESSION['OpenEMR']`` — newer web-UI cookie sessions namespace
     *   clinician data under the core AttributeBag.
     * * ``$_SESSION['apiOpenEMR']`` — OAuth2 bearer-token auth on /api/*
     *   routes writes ``authUserID`` here via
     *   :class:`BearerTokenAuthorizationStrategy::setupSessionForUserRole`.
     *   The Co-Pilot chat route lives at ``/api/agent/query`` so OAuth2
     *   clients arrive with their identity in this bag.
     * * ``$_SESSION`` (top level) — older OpenEMR versions wrote keys
     *   straight to the superglobal; kept as the legacy fallback.
     *
     * The resolver is injected for testability; production wiring at
     * :file:`apis/routes/_rest_routes_copilot.inc.php` passes the database
     * implementation. Defaulting here means the route doesn't have to
     * spell out the dependency, while tests that exercise
     * :meth:`fromGlobalSession` can pin the role they want by supplying a
     * fake.
     */
    public static function fromGlobalSession(?RoleResolverInterface $roleResolver = null): self
    {
        $resolver = $roleResolver ?? new DatabaseRoleResolver();
        foreach ([self::CORE_SESSION_BAG, self::API_SESSION_BAG] as $bagName) {
            /** @var mixed $bag */
            $bag = $_SESSION[$bagName] ?? null;
            if (is_array($bag) && array_key_exists('authUserID', $bag)) {
                /** @var array<string, mixed> $bag */
                return new self($bag, $resolver);
            }
        }
        /** @var array<string, mixed> $session */
        $session = $_SESSION ?? [];
        return new self($session, $resolver);
    }

    /**
     * Build a :class:`ClinicianIdentity` from the active OpenEMR session.
     *
     * @throws RuntimeException When the session is unauthenticated or has no
     *                          patient in context.
     */
    public function map(): ClinicianIdentity
    {
        $userId = self::scalarToString($this->session['authUserID'] ?? null);
        if ($userId === '') {
            throw new RuntimeException(
                'Co-Pilot gateway called from an unauthenticated session',
            );
        }
        $patientId = self::scalarToString($this->session['pid'] ?? null);
        if ($patientId === '') {
            throw new RuntimeException(
                'Co-Pilot gateway called without a patient context',
            );
        }

        $scopesRaw = $this->session['copilot_scopes'] ?? [];
        $scopes = is_array($scopesRaw) ? array_values(array_map(
            self::scalarToString(...),
            $scopesRaw,
        )) : [];

        return new ClinicianIdentity(
            userId: $userId,
            role: $this->roleResolver->resolve($userId),
            patientId: $patientId,
            scopes: $scopes,
        );
    }

    /**
     * Narrow a ``mixed`` session value into a string without using the cast
     * operator (which PHPStan refuses at level 10 because it silently
     * coerces arrays / objects). Anything that isn't already a scalar
     * resolves to the empty string, which the callers treat as "missing".
     */
    private static function scalarToString(mixed $value): string
    {
        if (is_string($value)) {
            return $value;
        }
        if (is_int($value) || is_float($value)) {
            return (string) $value;
        }
        if (is_bool($value)) {
            return $value ? '1' : '0';
        }
        return '';
    }

    /**
     * Generate a 16-byte CSPRNG nonce as a 32-char lowercase hex string.
     *
     * 16 bytes is the entropy budget the agent service's replay store is
     * sized for, and hex keeps the value safe to drop into a JWT claim
     * without further encoding.
     */
    public function generateNonce(): string
    {
        return bin2hex(random_bytes(16));
    }

    /**
     * Build a :class:`ClinicianIdentity` for the M3 chat-query path, where
     * the patient is selected from the chat UI's dropdown rather than from
     * OpenEMR's chart context.
     *
     * Differences from :meth:`map`:
     *
     * * ``patient_id`` is supplied by the caller (the controller validates
     *   it from the request body).
     * * Scopes fall back to ``$fallbackScopes`` when the session has none —
     *   this is the path the standard MVP scope set from
     *   :class:`CopilotConfig` flows through. Per-role scope assignment in
     *   the agent service's tool layer will replace this fallback in the
     *   next slice.
     *
     * @param list<string> $fallbackScopes
     *
     * @throws RuntimeException When the session is unauthenticated.
     */
    public function mapWithPatient(string $patientId, array $fallbackScopes): ClinicianIdentity
    {
        if ($patientId === '') {
            throw new RuntimeException(
                'mapWithPatient requires a non-empty patient_id',
            );
        }

        $userId = self::scalarToString($this->session['authUserID'] ?? null);
        if ($userId === '') {
            throw new RuntimeException(
                'Co-Pilot gateway called from an unauthenticated session',
            );
        }

        $scopesRaw = $this->session['copilot_scopes'] ?? null;
        if (is_array($scopesRaw) && $scopesRaw !== []) {
            $scopes = array_values(array_map(self::scalarToString(...), $scopesRaw));
        } else {
            $scopes = $fallbackScopes;
        }

        return new ClinicianIdentity(
            userId: $userId,
            role: $this->roleResolver->resolve($userId),
            patientId: $patientId,
            scopes: $scopes,
        );
    }
}
