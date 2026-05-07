<?php

/**
 * Thrown by {@see DocumentClassifier::classify()} when no rule matches
 * the input file. The universal upload page surfaces the message to the
 * clinician so they can re-upload as a known type.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot;

final class ClassifierException extends \RuntimeException
{
}
