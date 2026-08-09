"""Alembic environment.

The database URL comes from MPVF settings (or ``MPVF_DB_URL``) rather than
``alembic.ini``, so migrations always target the same database the pipeline
uses and no connection string is duplicated in two places.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from mpvf.config.settings import load_settings
from mpvf.models.db import Base

config = context.config

# ``Base.metadata`` is the autogenerate target: a model change plus
# `alembic revision --autogenerate` produces the migration.
target_metadata = Base.metadata


def database_url() -> str:
    override = os.environ.get("MPVF_DB_URL")
    if override:
        return override
    settings = load_settings(os.environ.get("MPVF_CONFIG"))
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.database_url()


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most things in place; batch mode rewrites the
            # table instead, which is what makes future migrations viable here.
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
