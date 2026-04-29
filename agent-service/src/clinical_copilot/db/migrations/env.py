"""Alembic environment.

The DSN comes from :class:`clinical_copilot.config.Settings` rather than
``alembic.ini`` so dev (SQLite) and prod (Postgres) use the same env-var-
driven config the running app uses. We rewrite ``postgresql://`` to
``postgresql+psycopg://`` here too — Railway hands out the legacy form, but
we want the v3 driver.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from clinical_copilot.config import get_settings

# Import models so their tables are registered on Base.metadata.
from clinical_copilot.db import models  # noqa: F401
from clinical_copilot.db.base import Base
from clinical_copilot.db.engine import normalize_database_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

settings = get_settings()
config.set_main_option("sqlalchemy.url", normalize_database_url(settings.database_url))


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL without a live connection."""

    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url is not None and url.startswith("sqlite"),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — open a connection and apply."""

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
