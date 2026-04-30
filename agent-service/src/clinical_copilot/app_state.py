"""Composition root for the agent-service runtime.

Builds the orchestrator and JWT verifier from :class:`Settings`. Pulled
out of :mod:`main` so tests can build an :class:`AppState` with stubbed
collaborators (a fake LLM gateway, in-memory audit writer) and pass it
straight into :func:`create_app`.

The wiring lives here, the request-routing lives in :mod:`main`. Adding
a new collaborator means editing exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from anthropic import Anthropic

from clinical_copilot.audit.log import AuditLogWriter
from clinical_copilot.auth.jwt_verifier import JwtVerifier
from clinical_copilot.auth.session import NonceStore
from clinical_copilot.db.engine import create_engine_from_url, create_session_factory
from clinical_copilot.observability import configure_tracing
from clinical_copilot.orchestrator.agent import Orchestrator
from clinical_copilot.orchestrator.llm_gateway import AnthropicLlmGateway, LlmGateway
from clinical_copilot.tools.fixtures import FixtureStore
from clinical_copilot.tools.registry import ToolRegistry
from clinical_copilot.verification.middleware import VerificationMiddleware

if TYPE_CHECKING:
    from clinical_copilot.config import Settings

DEFAULT_MODEL = "claude-sonnet-4-6"
NONCE_TTL_SECONDS = 600


@dataclass(frozen=True, slots=True)
class AppState:
    """Bag of fully-wired runtime collaborators.

    ``frozen`` because nothing in this set should change after startup —
    a request handler that wants different behavior should either route
    to a different app instance or be tested in isolation.
    """

    settings: Settings
    jwt_verifier: JwtVerifier
    orchestrator: Orchestrator


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
    production wiring.
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

    if fixture_store is None:
        fixture_store = FixtureStore.from_file()

    registry = ToolRegistry.from_fixture(
        store=fixture_store,
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
    )
