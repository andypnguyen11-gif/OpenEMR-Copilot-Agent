"""Composition root for the agent-service runtime.

Builds the orchestrator and JWT verifier from :class:`Settings`. Pulled
out of :mod:`main` so tests can build an :class:`AppState` with stubbed
collaborators (a fake LLM gateway, in-memory audit writer) and pass it
straight into :func:`create_app`.

The wiring lives here, the request-routing lives in :mod:`main`. Adding
a new collaborator means editing exactly one place.

Two tool-layer wirings:

* **Production** — when no ``fixture_store`` override is supplied,
  :func:`build_app_state` constructs the live FHIR stack: a single
  long-lived :class:`AsyncBridge`, a process-wide
  :class:`httpx.AsyncClient` *built inside the bridge loop* so its
  internal locks bind to the same event loop the FHIR calls run on, an
  :class:`OAuthClient`, and a :class:`FhirClient`. The orchestrator
  then dispatches via :meth:`ToolRegistry.from_fhir`.

* **Tests / eval** — passing ``fixture_store=`` (or ``llm=`` / ``audit=``
  for partial overrides) routes through :meth:`ToolRegistry.from_fixture`
  so a TestClient can exercise the full ``/api/agent/query`` flow
  without an Anthropic key, a database, or a real OpenEMR. None of the
  test bindings construct an :class:`AsyncBridge`, so no daemon thread
  leaks per test.

Two lanes (PR 10):

* **Slow lane** — full tool surface, ``settings.model_slow``,
  ``system_slow.md``. Default for every request that doesn't pin a lane.
* **Fast lane** — four-tool subset (``get_flags``, ``get_problems``,
  ``get_meds``, ``get_visits``), ``settings.model_fast``,
  ``system_fast.md``. Driven by the in-chart side panel that has a ≤5s
  p50 budget per PRD §13.

Each lane holds its own :class:`AnthropicLlmGateway` instance so its
prompt-cache key is bound to a single (model, tool-defs, system-prompt)
triple — slow-lane traffic and fast-lane traffic never share cache
state. A test passing ``llm=`` applies that single stub to both lanes;
the unit-test surface only exercises one lane at a time and asserts on
``gateway.calls[i]["system"]`` to confirm the right prompt was used.

PR 15 hoists the :class:`DiscrepancyCache` onto :class:`AppState` so
the warm and invalidate routes operate on the same instance the
``get_flags`` tool reads through. Building it once here (rather than
twice — once inside the registry, once for the routes) is what
guarantees a warmed entry is visible to the next chat-side
``get_flags`` call without a durable-tier round trip.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from anthropic import Anthropic

from clinical_copilot.audit.log import AuditLogWriter
from clinical_copilot.audit.reader import AuditLogReader
from clinical_copilot.auth.jwt_verifier import JwtVerifier
from clinical_copilot.auth.oauth_client import OAuthClient
from clinical_copilot.auth.session import NonceStore
from clinical_copilot.data.fhir_client import FhirClient
from clinical_copilot.db.engine import create_engine_from_url, create_session_factory
from clinical_copilot.discrepancy.cache import DiscrepancyCache
from clinical_copilot.discrepancy.chart_provider import (
    FhirChartProvider,
    FixtureChartProvider,
)
from clinical_copilot.discrepancy.engine import DiscrepancyEngine
from clinical_copilot.discrepancy.rules import DEFAULT_PACK_PATHS, DEFAULT_REGISTRY
from clinical_copilot.observability import MetricsService, configure_tracing
from clinical_copilot.orchestrator.agent import Orchestrator
from clinical_copilot.orchestrator.lanes import Lane, LaneConfig
from clinical_copilot.orchestrator.llm_gateway import AnthropicLlmGateway, LlmGateway
from clinical_copilot.orchestrator.sessions import SessionStore
from clinical_copilot.runtime.async_bridge import AsyncBridge
from clinical_copilot.tools.fixtures import FixtureStore
from clinical_copilot.tools.registry import ToolRegistry
from clinical_copilot.verification.middleware import VerificationMiddleware

if TYPE_CHECKING:
    from clinical_copilot.config import Settings

NONCE_TTL_SECONDS = 600

_PROMPTS_DIR = Path(__file__).resolve().parent / "orchestrator" / "prompts"
_SYSTEM_SLOW_PATH = _PROMPTS_DIR / "system_slow.md"
_SYSTEM_FAST_PATH = _PROMPTS_DIR / "system_fast.md"

# Fast lane tool subset per ARCHITECTURE §2 / PR 10. Slow lane gets the
# registry's full set (passed as ``tool_names=None``). The set is
# ``frozenset`` so :class:`LaneConfig` can stay frozen-dataclass.
_FAST_LANE_TOOLS: frozenset[str] = frozenset(
    {"get_flags", "get_problems", "get_meds", "get_visits"}
)

# Bound on the shared httpx.AsyncClient. PR 25 owns the request-level
# timeout / circuit-breaker policy; this ceiling is the outer envelope
# so a hung connection can't pin a daemon-thread coroutine forever.
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


@dataclass(frozen=True, slots=True)
class AppState:
    """Bag of fully-wired runtime collaborators.

    ``frozen`` because nothing in this set should change after startup —
    a request handler that wants different behavior should either route
    to a different app instance or be tested in isolation.

    ``bridge`` is held here (not just kept alive by closure) so the
    loop-on-daemon-thread can't be GC'd while the route is still
    issuing tool calls into it. ``None`` on the test path that uses
    fixture-backed tools — those don't need an event loop.
    """

    settings: Settings
    jwt_verifier: JwtVerifier
    orchestrator: Orchestrator
    session_store: SessionStore
    discrepancy_cache: DiscrepancyCache
    metrics_service: MetricsService
    audit_reader: AuditLogReader | None
    bridge: AsyncBridge | None


def build_app_state(
    settings: Settings,
    *,
    llm: LlmGateway | None = None,
    audit: AuditLogWriter | None = None,
    audit_reader: AuditLogReader | None = None,
    fixture_store: FixtureStore | None = None,
) -> AppState:
    """Wire everything from :class:`Settings`.

    Each collaborator is overridable for tests so a TestClient can run
    the full route without an Anthropic API key, without a database,
    and without a real fixture file. The defaults produce the
    production wiring — FHIR-backed tools against ``settings.fhir_base_url``.

    Passing ``fixture_store`` flips to the M1 fixture path: no
    :class:`AsyncBridge` is constructed, no live FHIR calls happen.
    Tests / eval rely on this; production never passes one.
    """

    configure_tracing(audit_salt=settings.audit_salt)

    nonce_store = NonceStore(ttl_seconds=NONCE_TTL_SECONDS)
    jwt_verifier = JwtVerifier(
        secret=settings.hmac_secret,
        replay_store=nonce_store,
    )

    # The session factory feeds the audit writer, the supervisor-side
    # audit reader (PR 18), and the durable tier of :class:`DiscrepancyCache`
    # (PR 14). Build it once if any collaborator needs it; tests
    # overriding ``audit`` typically also override ``audit_reader`` and
    # the cache then falls back to in-process-only.
    session_factory = None
    if audit is None:
        db_engine = create_engine_from_url(settings.database_url)
        session_factory = create_session_factory(db_engine)
        audit = AuditLogWriter(session_factory=session_factory)
    if audit_reader is None and session_factory is not None:
        audit_reader = AuditLogReader(session_factory=session_factory)

    # The discrepancy engine is the same in every wiring — only the
    # chart provider differs. Build the engine once and pass it (with
    # the wiring-specific chart provider) into a single
    # :class:`DiscrepancyCache`. Holding the cache on :class:`AppState`
    # is what lets PR 15's warm/invalidate routes share memory + DB
    # state with the ``get_flags`` reads coming through the registry.
    engine = DiscrepancyEngine.from_yaml(DEFAULT_PACK_PATHS, DEFAULT_REGISTRY)

    bridge: AsyncBridge | None
    if fixture_store is not None:
        bridge = None
        chart_provider = FixtureChartProvider(fixture_store)
        discrepancy_cache = DiscrepancyCache(
            chart_provider=chart_provider,
            engine=engine,
            session_factory=session_factory,
        )
        registry = ToolRegistry.from_fixture(
            store=fixture_store,
            audit=audit,
            audit_salt=settings.audit_salt,
            cache=discrepancy_cache,
        )
    elif not settings.oauth_client_id:
        # Dev / test fallback. ``Settings`` lets ``oauth_client_id`` be
        # empty in non-prod envs (see :func:`config._load`); without
        # OAuth creds the FHIR stack can't talk to OpenEMR, so default
        # to the fixture store. Production fails fast at config load
        # via ``_require``, so this branch never fires there.
        bridge = None
        store = FixtureStore.from_file()
        chart_provider = FixtureChartProvider(store)
        discrepancy_cache = DiscrepancyCache(
            chart_provider=chart_provider,
            engine=engine,
            session_factory=session_factory,
        )
        registry = ToolRegistry.from_fixture(
            store=store,
            audit=audit,
            audit_salt=settings.audit_salt,
            cache=discrepancy_cache,
        )
    else:
        bridge = AsyncBridge()
        # Build the AsyncClient (and the OAuth + FHIR clients that own
        # references to it) inside the bridge loop. ``httpx.AsyncClient``
        # binds an internal asyncio.Lock to the running loop on first
        # use; constructing it here means every subsequent FHIR call
        # routed through the bridge talks to that same loop, not the
        # main thread's loop (or no loop at all).
        fhir_client = bridge.run(_build_fhir_stack(settings))
        fhir_chart_provider = FhirChartProvider(fhir=fhir_client, bridge=bridge)
        discrepancy_cache = DiscrepancyCache(
            chart_provider=fhir_chart_provider,
            engine=engine,
            session_factory=session_factory,
        )
        registry = ToolRegistry.from_fhir(
            fhir=fhir_client,
            bridge=bridge,
            audit=audit,
            audit_salt=settings.audit_salt,
            cache=discrepancy_cache,
        )

    verifier_mw = VerificationMiddleware()

    # Metrics writer is fail-open against the same session_factory the
    # audit writer uses (PR 21). When ``session_factory`` is None — the
    # test path that overrides ``audit`` — the service becomes a no-op
    # writer, ``summarize`` raises, and the metrics route is responsible
    # for not registering itself; that's safer than silently returning
    # zero counts that look like a healthy steady state.
    metrics_service = MetricsService(session_factory=session_factory)

    # Lane gateways: when ``llm`` is supplied (test path), the same
    # stub drives both lanes — unit tests assert on which prompt /
    # tool subset was sent rather than which model. Production builds
    # one gateway per lane so each gets its own prompt-cache key and
    # an env var bump (MODEL_FAST=...) takes effect on next deploy
    # without touching the slow lane.
    slow_llm: LlmGateway
    fast_llm: LlmGateway
    if llm is not None:
        slow_llm = llm
        fast_llm = llm
    else:
        client = Anthropic(api_key=settings.llm_api_key)
        slow_llm = AnthropicLlmGateway(client=client, model=settings.model_slow)
        fast_llm = AnthropicLlmGateway(client=client, model=settings.model_fast)

    system_slow = _SYSTEM_SLOW_PATH.read_text(encoding="utf-8")
    system_fast = _SYSTEM_FAST_PATH.read_text(encoding="utf-8")

    lanes: dict[Lane, LaneConfig] = {
        Lane.SLOW: LaneConfig(
            llm=slow_llm,
            system_prompt=system_slow,
            tool_names=None,
        ),
        Lane.FAST: LaneConfig(
            llm=fast_llm,
            system_prompt=system_fast,
            tool_names=_FAST_LANE_TOOLS,
        ),
    }

    session_store = SessionStore()

    orchestrator = Orchestrator(
        lanes=lanes,
        registry=registry,
        verifier=verifier_mw,
        sessions=session_store,
        metrics=metrics_service,
    )

    return AppState(
        settings=settings,
        jwt_verifier=jwt_verifier,
        orchestrator=orchestrator,
        session_store=session_store,
        discrepancy_cache=discrepancy_cache,
        metrics_service=metrics_service,
        audit_reader=audit_reader,
        bridge=bridge,
    )


async def _build_fhir_stack(settings: Settings) -> FhirClient:
    """Construct the OAuth + FHIR clients on the bridge loop.

    Coroutine instead of plain function so :meth:`AsyncBridge.run` is
    the only way to call it — that's what guarantees the
    ``httpx.AsyncClient`` lifecycle stays loop-bound. The client is
    intentionally not closed here; it lives for the lifetime of the
    process and tears down with the daemon thread.
    """

    # Local OpenEMR ships a self-signed cert on https://localhost:9300.
    # Production uses a real certificate, so cert verification stays on
    # everywhere except dev/test where the only available transport is
    # the self-signed loopback.
    verify_tls = settings.env not in {"development", "test"}
    http = httpx.AsyncClient(timeout=_HTTP_TIMEOUT, verify=verify_tls)
    oauth = OAuthClient(
        token_url=settings.oauth_token_url,
        client_id=settings.oauth_client_id,
        private_key_pem=settings.oauth_private_key_pem,
        key_id=settings.oauth_key_id,
        http_client=http,
    )
    return FhirClient(
        base_url=settings.fhir_base_url,
        oauth=oauth,
        http_client=http,
    )
