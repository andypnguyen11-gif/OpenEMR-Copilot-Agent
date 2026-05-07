<?php

/**
 * Isolated tests for PatientMatchScorer.
 *
 * Locks the scoring tiers the document-review UI depends on. Each
 * scenario covers one tier of the matrix in
 * {@see PatientMatchScorer} so a future change to a threshold or
 * a tier is caught here rather than at runtime.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Services\Copilot;

use OpenEMR\Services\Copilot\PatientMatch\PatientMatchScorer;
use PHPUnit\Framework\TestCase;

final class PatientMatchScorerTest extends TestCase
{
    public function testMrnMatchOverridesEverythingElse(): void
    {
        // Wrong name AND wrong DOB, but the MRN matches → identity.
        $result = PatientMatchScorer::score(
            'Bob', 'Smith', '2000-01-01', 'MRN-42',
            'Margaret', 'Chen', '1968-03-12', 'mrn-42',
        );

        self::assertSame(1.00, $result['score']);
        self::assertSame('MRN match', $result['reason']);
    }

    public function testFullNameAndDobExactScores095(): void
    {
        $result = PatientMatchScorer::score(
            'Margaret', 'Chen', '1968-03-12', null,
            'margaret', 'CHEN', '1968-03-12', null,  // case-insensitive
        );

        self::assertSame(0.95, $result['score']);
        self::assertSame('Full name + DOB exact', $result['reason']);
    }

    public function testLastNameAndDobExactWithDifferentFirstScores085(): void
    {
        // Margaret on the document, "Maggie" in the chart — common
        // nickname pattern that should still surface as a candidate.
        $result = PatientMatchScorer::score(
            'Margaret', 'Chen', '1968-03-12', null,
            'Maggie', 'Chen', '1968-03-12', null,
        );

        self::assertSame(0.85, $result['score']);
        self::assertStringContainsString('first-name differs', $result['reason']);
    }

    public function testLastNameAndDobYearOnlyScores055(): void
    {
        // Year matches, month/day don't — soft signal, sub-review threshold.
        $result = PatientMatchScorer::score(
            'Margaret', 'Chen', '1968-03-12', null,
            'Margaret', 'Chen', '1968-08-22', null,
        );

        self::assertSame(0.55, $result['score']);
        self::assertSame('Last name + DOB year', $result['reason']);
        self::assertFalse(PatientMatchScorer::shouldShowAsCandidate($result['score']));
    }

    public function testNoOverlapScoresZero(): void
    {
        $result = PatientMatchScorer::score(
            'Margaret', 'Chen', '1968-03-12', null,
            'Bob', 'Smith', '1990-01-01', null,
        );

        self::assertSame(0.00, $result['score']);
    }

    public function testMrnMismatchOnlyDoesNotBoostScore(): void
    {
        // Both sides have an MRN but they differ — no override, fall
        // through to name/DOB tiers (which here don't match either).
        $result = PatientMatchScorer::score(
            'Margaret', 'Chen', '1968-03-12', 'MRN-A',
            'Bob', 'Smith', '1990-01-01', 'MRN-B',
        );

        self::assertSame(0.00, $result['score']);
    }

    public function testPreselectThresholdAtNinety(): void
    {
        self::assertTrue(PatientMatchScorer::shouldPreselect(0.95));
        self::assertTrue(PatientMatchScorer::shouldPreselect(0.90));
        self::assertFalse(PatientMatchScorer::shouldPreselect(0.85));
    }

    public function testReviewThresholdAtSixty(): void
    {
        self::assertTrue(PatientMatchScorer::shouldShowAsCandidate(0.60));
        self::assertTrue(PatientMatchScorer::shouldShowAsCandidate(0.85));
        self::assertFalse(PatientMatchScorer::shouldShowAsCandidate(0.55));
    }

    public function testNullExtractedFieldsDoNotMatchAnything(): void
    {
        $result = PatientMatchScorer::score(
            null, null, null, null,
            'Margaret', 'Chen', '1968-03-12', 'MRN-42',
        );

        self::assertSame(0.00, $result['score']);
    }

    public function testWhitespaceAndCaseDifferencesIgnoredForName(): void
    {
        $result = PatientMatchScorer::score(
            '  margaret ', '  CHEN  ', '1968-03-12', null,
            'Margaret', 'Chen', '1968-03-12', null,
        );

        self::assertSame(0.95, $result['score']);
    }
}
