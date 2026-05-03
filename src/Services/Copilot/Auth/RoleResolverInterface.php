<?php

/**
 * Resolve a clinician's :class:`Role` from their OpenEMR user id.
 *
 * Sits between :class:`SessionMapper` and the OpenEMR ``users`` table. The
 * mapper holds ``$_SESSION['authUserID']`` and needs the typed role to stamp
 * into the JWT; the database holds ``physician_type`` and the supervision
 * graph. Hiding the lookup behind an interface keeps the mapper testable
 * without a live database — :class:`SessionMapperTest` injects a fake.
 *
 * Implementations must be deterministic for a given ``$userId`` within a
 * single request and must fail safely (fall back to ``Role::UNKNOWN`` rather
 * than raise) on missing rows or malformed input. Raising would convert a
 * benign "user not in the role table yet" into a 5xx for an authenticated
 * request, which is worse than the deny-by-default UNKNOWN path.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\Auth;

interface RoleResolverInterface
{
    /**
     * Return the clinical role for ``$userId`` or :enumcase:`Role::UNKNOWN`
     * when the row is missing, malformed, or the user has no recognised
     * ``physician_type`` and no supervision relationship.
     */
    public function resolve(string $userId): Role;
}
