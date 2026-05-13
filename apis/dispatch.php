<?php

/**
 * Rest Dispatch
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Matthew Vita <matthewvita48@gmail.com>
 * @author    Jerry Padgett <sjpadgett@gmail.com>
 * @author    Brady Miller <brady.g.miller@gmail.com>
 * @copyright Copyright (c) 2018 Matthew Vita <matthewvita48@gmail.com>
 * @copyright Copyright (c) 2020 Jerry Padgett <sjpadgett@gmail.com>
 * @copyright Copyright (c) 2019-2020 Brady Miller <brady.g.miller@gmail.com>
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

// below brings in autoloader
require_once "../vendor/autoload.php";

use OpenEMR\BC\FallbackRouter;
use OpenEMR\Common\Http\HttpRestRequest;
use OpenEMR\RestControllers\ApiApplication;
use OpenEMR\Services\Copilot\Listeners\CopilotKernelBootstrapSubscriber;
use Symfony\Component\HttpFoundation\Response;

// create the Request object
try {
    $request = HttpRestRequest::createFromGlobals();
    FallbackRouter::handleRoutingTestIfRequested($request->getRequestUri(), 'apis');
    $apiApplication = new ApiApplication();
    // Co-Pilot module wiring: register the scope-listener bootstrap so the
    // chat route's `user/query.c` SMART scope is published to OpenEMR's
    // OAuth2 scope registry on every request lifecycle. The bootstrap
    // attaches the actual scope listener to the kernel-DI dispatcher at
    // kernel.request priority 95 (after SiteSetupListener bootstraps the
    // kernel at 100, before OAuth2AuthorizationListener fires at 50).
    $apiApplication->getDispatcher()->addSubscriber(new CopilotKernelBootstrapSubscriber());
    $apiApplication->run($request);
} catch (\Throwable $e) {
    // should never reach here, but if we do, we can log the error and return a generic error response
    // we manually handle it as we don't know if something failed in the symfony component or in our code
    error_log($e->getMessage());
    error_log($e->getTraceAsString());
    // should never get here, but if we do, we can return a generic error response
    if (!headers_sent()) {
        header('Content-Type: application/json');
        http_response_code(Response::HTTP_INTERNAL_SERVER_ERROR);
    }
    die(json_encode([
        'error' => 'An error occurred while processing the request.',
        'message' => $e->getMessage(),
    ]));
}
