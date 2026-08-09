"""Alembic integration (§22: every schema change is a migration).

Alembic is the source of truth for the production schema. ``Base.metadata``
remains the autogenerate target and is still used directly by tests, so
``schema_drift`` exists to prove the two never diverge.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import inspect

from mpvf.config.settings import Settings
from mpvf.models.db import Base, Database


def _migrations_dir() -> Path:
    """Locate the migrations tree.

    Migrations live beside the source checkout, not inside the wheel, so an
    installed deployment points at them with ``MPVF_MIGRATIONS_DIR``.
    """

    override = os.environ.get("MPVF_MIGRATIONS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[3] / "migrations"


MIGRATIONS_DIR = _migrations_dir()
ALEMBIC_INI = MIGRATIONS_DIR.parent / "alembic.ini"


class MigrationsUnavailable(RuntimeError):
    """Alembic is not installed, or the migrations directory is missing."""


@dataclass
class MigrationState:
    current: str | None
    head: str | None
    pending: bool
    detail: str = ""

    @property
    def up_to_date(self) -> bool:
        return self.current is not None and self.current == self.head


def _config(settings: Settings) -> Any:
    try:
        from alembic.config import Config
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise MigrationsUnavailable(
            "alembic is not installed; install the 'dev' extra to manage migrations"
        ) from exc

    if not MIGRATIONS_DIR.exists():
        raise MigrationsUnavailable(f"migrations directory not found at {MIGRATIONS_DIR}")

    config = Config(str(ALEMBIC_INI) if ALEMBIC_INI.exists() else None)
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", settings.database_url())
    # env.py reads this so the migration targets the same database the pipeline
    # uses, whatever the ini file happens to say.
    os.environ["MPVF_DB_URL"] = settings.database_url()
    return config


def head_revision(settings: Settings) -> str | None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_config(settings))
    return script.get_current_head()


def current_revision(settings: Settings) -> str | None:
    from sqlalchemy import text

    database = Database(settings.database_url())
    inspector = inspect(database.engine)
    if "alembic_version" not in inspector.get_table_names():
        return None
    with database.engine.connect() as connection:
        row = connection.execute(text("select version_num from alembic_version")).fetchone()
    return row[0] if row else None


def state(settings: Settings) -> MigrationState:
    try:
        head = head_revision(settings)
    except MigrationsUnavailable as exc:
        return MigrationState(current=None, head=None, pending=False, detail=str(exc))
    current = current_revision(settings)
    return MigrationState(current=current, head=head, pending=current != head)


class SchemaAdoptionRequired(RuntimeError):
    """An existing database predates migrations and does not match head."""


def _needs_adoption(settings: Settings) -> bool:
    """True when tables exist but Alembic has never been recorded.

    This is the database ``create_all`` built before migrations existed.
    """

    if current_revision(settings) is not None:
        return False
    database = Database(settings.database_url())
    existing = set(inspect(database.engine).get_table_names())
    return bool(existing & set(Base.metadata.tables))


def upgrade(settings: Settings, revision: str = "head") -> MigrationState:
    """Bring the database up to ``revision``; safe to run repeatedly.

    A pre-migration database is adopted rather than rebuilt: if its schema
    already matches the models exactly, it is stamped at head. If it differs,
    we refuse instead of guessing at somebody's data.
    """

    from alembic import command

    settings.data_dir.mkdir(parents=True, exist_ok=True)

    if _needs_adoption(settings):
        drift = schema_drift(settings)
        if drift:
            raise SchemaAdoptionRequired(
                f"{settings.db_path} predates migrations and differs from the models "
                f"({len(drift)} differences, e.g. {drift[0]}). Back it up, then either "
                "migrate it by hand or start a fresh database."
            )
        command.stamp(_config(settings), "head")

    command.upgrade(_config(settings), revision)
    return state(settings)


def downgrade(settings: Settings, revision: str) -> MigrationState:
    from alembic import command

    command.downgrade(_config(settings), revision)
    return state(settings)


def stamp(settings: Settings, revision: str = "head") -> MigrationState:
    """Mark an existing database as being at ``revision`` without running it.

    Used once when adopting migrations on a database that was created by
    ``create_all`` before this module existed.
    """

    from alembic import command

    command.stamp(_config(settings), revision)
    return state(settings)


def history(settings: Settings) -> list[dict[str, Any]]:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_config(settings))
    return [
        {
            "revision": revision.revision,
            "down_revision": revision.down_revision,
            "message": revision.doc,
            "is_head": revision.is_head,
        }
        for revision in script.walk_revisions()
    ]


def schema_drift(settings: Settings) -> list[str]:
    """Differences between the migrated database and ``Base.metadata``.

    An empty list means a migration exists for every model change. A non-empty
    one means somebody edited a model without generating a migration.
    """

    database = Database(settings.database_url())
    inspector = inspect(database.engine)
    live_tables = {name for name in inspector.get_table_names() if name != "alembic_version"}
    model_tables = set(Base.metadata.tables)

    problems: list[str] = []
    for missing in sorted(model_tables - live_tables):
        problems.append(f"table '{missing}' is defined in models but not in the database")
    for extra in sorted(live_tables - model_tables):
        problems.append(f"table '{extra}' exists in the database but not in the models")

    for name in sorted(model_tables & live_tables):
        live_columns = {column["name"] for column in inspector.get_columns(name)}
        model_columns = set(Base.metadata.tables[name].columns.keys())
        for missing in sorted(model_columns - live_columns):
            problems.append(f"{name}.{missing} is in the models but not in the database")
        for extra in sorted(live_columns - model_columns):
            problems.append(f"{name}.{extra} is in the database but not in the models")
    return problems
