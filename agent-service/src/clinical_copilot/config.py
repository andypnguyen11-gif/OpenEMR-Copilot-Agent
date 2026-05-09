"""Environment-driven configuration for the agent service.

Settings are loaded once at startup from environment variables. Each field is
typed; missing required values fail fast at import time so a misconfigured
deploy never silently runs with defaults that look like production.

Settings grow per PR as new code paths arrive. Currently carries the four
boundary settings from PR 1 (HMAC secret, LLM API key, FHIR base URL, Postgres
DSN), the audit-log patient-ID hashing salt added in PR 2, the OAuth2
JWT-bearer client-assertion settings (PR 5 originally as ``client_secret``,
migrated to RS384 private key + kid in PR 5.5 to match what OpenEMR's
confidential-client OAuth2 endpoint actually accepts for ``system/*`` scopes),
and per-lane model tiers added in PR 10 (``MODEL_SLOW`` / ``MODEL_FAST`` so
eval can A/B Sonnet vs Haiku without redeploy). Cache settings land in PR 14.
PR 15 adds the service-to-service ``COPILOT_INTERNAL_TOKEN`` for the warm
and invalidate routes — separate from the user-facing HMAC secret so
rotating one doesn't drop in-flight chat sessions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# Load ``agent-service/.env`` once at import time so a developer who
# launches uvicorn directly (IDE runner, ``uvicorn ... --reload`` from a
# fresh shell) gets the same env the documented ``set -a; source .env``
# flow produces. ``override=False`` keeps real process env (Railway,
# Docker ``environment:``, CI) authoritative — a stray ``.env`` cannot
# stomp a platform-injected secret. Path is anchored on this file rather
# than ``cwd`` so the loader doesn't depend on where the process was
# started from. Missing file is a no-op (``load_dotenv`` returns False),
# which is exactly what we want in containers that ship no ``.env``.
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(_ENV_PATH, override=False)


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing or invalid."""


def _require(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise ConfigError(f"Required environment variable {name!r} is not set")
    return value


def _optional(name: str, default: str) -> str:
    return os.environ.get(name) or default


# Default model ids. Override via env vars in any environment; the
# defaults below match the versions we eval against today (Sonnet 4.6
# for the slow lane's full reconciliation work, Haiku 4.5 for the
# in-chart side panel's ≤5s budget per PRD §13). Bump these on a
# deliberate model upgrade so an env that doesn't set the var still
# tracks the current canonical pair.
DEFAULT_MODEL_SLOW = "claude-sonnet-4-6"
DEFAULT_MODEL_FAST = "claude-haiku-4-5-20251001"


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime settings.

    Construct via :func:`get_settings`; do not instantiate directly elsewhere
    in the codebase. Treat this object as read-only — never mutate fields at
    runtime; restart the process to reload configuration.
    """

    env: str
    log_level: str
    hmac_secret: str
    llm_api_key: str
    fhir_base_url: str
    database_url: str
    audit_salt: str
    oauth_client_id: str
    oauth_private_key_pem: bytes
    oauth_key_id: str
    oauth_token_url: str
    model_slow: str
    model_fast: str
    internal_token: str
    # Optional in every env. Empty string keeps the existing LLM-judge
    # rerank path active so a deploy without the key is still
    # functional (best-effort fallback contract). When non-empty,
    # ``app_state.build_app_state`` constructs a Cohere client and
    # the evidence_retriever worker prefers it over the LLM judge —
    # see ``corpus.rerank.rerank_with_cohere``.
    cohere_api_key: str = ""
    # Default ``True`` matches the production default and keeps existing
    # test factories that build ``Settings(...)`` directly working
    # without the new kwarg. The route still gates on
    # ``AppState.supervisor_anthropic is not None``, which is ``None``
    # on every test/fixture path, so defaulting True here cannot
    # accidentally route a test through the live supervisor.
    use_supervisor: bool = True
    # Opt-in flag for the W2-07 LangGraph supervisor. When ``True`` AND
    # ``use_supervisor`` is ``True`` AND every supervisor collaborator
    # is wired (anthropic client, corpus retriever, model strings), the
    # slow lane routes through ``supervisor_langgraph.run_turn``. Any
    # exception falls back to the plain-Python ``supervisor.run`` (which
    # itself falls back to the v1 Orchestrator on its own exception path),
    # so flipping this on can never strand a request. Defaults ``False``
    # for early submission so the production demo path stays on the
    # currently-deployed plain-Python supervisor unless the env var
    # is set deliberately.
    use_langgraph: bool = False

    @property
    def is_production(self) -> bool:
        return self.env == "production"


def _load() -> Settings:
    env = _optional("APP_ENV", "development")
    log_level = _optional("LOG_LEVEL", "INFO").upper()

    # Lane model tiers default to the canonical pair in every env so
    # an undeclared MODEL_SLOW / MODEL_FAST never falls back to a
    # placeholder string that would 404 on the Anthropic API at first
    # request. Eval can override either to A/B without code changes.
    model_slow = _optional("MODEL_SLOW", DEFAULT_MODEL_SLOW)
    model_fast = _optional("MODEL_FAST", DEFAULT_MODEL_FAST)

    # USE_SUPERVISOR routes slow-lane /api/agent/query traffic through the
    # W2 Supervisor (intake_extractor + evidence_retriever workers) instead
    # of the v1 single-loop Orchestrator. Defaults ON; flip to "false" on
    # Railway to roll back without a redeploy. Fast lane is unaffected
    # (side-panel chat keeps its v1 path / ≤5s p50 budget).
    use_supervisor = _optional("USE_SUPERVISOR", "true").strip().lower() in {
        "true",
        "1",
        "yes",
        "on",
    }

    # USE_LANGGRAPH gates the W2-07 LangGraph StateGraph supervisor.
    # Defaults OFF so an env that doesn't set the var keeps the
    # currently-deployed plain-Python supervisor. Flip to "true" on
    # Railway to roll the LangGraph path forward. Has no effect when
    # ``USE_SUPERVISOR`` is false (LangGraph requires the supervisor
    # branch to be live; the route does not bypass the supervisor
    # gate to reach LangGraph).
    use_langgraph = _optional("USE_LANGGRAPH", "false").strip().lower() in {
        "true",
        "1",
        "yes",
        "on",
    }

    # Optional in every env (no _require branch). Empty string keeps the
    # existing LLM-judge rerank path active so production stays functional
    # without the key — promoting Cohere to primary requires setting the
    # env var deliberately.
    cohere_api_key = _optional("COHERE_API_KEY", "")

    if env in {"development", "test"}:
        hmac_secret = _optional("COPILOT_HMAC_SECRET", "dev-insecure-hmac-secret")
        llm_api_key = _optional("ANTHROPIC_API_KEY", "")
        fhir_base_url = _optional("FHIR_BASE_URL", "http://localhost:8300/apis/default/fhir")
        database_url = _optional("DATABASE_URL", "sqlite:///./agent.db")
        audit_salt = _optional("COPILOT_AUDIT_SALT", "dev-insecure-audit-salt")
        oauth_client_id = _optional("OAUTH_CLIENT_ID", "")
        oauth_private_key_pem = _optional("OAUTH_PRIVATE_KEY_PEM", "").encode("utf-8")
        oauth_key_id = _optional("OAUTH_KEY_ID", "")
        oauth_token_url = _optional("OAUTH_TOKEN_URL", "http://localhost:8300/oauth2/default/token")
        internal_token = _optional("COPILOT_INTERNAL_TOKEN", "dev-insecure-internal-token")
    else:
        hmac_secret = _require("COPILOT_HMAC_SECRET")
        llm_api_key = _require("ANTHROPIC_API_KEY")
        fhir_base_url = _require("FHIR_BASE_URL")
        database_url = _require("DATABASE_URL")
        audit_salt = _require("COPILOT_AUDIT_SALT")
        oauth_client_id = _require("OAUTH_CLIENT_ID")
        oauth_private_key_pem = _require("OAUTH_PRIVATE_KEY_PEM").encode("utf-8")
        oauth_key_id = _require("OAUTH_KEY_ID")
        oauth_token_url = _require("OAUTH_TOKEN_URL")
        internal_token = _require("COPILOT_INTERNAL_TOKEN")

    return Settings(
        env=env,
        log_level=log_level,
        hmac_secret=hmac_secret,
        llm_api_key=llm_api_key,
        fhir_base_url=fhir_base_url,
        database_url=database_url,
        audit_salt=audit_salt,
        oauth_client_id=oauth_client_id,
        oauth_private_key_pem=oauth_private_key_pem,
        oauth_key_id=oauth_key_id,
        oauth_token_url=oauth_token_url,
        model_slow=model_slow,
        model_fast=model_fast,
        internal_token=internal_token,
        use_supervisor=use_supervisor,
        use_langgraph=use_langgraph,
        cohere_api_key=cohere_api_key,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` instance.

    Cached so repeated calls cost nothing. Tests that need to vary settings
    should call :func:`get_settings.cache_clear` after mutating ``os.environ``.
    """

    return _load()
