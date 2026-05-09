"""Structured-logging setup for the agent service.

structlog is the project's PSR-3-equivalent. Every log record is a key/value
event; never interpolate values into the message string. JSON output in
production lets LangSmith / Railway log shippers parse fields directly.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging(level: str, *, json_logs: bool) -> None:
    """Install the project's structlog configuration.

    Idempotent: safe to call multiple times (e.g. once at app startup and
    again from test fixtures).
    """

    log_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
        force=True,
    )

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: Any
    if json_logs:
        # JSONRenderer needs the exc_info tuple flattened to a string
        # before the renderer runs.
        shared_processors.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()
    else:
        # ConsoleRenderer formats exceptions itself; including
        # format_exc_info ahead of it triggers a structlog UserWarning
        # ("Remove format_exc_info from your processor chain if you
        # want pretty exceptions") which pytest's filterwarnings=error
        # promotes to a hard exception that escapes route handlers and
        # masks the real error type. Conditional inclusion is the
        # documented structlog idiom.
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
