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
    exported = source.export_json()
    source.close()

    restored = SQLiteRepository(backup)
    restored.initialize()
    assert restored.list_observations(scope)[0].content == "preserved"
    restored.close()

    imported = SQLiteRepository(tmp_path / "imported.db")
    imported.initialize()
    imported.import_json(exported)
    assert imported.list_observations(scope)[0].source == {"uri": "x"}
    imported.close()
