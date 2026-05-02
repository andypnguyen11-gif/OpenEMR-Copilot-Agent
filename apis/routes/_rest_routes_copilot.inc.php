<?php

/**
 * Clinical Co-Pilot Routes
 *
 * The OpenEMR-side gateway for ``/api/agent/*``. Routes here are merged into
 * the standard route map by ``_rest_routes_standard.inc.php``.
 *
 * Routes:
 *
 * * ``GET /api/agent/healthz`` — unauthenticated proxy for the agent
 *   service's liveness check (PR 3).
 * * ``POST /api/agent/query`` — the M3 chat-query route. Mints a per-request
 *   HS256 JWT from the current session and forwards the user's natural-
 *   language query to the agent service. Body: ``{patient_id, query}``;
 *   response: the structured :class:`AgentResponse` from M2.
 * * ``POST /api/agent/warm`` — pre-warms the discrepancy cache for a
 *   panel of patient_ids (PR 15). Fire-and-forget: returns 202 once the
 *   internal call has been dispatched; the warm itself happens
 *   asynchronously from the clinician's perspective.
 *
 * Auth: ``GET /api/agent/healthz`` requires only a valid OAuth2 session.
 * ``POST /api/agent/query`` and ``POST /api/agent/warm`` likewise rely on
 * the kernel's authorization listener for the OAuth2 gate; the per-request
 * RBAC that matters at the agent boundary is done downstream by the Python
 * tool layer using the minted JWT's claims (warm only carries the gateway's
 * shared internal token, not a per-clinician identity).
 *
 * PR 15 also registers a :class:`CopilotInvalidationListener` on the
 * kernel event dispatcher so a :class:`PatientUpdatedEvent` fires a
 * cache invalidation. Registration happens at module-routes-load time
 * because that's the only Copilot-owned hook into the request lifecycle
 * that already exists; this means the listener is active on every API
 * request that loads the route map. Per AUDIT §10 #4 the legacy demographics
 * write path lives outside the API and would need a global-bootstrap hook
 * for full coverage; until then it degrades to TTL-only freshness, which
 * is the architecture's documented fallback (PRD §5).
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

use DateTimeZone;
use GuzzleHttp\Client as GuzzleClient;
use GuzzleHttp\Psr7\HttpFactory;
use JsonException;
use Lcobucci\Clock\SystemClock;
use Monolog\Handler\StreamHandler;
use Monolog\Logger;
use OpenEMR\Common\Http\HttpRestRequest;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Services\Copilot\AgentHttpClient;
use OpenEMR\Services\Copilot\Config\CopilotConfig;
use OpenEMR\Services\Copilot\Config\CopilotConfigException;
use OpenEMR\Services\Copilot\GatewayController;
use OpenEMR\Services\Copilot\InvalidationDispatcher;
use OpenEMR\Services\Copilot\JwtSigner;
use OpenEMR\Services\Copilot\Listeners\CopilotInvalidationListener;
use OpenEMR\Services\Copilot\QueryController;
use OpenEMR\Services\Copilot\SessionDeleteController;
use OpenEMR\Services\Copilot\SessionMapper;
use Psr\Log\LoggerInterface;
use RuntimeException;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Response;

/**
 * Route-local PSR-3 logger that writes to PHP's error log via Monolog. The
 * older openemr/openemr base images we ship on Railway don't expose
 * OpenEMR's own logger through any importable surface, so we stand up a
 * stderr-bound Monolog instance that's good enough for 502 forensics.
 */
$copilotLogger = static function (): LoggerInterface {
    $logger = new Logger('copilot');
    $logger->pushHandler(new StreamHandler('php://stderr'));
    return $logger;
};

/**
 * Build an :class:`InvalidationDispatcher` from the same wiring the
 * route closures use. Pulled out of the closures so the warm route and
 * the listener registration share one construction site.
 */
$copilotDispatcherFactory = static function (
    OEGlobalsBag $globals,
    LoggerInterface $logger,
): InvalidationDispatcher {
    $config = new CopilotConfig($globals);
    // Internal calls run with the short cache-freshness timeout, not
    // the long chat-query timeout — see CopilotConfig::getInternalTimeoutSeconds.
    $httpClient = new GuzzleClient([
        'timeout' => $config->getInternalTimeoutSeconds(),
        'http_errors' => false,
    ]);
    $agentClient = new AgentHttpClient($httpClient, new HttpFactory(), $config);
    return new InvalidationDispatcher($agentClient, $config, $logger);
};

// Register the PatientUpdatedEvent subscriber on the kernel dispatcher
// the first time this route file is loaded for a request. The
// subscriber is fire-and-forget — see CopilotInvalidationListener for
// the failure-mode contract — so a missing internal token (or any
// other dispatcher-side wiring problem) cannot bubble out of a clinical
// write. The only realistic failure here is :meth:`OEGlobalsBag::getKernel`
// raising :class:`RuntimeException` when the kernel hasn't been
// bootstrapped (CLI / install paths); narrowing the catch to that lets
// real Errors (autoloader misalignment, undefined class) propagate to
// the global handler where they should.
try {
    $copilotKernel = OEGlobalsBag::getInstance()->getKernel();
    $copilotDispatcher = $copilotDispatcherFactory(
        OEGlobalsBag::getInstance(),
        $copilotLogger(),
    );
    $copilotKernel->getEventDispatcher()->addSubscriber(
        new CopilotInvalidationListener($copilotDispatcher),
    );
} catch (RuntimeException $e) {
    $copilotLogger()->warning('Co-Pilot invalidation listener not registered', [
        'exception' => $e,
    ]);
}

return [
    "GET /api/agent/healthz" => function (HttpRestRequest $request, OEGlobalsBag $globals) use ($copilotLogger) {
        $config = new CopilotConfig($globals);
        $factory = new HttpFactory();
        $httpClient = new GuzzleClient([
            'timeout' => $config->getAgentTimeoutSeconds(),
            'http_errors' => false,
        ]);
        $agentClient = new AgentHttpClient($httpClient, $factory, $config);
        $controller = new GatewayController($agentClient, $copilotLogger());
        return $controller->healthz();
    },
    "POST /api/agent/query" => function (HttpRestRequest $request, OEGlobalsBag $globals) use ($copilotLogger) {
        $config = new CopilotConfig($globals);
        $factory = new HttpFactory();
        // Slow-lane query timeouts can run 15-20s once the model is invoked;
        // override the short healthz default by a configurable margin.
        $httpClient = new GuzzleClient([
            'timeout' => max($config->getAgentTimeoutSeconds() * 6, 30),
            'http_errors' => false,
        ]);
        $agentClient = new AgentHttpClient($httpClient, $factory, $config);
        $signer = new JwtSigner(
            $config->getJwtSecret(),
            new SystemClock(new DateTimeZone('UTC')),
        );
        // Read the OpenEMR session bag directly rather than calling
        // SessionWrapperFactory — the latter's modern helper methods are
        // missing from older openemr/openemr base images on Docker Hub.
        $sessionMapper = SessionMapper::fromGlobalSession();
        $controller = new QueryController(
            $agentClient,
            $signer,
            $sessionMapper,
            $config,
            $copilotLogger(),
        );
        // The HttpRestRequest is OpenEMR's narrowed wrapper; the
        // QueryController wants the raw body which Symfony's Request
        // exposes via ``getContent``. They share the same parent, so a
        // direct pass-through works here.
        return $controller->query($request);
    },
    "POST /api/agent/warm" => function (
        HttpRestRequest $request,
        OEGlobalsBag $globals,
    ) use ($copilotLogger, $copilotDispatcherFactory) {
        // Body shape: ``{patient_ids: ["101", "102", ...]}``. The chat
        // UI hits this on patient-select so the first ``get_flags`` call
        // inside the chat lands on a hot cache. Keeping the JSON parse
        // tight here means the route can return 400 on malformed input
        // before the dispatcher's network round-trip; the dispatcher
        // itself is fire-and-forget but a JSON parse error is the
        // caller's bug, not the agent's.
        $logger = $copilotLogger();

        $raw = $request->getContent();
        if ($raw === '') {
            return new JsonResponse(['error' => 'bad_request'], Response::HTTP_BAD_REQUEST);
        }
        try {
            $decoded = json_decode($raw, true, flags: JSON_THROW_ON_ERROR);
        } catch (JsonException $e) {
            $logger->info('Co-Pilot warm rejected: invalid JSON', ['exception' => $e]);
            return new JsonResponse(['error' => 'bad_request'], Response::HTTP_BAD_REQUEST);
        }
        if (!is_array($decoded) || !isset($decoded['patient_ids']) || !is_array($decoded['patient_ids'])) {
            return new JsonResponse(['error' => 'bad_request'], Response::HTTP_BAD_REQUEST);
        }

        // Cap panel size at the agent service's bound (200) so a runaway
        // client can't tie up the warm path serially. The agent enforces
        // the same cap; checking here saves a wasted round-trip.
        if (count($decoded['patient_ids']) > 200) {
            return new JsonResponse(['error' => 'panel_too_large'], Response::HTTP_BAD_REQUEST);
        }

        $patientIds = [];
        foreach ($decoded['patient_ids'] as $id) {
            // Skip non-scalar / blank entries silently — the dispatcher
            // also filters but doing it here keeps the wire payload
            // tighter and lets us reject "all blanks" as bad_request.
            if (is_string($id) && $id !== '') {
                $patientIds[] = $id;
            } elseif (is_int($id)) {
                $patientIds[] = (string) $id;
            }
        }
        if ($patientIds === []) {
            return new JsonResponse(['error' => 'bad_request'], Response::HTTP_BAD_REQUEST);
        }

        try {
            $dispatcher = $copilotDispatcherFactory($globals, $logger);
        } catch (CopilotConfigException $e) {
            // Misconfigured gateway — the dispatcher would log + skip,
            // but the warm route is a synchronous user request and the
            // operator deserves a visible 500 on the wire so the misconfig
            // surfaces in deploy smoke tests rather than as silent staleness.
            $logger->warning('Co-Pilot warm misconfigured', ['exception' => $e]);
            return new JsonResponse(
                ['error' => 'service_misconfigured'],
                Response::HTTP_INTERNAL_SERVER_ERROR,
            );
        }

        $dispatcher->warmPanel($patientIds);

        // 202: accepted for processing; the warm itself happens
        // asynchronously from the caller's perspective. Empty body —
        // there's nothing useful to return without polling, and the chat
        // UI has no use for a per-patient summary at this point in the
        // flow (it cares whether the next get_flags is fast, not which
        // ids warmed).
        return new JsonResponse(null, Response::HTTP_ACCEPTED);
    },
    "DELETE /api/agent/session/:session_id" => function (
        string $sessionId,
        HttpRestRequest $request,
        OEGlobalsBag $globals,
    ) use ($copilotLogger) {
        $config = new CopilotConfig($globals);
        $factory = new HttpFactory();
        // DELETE traffic is small and infrequent; reuse the short
        // timeout from healthz rather than the long query timeout.
        $httpClient = new GuzzleClient([
            'timeout' => $config->getAgentTimeoutSeconds(),
            'http_errors' => false,
        ]);
        $agentClient = new AgentHttpClient($httpClient, $factory, $config);
        $signer = new JwtSigner(
            $config->getJwtSecret(),
            new SystemClock(new DateTimeZone('UTC')),
        );
        $sessionMapper = SessionMapper::fromGlobalSession();
        $controller = new SessionDeleteController(
            $agentClient,
            $signer,
            $sessionMapper,
            $config,
            $copilotLogger(),
        );
        return $controller->delete($request, $sessionId);
    },
];
