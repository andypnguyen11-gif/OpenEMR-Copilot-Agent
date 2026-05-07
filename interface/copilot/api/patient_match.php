<?php

/**
 * Clinical Co-Pilot — patient match endpoint
 * (Week 2 multimodal expansion, Step 4).
 *
 * Accepts a JSON body of extracted demographics and returns a ranked
 * candidate list of existing patients that the document might belong
 * to. The chart-side ``document_review.php`` calls this when the
 * extractor surfaces patient_name / patient_dob / patient_mrn so the
 * clinician sees a "Confirm match" / "Pick different" / "Create new"
 * decision rather than a free-form patient picker.
 *
 * Request body (JSON):
 *   {
 *     "first_name": "Margaret",
 *     "last_name": "Chen",
 *     "dob": "1968-03-12",
 *     "mrn": "BHS-2847163"
 *   }
 *   All four fields are optional but the matcher returns nothing
 *   when last_name AND mrn are both null.
 *
 * Response body (JSON):
 *   {
 *     "candidates": [
 *       { "pid": 42, "uuid": "...", "first_name": "...", "last_name": "...",
 *         "dob": "1968-03-12", "mrn": "...", "score": 0.95,
 *         "match_reason": "Full name + DOB exact" }, ...
 *     ],
 *     "preselect_threshold": 0.90,
 *     "review_threshold": 0.60
 *   }
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

require_once(__DIR__ . "/../_site_recovery.php");
require_once(__DIR__ . "/../../globals.php");

use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Services\Copilot\PatientMatch\PatientMatchScorer;
use OpenEMR\Services\Copilot\PatientMatch\PatientMatchService;

if (!AclMain::aclCheckCore('patients', 'demo')) {
    http_response_code(403);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'forbidden']);
    exit;
}

$body = file_get_contents('php://input');
$decoded = $body !== false && $body !== '' ? json_decode($body, true) : null;
if (!is_array($decoded)) {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'request body must be a JSON object']);
    exit;
}

$firstName = isset($decoded['first_name']) && is_string($decoded['first_name']) && $decoded['first_name'] !== ''
    ? $decoded['first_name']
    : null;
$lastName = isset($decoded['last_name']) && is_string($decoded['last_name']) && $decoded['last_name'] !== ''
    ? $decoded['last_name']
    : null;
$dob = isset($decoded['dob']) && is_string($decoded['dob']) && $decoded['dob'] !== ''
    ? $decoded['dob']
    : null;
$mrn = isset($decoded['mrn']) && is_string($decoded['mrn']) && $decoded['mrn'] !== ''
    ? $decoded['mrn']
    : null;

$service = new PatientMatchService();
$candidates = $service->match($firstName, $lastName, $dob, $mrn);

header('Content-Type: application/json');
echo json_encode([
    'candidates' => array_map(
        static fn ($c) => $c->toArray(),
        $candidates,
    ),
    'preselect_threshold' => PatientMatchScorer::PRESELECT_THRESHOLD,
    'review_threshold' => PatientMatchScorer::REVIEW_THRESHOLD,
]);
