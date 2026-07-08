<?php

/**
 * Adds chart-write idempotency markers to the ``documents`` table so
 * the Co-Pilot save-document endpoint can detect (and short-circuit)
 * duplicate submissions.
 *
 * Three columns:
 *   - ``chart_write_started_at`` — set when a save acquires the lock
 *     via the conditional UPDATE in
 *     ``interface/copilot/api/save_document.php``. NULL until first
 *     attempt; bounded by the 5-minute TTL clause so a crashed worker
 *     doesn't hold the lock forever.
 *   - ``chart_written_at`` — set on successful COMMIT. Once non-NULL
 *     the row is "done"; subsequent submits short-circuit to a
 *     200-idempotent response carrying the original summary.
 *   - ``chart_write_summary`` — JSON dump of pid / patient_created /
 *     selected_sections / row_counts / redirect_target so the
 *     idempotent reply can rebuild the success URL without re-running
 *     the chart-write block.
 *
 * Stored as LONGTEXT (not the MariaDB JSON alias) so the column reads
 * back as a plain string regardless of driver flags. The endpoint
 * encodes via ``json_encode`` and decodes via ``json_decode``; no
 * server-side JSON path queries are needed.
 *
 * Idempotency / production-drift safety
 * -------------------------------------
 * The three columns already exist on some environments that never had a
 * ``doctrine_migration_versions`` tracking table (e.g. production, where
 * they were applied out-of-band, and fresh installs built from
 * ``sql/database.sql``, which now declares them). Running this migration
 * there with an unconditional ``ALTER TABLE ... ADD COLUMN`` would abort
 * with a duplicate-column error. Both ``up()`` and ``down()`` therefore
 * inspect the live schema and only emit DDL for the columns whose current
 * state actually needs changing, skipping entirely when there is nothing
 * to do. This keeps the migration safe to (re-)run against a drifted DB.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Core\Migrations;

use Doctrine\DBAL\Schema\Column;
use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

final class Version20260509201506 extends AbstractMigration
{
    /**
     * ``ADD COLUMN`` clause for each chart-write column, keyed by name.
     */
    private const ADD_CLAUSES = [
        'chart_write_started_at' => 'ADD COLUMN chart_write_started_at DATETIME NULL DEFAULT NULL '
            . "COMMENT 'Co-Pilot: lock-acquired timestamp, bounded by 5-minute TTL'",
        'chart_written_at' => 'ADD COLUMN chart_written_at DATETIME NULL DEFAULT NULL '
            . "COMMENT 'Co-Pilot: successful chart-write COMMIT timestamp'",
        'chart_write_summary' => 'ADD COLUMN chart_write_summary LONGTEXT NULL DEFAULT NULL '
            . "COMMENT 'Co-Pilot: JSON summary of the original chart write for idempotent replay'",
    ];

    public function getDescription(): string
    {
        return 'Add chart-write idempotency markers to documents table';
    }

    public function up(Schema $schema): void
    {
        $this->skipIf(
            !$this->sm->tablesExist(['documents']),
            'documents table not present; skipping chart-write column migration.'
        );

        $existing = $this->existingDocumentColumns();

        $clauses = [];
        foreach (self::ADD_CLAUSES as $column => $clause) {
            if (!in_array($column, $existing, true)) {
                $clauses[] = $clause;
            }
        }

        $this->skipIf(
            $clauses === [],
            'All chart-write columns already present on documents; nothing to add.'
        );

        $this->addSql('ALTER TABLE documents ' . implode(', ', $clauses));
    }

    public function down(Schema $schema): void
    {
        $this->skipIf(
            !$this->sm->tablesExist(['documents']),
            'documents table not present; skipping chart-write column rollback.'
        );

        $existing = $this->existingDocumentColumns();

        $clauses = [];
        // Drop in reverse declaration order for symmetry with up().
        foreach (['chart_write_summary', 'chart_written_at', 'chart_write_started_at'] as $column) {
            if (in_array($column, $existing, true)) {
                $clauses[] = 'DROP COLUMN ' . $column;
            }
        }

        $this->skipIf(
            $clauses === [],
            'No chart-write columns present on documents; nothing to drop.'
        );

        $this->addSql('ALTER TABLE documents ' . implode(', ', $clauses));
    }

    /**
     * Lower-cased column names currently defined on the ``documents`` table.
     *
     * @return list<string>
     */
    private function existingDocumentColumns(): array
    {
        return array_values(array_map(
            static fn (Column $column): string => strtolower($column->getName()),
            $this->sm->listTableColumns('documents')
        ));
    }
}
