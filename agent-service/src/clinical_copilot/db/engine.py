"""Engine + session factory.

Centralized so the rest of the codebase never calls :func:`create_engine`
directly — the URL normalization, SQLite-specific connect args, and
sessionmaker config all live here.

Two databases the engine factory has to handle gracefully:

* **Postgres in production / staging.** Railway hands out URLs in the
  ``postgresql://`` form. SQLAlchemy 2 routes that to the legacy ``psycopg2``
  driver by default; we want ``psycopg`` (v3), so we rewrite the scheme.
* **SQLite in local dev.** Per PRD §8 we keep dev runnable without a Postgres
  container. SQLite needs ``check_same_thread=False`` because FastAPI runs
  request handlers from a worker thread distinct from the connection's
  creator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session


def normalize_database_url(url: str) -> str:
    """Pin Postgres URLs to the psycopg v3 driver.

    Railway and most managed Postgres providers hand out ``postgresql://``
    URLs, which SQLAlchemy 2 routes to ``psycopg2`` by default. We installed
    ``psycopg`` (v3) instead, so rewrite the scheme. Leave any URL that
    already specifies a driver alone.
    """

    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def create_engine_from_url(url: str) -> Engine:
    """Create a SQLAlchemy :class:`Engine` for ``url``.

    SQLite gets ``check_same_thread=False`` so a single connection can be
    reused across FastAPI's threadpool. Postgres uses pool defaults; tuning
    lands in a later PR alongside load testing.
    """

    normalized = normalize_database_url(url)
    if normalized.startswith("sqlite"):
        return create_engine(normalized, connect_args={"check_same_thread": False}, future=True)
    return create_engine(normalized, future=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build a :class:`sessionmaker` bound to ``engine``.

    ``expire_on_commit=False`` keeps ORM objects usable after commit — handy
    for returning IDs from the audit-log writer without re-querying.
    """

    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
