<?php

/**
 * Co-Pilot clinical role.
 *
 * The MVP recognises three operational roles (PRD §6 / ARCHITECTURE §4.4) plus
 * a fourth ``UNKNOWN`` slot for users the resolver can't classify (e.g. admin
 * accounts, service users, freshly-provisioned clinicians whose
 * ``physician_type`` row is still null). ``UNKNOWN`` is intentionally distinct
 * from ``PHYSICIAN`` so the agent service's tool layer can deny scopes by
 * default rather than granting attending-level read on ambiguous principals.
 *
 * Backed by a string for two reasons: the value is serialised into the JWT's
 * ``role`` claim and round-trips through PyJWT into the agent service's
 * matching :class:`Role` ``StrEnum``; and it's persisted into audit-log rows
 * where a stable wire format makes incident analysis tractable.
 *
 * The role is **not** a permission set — it's a categorical label. Per-role
 * scope assignment lives in the agent service's tool-base RBAC layer; this
 * enum is only the discriminator that layer keys off of.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\Auth;

enum Role: string
{
    case UNKNOWN = 'unknown';
    case PHYSICIAN = 'physician';
    case RESIDENT = 'resident';
    case SUPERVISOR = 'supervisor';

    /**
     * Map an OpenEMR ``users`` row's role signals onto a Co-Pilot :class:`Role`.
     *
     * Inputs:
     *
     * * ``$physicianType`` — the ``users.physician_type`` column, which
     *   references the ``physician_type`` ``list_options`` list. The
     *   ``resident_physician`` option is the only resident discriminator; all
     *   other populated values are attending-class clinicians.
     * * ``$isSupervisor`` — true iff this user appears as ``supervisor_id`` of
     *   any other user. Computed by the resolver via a separate lookup
     *   (``Role`` itself stays a pure value type and never touches the DB).
     *
     * Precedence rules:
     *
     * 1. Resident wins over supervisor. A senior resident who supervises
     *    juniors is still operationally a resident — every action audit-logged,
     *    no attending-level read on cross-coverage panels. Demoting them to
     *    PHYSICIAN by treating supervision as the dominant signal would
     *    silently grant scopes the PRD reserves for attendings.
     * 2. Supervisor wins over plain physician. The supervisor role is
     *    attending + audit-visibility on supervised residents (USERS §1.4);
     *    granting it requires the supervision relationship to exist.
     * 3. Otherwise, any non-null ``physician_type`` resolves to PHYSICIAN —
     *    attending, general, specialist, etc. all collapse to "clinician with
     *    full per-patient read".
     * 4. ``null`` ``physician_type`` resolves to UNKNOWN. The agent service's
     *    tool layer treats UNKNOWN as having no scopes, so the request is
     *    denied at the next boundary even though the gateway minted a token.
     *    UNKNOWN is the safe default — never silently promoted to PHYSICIAN.
     */
    public static function fromPhysicianType(?string $physicianType, bool $isSupervisor): self
    {
        if ($physicianType === 'resident_physician') {
            return self::RESIDENT;
        }
        if ($isSupervisor) {
            return self::SUPERVISOR;
        }
        if ($physicianType === null || $physicianType === '') {
            return self::UNKNOWN;
        }
        return self::PHYSICIAN;
    }
}
