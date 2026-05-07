<?php

/**
 * Clinical Co-Pilot module bootstrap (PR 17).
 *
 * Wires the :class:`SidePanelSubscriber` into the kernel dispatcher.
 * Kept thin: the subscriber owns its own Twig rendering, so this class
 * does not need a Twig environment of its own.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot;

use OpenEMR\Core\Kernel;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Modules\Copilot\EventSubscriber\SidePanelSubscriber;
use OpenEMR\Modules\Copilot\EventSubscriber\UploadMimeWhitelistSubscriber;
use Symfony\Component\EventDispatcher\EventDispatcherInterface;

final readonly class Bootstrap
{
    public const MODULE_NAME = 'oe-module-copilot';

    public function __construct(
        private EventDispatcherInterface $eventDispatcher,
        ?Kernel $kernel = null,
    ) {
        // Kernel parameter kept for parity with other module bootstraps;
        // the subscriber is self-contained and does not need it.
        unset($kernel);
    }

    public function subscribeToEvents(): void
    {
        // Plain get() + cast: the older OEGlobalsBag on the openemr/openemr
        // base image we layer on for prod doesn't ship the typed getString()
        // accessor, so calling it fatals every chart-page request.
        $webroot = OEGlobalsBag::getInstance()->get('webroot', '');
        $this->eventDispatcher->addSubscriber(
            new SidePanelSubscriber(
                webroot: is_string($webroot) ? $webroot : '',
            ),
        );
        // Augments the documents-upload MIME whitelist so the universal
        // upload page can accept TIFF / DOCX / XLSX / generic-octet-
        // stream files. See ``UploadMimeWhitelistSubscriber`` for the
        // full list and rationale.
        $this->eventDispatcher->addSubscriber(new UploadMimeWhitelistSubscriber());
    }
}
