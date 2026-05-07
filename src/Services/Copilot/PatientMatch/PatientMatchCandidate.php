<?php

/**
 * One scored candidate returned by {@see PatientMatchService::match()}.
 *
 * Carries the patient identifiers (pid + uuid) the chart-side review
 * page needs to confirm a match, the printed demographics so the
 * clinician can eyeball it, the score the matcher assigned, and a
 * one-line human-readable reason for the score so the UI can surface
 * "Last name + DOB exact" next to a candidate row.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\PatientMatch;

final readonly class PatientMatchCandidate
{
    public function __construct(
        public int $pid,
        public string $uuid,
        public string $firstName,
        public string $lastName,
        public string $dob,
        public ?string $mrn,
        public float $score,
        public string $matchReason,
    ) {
    }

    /**
     * @return array{pid:int, uuid:string, first_name:string, last_name:string, dob:string, mrn:?string, score:float, match_reason:string}
     */
    public function toArray(): array
    {
        return [
            'pid' => $this->pid,
            'uuid' => $this->uuid,
            'first_name' => $this->firstName,
            'last_name' => $this->lastName,
            'dob' => $this->dob,
            'mrn' => $this->mrn,
            'score' => $this->score,
            'match_reason' => $this->matchReason,
        ];
    }
}
