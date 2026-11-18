from types import SimpleNamespace

from behaviour_detection import importer


class _LockConnection:
    def __init__(self, engine):
        self.engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.engine.lock_held = False

    def exec_driver_sql(self, statement):
        if "pg_advisory_unlock" in statement:
            self.engine.lock_held = False
        elif "pg_advisory_lock" in statement:
            self.engine.lock_held = True

    def commit(self):
        pass


class _LockEngine:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self):
        self.lock_held = False

    def connect(self):
        return _LockConnection(self)


def test_import_and_motif_enrichment_share_one_command_lock(monkeypatch, tmp_path):
    engine = _LockEngine()
    calls = []

    def fake_import(active_engine, research_root, *, batch_size):
        assert active_engine is engine
        assert research_root == tmp_path
        assert batch_size == 800
        assert engine.lock_held
        calls.append("import")
        return {"status": "imported"}

    def fake_enrichment(active_engine, research_root):
        assert active_engine is engine
        assert research_root == tmp_path
        assert engine.lock_held
        calls.append("enrich")
        return {"status": "complete"}

    monkeypatch.setattr(importer, "import_phase1", fake_import)
    monkeypatch.setattr(importer, "enrich_motif_research", fake_enrichment)

    result = importer.import_phase1_and_enrich(engine, tmp_path, batch_size=800)

    assert calls == ["import", "enrich"]
    assert result == {
        "status": "imported",
        "motif_research_enrichment": {"status": "complete"},
    }
    assert not engine.lock_held
