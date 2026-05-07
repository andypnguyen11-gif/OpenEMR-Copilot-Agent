<?php

/**
 * Patient resolver — given extracted demographics from a Co-Pilot
 * document, returns a ranked candidate list of existing patient_data
 * rows that the document might belong to.
 *
 * Used by ``document_review.php`` after a successful extraction. The
 * page renders the top candidates so the clinician can confirm a
 * match (high-score), pick from a list (mid-score), or proceed to
 * the create-new-patient workflow (low-score / no candidates).
 *
 * The scorer is a separate class so it can be unit-tested without
 * a database. This service composes the scorer with a SQL fetch
 * scoped to "any patient who shares the last name OR the MRN" — a
 * narrow enough set that an exhaustive in-PHP score is cheap even
 * on real-world charts.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\PatientMatch;

use OpenEMR\Common\Database\QueryUtils;

final class PatientMatchService
{
    private const MAX_CANDIDATES_RETURNED = 3;
    private const MAX_CANDIDATES_FETCHED = 50;

    /**
     * Match extracted demographics against existing patients.
     *
     * @return list<PatientMatchCandidate> Top 3 candidates ordered by
     *         score descending. Empty when nothing crossed the
     *         {@see PatientMatchScorer::REVIEW_THRESHOLD}.
     */
    public function match(
        ?string $firstName,
        ?string $lastName,
        ?string $dob,
        ?string $mrn,
    ): array {
        if ($firstName === null && $lastName === null && $mrn === null) {
            // Nothing to match on.
            return [];
        }

        $rows = $this->fetchCandidateRows($lastName, $mrn);
        if ($rows === []) {
            return [];
        }

        $candidates = [];
        foreach ($rows as $row) {
            $candidate = $this->scoreRow($row, $firstName, $lastName, $dob, $mrn);
            if ($candidate !== null) {
                $candidates[] = $candidate;
            }
        }

        // Sort by score descending, then by lower pid (older record first
        // when scores tie — gives stable ordering across page reloads).
        usort($candidates, static function (PatientMatchCandidate $a, PatientMatchCandidate $b): int {
            $scoreCmp = $b->score <=> $a->score;
            if ($scoreCmp !== 0) {
                return $scoreCmp;
            }
            return $a->pid <=> $b->pid;
        });

        return array_slice($candidates, 0, self::MAX_CANDIDATES_RETURNED);
    }

    /**
     * @return list<array{pid:int, uuid:string, fname:string, lname:string, DOB:string, pubpid:?string}>
     */
    private function fetchCandidateRows(?string $lastName, ?string $mrn): array
    {
        $clauses = [];
        $params = [];
        if ($lastName !== null && $lastName !== '') {
            $clauses[] = 'LOWER(lname) = LOWER(?)';
            $params[] = $lastName;
        }
        if ($mrn !== null && $mrn !== '') {
            $clauses[] = 'UPPER(pubpid) = UPPER(?)';
            $params[] = $mrn;
        }
        if ($clauses === []) {
            return [];
        }

        $sql = sprintf(
            'SELECT pid, uuid, fname, lname, DATE_FORMAT(DOB, %s) AS DOB, pubpid '
                . 'FROM patient_data WHERE %s LIMIT %d',
            "'%Y-%m-%d'",
            implode(' OR ', $clauses),
            self::MAX_CANDIDATES_FETCHED,
        );

        $rows = QueryUtils::fetchRecords($sql, $params);
        $out = [];
        foreach ($rows as $row) {
            // Each column comes back as ``mixed`` from the DB layer.
            // Narrow at this boundary rather than (string)/(int) cast
            // — phpstan level 10 forbids the cast-from-mixed pattern,
            // and a row with an unexpected type for one of these
            // columns is data corruption we should NOT silently coerce.
            $pidRaw = $row['pid'] ?? null;
            $uuidRaw = $row['uuid'] ?? null;
            $fnameRaw = $row['fname'] ?? null;
            $lnameRaw = $row['lname'] ?? null;
            $dobRaw = $row['DOB'] ?? null;
            $pubpidRaw = $row['pubpid'] ?? null;

            $out[] = [
                'pid' => is_int($pidRaw) ? $pidRaw : (is_numeric($pidRaw) ? (int) $pidRaw : 0),
                'uuid' => is_string($uuidRaw) ? bin2hex($uuidRaw) : '',
                'fname' => is_string($fnameRaw) ? $fnameRaw : '',
                'lname' => is_string($lnameRaw) ? $lnameRaw : '',
                'DOB' => is_string($dobRaw) ? $dobRaw : '',
                'pubpid' => is_string($pubpidRaw) && $pubpidRaw !== '' ? $pubpidRaw : null,
            ];
        }
        return $out;
    }

    /**
     * @param array{pid:int, uuid:string, fname:string, lname:string, DOB:string, pubpid:?string} $row
     */
    private function scoreRow(
        array $row,
        ?string $firstName,
        ?string $lastName,
        ?string $dob,
        ?string $mrn,
    ): ?PatientMatchCandidate {
        $scored = PatientMatchScorer::score(
            $firstName,
            $lastName,
            $dob,
            $mrn,
            $row['fname'],
            $row['lname'],
            $row['DOB'],
            $row['pubpid'],
        );
        if (!PatientMatchScorer::shouldShowAsCandidate($scored['score'])) {
            return null;
        }
        return new PatientMatchCandidate(
            pid: $row['pid'],
            uuid: $row['uuid'],
            firstName: $row['fname'],
            lastName: $row['lname'],
            dob: $row['DOB'],
            mrn: $row['pubpid'],
            score: $scored['score'],
            matchReason: $scored['reason'],
        );
    }
}
