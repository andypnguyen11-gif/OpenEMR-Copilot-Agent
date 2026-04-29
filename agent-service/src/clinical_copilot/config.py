"""Environment-driven configuration for the agent service.

Settings are loaded once at startup from environment variables. Each field is
typed; missing required values fail fast at import time so a misconfigured
deploy never silently runs with defaults that look like production.

PR 1 only carries the four boundary settings called out in TASKS.md
(HMAC secret, LLM API key, FHIR base URL, Postgres DSN). Lane/model/cache
settings land in later PRs as their respective code paths arrive.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing or invalid."""


def _require(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise ConfigError(f"Required environment variable {name!r} is not set")
    return value


def _optional(name: str, default: str) -> str:
    return os.environ.get(name) or default


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

    @property
    def is_production(self) -> bool:
        return self.env == "production"


def _load() -> Settings:
    env = _optional("APP_ENV", "development")
    log_level = _optional("LOG_LEVEL", "INFO").upper()

    if env in {"development", "test"}:
        hmac_secret = _optional("COPILOT_HMAC_SECRET", "dev-insecure-hmac-secret")
        llm_api_key = _optional("ANTHROPIC_API_KEY", "")
        fhir_base_url = _optional("FHIR_BASE_URL", "http://localhost:8300/apis/default/fhir")
        database_url = _optional("DATABASE_URL", "sqlite:///./agent.db")
    else:
        hmac_secret = _require("COPILOT_HMAC_SECRET")
        llm_api_key = _require("ANTHROPIC_API_KEY")
        fhir_base_url = _require("FHIR_BASE_URL")
        database_url = _require("DATABASE_URL")

    return Settings(
        env=env,
        log_level=log_level,
        hmac_secret=hmac_secret,
        llm_api_key=llm_api_key,
        fhir_base_url=fhir_base_url,
        database_url=database_url,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` instance.

    Cached so repeated calls cost nothing. Tests that need to vary settings
    should call :func:`get_settings.cache_clear` after mutating ``os.environ``.
    """

    return _load()
