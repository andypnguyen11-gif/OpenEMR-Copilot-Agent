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

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import structlog
from anthropic import Anthropic

from clinical_copilot.audit.log import AuditLogWriter
from clinical_copilot.audit.reader import AuditLogReader
from clinical_copilot.auth.jwt_verifier import JwtVerifier
from clinical_copilot.auth.oauth_client import OAuthClient
from clinical_copilot.auth.session import NonceStore
from clinical_copilot.config import ConfigError
from clinical_copilot.corpus.retriever import CorpusRetriever
from clinical_copilot.data.fhir_client import FhirClient
from clinical_copilot.db.engine import create_engine_from_url, create_session_factory
from clinical_copilot.discrepancy.cache import DiscrepancyCache
from clinical_copilot.discrepancy.chart_provider import (
    FhirChartProvider,
    FixtureChartProvider,
)
from clinical_copilot.discrepancy.engine import DiscrepancyEngine
from clinical_copilot.discrepancy.rules import DEFAULT_PACK_PATHS, DEFAULT_REGISTRY
from clinical_copilot.observability import MetricsService, TracesService, configure_tracing
from clinical_copilot.orchestrator.agent import Orchestrator
from clinical_copilot.orchestrator.lanes import Lane, LaneConfig
from clinical_copilot.orchestrator.llm_gateway import AnthropicLlmGateway, LlmGateway
from clinical_copilot.orchestrator.sessions import SessionStore
from clinical_copilot.orchestrator.supervisor import (
    EvidenceRetrieverFn,
    IntakeExtractorFn,
)
from clinical_copilot.orchestrator.workers.evidence_retriever import (
    run_evidence_retriever,
)
from clinical_copilot.orchestrator.workers.intake_extractor import (
    run_intake_extractor,
)
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
    {"get_flags", "get_problems", "get_meds", "get_visits", "get_labs"}
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
    traces_service: TracesService
    audit_reader: AuditLogReader | None
    bridge: AsyncBridge | None
    # The tool registry the v1 Orchestrator already holds privately
    # (``orchestrator/agent.py``: ``self._registry``). Surfaced on
    # AppState so the supervisor branch can build a per-request
    # :class:`PatientScopedToolRegistry` for the chart-pack pre-fetch
    # without reaching into the orchestrator's internals. ``None`` on
    # tests that build AppState directly without a registry.
    tool_registry: ToolRegistry | None = None
    # Best-effort sync lookup of the bound patient's display name.
    # ``None`` is a valid return — the orchestrator's cross-patient
    # guard treats a missing name as "no comparator available" and
    # skips the check rather than over-firing. Default is a no-op so
    # tests that build :class:`AppState` directly don't have to wire
    # the resolver.
    patient_name_resolver: Callable[[str], str | None] = field(default=lambda _pid: None)
    # Supervisor wiring (W2-07). ``None`` on the test/fixture path and
    # whenever the corpus index is unavailable; ``main.py`` checks all
    # four together before routing through Supervisor and falls back to
    # the v1 Orchestrator otherwise.
    supervisor_anthropic: Anthropic | None = None
    supervisor_intake_extractor: IntakeExtractorFn | None = None
    supervisor_evidence_retriever: EvidenceRetrieverFn | None = None
    supervisor_model: str | None = None
    # Raw corpus retriever surfaced for the LangGraph supervisor's
    # evidence_retriever node, which calls the retriever directly
    # (rather than via the Anthropic ``tool_use`` partial used by the
    # plain-Python supervisor). ``None`` whenever the corpus index is
    # unavailable; same fail-soft contract.
    supervisor_corpus_retriever: CorpusRetriever | None = None
    # Cohere rerank client (W2-RR). ``None`` when ``COHERE_API_KEY`` is
    # not set in the environment — in that case ``evidence_retriever``
    # falls back to the LLM-judge rerank using ``supervisor_anthropic``.
    # Typed as ``Any`` so a deploy without the cohere package never
    # imports the SDK at module load (lazy import in
    # ``build_app_state``).
    supervisor_cohere_client: Any = None


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
    patient_name_resolver: Callable[[str], str | None]
    if fixture_store is not None:
        # Test path only — unit/integration suites pass a hand-built
        # ``FixtureStore`` so they never depend on a running OpenEMR.
        # Production and ``uv run uvicorn`` both leave this argument
        # ``None`` and fall through to the FHIR branch.
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
        # The fixture store doesn't carry display names per-patient and
        # the test suites don't depend on the cross-patient guard. Drop
        # back to the no-op so the orchestrator's guard becomes a passive
        # check (the prompt-side rule still applies).
        patient_name_resolver = lambda _pid: None  # noqa: E731 — intentional inline no-op
    else:
        # FHIR is the one product data path. Dev needs OAuth creds in
        # ``agent-service/.env`` against the local OpenEMR API client
        # (see scripts/copilot/assign_patients_to_clinicians.php for the
        # parallel patient-side wiring); without them the OAuth handshake
        # fails fast at the first tool call instead of silently swapping
        # in the M5 fixture data the seed script used to ship.
        if not settings.oauth_client_id:
            raise ConfigError(
                "OAUTH_CLIENT_ID is required to bring up agent-service. "
                "Set it in agent-service/.env (and OAUTH_PRIVATE_KEY_PEM, "
                "OAUTH_KEY_ID, OAUTH_TOKEN_URL, FHIR_BASE_URL) — see the "
                "Co-Pilot setup notes for the local OAuth client.",
            )
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
        patient_name_resolver = _build_fhir_patient_name_resolver(
            fhir=fhir_client,
            bridge=bridge,
        )

    verifier_mw = VerificationMiddleware()

    # Metrics writer is fail-open against the same session_factory the
    # audit writer uses (PR 21). When ``session_factory`` is None — the
    # test path that overrides ``audit`` — the service becomes a no-op
    # writer, ``summarize`` raises, and the metrics route is responsible
    # for not registering itself; that's safer than silently returning
    # zero counts that look like a healthy steady state.
    metrics_service = MetricsService(session_factory=session_factory)
    # Trace writer is the sibling fail-open observability writer (PR W2-04).
    # Same ``session_factory`` as the audit and metrics writers so a single
    # DB outage shows up on all three panels at once. ``None`` factory on
    # the test path that overrides ``audit`` becomes a logged no-op
    # writer — the integration tests pass an explicit factory when they
    # want to assert on the row.
    traces_service = TracesService(session_factory=session_factory)

    # Lane gateways: when ``llm`` is supplied (test path), the same
    # stub drives both lanes — unit tests assert on which prompt /
    # tool subset was sent rather than which model. Production builds
    # one gateway per lane so each gets its own prompt-cache key and
    # an env var bump (MODEL_FAST=...) takes effect on next deploy
    # without touching the slow lane.
    slow_llm: LlmGateway
    fast_llm: LlmGateway
    supervisor_anthropic: Anthropic | None = None
    if llm is not None:
        slow_llm = llm
        fast_llm = llm
    else:
        # Wrap the Anthropic client with langsmith.wrappers.wrap_anthropic
        # so every .messages.create call (including the raw-SDK calls
        # planner/critic/synthesizer/v1_single make outside the gateway)
        # emits an llm-typed LangSmith span carrying usage_metadata,
        # ls_provider, and ls_model_name. Without this, only call sites
        # that go through gateway.complete (decorated with
        # @traceable_llm_complete) produce llm spans — and most of the
        # supervisor nodes go through the raw SDK, leaving the LangSmith
        # Tokens/Cost columns blank for chat queries. PHI in the
        # request/response payloads is scrubbed at the tracer level by
        # RedactingLangChainTracer's llm-run redactor.
        from langsmith.wrappers import wrap_anthropic

        client = wrap_anthropic(Anthropic(api_key=settings.llm_api_key))
        slow_llm = AnthropicLlmGateway(client=client, model=settings.model_slow)
        fast_llm = AnthropicLlmGateway(client=client, model=settings.model_fast)
        # Reuse the same Anthropic client for the supervisor and the
        # rerank-judge stage. Single shared client = one TLS pool, one
        # prompt-cache scope; nothing about the supervisor's tool_use
        # loop or rerank's classification call would benefit from a
        # second client.
        supervisor_anthropic = client

    # Supervisor wiring (W2-07). Construct the corpus retriever once at
    # startup and partial-apply both workers so :func:`supervisor.run`
    # can call them with model-supplied kwargs.
    #
    # The retriever's BM25 index ships in the repo at
    # ``data/corpus/bm25.pkl`` (required) and the dense index at
    # ``data/corpus/dense.pkl`` (optional, gated on OPENAI_API_KEY at
    # build time). When the bm25 pickle is missing we leave the
    # supervisor wiring at ``None`` and ``main.py`` falls back to v1
    # Orchestrator for slow-lane queries — a missing corpus index
    # should never crash the app on startup.
    supervisor_intake_extractor: IntakeExtractorFn | None = None
    supervisor_evidence_retriever: EvidenceRetrieverFn | None = None
    corpus_retriever: CorpusRetriever | None = None
    # Cohere rerank client (W2-RR). Lazy-import the SDK inside the
    # conditional so a deploy that doesn't set ``COHERE_API_KEY`` never
    # imports the cohere package and never pays the import cost on the
    # hot path. Best-effort: any construction failure logs and degrades
    # to the LLM-judge rerank path (the worker handles ``None``).
    supervisor_cohere_client: Any = None
    if settings.cohere_api_key:
        try:
            import cohere as _cohere  # noqa: PLC0415  (lazy import is intentional)

            supervisor_cohere_client = _cohere.ClientV2(api_key=settings.cohere_api_key)
            structlog.get_logger(__name__).info("supervisor.cohere_client_loaded")
        except Exception as exc:
            structlog.get_logger(__name__).warning(
                "supervisor.cohere_client_init_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            supervisor_cohere_client = None
    # One-line boot log so a deploy with the wrong env vars surfaces
    # the resolved rerank backend in the startup logs without having
    # to fire a query first. ``cohere`` wins when the SDK loaded;
    # ``llm_judge`` is the fallback when only Anthropic is wired (the
    # supervisor's evidence retriever still runs the judge); ``bm25_only``
    # is the offline / no-network path. Mirrors the per-request label
    # the worker stamps on :class:`AgentResponse.rerank_backend`.
    if supervisor_cohere_client is not None:
        resolved_backend = "cohere"
    elif supervisor_anthropic is not None:
        resolved_backend = "llm_judge"
    else:
        resolved_backend = "bm25_only"
    structlog.get_logger(__name__).info(
        "supervisor.rerank_backend_resolved",
        rerank_backend=resolved_backend,
    )
    if supervisor_anthropic is not None:
        try:
            corpus_retriever = CorpusRetriever()
        except FileNotFoundError as exc:
            # bm25.pkl missing — log and leave supervisor disabled.
            structlog.get_logger(__name__).warning(
                "supervisor.corpus_index_missing",
                error=str(exc),
            )
            corpus_retriever = None
        else:
            structlog.get_logger(__name__).info(
                "supervisor.corpus_loaded",
                hybrid_enabled=corpus_retriever.hybrid_enabled,
            )

        if corpus_retriever is not None:
            # Capture into closure-friendly names so the inner lambdas
            # don't re-bind across iterations of build_app_state. Each
            # process boots one AppState; capture is safe.
            _corpus = corpus_retriever
            _client = supervisor_anthropic
            _slow_model = settings.model_slow
            _fast_model = settings.model_fast
            _cohere_client = supervisor_cohere_client

            def _evidence_partial(**kwargs: object) -> dict[str, object]:
                # Pass both rerank clients; the worker prefers Cohere
                # when present and falls back to the LLM-judge when
                # ``cohere_client`` is ``None``. Either way the rerank
                # stage runs on top of the full BM25 + (dense if
                # available) retrieval.
                return run_evidence_retriever(
                    retriever=_corpus,
                    rerank_client=_client,
                    rerank_model=_fast_model,
                    cohere_client=_cohere_client,
                    **kwargs,  # type: ignore[arg-type]
                ).to_tool_result()

            def _intake_partial(**kwargs: object) -> dict[str, object]:
                # NOTE: on the chat path the model has no document_path
                # to invent, so the supervisor rarely picks this worker
                # in practice. Kept wired so a future chat surface that
                # carries an upload reference can fire it without
                # additional plumbing. Bridging to facts_store.read()
                # for already-extracted documents is a follow-on change
                # (out of scope for early submission per
                # plans/week2-early-submission.md:20).
                return run_intake_extractor(
                    client=_client,
                    model=_slow_model,
                    **kwargs,  # type: ignore[arg-type]
                ).to_tool_result()

            supervisor_intake_extractor = _intake_partial
            supervisor_evidence_retriever = _evidence_partial

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
        traces=traces_service,
    )

    return AppState(
        settings=settings,
        jwt_verifier=jwt_verifier,
        orchestrator=orchestrator,
        session_store=session_store,
        discrepancy_cache=discrepancy_cache,
        metrics_service=metrics_service,
        traces_service=traces_service,
        audit_reader=audit_reader,
        bridge=bridge,
        tool_registry=registry,
        patient_name_resolver=patient_name_resolver,
        supervisor_anthropic=supervisor_anthropic,
        supervisor_intake_extractor=supervisor_intake_extractor,
        supervisor_evidence_retriever=supervisor_evidence_retriever,
        supervisor_model=settings.model_slow if supervisor_anthropic is not None else None,
        supervisor_corpus_retriever=corpus_retriever,
        supervisor_cohere_client=supervisor_cohere_client,
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


def _build_fhir_patient_name_resolver(
    *,
    fhir: FhirClient,
    bridge: AsyncBridge,
) -> Callable[[str], str | None]:
    """Build a sync resolver that runs FHIR ``GET /Patient/{id}`` over the bridge.

    The orchestrator's cross-patient guard expects a sync callable so it
    can compare the bound patient's name against the user's query before
    the LLM loop. ``FhirClient.get_patient`` is async (it shares the
    bridge's loop with every other tool call), so wrap it in a
    bridge-pumped lookup. The resolver is fail-soft: any exception
    (network, FHIR 5xx, parse error, missing name fields) returns
    ``None``, which the guard treats as "no comparator available" and
    skips the deterministic check rather than firing on every query.

    Each call is one HTTP round-trip. Caching is intentionally absent
    today — the bound patient changes between requests, the LRU would
    have to key on patient_id, and the chart-load round-trip already
    dominates the latency. Add an LRU here if profiling shows the
    Patient lookup is meaningful overhead.
    """

    def resolver(patient_id: str) -> str | None:
        if not patient_id:
            return None
        try:
            patient = bridge.run(fhir.get_patient(patient_id))
        except Exception:
            return None
        if not patient.name:
            return None
        primary = patient.name[0]
        # Prefer ``given[0] family`` so the comparator matches the
        # informal way clinicians refer to patients in a chat box
        # ("Maria Lopez", "Marcus Hayes"). Fall back to ``text`` when
        # the name parts are missing.
        given = primary.given[0] if primary.given else ""
        family = primary.family or ""
        formatted = f"{given} {family}".strip()
        if formatted:
            return formatted
        return primary.text or None

    return resolver
