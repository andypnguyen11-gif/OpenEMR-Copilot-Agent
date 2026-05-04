-- Delete the five M5 fixture patients (pids 90001..90005) and their
-- attached clinical data. Run after the Synthea backfill is in place
-- and clinician panels are populated, otherwise the daily-brief renders
-- empty for admin until ``assign_patients_to_clinicians.php`` is re-run.
--
-- Safe to re-run: every DELETE filters by pid (or a join on patient_id)
-- and missing rows produce no error. Tables this touches were surveyed
-- on the development-easy local stack (lists=5, pnotes=3,
-- procedure_order=1, procedure_report=1, procedure_result=1,
-- patient_data=5) and on prod (assumed similar shape from
-- seed_fixture_patients.py's POST flow). If a deployment has additional
-- tables backing the M5 patients (form_encounter, prescriptions,
-- documents, etc.), extend this script — the survey query is in
-- ``scripts/copilot/README``-equivalent comments above each block.
--
-- Run locally:
--   docker compose exec mysql mariadb -u openemr -popenemr openemr \
--     < scripts/copilot/drop_m5_fixtures.sql
--
-- Run on prod (after backing up):
--   railway run -- mariadb $DATABASE_URL < scripts/copilot/drop_m5_fixtures.sql
--
-- After this completes, re-run scripts/copilot/assign_patients_to_clinicians.php
-- to top admin's panel back up to 7 patients (the five fixtures will be
-- replaced by random Synthea picks).

START TRANSACTION;

-- procedure_result → procedure_report → procedure_order (FK chain).
-- Delete leaves first so the joins still resolve.
DELETE pr FROM procedure_result pr
  JOIN procedure_report rpt ON rpt.procedure_report_id = pr.procedure_report_id
  JOIN procedure_order po   ON po.procedure_order_id   = rpt.procedure_order_id
 WHERE po.patient_id IN (90001, 90002, 90003, 90004, 90005);

DELETE rpt FROM procedure_report rpt
  JOIN procedure_order po ON po.procedure_order_id = rpt.procedure_order_id
 WHERE po.patient_id IN (90001, 90002, 90003, 90004, 90005);

DELETE FROM procedure_order WHERE patient_id IN (90001, 90002, 90003, 90004, 90005);

-- Conditions / Medications / Allergies all live in `lists` keyed by pid.
DELETE FROM lists WHERE pid IN (90001, 90002, 90003, 90004, 90005);

-- Notes (DocumentReference shape from the seed script).
DELETE FROM pnotes WHERE pid IN (90001, 90002, 90003, 90004, 90005);

-- Finally the demographics row. uuid_registry is intentionally not
-- touched — the M5 fixtures store their uuid directly on
-- patient_data.uuid (binary), and uuid_registry has no rows for these
-- pids on the surveyed local stack.
DELETE FROM patient_data WHERE pid IN (90001, 90002, 90003, 90004, 90005);

COMMIT;

SELECT
  (SELECT COUNT(*) FROM patient_data WHERE pid IN (90001,90002,90003,90004,90005)) AS patient_data_remaining,
  (SELECT COUNT(*) FROM lists       WHERE pid IN (90001,90002,90003,90004,90005)) AS lists_remaining,
  (SELECT COUNT(*) FROM pnotes      WHERE pid IN (90001,90002,90003,90004,90005)) AS pnotes_remaining;
