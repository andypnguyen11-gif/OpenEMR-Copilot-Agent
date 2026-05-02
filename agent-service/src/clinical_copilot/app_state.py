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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from anthropic import Anthropic

from clinical_copilot.audit.log import AuditLogWriter
from clinical_copilot.auth.jwt_verifier import JwtVerifier
from clinical_copilot.auth.oauth_client import OAuthClient
from clinical_copilot.auth.session import NonceStore
from clinical_copilot.data.fhir_client import FhirClient
from clinical_copilot.db.engine import create_engine_from_url, create_session_factory
from clinical_copilot.observability import configure_tracing
from clinical_copilot.orchestrator.agent import Orchestrator
from clinical_copilot.orchestrator.llm_gateway import AnthropicLlmGateway, LlmGateway
from clinical_copilot.runtime.async_bridge import AsyncBridge
from clinical_copilot.tools.fixtures import FixtureStore
from clinical_copilot.tools.registry import ToolRegistry
from clinical_copilot.verification.middleware import VerificationMiddleware

if TYPE_CHECKING:
    from clinical_copilot.config import Settings

DEFAULT_MODEL = "claude-sonnet-4-6"
NONCE_TTL_SECONDS = 600

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
    bridge: AsyncBridge | None


def build_app_state(
    settings: Settings,
    *,
    llm: LlmGateway | None = None,
    audit: AuditLogWriter | None = None,
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

    if audit is None:
        engine = create_engine_from_url(settings.database_url)
        session_factory = create_session_factory(engine)
        audit = AuditLogWriter(session_factory=session_factory)

    bridge: AsyncBridge | None
    if fixture_store is not None:
        bridge = None
        registry = ToolRegistry.from_fixture(
            store=fixture_store,
            audit=audit,
            audit_salt=settings.audit_salt,
        )
    elif not settings.oauth_client_id:
        # Dev / test fallback. ``Settings`` lets ``oauth_client_id`` be
        # empty in non-prod envs (see :func:`config._load`); without
        # OAuth creds the FHIR stack can't talk to OpenEMR, so default
        # to the fixture store. Production fails fast at config load
        # via ``_require``, so this branch never fires there.
        bridge = None
        registry = ToolRegistry.from_fixture(
            store=FixtureStore.from_file(),
            audit=audit,
            audit_salt=settings.audit_salt,
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
        registry = ToolRegistry.from_fhir(
            fhir=fhir_client,
            bridge=bridge,
            audit=audit,
            audit_salt=settings.audit_salt,
        )

    verifier_mw = VerificationMiddleware()

    if llm is None:
        client = Anthropic(api_key=settings.llm_api_key)
        llm = AnthropicLlmGateway(client=client, model=DEFAULT_MODEL)

    orchestrator = Orchestrator(
        llm=llm,
        registry=registry,
        verifier=verifier_mw,
    )

    return AppState(
        settings=settings,
        jwt_verifier=jwt_verifier,
        orchestrator=orchestrator,
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

    http = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
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
