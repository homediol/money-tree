import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from app.database.repository import Repository
from app.services.operations import OperationsManager


def make_app(tmp_path):
    repo = Repository(tmp_path / "ops.sqlite"); repo.init()
    wp = SimpleNamespace(repository=repo)
    health = SimpleNamespace(refresh=lambda: {"state": "HEALTHY", "components": {}, "reason": None})
    betting = SimpleNamespace(status=lambda: {"browser_status": "NOT_CONNECTED"})
    return SimpleNamespace(state=SimpleNamespace(wp=wp, system_health=health, betting=betting, live=None, manager=None)), repo


def test_backup_integrity_and_reports(tmp_path):
    app, repo = make_app(tmp_path)
    operations = OperationsManager(app)
    backup = operations.backup(str(tmp_path / "backup.sqlite"))
    assert backup["integrity"] == "ok"
    restored = operations.restore(str(tmp_path / "backup.sqlite"), confirmation="RESTORE DATABASE")
    assert restored["integrity"] == "ok"
    report = operations.report("daily")
    assert report["executions"] == 0


def test_cleanup_is_bounded_and_incident_state_is_observable(tmp_path):
    app, repo = make_app(tmp_path)
    operations = OperationsManager(app)
    assert operations.cleanup()["system_audit_log"] == 0
    assert operations.incidents == []


def test_resource_metrics_and_unavailable_dependency_are_explicit(tmp_path, monkeypatch):
    app, _repo = make_app(tmp_path)
    operations = OperationsManager(app)
    available = operations._resource()
    assert isinstance(available["cpu_percent"], (int, float))
    assert isinstance(available["memory_percent"], (int, float))
    assert available["disk_free_bytes"] >= 0
    monkeypatch.setitem(sys.modules, "psutil", None)
    unavailable = operations._resource()
    assert unavailable["cpu_percent"] is None
    assert unavailable["memory_percent"] is None
    assert unavailable["disk_free_bytes"] is None
    assert "resource metrics unavailable" in unavailable["error"]
