"""SQLAlchemy declarative base.

Kept in its own module so Alembic's ``env.py`` can import :data:`Base.metadata`
without dragging in engine/session machinery (which depends on settings).
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Project-wide declarative base for SQLAlchemy 2.0 typed models."""
