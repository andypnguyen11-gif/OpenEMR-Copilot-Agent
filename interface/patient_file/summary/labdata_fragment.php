<?php

/**
 * labdata_fragment.php
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Joe Slam <joe@produnis.de>
 * @author    Brady Miller <brady.g.miller@gmail.com>
 * @copyright Copyright (c) 2014 Joe Slam <joe@produnis.de>
 * @copyright Copyright (c) 2018 Brady Miller <brady.g.miller@gmail.com>
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

require_once("../../globals.php");

use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Core\OEGlobalsBag;

$session = SessionWrapperFactory::getInstance()->getActiveSession();
CsrfUtils::checkCsrfInput(INPUT_POST, dieOnFail: true);

$labFragmentWebrootRaw = OEGlobalsBag::getInstance()->get('webroot', '');
$labFragmentWebroot = is_string($labFragmentWebrootRaw) ? $labFragmentWebrootRaw : '';

// Resolve $pid explicitly from the patient session rather than relying
// on the implicit global set in globals.php — keeps the value typed
// for downstream uses (the SELECT below, the upload-doc link).
$pidFromSession = $session->get('pid');
$pid = is_int($pidFromSession)
    ? $pidFromSession
    : (is_numeric($pidFromSession) ? (int) $pidFromSession : 0);

?>
<div id='labdata' style='margin-top: 3px; margin-left: 10px; margin-right: 10px'><!--outer div-->
<div class="copilot-lab-upload" style="margin-top: 6px; margin-bottom: 6px;">
    <a href="<?php echo attr($labFragmentWebroot); ?>/interface/copilot/upload_lab.php?pid=<?php echo attr_url((string) $pid); ?>"
       target="_blank"
       rel="noopener"
       class="btn btn-sm btn-outline-primary"
       onclick="top.restoreSession()">
        <?php echo xlt('Upload lab document (AI extract)'); ?>
    </a>
</div>
<?php
//retrieve most recent set of labdata.
$spell = "SELECT procedure_report.date_collected AS thedate, " .
            "procedure_order_code.procedure_name AS theprocedure, " .
            "procedure_order.encounter_id AS theencounter " .
            "FROM procedure_report " .
            "JOIN procedure_order ON  procedure_report.procedure_order_id = procedure_order.procedure_order_id " .
            "JOIN procedure_order_code ON procedure_order.procedure_order_id = procedure_order_code.procedure_order_id " .
            "WHERE procedure_order.patient_id = ? " .
            "ORDER BY procedure_report.date_collected DESC ";
$result = sqlQuery($spell, [$pid]);

if (!$result) { //If there are no lab data recorded
    ?>
  <span class='text'> <?php echo xlt("No lab data documented.");
    ?>
  </span>
    <?php
} else {
    ?>
  <span class='text'><b>
    <?php echo xlt('Most recent lab data:'); ?>
  </b>
  <br />
    <?php
    echo xlt('Procedure') . ": " . text($result['theprocedure']) . " (" . text($result['thedate']) . ")<br />";
    echo xlt('Encounter') . ": <a href='../../patient_file/encounter/encounter_top.php?set_encounter=" . attr_url($result['theencounter']) . "' target='RBot'>" . text($result['theencounter']) . "</a>";
    ?>
  <br />
  </span><span class='text'>
  <br />
  <a href='../summary/labdata.php' onclick='top.restoreSession()'><?php echo xlt('Click here to view and graph all labdata.');?></a>
  </span><?php
} ?>
<br />
<br />
</div>
