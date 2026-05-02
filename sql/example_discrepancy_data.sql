-- Generated from tests/Tests/Fixtures/discrepancy-scenarios.php — do not edit.
-- Run: php bin/generate-discrepancy-sql.php
--
-- Loads five seeded conflict scenarios for the discrepancy engine.
-- Apply after sql/example_patient_data.sql:
--   mysql -u openemr -p openemr < sql/example_discrepancy_data.sql

-- --------------------------------------------------------
-- Scenario: med_vs_note_conflict (pid=90001, pubpid=test-fixture-discrepancy-1)
-- Active metoprolol in lists; most recent note says discontinued for bradycardia.

INSERT INTO `patient_data` (`title`, `fname`, `lname`, `mname`, `DOB`, `sex`, `street`, `city`, `state`, `postal_code`, `language`, `status`, `pid`, `pubpid`, `date`) VALUES ('Mr.', 'Discrepancy', 'MedNoteOne', 'A', '1962-04-12', 'Male', '1 Conflict Way', 'San Diego', 'CA', '92101', 'english', 'married', 90001, 'test-fixture-discrepancy-1', '2024-06-01 00:00:00');
INSERT INTO `lists` (`type`, `title`, `diagnosis`, `begdate`, `enddate`, `activity`, `comments`, `pid`) VALUES ('medication', 'Metoprolol Tartrate 50mg', 'RXCUI:866924', '2024-06-01', NULL, 1, 'For hypertension management', 90001);
INSERT INTO `pnotes` (`date`, `title`, `body`, `authorized`, `activity`, `pid`) VALUES ('2026-04-15 09:00:00', 'Office visit', 'Patient developed bradycardia. Discontinued metoprolol effective today; follow up in two weeks for BP recheck.', 1, 1, 90001);

-- --------------------------------------------------------
-- Scenario: narrative_only_allergy (pid=90002, pubpid=test-fixture-discrepancy-2)
-- Sulfa allergy mentioned only in note text; no row in lists.type=allergy.

INSERT INTO `patient_data` (`title`, `fname`, `lname`, `mname`, `DOB`, `sex`, `street`, `city`, `state`, `postal_code`, `language`, `status`, `pid`, `pubpid`, `date`) VALUES ('Ms.', 'Discrepancy', 'NarrativeAllergy', 'B', '1978-09-30', 'Female', '2 Conflict Way', 'San Diego', 'CA', '92101', 'english', 'single', 90002, 'test-fixture-discrepancy-2', '2024-06-01 00:00:00');
INSERT INTO `pnotes` (`date`, `title`, `body`, `authorized`, `activity`, `pid`) VALUES ('2026-04-15 10:30:00', 'Intake note', 'Patient reports a sulfa allergy — developed rash on Bactrim as a teen. No allergy listed in chart prior to this visit.', 1, 1, 90002);

-- --------------------------------------------------------
-- Scenario: resolved_problem_still_active (pid=90003, pubpid=test-fixture-discrepancy-3)
-- Hypertension marked active in lists but recent note documents tapering complete.

INSERT INTO `patient_data` (`title`, `fname`, `lname`, `mname`, `DOB`, `sex`, `street`, `city`, `state`, `postal_code`, `language`, `status`, `pid`, `pubpid`, `date`) VALUES ('Mr.', 'Discrepancy', 'ResolvedActive', 'C', '1955-01-18', 'Male', '3 Conflict Way', 'San Diego', 'CA', '92101', 'english', 'married', 90003, 'test-fixture-discrepancy-3', '2024-06-01 00:00:00');
INSERT INTO `lists` (`type`, `title`, `diagnosis`, `begdate`, `enddate`, `activity`, `comments`, `pid`) VALUES ('medical_problem', 'Hypertension', 'ICD10:I10', '2024-06-01', NULL, 1, 'Diet- and exercise-controlled.', 90003);
INSERT INTO `pnotes` (`date`, `title`, `body`, `authorized`, `activity`, `pid`) VALUES ('2026-04-15 14:00:00', 'Office visit', 'BP 118/76 today, sustained over six months. Patient has completed taper off lisinopril. Considering hypertension resolved.', 1, 1, 90003);

-- --------------------------------------------------------
-- Scenario: allergen_med_safety_conflict (pid=90004, pubpid=test-fixture-discrepancy-4)
-- Penicillin allergy in lists alongside active Amoxicillin medication.

INSERT INTO `patient_data` (`title`, `fname`, `lname`, `mname`, `DOB`, `sex`, `street`, `city`, `state`, `postal_code`, `language`, `status`, `pid`, `pubpid`, `date`) VALUES ('Mrs.', 'Discrepancy', 'AllergyMed', 'D', '1970-11-05', 'Female', '4 Conflict Way', 'San Diego', 'CA', '92101', 'english', 'married', 90004, 'test-fixture-discrepancy-4', '2024-06-01 00:00:00');
INSERT INTO `lists` (`type`, `title`, `diagnosis`, `reaction`, `verification`, `begdate`, `enddate`, `activity`, `pid`) VALUES ('allergy', 'Penicillin', 'RXCUI:7980', 'hives', 'confirmed', '2010-01-01', NULL, 1, 90004);
INSERT INTO `lists` (`type`, `title`, `diagnosis`, `begdate`, `enddate`, `activity`, `comments`, `pid`) VALUES ('medication', 'Amoxicillin 500mg', 'RXCUI:723', '2026-03-20', NULL, 1, 'For sinusitis — 10-day course.', 90004);

-- --------------------------------------------------------
-- Scenario: stale_chronic_lab (pid=90005, pubpid=test-fixture-discrepancy-5)
-- Type 2 Diabetes diagnosis with HbA1c result older than 12 months.

INSERT INTO `patient_data` (`title`, `fname`, `lname`, `mname`, `DOB`, `sex`, `street`, `city`, `state`, `postal_code`, `language`, `status`, `pid`, `pubpid`, `date`) VALUES ('Mr.', 'Discrepancy', 'StaleLab', 'E', '1958-07-22', 'Male', '5 Conflict Way', 'San Diego', 'CA', '92101', 'english', 'married', 90005, 'test-fixture-discrepancy-5', '2024-06-01 00:00:00');
INSERT INTO `lists` (`type`, `title`, `diagnosis`, `begdate`, `enddate`, `activity`, `comments`, `pid`) VALUES ('medical_problem', 'Type 2 Diabetes Mellitus', 'ICD10:E11.9', '2022-03-15', NULL, 1, 'Initial diagnosis fasting glucose 210; on metformin.', 90005);
INSERT INTO `procedure_order` (`procedure_order_id`, `date_ordered`, `date_collected`, `order_status`, `activity`, `procedure_order_type`, `order_diagnosis`, `patient_id`) VALUES (90001, '2024-08-10 09:00:00', '2024-08-15 08:30:00', 'complete', 1, 'laboratory_test', 'ICD10:E11.9', 90005);
INSERT INTO `procedure_report` (`procedure_report_id`, `procedure_order_id`, `date_collected`, `date_report`, `report_status`, `review_status`) VALUES (90001, 90001, '2024-08-15 08:30:00', '2024-08-15 16:00:00', 'complete', 'reviewed');
INSERT INTO `procedure_result` (`procedure_result_id`, `procedure_report_id`, `result_data_type`, `result_code`, `result_text`, `date`, `units`, `result`, `range`, `abnormal`, `result_status`) VALUES (90001, 90001, 'N', '4548-4', 'Hemoglobin A1c', '2024-08-15 16:00:00', '%', '7.8', '4.0-5.6', 'high', 'final');
