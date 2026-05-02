<?php

/**
 * Discrepancy engine seeded scenarios — single source of truth.
 *
 * Each scenario describes one conflict shape from AUDIT §3.2 that the
 * discrepancy engine (PR 13b/c) is expected to detect. This file is consumed
 * by two layers:
 *
 *   1. DiscrepancyFixtureManager — installs scenarios into the database for
 *      PHPUnit integration tests via QueryUtils + UuidRegistry.
 *   2. bin/generate-discrepancy-sql.php — emits sql/example_discrepancy_data.sql
 *      so the same scenarios can be loaded into the demo database via
 *      `mysql < sql/example_discrepancy_data.sql` (Railway demo / Python eval).
 *
 * Both paths must produce data the engine flags identically; drift between
 * them is caught by the `composer fixture-check` drift gate (PR 13a).
 *
 * Scenario shape:
 *   - name, description, expected_flags    — engine-facing metadata
 *   - pid, pubpid                           — patient identifiers (fixed so SQL
 *                                             and PHP paths anchor identically)
 *   - patient                               — single patient_data row (without
 *                                             pid/pubpid/uuid — those are added
 *                                             by the manager / generator)
 *   - lists, pnotes, prescriptions          — child rows tied to the scenario's
 *                                             pid (manager / generator inject)
 *   - procedure_orders / _reports / _results — procedure chain rows for the
 *                                             stale-lab scenario; ids are fixed
 *                                             so child rows resolve cleanly
 *
 * Reference dates:
 *   - "now"            — 2026-05-02 (matches MVP submission window)
 *   - "recent" notes   — 2026-04-15
 *   - "older" entries  — 2024-06-01
 *   - "stale" labs     — 2024-08-15  (>18 months pre-now; trips stale-lab rule)
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

/** @return array<int, array<string, mixed>> */
return [
    [
        'name' => 'med_vs_note_conflict',
        'description' => 'Active metoprolol in lists; most recent note says discontinued for bradycardia.',
        'pid' => 90001,
        'pubpid' => 'test-fixture-discrepancy-1',
        'patient' => [
            'title' => 'Mr.',
            'fname' => 'Discrepancy',
            'lname' => 'MedNoteOne',
            'mname' => 'A',
            'DOB' => '1962-04-12',
            'sex' => 'Male',
            'street' => '1 Conflict Way',
            'city' => 'San Diego',
            'state' => 'CA',
            'postal_code' => '92101',
            'language' => 'english',
            'status' => 'married',
        ],
        'lists' => [
            [
                'type' => 'medication',
                'title' => 'Metoprolol Tartrate 50mg',
                'diagnosis' => 'RXCUI:866924',
                'begdate' => '2024-06-01',
                'enddate' => null,
                'activity' => 1,
                'comments' => 'For hypertension management',
            ],
        ],
        'pnotes' => [
            [
                'date' => '2026-04-15 09:00:00',
                'title' => 'Office visit',
                'body' => 'Patient developed bradycardia. Discontinued metoprolol effective today; follow up in two weeks for BP recheck.',
                'authorized' => 1,
                'activity' => 1,
            ],
        ],
        'prescriptions' => [],
        'procedure_orders' => [],
        'procedure_reports' => [],
        'procedure_results' => [],
        'expected_flags' => [
            ['rule_id' => 'med_vs_note_conflict', 'category' => 'consistency'],
        ],
    ],

    [
        'name' => 'narrative_only_allergy',
        'description' => 'Sulfa allergy mentioned only in note text; no row in lists.type=allergy.',
        'pid' => 90002,
        'pubpid' => 'test-fixture-discrepancy-2',
        'patient' => [
            'title' => 'Ms.',
            'fname' => 'Discrepancy',
            'lname' => 'NarrativeAllergy',
            'mname' => 'B',
            'DOB' => '1978-09-30',
            'sex' => 'Female',
            'street' => '2 Conflict Way',
            'city' => 'San Diego',
            'state' => 'CA',
            'postal_code' => '92101',
            'language' => 'english',
            'status' => 'single',
        ],
        // No allergy row in `lists` — that's the conflict.
        'lists' => [],
        'pnotes' => [
            [
                'date' => '2026-04-15 10:30:00',
                'title' => 'Intake note',
                'body' => 'Patient reports a sulfa allergy — developed rash on Bactrim as a teen. No allergy listed in chart prior to this visit.',
                'authorized' => 1,
                'activity' => 1,
            ],
        ],
        'prescriptions' => [],
        'procedure_orders' => [],
        'procedure_reports' => [],
        'procedure_results' => [],
        'expected_flags' => [
            ['rule_id' => 'narrative_only_allergy', 'category' => 'consistency'],
        ],
    ],

    [
        'name' => 'resolved_problem_still_active',
        'description' => 'Hypertension marked active in lists but recent note documents tapering complete.',
        'pid' => 90003,
        'pubpid' => 'test-fixture-discrepancy-3',
        'patient' => [
            'title' => 'Mr.',
            'fname' => 'Discrepancy',
            'lname' => 'ResolvedActive',
            'mname' => 'C',
            'DOB' => '1955-01-18',
            'sex' => 'Male',
            'street' => '3 Conflict Way',
            'city' => 'San Diego',
            'state' => 'CA',
            'postal_code' => '92101',
            'language' => 'english',
            'status' => 'married',
        ],
        'lists' => [
            [
                'type' => 'medical_problem',
                'title' => 'Hypertension',
                'diagnosis' => 'ICD10:I10',
                'begdate' => '2024-06-01',
                'enddate' => null,
                'activity' => 1,
                'comments' => 'Diet- and exercise-controlled.',
            ],
        ],
        'pnotes' => [
            [
                'date' => '2026-04-15 14:00:00',
                'title' => 'Office visit',
                'body' => 'BP 118/76 today, sustained over six months. Patient has completed taper off lisinopril. Considering hypertension resolved.',
                'authorized' => 1,
                'activity' => 1,
            ],
        ],
        'prescriptions' => [],
        'procedure_orders' => [],
        'procedure_reports' => [],
        'procedure_results' => [],
        'expected_flags' => [
            ['rule_id' => 'resolved_problem_still_active', 'category' => 'data_quality'],
        ],
    ],

    [
        'name' => 'allergen_med_safety_conflict',
        'description' => 'Penicillin allergy in lists alongside active Amoxicillin medication.',
        'pid' => 90004,
        'pubpid' => 'test-fixture-discrepancy-4',
        'patient' => [
            'title' => 'Mrs.',
            'fname' => 'Discrepancy',
            'lname' => 'AllergyMed',
            'mname' => 'D',
            'DOB' => '1970-11-05',
            'sex' => 'Female',
            'street' => '4 Conflict Way',
            'city' => 'San Diego',
            'state' => 'CA',
            'postal_code' => '92101',
            'language' => 'english',
            'status' => 'married',
        ],
        'lists' => [
            [
                'type' => 'allergy',
                'title' => 'Penicillin',
                'diagnosis' => 'RXCUI:7980',
                'reaction' => 'hives',
                'verification' => 'confirmed',
                'begdate' => '2010-01-01',
                'enddate' => null,
                'activity' => 1,
            ],
            [
                'type' => 'medication',
                'title' => 'Amoxicillin 500mg',
                'diagnosis' => 'RXCUI:723',
                'begdate' => '2026-03-20',
                'enddate' => null,
                'activity' => 1,
                'comments' => 'For sinusitis — 10-day course.',
            ],
        ],
        'pnotes' => [],
        'prescriptions' => [],
        'procedure_orders' => [],
        'procedure_reports' => [],
        'procedure_results' => [],
        'expected_flags' => [
            ['rule_id' => 'allergen_med_safety_conflict', 'category' => 'safety'],
        ],
    ],

    [
        'name' => 'stale_chronic_lab',
        'description' => 'Type 2 Diabetes diagnosis with HbA1c result older than 12 months.',
        'pid' => 90005,
        'pubpid' => 'test-fixture-discrepancy-5',
        'patient' => [
            'title' => 'Mr.',
            'fname' => 'Discrepancy',
            'lname' => 'StaleLab',
            'mname' => 'E',
            'DOB' => '1958-07-22',
            'sex' => 'Male',
            'street' => '5 Conflict Way',
            'city' => 'San Diego',
            'state' => 'CA',
            'postal_code' => '92101',
            'language' => 'english',
            'status' => 'married',
        ],
        'lists' => [
            [
                'type' => 'medical_problem',
                'title' => 'Type 2 Diabetes Mellitus',
                'diagnosis' => 'ICD10:E11.9',
                'begdate' => '2022-03-15',
                'enddate' => null,
                'activity' => 1,
                'comments' => 'Initial diagnosis fasting glucose 210; on metformin.',
            ],
        ],
        'pnotes' => [],
        'prescriptions' => [],
        'procedure_orders' => [
            [
                'procedure_order_id' => 90001,
                'date_ordered' => '2024-08-10 09:00:00',
                'date_collected' => '2024-08-15 08:30:00',
                'order_status' => 'complete',
                'activity' => 1,
                'procedure_order_type' => 'laboratory_test',
                'order_diagnosis' => 'ICD10:E11.9',
            ],
        ],
        'procedure_reports' => [
            [
                'procedure_report_id' => 90001,
                'procedure_order_id' => 90001,
                'date_collected' => '2024-08-15 08:30:00',
                'date_report' => '2024-08-15 16:00:00',
                'report_status' => 'complete',
                'review_status' => 'reviewed',
            ],
        ],
        'procedure_results' => [
            [
                'procedure_result_id' => 90001,
                'procedure_report_id' => 90001,
                'result_data_type' => 'N',
                'result_code' => '4548-4',
                'result_text' => 'Hemoglobin A1c',
                'date' => '2024-08-15 16:00:00',
                'units' => '%',
                'result' => '7.8',
                'range' => '4.0-5.6',
                'abnormal' => 'high',
                'result_status' => 'final',
            ],
        ],
        'expected_flags' => [
            ['rule_id' => 'stale_chronic_lab', 'category' => 'data_quality'],
        ],
    ],
];
