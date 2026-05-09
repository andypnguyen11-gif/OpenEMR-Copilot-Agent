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
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Core\Migrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

final class Version20260509201506 extends AbstractMigration
{
    public function getDescription(): string
    {
        return 'Add chart-write idempotency markers to documents table';
    }

    public function up(Schema $schema): void
    {
        $this->addSql(
            'ALTER TABLE documents '
            . 'ADD COLUMN chart_write_started_at DATETIME NULL DEFAULT NULL '
            . 'COMMENT \'Co-Pilot: lock-acquired timestamp, bounded by 5-minute TTL\', '
            . 'ADD COLUMN chart_written_at DATETIME NULL DEFAULT NULL '
            . 'COMMENT \'Co-Pilot: successful chart-write COMMIT timestamp\', '
            . 'ADD COLUMN chart_write_summary LONGTEXT NULL DEFAULT NULL '
            . 'COMMENT \'Co-Pilot: JSON summary of the original chart write for idempotent replay\''
        );
    }

    public function down(Schema $schema): void
    {
        $this->addSql(
            'ALTER TABLE documents '
            . 'DROP COLUMN chart_write_summary, '
            . 'DROP COLUMN chart_written_at, '
            . 'DROP COLUMN chart_write_started_at'
        );
    }
}
