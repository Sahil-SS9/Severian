from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from severian.domain.models import Collection, Observation, Scope
from severian.infrastructure.sqlite import SQLiteRepository


def test_backup_restore_and_portable_export_import(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    source.initialize()
    scope = Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="s")
    source.create_collection(Collection(id="c", tenant_id="t", name="C"))
    source.add_observation(Observation(scope=scope, content="preserved", source={"uri": "x"}))
    backup = tmp_path / "backup.db"
    source.backup(backup)
    assert not (tmp_path / "backup.db-wal").exists()
    assert not (tmp_path / "backup.db-shm").exists()
    exported = source.export_json()
    source.close()

    restored = SQLiteRepository(backup)
    restored.initialize()
    assert restored.list_observations(scope)[0].content == "preserved"
    restored.close()

    imported = SQLiteRepository(tmp_path / "imported.db")
    imported.initialize()
    imported.import_json(exported)
    imported.import_json(exported)
    assert imported.list_observations(scope)[0].source == {"uri": "x"}
    imported.close()


def test_existing_0001_database_converges_to_scoped_identity_schema(tmp_path):
    database = tmp_path / "migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")

    command.upgrade(config, "0001")
    old_engine = create_engine(f"sqlite:///{database}")
    assert {item["name"] for item in inspect(old_engine).get_unique_constraints("records")} == {
        "uq_observation_content"
    }
    old_engine.dispose()

    command.upgrade(config, "head")
    upgraded_engine = create_engine(f"sqlite:///{database}")
    inspector = inspect(upgraded_engine)
    assert inspector.get_unique_constraints("records") == []
    indexes = {item["name"]: item for item in inspector.get_indexes("records")}
    assert indexes["uq_records_scoped_identity"]["unique"] == 1
    assert "identity_key" in {item["name"] for item in inspector.get_columns("records")}
    upgraded_engine.dispose()
