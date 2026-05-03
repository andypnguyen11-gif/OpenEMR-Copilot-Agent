--
-- Clinical Co-Pilot custom-module registration (PR 17).
--
-- The side-panel subscriber lives at
-- ``interface/modules/custom_modules/oe-module-copilot``. OpenEMR's
-- module loader (``OpenEMR\Core\ModulesApplication::bootstrapCustomModules``)
-- only includes a custom module's ``openemr.bootstrap.php`` when the
-- ``modules`` table has an active row for it. This file does that
-- registration idempotently:
--
--   docker compose exec mysql mysql -uopenemr -popenemr -hmysql openemr \
--     < sql/copilot_module_install.sql
--
-- Run once per environment (dev / prod). Re-running is safe — the
-- INSERT is gated by NOT EXISTS.
--

INSERT INTO `modules`
    (`mod_name`, `mod_directory`, `mod_parent`, `mod_type`, `mod_active`,
     `mod_ui_name`, `mod_relative_link`, `mod_ui_order`, `mod_ui_active`,
     `mod_description`, `mod_nick_name`, `mod_enc_menu`,
     `permissions_item_table`, `directory`, `date`, `sql_run`, `type`,
     `sql_version`, `acl_version`)
SELECT
    'Copilot', 'oe-module-copilot', '', '', 1,
    'Co-Pilot', '', 0, 1,
    'Clinical Co-Pilot in-chart side panel', '', '',
    NULL, '', NOW(), 1, 0,
    '0', ''
FROM DUAL
WHERE NOT EXISTS (
    SELECT 1 FROM `modules` WHERE `mod_directory` = 'oe-module-copilot'
);

-- Re-enable an existing row (covers a manual disable in the UI):
UPDATE `modules`
   SET `mod_active` = 1, `mod_ui_active` = 1
 WHERE `mod_directory` = 'oe-module-copilot';
