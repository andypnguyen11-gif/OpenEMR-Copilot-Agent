<?php

/**
 * Clinical Co-Pilot — extracted-intake review page (PR W2-02).
 *
 * Renders the agent-extracted intake form facts as a single editable
 * form. The clinician confirms / adjusts demographics, problems,
 * medications, allergies, and family history; on submit
 * ``new_patient_save_ai.php`` creates the patient record and seeds the
 * lists tables in one transaction.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

require_once(__DIR__ . "/../globals.php");

use GuzzleHttp\Client as GuzzleClient;
use GuzzleHttp\Psr7\HttpFactory;
use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Core\Header;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\AgentServiceException;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use OpenEMR\Services\Copilot\ExtractedFieldHelper;

if (!AclMain::aclCheckCore('admin', 'super')) {
    http_response_code(403);
    exit('forbidden');
}

$documentId = (string) (filter_input(INPUT_GET, 'document_id') ?? '');
if ($documentId === '') {
    http_response_code(400);
    exit('missing document_id');
}

$globals = OEGlobalsBag::getInstance();
$webrootRaw = $globals->get('webroot', '');
$webroot = is_string($webrootRaw) ? $webrootRaw : '';

$config = new CopilotConfig($globals);
$factory = new HttpFactory();
$httpClient = new GuzzleClient([
    'timeout' => max($config->getAgentTimeoutSeconds() * 4, 30),
    'http_errors' => false,
]);
$agentClient = new AgentHttpClient($httpClient, $factory, $config);

$facts = null;
$loadError = '';
try {
    $response = $agentClient->getInternal(
        '/api/agent/internal/extracted/' . rawurlencode($documentId),
        $config->getInternalToken(),
    );
    if ($response->statusCode === 200) {
        $facts = $response->body;
    } elseif ($response->statusCode === 404) {
        $loadError = 'Extraction record not found. Re-upload the document.';
    } else {
        $loadError = 'Unexpected status ' . $response->statusCode . ' from extractor.';
    }
} catch (AgentServiceException $e) {
    $loadError = 'Could not reach extractor: ' . $e->getMessage();
}

/** @var array<string, mixed> $factsArr */
$factsArr = is_array($facts) ? $facts : [];
$fname = ExtractedFieldHelper::value($factsArr['legal_first_name'] ?? null);
$lname = ExtractedFieldHelper::value($factsArr['legal_last_name'] ?? null);
$dob = ExtractedFieldHelper::value($factsArr['date_of_birth'] ?? null);
$sex = ExtractedFieldHelper::value($factsArr['sex_assigned_at_birth'] ?? null);
$phone = ExtractedFieldHelper::value($factsArr['phone'] ?? null);
$email = ExtractedFieldHelper::value($factsArr['email'] ?? null);
$mrn = ExtractedFieldHelper::value($factsArr['medical_record_number'] ?? null);
$chiefComplaint = ExtractedFieldHelper::value($factsArr['chief_complaint'] ?? null);
$tobaccoStatus = ExtractedFieldHelper::value($factsArr['tobacco_status'] ?? null);
$tobaccoPackYears = ExtractedFieldHelper::value($factsArr['tobacco_pack_years'] ?? null);

$problems = ExtractedFieldHelper::rowList($factsArr['active_problems'] ?? null);
$meds = ExtractedFieldHelper::rowList($factsArr['current_medications'] ?? null);
$allergies = ExtractedFieldHelper::rowList($factsArr['reported_allergies'] ?? null);
$family = ExtractedFieldHelper::rowList($factsArr['family_history'] ?? null);

$csrfToken = CsrfUtils::collectCsrfToken(
    session: SessionWrapperFactory::getInstance()->getActiveSession(),
);

Header::setupHeader();
?>
<!DOCTYPE html>
<html>
<head>
    <title>Review extracted intake form</title>
    <style>
        body { font-family: system-ui, sans-serif; padding: 2rem; max-width: 1100px; }
        h1 { margin-top: 0; }
        h2 { border-bottom: 2px solid #ddd; padding-bottom: 0.3rem; margin-top: 2rem; }
        .alert-error { background: #fee; border: 1px solid #faa; padding: 0.75rem 1rem; margin: 1rem 0; }
        table { border-collapse: collapse; width: 100%; margin-bottom: 1rem; }
        th, td { border: 1px solid #ccc; padding: 0.4rem 0.6rem; vertical-align: top; }
        th { background: #f4f4f4; text-align: left; }
        input[type=text], input[type=date], select, textarea { width: 100%; box-sizing: border-box; }
        textarea { min-height: 60px; }
        .demo-grid { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 0.75rem; }
        .demo-grid > div { display: flex; flex-direction: column; }
        .demo-grid label { font-weight: 600; margin-bottom: 0.3rem; }
        .citation { font-size: 0.8em; color: #555; margin-top: 0.2rem; }
        .actions { margin: 1.5rem 0; }
        button { padding: 0.6rem 1.2rem; font-size: 1rem; }
    </style>
</head>
<body>
<h1>Review extracted intake form</h1>
<p>
    Document: <code><?php echo htmlspecialchars($documentId, ENT_QUOTES, 'UTF-8'); ?></code>
    | Problems: <?php echo count($problems); ?>
    | Meds: <?php echo count($meds); ?>
    | Allergies: <?php echo count($allergies); ?>
    | Family: <?php echo count($family); ?>
</p>

<?php if ($loadError !== ''): ?>
    <div class="alert-error"><?php echo htmlspecialchars($loadError, ENT_QUOTES, 'UTF-8'); ?></div>
<?php else: ?>

<form method="post" action="<?php echo htmlspecialchars($webroot . '/interface/copilot/new_patient_save_ai.php', ENT_QUOTES, 'UTF-8'); ?>">
    <input type="hidden" name="csrf_token_form" value="<?php echo htmlspecialchars($csrfToken, ENT_QUOTES, 'UTF-8'); ?>">
    <input type="hidden" name="document_id" value="<?php echo htmlspecialchars($documentId, ENT_QUOTES, 'UTF-8'); ?>">

    <h2>Demographics</h2>
    <div class="demo-grid">
        <div>
            <label>First name</label>
            <input type="text" name="fname" value="<?php echo htmlspecialchars($fname, ENT_QUOTES, 'UTF-8'); ?>" required>
            <div class="citation"><?php echo htmlspecialchars(ExtractedFieldHelper::citationText($factsArr['legal_first_name'] ?? null), ENT_QUOTES, 'UTF-8'); ?></div>
        </div>
        <div>
            <label>Last name</label>
            <input type="text" name="lname" value="<?php echo htmlspecialchars($lname, ENT_QUOTES, 'UTF-8'); ?>" required>
            <div class="citation"><?php echo htmlspecialchars(ExtractedFieldHelper::citationText($factsArr['legal_last_name'] ?? null), ENT_QUOTES, 'UTF-8'); ?></div>
        </div>
        <div>
            <label>Date of birth</label>
            <input type="date" name="dob" value="<?php echo htmlspecialchars($dob, ENT_QUOTES, 'UTF-8'); ?>" required>
            <div class="citation"><?php echo htmlspecialchars(ExtractedFieldHelper::citationText($factsArr['date_of_birth'] ?? null), ENT_QUOTES, 'UTF-8'); ?></div>
        </div>
        <div>
            <label>Sex</label>
            <select name="sex" required>
                <option value="">—</option>
                <?php foreach (['Female', 'Male', 'Other', 'Unknown'] as $opt): ?>
                    <option value="<?php echo $opt; ?>" <?php echo $sex === $opt ? 'selected' : ''; ?>><?php echo $opt; ?></option>
                <?php endforeach; ?>
            </select>
            <div class="citation"><?php echo htmlspecialchars(ExtractedFieldHelper::citationText($factsArr['sex_assigned_at_birth'] ?? null), ENT_QUOTES, 'UTF-8'); ?></div>
        </div>
        <div>
            <label>Phone</label>
            <input type="text" name="phone" value="<?php echo htmlspecialchars($phone, ENT_QUOTES, 'UTF-8'); ?>">
        </div>
        <div>
            <label>Email</label>
            <input type="text" name="email" value="<?php echo htmlspecialchars($email, ENT_QUOTES, 'UTF-8'); ?>">
        </div>
        <div>
            <label>External MRN</label>
            <input type="text" name="external_mrn" value="<?php echo htmlspecialchars($mrn, ENT_QUOTES, 'UTF-8'); ?>">
        </div>
        <div>
            <label>Tobacco</label>
            <select name="tobacco_status">
                <option value="">—</option>
                <?php foreach (['never', 'former', 'current'] as $opt): ?>
                    <option value="<?php echo $opt; ?>" <?php echo $tobaccoStatus === $opt ? 'selected' : ''; ?>><?php echo $opt; ?></option>
                <?php endforeach; ?>
            </select>
            <input type="text" name="tobacco_pack_years" placeholder="pack-years" value="<?php echo htmlspecialchars($tobaccoPackYears, ENT_QUOTES, 'UTF-8'); ?>" style="margin-top:0.3rem;">
        </div>
    </div>

    <h2>Chief complaint</h2>
    <textarea name="chief_complaint"><?php echo htmlspecialchars($chiefComplaint, ENT_QUOTES, 'UTF-8'); ?></textarea>

    <h2>Active problems / past medical history</h2>
    <table>
        <thead><tr><th>Save?</th><th>Condition</th><th>ICD-10</th><th>SNOMED</th><th>Onset</th><th>Citation</th></tr></thead>
        <tbody>
        <?php foreach ($problems as $i => $p): ?>
            <?php
            $condition = ExtractedFieldHelper::value($p['condition'] ?? null);
            $icd = ExtractedFieldHelper::value($p['icd10'] ?? null);
            $snomed = ExtractedFieldHelper::value($p['snomed'] ?? null);
            $onset = ExtractedFieldHelper::value($p['onset_year'] ?? null);
            $cite = ExtractedFieldHelper::citationText($p['condition'] ?? null);
            ?>
            <tr>
                <td><input type="checkbox" name="problem_save[<?php echo $i; ?>]" value="1" <?php echo $condition !== '' ? 'checked' : ''; ?>></td>
                <td><input type="text" name="problem_condition[<?php echo $i; ?>]" value="<?php echo htmlspecialchars($condition, ENT_QUOTES, 'UTF-8'); ?>"></td>
                <td><input type="text" name="problem_icd10[<?php echo $i; ?>]" value="<?php echo htmlspecialchars($icd, ENT_QUOTES, 'UTF-8'); ?>" style="width:80px;"></td>
                <td><input type="text" name="problem_snomed[<?php echo $i; ?>]" value="<?php echo htmlspecialchars($snomed, ENT_QUOTES, 'UTF-8'); ?>" style="width:100px;"></td>
                <td><input type="text" name="problem_onset[<?php echo $i; ?>]" value="<?php echo htmlspecialchars($onset, ENT_QUOTES, 'UTF-8'); ?>" style="width:80px;"></td>
                <td class="citation"><?php echo htmlspecialchars($cite, ENT_QUOTES, 'UTF-8'); ?></td>
            </tr>
        <?php endforeach; ?>
        </tbody>
    </table>

    <h2>Current medications</h2>
    <table>
        <thead><tr><th>Save?</th><th>Name</th><th>Dose</th><th>Frequency</th><th>RxNorm</th><th>Indication</th><th>Started</th><th>Citation</th></tr></thead>
        <tbody>
        <?php foreach ($meds as $i => $m): ?>
            <?php
            $name = ExtractedFieldHelper::value($m['name'] ?? null);
            $dose = ExtractedFieldHelper::value($m['dose'] ?? null);
            $freq = ExtractedFieldHelper::value($m['frequency'] ?? null);
            $rxnorm = ExtractedFieldHelper::value($m['rxnorm'] ?? null);
            $indication = ExtractedFieldHelper::value($m['indication'] ?? null);
            $started = ExtractedFieldHelper::value($m['started_year'] ?? null);
            $cite = ExtractedFieldHelper::citationText($m['name'] ?? null);
            ?>
            <tr>
                <td><input type="checkbox" name="med_save[<?php echo $i; ?>]" value="1" <?php echo $name !== '' ? 'checked' : ''; ?>></td>
                <td><input type="text" name="med_name[<?php echo $i; ?>]" value="<?php echo htmlspecialchars($name, ENT_QUOTES, 'UTF-8'); ?>"></td>
                <td><input type="text" name="med_dose[<?php echo $i; ?>]" value="<?php echo htmlspecialchars($dose, ENT_QUOTES, 'UTF-8'); ?>" style="width:80px;"></td>
                <td><input type="text" name="med_freq[<?php echo $i; ?>]" value="<?php echo htmlspecialchars($freq, ENT_QUOTES, 'UTF-8'); ?>" style="width:120px;"></td>
                <td><input type="text" name="med_rxnorm[<?php echo $i; ?>]" value="<?php echo htmlspecialchars($rxnorm, ENT_QUOTES, 'UTF-8'); ?>" style="width:80px;"></td>
                <td><input type="text" name="med_indication[<?php echo $i; ?>]" value="<?php echo htmlspecialchars($indication, ENT_QUOTES, 'UTF-8'); ?>"></td>
                <td><input type="text" name="med_started[<?php echo $i; ?>]" value="<?php echo htmlspecialchars($started, ENT_QUOTES, 'UTF-8'); ?>" style="width:70px;"></td>
                <td class="citation"><?php echo htmlspecialchars($cite, ENT_QUOTES, 'UTF-8'); ?></td>
            </tr>
        <?php endforeach; ?>
        </tbody>
    </table>

    <h2>Allergies</h2>
    <table>
        <thead><tr><th>Save?</th><th>Substance</th><th>Reaction</th><th>Severity</th><th>Citation</th></tr></thead>
        <tbody>
        <?php foreach ($allergies as $i => $a): ?>
            <?php
            $substance = ExtractedFieldHelper::value($a['substance'] ?? null);
            $reaction = ExtractedFieldHelper::value($a['reaction'] ?? null);
            $severity = ExtractedFieldHelper::value($a['severity'] ?? null);
            $cite = ExtractedFieldHelper::citationText($a['substance'] ?? null);
            ?>
            <tr>
                <td><input type="checkbox" name="allergy_save[<?php echo $i; ?>]" value="1" <?php echo $substance !== '' ? 'checked' : ''; ?>></td>
                <td><input type="text" name="allergy_substance[<?php echo $i; ?>]" value="<?php echo htmlspecialchars($substance, ENT_QUOTES, 'UTF-8'); ?>"></td>
                <td><input type="text" name="allergy_reaction[<?php echo $i; ?>]" value="<?php echo htmlspecialchars($reaction, ENT_QUOTES, 'UTF-8'); ?>"></td>
                <td><input type="text" name="allergy_severity[<?php echo $i; ?>]" value="<?php echo htmlspecialchars($severity, ENT_QUOTES, 'UTF-8'); ?>" style="width:100px;"></td>
                <td class="citation"><?php echo htmlspecialchars($cite, ENT_QUOTES, 'UTF-8'); ?></td>
            </tr>
        <?php endforeach; ?>
        </tbody>
    </table>

    <h2>Family history</h2>
    <table>
        <thead><tr><th>Save?</th><th>Relation</th><th>Condition</th><th>Onset age</th><th>Status</th><th>Citation</th></tr></thead>
        <tbody>
        <?php foreach ($family as $i => $fh): ?>
            <?php
            $relation = ExtractedFieldHelper::value($fh['relation'] ?? null);
            $condition = ExtractedFieldHelper::value($fh['condition'] ?? null);
            $onsetAge = ExtractedFieldHelper::value($fh['onset_age'] ?? null);
            $statusStr = ExtractedFieldHelper::value($fh['status'] ?? null);
            $cite = ExtractedFieldHelper::citationText($fh['relation'] ?? null);
            ?>
            <tr>
                <td><input type="checkbox" name="fhx_save[<?php echo $i; ?>]" value="1" <?php echo $relation !== '' ? 'checked' : ''; ?>></td>
                <td><input type="text" name="fhx_relation[<?php echo $i; ?>]" value="<?php echo htmlspecialchars($relation, ENT_QUOTES, 'UTF-8'); ?>" style="width:100px;"></td>
                <td><input type="text" name="fhx_condition[<?php echo $i; ?>]" value="<?php echo htmlspecialchars($condition, ENT_QUOTES, 'UTF-8'); ?>"></td>
                <td><input type="text" name="fhx_onset_age[<?php echo $i; ?>]" value="<?php echo htmlspecialchars($onsetAge, ENT_QUOTES, 'UTF-8'); ?>" style="width:80px;"></td>
                <td><input type="text" name="fhx_status[<?php echo $i; ?>]" value="<?php echo htmlspecialchars($statusStr, ENT_QUOTES, 'UTF-8'); ?>"></td>
                <td class="citation"><?php echo htmlspecialchars($cite, ENT_QUOTES, 'UTF-8'); ?></td>
            </tr>
        <?php endforeach; ?>
        </tbody>
    </table>

    <div class="actions">
        <button type="submit">Confirm and create patient</button>
    </div>
</form>
<?php endif; ?>
</body>
</html>
