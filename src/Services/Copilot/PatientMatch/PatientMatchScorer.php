<?php

/**
 * Pure scoring function for the patient resolver
 * (Week 2 multimodal expansion, Step 4).
 *
 * Given an extracted (first_name, last_name, dob, mrn) tuple and a
 * candidate patient_data row, returns a score in [0.0, 1.0] and a
 * one-line human-readable reason. The matcher in
 * {@see PatientMatchService} composes this scorer with a SQL fetch
 * so the scoring can be unit-tested in isolation without a DB.
 *
 * Scoring tiers (highest first):
 *
 * - **MRN exact match** → 1.00 ("MRN match"). Any other field
 *   mismatch is irrelevant; an MRN match is treated as identity.
 * - **First + last (case-insensitive) + DOB exact** → 0.95
 *   ("Full name + DOB exact").
 * - **Last name (case-insensitive) + DOB exact** → 0.85
 *   ("Last name + DOB exact"). Common first-name aliases
 *   (Margaret/Maggie) lose 0.10 to flag the soft match.
 * - **Last name + DOB year-only** → 0.55 ("Last name + DOB year").
 *   Below the 0.6 review threshold so the UI defaults to
 *   "Create new" rather than auto-suggesting a match on year alone.
 * - **No useful overlap** → 0.0. The candidate is filtered out by
 *   PatientMatchService rather than returned.
 *
 * The thresholds the document-review UI uses:
 *
 * - score ≥ 0.90 → preselected as "Confirm match"
 * - 0.60 ≤ score < 0.90 → shown as a candidate, clinician picks
 * - score < 0.60 → not surfaced; UI defaults to "Create new patient"
 *
 * Match decisions never auto-execute. The clinician confirms or
 * picks "Create new" on the review page before any chart write.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\PatientMatch;

final class PatientMatchScorer
{
    public const PRESELECT_THRESHOLD = 0.90;
    public const REVIEW_THRESHOLD = 0.60;

    /**
     * Score one candidate against the extracted demographics.
     *
     * @return array{score:float, reason:string}
     */
    public static function score(
        ?string $extractedFirstName,
        ?string $extractedLastName,
        ?string $extractedDob,  // YYYY-MM-DD
        ?string $extractedMrn,
        string $candidateFirstName,
        string $candidateLastName,
        string $candidateDob,  // YYYY-MM-DD
        ?string $candidateMrn,
    ): array {
        // MRN match overrides everything else. If both sides report
        // an MRN and they match (case-insensitive), treat as identity.
        if ($extractedMrn !== null && $candidateMrn !== null
            && self::normalizeMrn($extractedMrn) === self::normalizeMrn($candidateMrn)
        ) {
            return ['score' => 1.00, 'reason' => 'MRN match'];
        }

        $lastEq = $extractedLastName !== null
            && self::normalizeName($extractedLastName) === self::normalizeName($candidateLastName);
        $firstEq = $extractedFirstName !== null
            && self::normalizeName($extractedFirstName) === self::normalizeName($candidateFirstName);
        $dobEq = $extractedDob !== null && $extractedDob === $candidateDob;
        $dobYearEq = $extractedDob !== null
            && substr($extractedDob, 0, 4) === substr($candidateDob, 0, 4);

        if ($lastEq && $firstEq && $dobEq) {
            return ['score' => 0.95, 'reason' => 'Full name + DOB exact'];
        }
        if ($lastEq && $dobEq) {
            // First name not exact — common nickname (Margaret/Maggie)
            // costs 0.10 so the UI flags this as a soft match and the
            // clinician notices the discrepancy.
            return ['score' => 0.85, 'reason' => 'Last name + DOB exact (first-name differs)'];
        }
        if ($lastEq && $dobYearEq) {
            return ['score' => 0.55, 'reason' => 'Last name + DOB year'];
        }
        return ['score' => 0.00, 'reason' => 'No useful overlap'];
    }

    /**
     * Return true when the score crosses the auto-preselect threshold.
     * The UI uses this to decide whether to default the radio button
     * to "Confirm match" vs leave the user on "Create new patient".
     */
    public static function shouldPreselect(float $score): bool
    {
        return $score >= self::PRESELECT_THRESHOLD;
    }

    /**
     * Return true when the score is high enough to surface in the
     * candidate list at all. Below this, the UI hides the candidate
     * and defaults to the create-new path.
     */
    public static function shouldShowAsCandidate(float $score): bool
    {
        return $score >= self::REVIEW_THRESHOLD;
    }

    private static function normalizeName(string $name): string
    {
        return strtolower(trim(preg_replace('/\s+/', ' ', $name) ?? $name));
    }

    private static function normalizeMrn(string $mrn): string
    {
        return strtoupper(trim($mrn));
    }
}
