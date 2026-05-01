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
 * Optional session keys (Co-Pilot-specific, populated by later PRs):
 *   - ``copilot_role`` — defaults to ``"unknown"`` until PR 18 wires the
 *     real role lookup. The agent service's tool layer treats unknown roles
 *     as having no scopes, so the request is denied at the next boundary
 *     even if a placeholder token is minted.
 *   - ``copilot_scopes`` — defaults to an empty list, same rationale.
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
     * @param array<string, mixed> $session The ``$_SESSION[...]`` bag
     *        contents — typically extracted from
     *        ``$_SESSION[self::CORE_SESSION_BAG]`` at the route boundary.
     *        Reading the bag-scoped array directly avoids depending on
     *        SessionWrapperFactory methods that vary across OpenEMR
     *        versions.
     */
    public function __construct(private array $session)
    {
    }

    /**
     * Construct a mapper from the live ``$_SESSION``, drilling into the
     * core AttributeBag namespace. Convenience for route closures that
     * have already been hit by OpenEMR's session bootstrap.
     */
    public static function fromGlobalSession(): self
    {
        /** @var array<string, mixed> $bag */
        $bag = $_SESSION[self::CORE_SESSION_BAG] ?? [];
        return new self($bag);
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

        $roleRaw = $this->session['copilot_role'] ?? null;
        $role = is_string($roleRaw) && $roleRaw !== '' ? $roleRaw : 'unknown';

        return new ClinicianIdentity(
            userId: $userId,
            role: $role,
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
     *   :class:`CopilotConfig` flows through. PR 18's role/scope plumbing
     *   replaces both legs.
     * * The role default is ``'physician'`` (rather than ``'unknown'``)
     *   because the chat surface is gated behind OpenEMR's ACL; reaching
     *   this code with an authenticated session implies a clinician role
     *   for the MVP. Real role lookup lands with PR 18.
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

        $roleRaw = $this->session['copilot_role'] ?? null;
        $role = is_string($roleRaw) && $roleRaw !== '' ? $roleRaw : 'physician';

        return new ClinicianIdentity(
            userId: $userId,
            role: $role,
            patientId: $patientId,
            scopes: $scopes,
        );
    }
}
