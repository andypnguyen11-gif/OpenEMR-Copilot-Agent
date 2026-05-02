<?php

/**
 * Symfony subscriber that turns OpenEMR write events into agent-side
 * cache invalidations (PR 15).
 *
 * Subscribes to :class:`PatientUpdatedEvent` — the one
 * write-path event in OpenEMR core that carries enough info to identify
 * a single patient deterministically. Per AUDIT §10 #4 the medication /
 * lab / allergy / note write paths do not dispatch native Symfony
 * events, so those degrade to TTL-only freshness; that's the
 * architecture's documented fallback (PRD §5) and not a bug here. This
 * subscriber is the seam — when OpenEMR grows richer events later, add
 * them in :meth:`getSubscribedEvents` and the dispatch path is already
 * fire-and-forget safe.
 *
 * The subscriber **must not throw**. A clinical write that just landed
 * in OpenEMR's database must not be rolled back because the agent
 * service is unreachable; :class:`InvalidationDispatcher` enforces
 * that by swallowing every failure mode internally.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\Listeners;

use OpenEMR\Events\Patient\PatientUpdatedEvent;
use OpenEMR\Services\Copilot\InvalidationDispatcher;
use Symfony\Component\EventDispatcher\EventSubscriberInterface;

final readonly class CopilotInvalidationListener implements EventSubscriberInterface
{
    public function __construct(private InvalidationDispatcher $dispatcher)
    {
    }

    /**
     * @return array<string, string>
     */
    public static function getSubscribedEvents(): array
    {
        return [
            PatientUpdatedEvent::EVENT_HANDLE => 'onPatientUpdated',
        ];
    }

    /**
     * Pull the patient's pid out of the event payload and ask the
     * dispatcher to drop both cache tiers for that id.
     *
     * The payload is :class:`PatientUpdatedEvent::getNewPatientData`,
     * which is whatever assoc array :class:`PatientService::update` (or
     * :meth:`databaseUpdate`) emitted — both paths include ``pid`` as a
     * stringified numeric. Demographics-only updates fire the same
     * event so the dispatcher may invalidate flags more often than
     * strictly necessary; that's a freshness/cost tradeoff (the engine
     * runs against the chart, not the demographics row), not a
     * correctness bug.
     *
     * Anything we can't resolve to a non-empty string id is a no-op;
     * the dispatcher itself also no-ops on a blank id, but short-
     * circuiting here keeps the dispatcher's log surface clean.
     */
    public function onPatientUpdated(PatientUpdatedEvent $event): void
    {
        $data = $event->getNewPatientData();
        if (!is_array($data)) {
            return;
        }
        $pid = $data['pid'] ?? null;
        if (!is_scalar($pid)) {
            return;
        }
        $patientId = (string) $pid;
        if ($patientId === '') {
            return;
        }
        $this->dispatcher->invalidate($patientId);
    }
}
