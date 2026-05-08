<?php

/**
 * Dispatches the per-section chart-write block for the document
 * confirm-and-attach flow. Lifted verbatim from the
 * ``$runChartWrites`` closure that used to live inline in
 * ``interface/copilot/api/save_document.php`` so the dispatch logic
 * (which sections were ticked → which {@see ChartWriteService}
 * methods to call → fold per-section row counts into a
 * {@see ChartWriteSummary}) can be unit-tested in isolation by
 * passing a spy {@see ChartWriteService} subclass.
 *
 * Both save-document branches (existing-patient match and
 * create-new-patient) call into this with the same shape — only the
 * pid differs — which is why the orchestrator stays parameterised on
 * ``$pid`` rather than capturing it.
 *
 * The orchestrator does not enforce ACLs or run validation; the
 * caller (``save_document.php``) handles ``AclMain::aclCheckCore``
 * and CSRF before constructing this. Same posture as
 * {@see ChartWriteService} itself.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\ChartWrite;

final readonly class ChartWriteOrchestrator
{
    public function __construct(private ChartWriteService $service)
    {
    }

    /**
     * Run the chart-write block for whichever sections the clinician
     * ticked on the review page. Returns a summary the success page
     * can render ("4 facts written, 2 abstained").
     *
     * @param int                    $pid     Patient pid for every write.
     * @param list<non-empty-string> $checked Section names from the
     *   review form's checked checkboxes — any of "allergies",
     *   "medications", "active_problems", "care_gaps",
     *   "lab_observations". Other strings are ignored.
     * @param array<mixed,mixed>     $facts   Merged extracted facts
     *   (the agent service's per-type Pydantic dump after the
     *   clinician's edits have been overlaid).
     * @param string                 $type    DocumentClassifier doc
     *   type — selects which FactsExtractor branch fires.
     */
    public function run(int $pid, array $checked, array $facts, string $type): ChartWriteSummary
    {
        $summary = new ChartWriteSummary();
        if (in_array('allergies', $checked, true)) {
            $summary->record('allergies', $this->service->writeAllergies(
                $pid,
                FactsExtractor::allergies($facts, $type),
            ));
        }
        if (in_array('medications', $checked, true)) {
            $summary->record('medications', $this->service->writeMedications(
                $pid,
                FactsExtractor::medications($facts, $type),
            ));
        }
        if (in_array('active_problems', $checked, true)) {
            $summary->record('active_problems', $this->service->writeActiveProblems(
                $pid,
                FactsExtractor::activeProblems($facts, $type),
            ));
        }
        if (in_array('care_gaps', $checked, true)) {
            $summary->record('care_gaps', $this->service->writeReminders(
                $pid,
                FactsExtractor::careGaps($facts, $type),
            ));
        }
        if (in_array('lab_observations', $checked, true)) {
            $payload = FactsExtractor::labObservations($facts, $type);
            $summary->record('lab_observations', $this->service->writeLabObservations(
                $pid,
                $payload['panel_name'],
                $payload['panel_loinc'],
                $payload['report_date'],
                $payload['observations'],
            ));
        }
        return $summary;
    }
}
