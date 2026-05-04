<?php

/**
 * Clinical Co-Pilot custom module bootstrap (PR 17).
 *
 * Registers a :class:`SidePanelSubscriber` on the kernel's event dispatcher
 * so the in-chart side panel mounts via the
 * :class:`OpenEMR\Events\Patient\Summary\Card\RenderEvent` non-fork hook
 * (AUDIT §2.2). This file runs once per request through
 * :class:`OpenEMR\Core\ModulesApplication::bootstrapCustomModules`,
 * provided the row in the ``modules`` table is ``mod_active = 1``.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot;

use OpenEMR\Core\ModulesClassLoader;
use Symfony\Component\EventDispatcher\EventDispatcherInterface;

// $classLoader and $eventDispatcher are injected into this file's scope
// by ``ModulesApplication::loadCustomModule``. PHPStan can't see across
// the include, so we narrow with explicit instanceof checks rather than
// adding a baseline entry — CLAUDE.md forbids new baseline rows for
// patterns that have a clean fix in the file itself.
assert(isset($classLoader) && $classLoader instanceof ModulesClassLoader);
assert(isset($eventDispatcher) && $eventDispatcher instanceof EventDispatcherInterface);

$classLoader->registerNamespaceIfNotExists(
    'OpenEMR\\Modules\\Copilot\\',
    __DIR__ . DIRECTORY_SEPARATOR . 'src',
);

// Kernel arg omitted: Bootstrap unsets it anyway, and OEGlobalsBag::getKernel()
// is not present on the stock openemr/openemr base image we layer on for
// prod — calling it there fatals every request that includes globals.php.
$bootstrap = new Bootstrap($eventDispatcher);
$bootstrap->subscribeToEvents();
