"""Alembic migrations (§22).

The load-bearing test here is ``test_migrations_produce_the_model_schema``:
it proves the committed migrations and the SQLAlchemy models describe the same
database. If someone edits a model without generating a migration, that test
fails rather than a production upgrade failing later.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from mpvf.models import migrations
from mpvf.models.db import Base, Database

pytest.importorskip("alembic", reason="alembic is part of the dev extra")


@pytest.fixture
def fresh_settings(settings):
    """Settings pointing at a database that does not exist yet."""

    if settings.db_path.exists():
        settings.db_path.unlink()
    return settings


class TestMigrationMechanics:
    def test_upgrade_creates_every_table(self, fresh_settings):
        migrations.upgrade(fresh_settings)
        inspector = inspect(Database(fresh_settings.database_url()).engine)
        tables = set(inspector.get_table_names())
        assert set(Base.metadata.tables) <= tables
        assert "alembic_version" in tables

    def test_upgrade_is_idempotent(self, fresh_settings):
        first = migrations.upgrade(fresh_settings)
        second = migrations.upgrade(fresh_settings)
        assert first.current == second.current
        assert second.up_to_date

    def test_state_reports_pending_before_upgrade(self, fresh_settings):
        state = migrations.state(fresh_settings)
        assert state.current is None
        assert state.head is not None
        assert state.pending

    def test_state_is_at_head_after_upgrade(self, fresh_settings):
        migrations.upgrade(fresh_settings)
        state = migrations.state(fresh_settings)
        assert state.up_to_date
        assert not state.pending

    def test_history_lists_the_initial_revision(self, fresh_settings):
        revisions = {item["revision"] for item in migrations.history(fresh_settings)}
        assert "0001_initial" in revisions

    def test_head_has_a_stable_identifier(self, fresh_settings):
        """A renamed head silently invalidates every deployed database."""

        assert migrations.head_revision(fresh_settings) == "0001_initial"


class TestSchemaParity:
    def test_migrations_produce_the_model_schema(self, fresh_settings):
        """The committed migrations and the models must not drift apart."""

        migrations.upgrade(fresh_settings)
        assert migrations.schema_drift(fresh_settings) == []

    def test_drift_is_detected_when_a_column_is_missing(self, fresh_settings):
        migrations.upgrade(fresh_settings)
        database = Database(fresh_settings.database_url())
        with database.engine.begin() as connection:
            connection.execute(text("alter table runs drop column quality_score"))

        drift = migrations.schema_drift(fresh_settings)
        assert any("runs.quality_score" in problem for problem in drift)

    def test_drift_is_detected_when_a_table_is_missing(self, fresh_settings):
        migrations.upgrade(fresh_settings)
        database = Database(fresh_settings.database_url())
        with database.engine.begin() as connection:
            connection.execute(text("drop table events"))

        drift = migrations.schema_drift(fresh_settings)
        assert any("'events'" in problem for problem in drift)


class TestAdoption:
    def test_a_create_all_database_is_adopted_not_rebuilt(self, fresh_settings):
        """The database built before migrations existed is stamped, not dropped."""

        database = Database(fresh_settings.database_url())
        database.create_all()
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "insert into properties (id, property_key, normalized_address, state,"
                    " first_seen_at, last_seen_at)"
                    " values ('prop_x', 'key_x', '1 main st', 'ME',"
                    " '2026-01-01', '2026-01-01')"
                )
            )

        state = migrations.upgrade(fresh_settings)

        assert state.up_to_date
        with database.engine.connect() as connection:
            rows = connection.execute(text("select count(*) from properties")).scalar()
        assert rows == 1, "adoption must preserve existing rows"

    def test_a_divergent_legacy_database_is_refused(self, fresh_settings):
        database = Database(fresh_settings.database_url())
        database.create_all()
        with database.engine.begin() as connection:
            connection.execute(text("drop table events"))

        with pytest.raises(migrations.SchemaAdoptionRequired) as excinfo:
            migrations.upgrade(fresh_settings)
        assert "differs from the models" in str(excinfo.value)

    def test_stamp_records_a_revision_without_running_it(self, fresh_settings):
        Database(fresh_settings.database_url()).create_all()
        state = migrations.stamp(fresh_settings)
        assert state.current == "0001_initial"


class TestDoctorIntegration:
    def test_doctor_fails_on_an_unmigrated_database(self, fresh_settings):
        from mpvf.cli.doctor import run_doctor

        report = run_doctor(fresh_settings)
        check = next(c for c in report["checks"] if c["name"] == "migrations")
        assert check["status"] == "fail"

    def test_doctor_passes_once_migrated(self, fresh_settings):
        from mpvf.cli.doctor import run_doctor

        migrations.upgrade(fresh_settings)
        report = run_doctor(fresh_settings)
        check = next(c for c in report["checks"] if c["name"] == "migrations")
        assert check["status"] == "ok", check["detail"]
