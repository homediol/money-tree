import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.database.repository import Repository
from app.services.disaster_recovery import DisasterRecoveryManager
from app.services.operations import OperationsManager


def make_app(tmp_path):
    repo = Repository(tmp_path / "recovery.sqlite"); repo.init()
    history = SimpleNamespace(rows=lambda: [], status=lambda: {"running": False})
    health = SimpleNamespace(refresh=lambda: {"state": "HEALTHY", "components": {}})
    betting = SimpleNamespace(status=lambda: {"browser_status": "NOT_CONNECTED"})
    state = SimpleNamespace(wp=SimpleNamespace(repository=repo), history_collector=history,
                            system_health=health, betting=betting, live=None,
                            reconciliation=None, manager=None)
    app = SimpleNamespace(state=state)
    operations = OperationsManager(app)
    state.operations = operations
    return app, repo, DisasterRecoveryManager(app, operations)


def test_atomic_write_and_verified_copy_restore(tmp_path):
    app, repo, recovery = make_app(tmp_path)
    target = tmp_path / "decision.json"
    result = recovery.atomic_write(str(target), {"decision_id": "d1"})
    assert result["ok"] and target.exists()
    backup = recovery.backup_verified(str(tmp_path / "backup.sqlite"))
    assert backup["verified_copy"]["ok"]


def test_recovery_is_safe_and_idempotent(tmp_path):
    app, repo, recovery = make_app(tmp_path)
    first = asyncio.run(recovery.recover("test"))
    second = asyncio.run(recovery.recover("test-repeat"))
    assert first["mode"] == "SAFE_MODE"  # history is not running, so no false READY
    assert second["open_after"] == 0
